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
TABLE = '743-ПП, п. 3.6.3, табл. 3.6.1'
WIDE_CROWN_M = 10.0
# примечание 1 к табл. 3.6.1: отступы — для деревьев с кроной не больше этого (crown_cap_m в placement.json)
CROWN_NOTE_M = 5.0
TERRITORY = {'Д': 'дворовые территории', 'С': 'дошкольные учреждения', 'Ш': 'школы и спортивные учреждения',
             'М': 'учреждения здравоохранения', 'Т': 'магистрали и местные проезды',
             'П': 'площади и общественные пространства', 'К': 'парки, бульвары и скверы',
             'З': 'производственные и санитарные зоны'}
MOS = 'ДПиООС, «Деревья и кустарники, предлагаемые для посадки на внутриквартальных территориях города Москвы»'
ASSORTMENT = {'о': f'основной ассортимент деревьев, кустарников и лиан для озеленения Москвы — {MOS}',
              'д': f'дополнительный ассортимент деревьев, кустарников и лиан для озеленения Москвы — {MOS}',
              'п': f'ассортимент перспективных видов для увеличения биоразнообразия — {MOS}',
              'г': 'городской ассортимент цветочного оформления (публикации mos.ru)'}
FACTS = {
    'н': 'неприхотлив',
    'з': 'засухоустойчив', 'у': 'устойчив у дорог', 'я': 'ядовит', 'г': 'чувствителен к реагентам и газам',
    'др': 'нужна дренированная почва', 'пл': 'не сажать рядом с плодовыми', 'рс': 'возможен самосев',
    'дп': 'не сажать у детских и спортивных площадок', 'шк': 'широкая крона',
    'ин': 'склонен к распространению: посадка с контролем, не на ООЗТ и природных территориях (п. 5 ПП Москвы № 369-ПП)',
    'уо': 'требует особого ухода: защита корней, дождевание кроны весной у дорог, мульчирование, рыхление',
}
# сезонные свойства справочника (из правил вида 3D-сцены, config/scene.json — справочные, не из НПА)
SEASON_FACTS = (('вц', 'цветёт весной', 'весна'), ('лц', 'цветёт летом', 'лето'), ('оц', 'цветёт осенью', 'осень'),
                ('ол', 'яркая осенняя листва', 'осень'), ('пд', 'декоративные плоды', 'осень'),
                ('зд', 'декоративен зимой', 'зима'))
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
PLAYGROUND = '623-ПП (МГСН 1.02-02), п. 4.12.7.3'


def cite(title, doc=''):
    """Полная ссылка на акт и пункт по названию объекта правила (cite в config/norms.json).

    В запрос LLM идёт короткая doc — от неё зависит кэш ответов; в обоснования и DXF — эта.
    """
    return TABLE_RULES.get(title, {}).get('cite') or doc


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
        doc = entry.get('cite') or entry.get('doc', rule['doc'])
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
    if planting == 'tree':
        lines.append(f'Отступы {TABLE} даны для кроны не больше {CROWN_NOTE_M:g} м (примечание 1): '
                     f'в рассадке крона дерева проектного возраста не больше {CROWN_NOTE_M:g} м')

    if 'шк' in plant['flags']:
        distance = zone.get('building_m')
        if distance is not None and distance < WIDE_CROWN_M:
            return lines, f'широкая крона ближе {WIDE_CROWN_M:g} м от здания ({distance:g} м), {TABLE}, примечание 3'
        lines.append(f'Широкая крона: до зданий {"больше 50" if distance is None else f"{distance:g}"} м, '
                     f'не ближе {WIDE_CROWN_M:g} м соблюдено ({TABLE}, примечание 3)')

    if zone.get('near_ground'):
        lines.append('Рядом детская или спортивная площадка: ограничения ассортимента по детским площадкам у вида нет '
                     f'(без колючек и ядовитых плодов — {PLAYGROUND})')

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
    season = [text for flag, text, _ in SEASON_FACTS if flag in plant['flags']]
    if season:
        result.append('по сезонам: ' + ', '.join(season))
    for flag in sorted(plant['flags']):
        if flag.startswith('h'):
            result.append(f'высота до {flag[1:]} м')
        elif flag.startswith('к') and flag[1:].isdigit():
            result.append(f'крона до {flag[1:]} м')
    return result


def seasons(plant):
    """Сезоны, в которые растение декоративно по справочнику: ['весна', 'осень', …] по порядку года."""
    found = {season for flag, _, season in SEASON_FACTS if flag in plant['flags']}
    return [s for s in ('весна', 'лето', 'осень', 'зима') if s in found]


def warnings(reason, plant):
    """Где текст LLM противоречит справочнику."""
    found = [message for regex, flag, message in CONTRADICTIONS if flag in plant['flags'] and regex.search(reason)]
    light = plant['flags'] & {'с', 'п', 'т'}
    if light == {'с'} and re.search(r'тенев|в тени|под кронами|затенен', reason, re.I):
        found.append('текст называет растение теневыносливым, а по справочнику оно светолюбиво')
    return found
