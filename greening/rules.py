"""Подбор растений правилами — запасной путь, когда LLM недоступна (нет ключа, нет сети, ошибка ответа).

Отвечает в том же формате, что LLM (recommend.SYSTEM): {'plantings', 'zones', 'justification'},
поэтому дальше работают те же проверки (_check), нормы (justify) и рассадка (place).

Правила выбора — те же ограничения, что в инструкции LLM, но жёстко:
- только разрешённая в зоне группа и допуск по типу территории (колонка «где» справочника);
- у детских и спортивных площадок — без «дп»; у зданий ближе 10 м — без широкой кроны («шк»);
  у проезжей части — без чувствительных к реагентам («г»); в тени — только с «т» или «п»;
- предпочтение основному ассортименту («о») и неприхотливым («н»), у дорог — устойчивым («у»).
Палитра выбирается на всю структуру (единый стиль), разная у разных структур — по номеру структуры.
Количество не задаётся: зону заполняет рассадка (place.py) выбранными видами и приёмами.
"""

import hashlib

from . import justify

GROUPS = {'tree': ('ДЛ', 'ДХ'), 'shrub': ('КЛ', 'КХ'), 'perennial': ('М',)}
LAWN, SHADE_LAWN = 'Т001', 'Т002'
SHADE_TAGS = ('existing_trees', 'near_building')
GROUND_TAGS = ('near_playground', 'near_sport_ground')
# минимальная зона, где вид посадки вообще назначается, м²
MIN_ZONE = {'tree': 40.0, 'shrub': 4.0, 'bed': 60.0}
# сколько видов на структуру
PALETTE = {'tree': 2, 'shrub': 3, 'perennial': 1}
TOP = 6


def _score(plant, roadside, shade):
    score = {'о': 3, 'д': 2, 'п': 1, 'г': 2}.get(plant['assortment'], 0)
    score += 1 if 'н' in plant['flags'] else 0
    score += 2 if roadside and 'у' in plant['flags'] else 0
    # лиственные — основа городского озеленения; в тени — виды с подтверждённой теневыносливостью
    score += 1 if plant['group'] in ('ДЛ', 'КЛ') else 0
    score += 1 if shade and plant['flags'] & {'п', 'т'} else 0
    return score


def _fits(plant, zone, territory):
    tags = set(zone['tags']) | {t for t, share in zone.get('tag_share', {}).items() if share > 0}
    flags = plant['flags']
    if plant['where'] != '*' and territory not in plant['where']:
        return False
    if tags & set(GROUND_TAGS) and 'дп' in flags:
        return False
    if 'шк' in flags and zone.get('building_m') is not None and zone['building_m'] < justify.WIDE_CROWN_M:
        return False
    if 'roadside' in tags and 'г' in flags:
        return False
    shade = tags & set(SHADE_TAGS)
    light = flags & {'с', 'п', 'т'}
    if shade and light and not light & {'п', 'т'}:
        return False
    return True


def _palette(kind, zones, plants, structure):
    """Виды на всю структуру: лучшие по правилам среди подходящих хотя бы одной зоне, со сдвигом по номеру
    структуры — чтобы соседние структуры не были одинаковыми."""
    roadside = any('roadside' in z['tags'] for z in zones)
    shade = any(set(z['tags']) & set(SHADE_TAGS) for z in zones)
    fitting = [p for p in plants if p['group'] in GROUPS[kind]
               and any(_fits(p, z, structure['territory']) for z in zones)]
    fitting.sort(key=lambda p: (-_score(p, roadside, shade), p['id']))
    top = fitting[:TOP]
    if not top:
        return []
    shift = int(hashlib.md5(structure['id'].encode()).hexdigest(), 16) % len(top)
    ordered = top[shift:] + top[:shift]
    return ordered[:PALETTE[kind]]


def _reason(plant, zone, kind):
    parts = [justify.ASSORTMENT.get(plant['assortment'], '')]
    facts = justify.facts(plant)
    if facts:
        parts.append(', '.join(facts))
    if set(zone['tags']) & set(SHADE_TAGS):
        parts.append('подходит для полутени и тени: место у здания или под кронами')
    if 'roadside' in zone['tags'] and 'у' in plant['flags']:
        parts.append('устойчив у дорог — зона у проезжей части')
    what = {'tree': 'Дерево подобрано', 'shrub': 'Кустарник подобран', 'perennial': 'Многолетник подобран'}[kind]
    return f"{what} правилами (без LLM): " + '; '.join(p for p in parts if p) + '.'


def answer(structure, plants):
    by_id = {p['id']: p for p in plants}
    zones = [z for patch in structure['patches'] if not patch['small'] for z in patch['zones']]
    palette = {kind: _palette(kind, [z for z in zones if (kind if kind != 'perennial' else 'herbaceous') in z['allowed']],
                              plants, structure) for kind in GROUPS}
    plantings, explained = [], []
    for zone in zones:
        placed = []
        for kind in ('tree', 'shrub'):
            if kind not in zone['allowed'] or zone['area_m2'] < MIN_ZONE[kind]:
                continue
            choice = next((p for p in palette[kind] if _fits(p, zone, structure['territory'])), None)
            if choice is None:
                continue
            room = zone.get('tree_capacity', 3) if kind == 'tree' else 3
            if room < 1:
                continue
            role = 'солитер' if room <= 2 else 'группа'
            plantings.append({'zone': zone['id'], 'plant_id': choice['id'], 'role': role,
                              'unit': 'шт', 'reason': _reason(choice, zone, kind)})
            placed.append(f"{choice['name'].lower()} ({role})")
        if 'herbaceous' in zone['allowed']:
            bed = False
            perennial = next((p for p in palette['perennial'] if _fits(p, zone, structure['territory'])), None)
            if perennial and zone['area_m2'] >= MIN_ZONE['bed'] and 'roadside' not in zone['tags']:
                bed = True
                plantings.append({'zone': zone['id'], 'plant_id': perennial['id'], 'role': 'цветник',
                                  'unit': 'м2', 'reason': _reason(perennial, zone, 'perennial')})
                placed.append(f"цветник из {perennial['name'].lower()}")
            shade = bool(set(zone['tags']) & set(SHADE_TAGS))
            lawn = by_id[SHADE_LAWN if shade else LAWN]
            plantings.append({'zone': zone['id'], 'plant_id': lawn['id'], 'role': 'газон', 'unit': 'м2',
                              'reason': 'Газон на свободной части зоны' + (' — теневыносливый: место в тени' if shade else '') + '.'})
            placed.append(lawn['name'].split(' (')[0].lower())
        if placed:
            decision = 'Посадки подобраны правилами: ' + ', '.join(placed) + '.'
        elif zone.get('empty_reason'):
            decision = f"Без посадок: {zone['empty_reason']}."
        else:
            decision = 'Без посадок: зона слишком мала для деревьев и кустарников, травянистые здесь не разрешены.'
        explained.append({'zone': zone['id'], 'place': zone['place'], 'decision': decision})
    names = [p['name'] for kind in ('tree', 'shrub', 'perennial') for p in palette[kind]]
    justification = ('Подбор выполнен правилами без LLM (LLM недоступна): для всей структуры выбрана общая палитра — '
                     + (', '.join(names) if names else 'только газон')
                     + '. Учтены разрешённые в каждой зоне группы, допуск ассортимента по типу территории, '
                       'условия места (тень, проезжая часть, площадки) и широкая крона у зданий.')
    return {'plantings': plantings, 'zones': explained, 'justification': justification}
