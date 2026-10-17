"""Разнообразие посадок на уровне плана — после подбора (LLM или правила), перед рассадкой.

Подбор даёт на зону вид и приём посадки. Здесь код добавляет к ним посадки-спутники (by: rules, mix_of — номер
посадки, к которой спутник добавлен), а рассадка (place.py) собирает из них композиции:
- цветник → миксбордер: многолетники и луковичные с другим сроком цветения (весна, лето, осень), подходящие месту
  по тем же правилам, что подбор правилами (rules._fits: допуск по территории, площадки, тень, проезд); пятно
  цветника делится на куртины видов (Placer.drifts);
- групповая посадка кустов, если в зоне куст один, → второй кустарник той же структуры другой высоты: из двух видов
  рассадка собирает смешанные группы, низкий — к дорожке (Placer.mixed_group).
Параметры — config variety в placement.json (mixborder_species, mixborder_min_m2, mixed_share). Пересчёт
идемпотентный: прежние спутники снимаются — план мог прийти из прошлой версии (пересчёт по параметрам).
"""

import hashlib

from . import catalog, justify, place, recommend, rules

SEASONS = ('весна', 'лето', 'осень')
BED_GROUPS = ('М', 'Б')
SHRUB_GROUPS = ('КЛ', 'КХ')
# второй вид смешанной группы — выше или ниже основного хотя бы во столько раз
HEIGHT_RATIO = 1.3
# смешанные группы — только в зонах не меньше этого, м²
MIXED_MIN_M2 = 30.0


def _seasons(plant):
    return set(justify.seasons(plant)) & set(SEASONS)


def _turn(*keys):
    return int(hashlib.md5(':'.join(map(str, keys)).encode()).hexdigest(), 16)


def _record(zone, plant, structure, reason, index, role):
    lines, _ = justify.norms(plant, zone, structure)
    return {'zone': zone['id'], 'plant_id': plant['id'], 'role': role, 'unit': 'шт' if role != 'цветник' else 'м2',
            'by': 'rules', 'mix_of': index, 'name': plant['name'], 'group': plant['group'], 'reason': reason,
            'norms': lines, 'facts': justify.facts(plant), 'warnings': []}


def _mixborder(structure, zone, index, main, plants, count):
    """До count − 1 видов к цветнику: каждый следующий закрывает сезон цветения, которого ещё нет."""
    chosen, covered, added = [main], _seasons(main), []
    while len(chosen) < count:
        options = [p for p in plants if p['group'] in BED_GROUPS and p not in chosen and _seasons(p) - covered
                   and rules._fits(p, zone, structure['territory'])
                   and not (zone.get('near_ground') and 'дп' in p['flags'])]
        if not options:
            break
        # больше новых сезонов, неприхотливые; среди равных — по очереди от структуры и зоны (соседи не одинаковы)
        rank = lambda p: (-len(_seasons(p) - covered), 'н' not in p['flags'])
        options.sort(key=lambda p: (rank(p), p['id']))
        best = [p for p in options if rank(p) == rank(options[0])]
        plant = best[_turn(structure['id'], zone['id'], len(chosen)) % len(best)]
        new = ', '.join(sorted(_seasons(plant) - covered, key=SEASONS.index))
        whole = ', '.join(s for s in SEASONS if s in covered | _seasons(plant))
        reason = (f"Миксбордер: к виду «{main['name']}» добавлен «{plant['name']}» — цветёт {_when(new)}, и цветник "
                  f"декоративен не один месяц ({whole}). Вид подобран правилами по условиям места: допуск ассортимента "
                  f"для территории, свет, близость площадок и проезда.")
        added.append(_record(zone, plant, structure, reason, index, 'цветник'))
        chosen.append(plant)
        covered |= _seasons(plant)
    return added


def _when(seasons):
    return {'весна': 'весной', 'лето': 'летом', 'осень': 'осенью'}.get(seasons, seasons)


def _partner(structure, zone, index, main, palette, crown_config):
    """Второй кустарник для смешанных групп: из уже выбранных для структуры, подходящий зоне, другой высоты."""
    height = place._height(main, place._crown(main, 'shrub', crown_config))
    options = []
    for plant in palette:
        if plant['id'] == main['id'] or not rules._fits(plant, zone, structure['territory']) \
                or (zone.get('near_ground') and 'дп' in plant['flags']):
            continue
        other = place._height(plant, place._crown(plant, 'shrub', crown_config))
        ratio = max(height, other) / max(min(height, other), 0.1)
        if ratio >= HEIGHT_RATIO:
            options.append((ratio, plant, other))
    if not options:
        return None
    ratio, plant, other = max(options, key=lambda o: (o[0], o[1]['id']))
    where = 'ниже' if other < height else 'выше'
    reason = (f"Второй вид для смешанных групп: «{plant['name']}» уже выбран для этой структуры и {where} вида "
              f"«{main['name']}» (до {other:g} м против {height:g} м) — алгоритм собирает из них группы, где низкий "
              f"вид стоит ближе к дорожке, высокий — за ним.")
    return _record(zone, plant, structure, reason, index, 'группа')


def compose(plan, config=None, plants=None):
    """Спутники посадок в плане (на месте); возвращает {'mixborder': число, 'mixed': число}."""
    config = config or place.load_config()
    variety = config.get('variety') or {}
    plants = plants or catalog.load()
    by_id = {p['id']: p for p in plants}
    species = int(variety.get('mixborder_species') or 1)
    bed_min = variety.get('mixborder_min_m2') or 0.0
    mixed = variety.get('mixed_share') or 0.0
    counts = {'mixborder': 0, 'mixed': 0}
    for record in plan['structures']:
        plantings = [p for p in record.get('plantings', []) if 'mix_of' not in p]
        record['plantings'] = plantings
        if not plantings or record.get('type') not in recommend.TYPES:
            continue
        structure = {'id': record['id'], 'territory': recommend.TYPES[record['type']][1]}
        zones = {z['id']: z for z in record['zones']}
        beds = {}
        for item in plantings:
            if item.get('role') == 'цветник':
                beds[item['zone']] = beds.get(item['zone'], 0) + 1
        palette = []
        for item in plantings:
            plant = by_id.get(item.get('plant_id'))
            if plant and plant['group'] in SHRUB_GROUPS and plant not in palette:
                palette.append(plant)
        added = []
        for index, item in enumerate(plantings):
            plant, zone = by_id.get(item.get('plant_id')), zones.get(item.get('zone'))
            if plant is None or zone is None:
                continue
            if species > 1 and item.get('role') == 'цветник' and plant['group'] in BED_GROUPS:
                area = min(config['bed_share'] * zone['area_m2'], config['bed_max_m2']) / beds[zone['id']]
                if area >= bed_min:
                    more = _mixborder(structure, zone, index, plant, plants, species)
                    added += more
                    counts['mixborder'] += bool(more)
            if mixed and plant['group'] in SHRUB_GROUPS and item.get('role') in place.GROUP_ROLES \
                    and 'shrub' in zone['allowed'] and zone['area_m2'] >= MIXED_MIN_M2:
                shrubs = {i.get('plant_id') for i in plantings if i.get('zone') == zone['id']
                          and by_id.get(i.get('plant_id'), {}).get('group') in SHRUB_GROUPS}
                if len(shrubs) == 1:
                    partner = _partner(structure, zone, index, plant, palette, config)
                    if partner is not None:
                        added.append(partner)
                        counts['mixed'] += 1
        record['plantings'] = plantings + added
    return counts
