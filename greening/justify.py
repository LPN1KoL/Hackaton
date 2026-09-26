"""Нормативное обоснование посадки — строго по данным зоны и справочника, без LLM.

LLM пишет, чем посадка хороша; здесь — почему она законна в этой зоне (ссылки на 743-ПП и
источники ассортимента), какие у растения свойства по справочнику и где текст LLM им противоречит.
"""

import re

from .sections import load_norms

KIND = {'ДЛ': 'tree', 'ДХ': 'tree', 'КЛ': 'shrub', 'КХ': 'shrub', 'Л': 'shrub',
        'М': 'herbaceous', 'О': 'herbaceous', 'Б': 'herbaceous', 'Г': 'herbaceous'}
# «для деревьев», «для кустарников»
FOR = {'tree': 'для деревьев', 'shrub': 'для кустарников', 'herbaceous': 'для травянистых'}
TABLE = '743-ПП, табл. 3.6.1'
WIDE_CROWN_M = 10.0
TERRITORY = {'Д': 'дворовые территории', 'С': 'дошкольные учреждения', 'Ш': 'школы и спортивные учреждения',
             'М': 'учреждения здравоохранения', 'Т': 'магистрали и местные проезды',
             'П': 'площади и общественные пространства', 'К': 'парки, бульвары и скверы',
             'З': 'производственные и санитарные зоны'}
ASSORTMENT = {'о': 'основной ассортимент деревьев, кустарников и лиан для озеленения Москвы',
              'д': 'дополнительный ассортимент деревьев, кустарников и лиан для озеленения Москвы',
              'п': 'ассортимент перспективных видов для озеленения Москвы',
              'г': 'городской ассортимент цветочного оформления (публикации mos.ru)'}
FACTS = {
    'н': 'неприхотлив',
    'з': 'засухоустойчив', 'у': 'устойчив у дорог', 'я': 'ядовит', 'г': 'чувствителен к реагентам и газам',
    'др': 'нужна дренированная почва', 'пл': 'не сажать рядом с плодовыми', 'рс': 'возможен самосев',
    'дп': 'не сажать у детских и спортивных площадок', 'шк': 'широкая крона',
}
HERBACEOUS_SOURCE = {'Г': 'Посев газонов предусмотрен 743-ПП'}
LIGHT = (('с', 'солнце'), ('п', 'полутень'), ('т', 'тень'))
# текст LLM → свойство справочника, которое этот текст утверждает или отрицает
CONTRADICTIONS = (
    (re.compile(r'(устойчив|переносит|выдерживает)[^.]{0,40}(газ|реагент|соль|солей|выхлоп)', re.I), 'г',
     'текст называет растение устойчивым к газам или реагентам, а по справочнику оно к ним чувствительно'),
)


def _table():
    rules = load_norms()['distance_rules']
    return {rule['title']: rule for rule in rules}


TABLE_RULES = _table()


def _short(title):
    return re.split(r'[,(]', title)[0].strip().lower()


def _nearby(zone, planting):
    """Отступы по объектам рядом с зоной — тем, чьи правила ограничили какой-то тип посадки в ней.

    Раз тип посадки в зоне разрешён, все её секции лежат за нормативными отступами для него:
    так они построены. Поэтому «соблюдён» здесь — следствие разметки, а не допущение.
    """
    kept, free = {}, {}
    for rule in zone['rules']:
        if rule['doc'] == 'config/norms.json':
            continue
        entry = TABLE_RULES.get(rule['object'], {})
        doc = entry.get('doc', rule['doc'])
        distance = entry.get(planting)
        name = _short(rule['object'])
        if planting != 'herbaceous' and distance:
            kept.setdefault(doc, {})[name] = distance
        else:
            free.setdefault(doc, []).append(name) if name not in free.get(doc, []) else None
    if planting == 'herbaceous':
        names = sorted({n for items in free.values() for n in items})
        return [f'{TABLE} отступов для травянистых не устанавливает' + (f'; рядом: {", ".join(names)}' if names else '')]
    lines = [f'Отступы {FOR[planting]} соблюдены: ' + ', '.join(f'{n} {d:g} м' for n, d in items.items()) + f' ({doc})'
             for doc, items in kept.items()]
    lines += [f'{FOR[planting].capitalize()} отступ не нормируется: {", ".join(items)} ({doc})' for doc, items in free.items()]
    return lines


def norms(plant, zone, structure):
    """Список утверждений о законности посадки в зоне. Второй результат — причина отказа или None."""
    planting = KIND[plant['group']]
    lines = _nearby(zone, planting)
    if not lines:
        lines.append(f'Отступы {TABLE} {FOR[planting]} соблюдены: нормируемые объекты дальше нормативных расстояний')

    if 'шк' in plant['flags']:
        distance = zone.get('building_m')
        if distance is not None and distance < WIDE_CROWN_M:
            return lines, f'широкая крона ближе {WIDE_CROWN_M:g} м от здания ({distance:g} м), {TABLE}, примечание'
        lines.append(f'Широкая крона: до зданий {"больше 50" if distance is None else f"{distance:g}"} м, '
                     f'не ближе {WIDE_CROWN_M:g} м соблюдено ({TABLE}, примечание)')

    if zone.get('near_ground'):
        lines.append('Рядом детская или спортивная площадка: ограничения ассортимента по детским площадкам у вида нет')

    source = HERBACEOUS_SOURCE.get(plant['group'], ASSORTMENT[plant['assortment']])
    lines.append(source[0].upper() + source[1:])
    territory = structure['territory']
    if plant['assortment'] != 'г':
        where = 'на всех типах территорий' if plant['where'] == '*' else f'для территории «{TERRITORY[territory]}»'
        lines.append(f'Разрешён ассортиментом {where}')
    return lines, None


def facts(plant):
    """Свойства растения по справочнику — проверяемая база для текста LLM."""
    result = [FACTS[f] for f in sorted(plant['flags']) if f in FACTS]
    light = [word for flag, word in LIGHT if flag in plant['flags']]
    if light:
        result.append('свет: ' + ', '.join(light))
    for flag in sorted(plant['flags']):
        if flag.startswith('h'):
            result.append(f'высота до {flag[1:]} м')
        elif flag.startswith('к') and flag[1:].isdigit():
            result.append(f'крона до {flag[1:]} м')
    return result


def warnings(reason, plant):
    """Где текст LLM противоречит справочнику."""
    found = [message for regex, flag, message in CONTRADICTIONS if flag in plant['flags'] and regex.search(reason)]
    light = plant['flags'] & {'с', 'п', 'т'}
    if light == {'с'} and re.search(r'тенев|в тени|под кронами|затенен', reason, re.I):
        found.append('текст называет растение теневыносливым, а по справочнику оно светолюбиво')
    return found
