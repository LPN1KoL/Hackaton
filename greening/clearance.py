"""Проверка отступов готовой рассадки по исходной геометрии чертежа, без зон разметки.

Рассадка ставит растения внутри зон, а зоны строит разметка из буферов по нормам (sections.py).
Здесь отступы меряются заново: от каждого посаженного дерева и куста до ближайшего объекта каждого
нормированного класса — по линиям слоёв, контурам поверхностей и площадок. Так ловятся ошибки разметки:
неверно отброшенная выноска, приближение буфера, склейка секций.

Не зависит от зон, буферов и склейки секций; общий с разметкой только класс слоя (config/layers.json).
Линии, которые фильтр выносок (leaders.py) счёл оформлением подписи, проверяются тоже: если растение
ближе нормы только к такой линии, это пометка «выноска», а не нарушение.

Расстояние — от точки посадки (ось ствола, центр куста) до объекта, как в 743-ПП, табл. 3.6.1.

Охранные зоны сетей (protection_zones в config/norms.json) посадку не запрещают, а требуют согласования
с владельцем сети: растения в них перечисляются отдельно (approvals), нарушением не считаются.
"""

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point, shape

from . import sections as markup

# координаты рассадки округлены до 1 см — меньшая недостача не нарушение, м
TOLERANCE = 0.01
# у растения записываются объекты не дальше нормы плюс этот запас: остальные к месту посадки не относятся, м
NEAR = 3.0
# типы посадок с нормативными отступами
KINDS = markup.PLANTING_TYPES


def _rules(norms):
    """Правила по объекту: одно на (источник, объект), нормы деревьев и кустарников."""
    return [r for r in norms['distance_rules'] if any(k in r for k in KINDS)]


def collect(items, surfaces, structures, norms, skip=frozenset()):
    """Объекты нормированных классов: {(источник, объект): [(геометрия, откуда, выноска)]}.

    skip — индексы items, которые фильтр выносок убрал из разметки; они сохраняются с пометкой.
    """
    wanted = {(r['source'], r['object']) for r in [*_rules(norms), *norms.get('protection_zones', [])]}
    found = {key: [] for key in wanted}
    layer_objects = {obj for source, obj in wanted if source == 'layer'}
    for n, item in enumerate(items):
        if item.kind in layer_objects and item.etype in markup.SHAPE_TYPES and not item.geometry.is_empty:
            found[('layer', item.kind)].append((item.geometry, item.layer, n in skip))
    if ('layer', 'building_wall') in found and 'building' in surfaces:
        found[('layer', 'building_wall')] += [(g, 'поверхность: здание', False) for g in markup._parts(surfaces['building'])]
    for source, obj in wanted:
        if source == 'surface' and obj in surfaces:
            found[(source, obj)] += [(g, f'поверхность: {obj}', False) for g in markup._parts(surfaces[obj])]
        elif source == 'structure':
            found[(source, obj)] += [(s['geometry'], f"площадка: {s.get('label') or s['type']}", False)
                                     for s in structures if s['type'] == obj]
    return {key: shapes for key, shapes in found.items() if shapes}


def check(placement, obstacles, norms, territory=None):
    """Отчёт: сводка по правилам, нарушения, у каждого растения — ближайшие объекты в пределах нормы + NEAR."""
    rules = _rules(norms)
    plants = [(shape(f['geometry']), f['properties']) for f in placement['features']
              if f['properties']['layer'] == 'plant' and f['properties']['kind'] in KINDS]
    points = np.array([g for g, _ in plants], dtype=object)
    near = [[] for _ in plants]
    violations, notes = [], []
    by_rule = {}

    for rule in rules:
        shapes = obstacles.get((rule['source'], rule['object']), [])
        stat = by_rule.setdefault(rule['rule'], {'doc': rule.get('cite', rule['doc']), 'objects': set(), 'checked': 0,
                                                 'violations': 0, 'min_m': None})
        stat['objects'].add(rule['title'])
        for kind in KINDS:
            norm = rule.get(kind)
            if norm is None:
                continue
            chosen = [n for n, (_, p) in enumerate(plants) if p['kind'] == kind]
            stat['checked'] += len(chosen)
            if not chosen or not shapes:
                continue
            # ближайшая трасса и ближайший объект без выносок — отдельно: выноска рядом не должна
            # заслонять настоящую сеть
            for real in (True, False):
                part = [s for s in shapes if not s[2]] if real else shapes
                if not part:
                    continue
                tree = STRtree([g for g, _, _ in part])
                found, distance = tree.query_nearest(points[chosen], max_distance=norm + NEAR, return_distance=True)
                for (p, o), d in zip(found.T, distance):
                    n = chosen[int(p)]
                    geometry, layer, leader = part[int(o)]
                    if real:
                        entry = {'rule': rule['rule'], 'doc': rule.get('cite', rule['doc']), 'object': rule['title'],
                                 'norm_m': norm, 'distance_m': round(float(d), 2), 'layer': layer}
                        if 'assumption' in rule:
                            entry['assumption'] = rule['assumption']
                        near[n].append(entry)
                        if stat['min_m'] is None or d < stat['min_m']:
                            stat['min_m'] = round(float(d), 2)
                        if d < norm - TOLERANCE:
                            stat['violations'] += 1
                            violations.append({**_plant(plants[n]), **entry})
                    elif leader and d < norm - TOLERANCE:
                        notes.append({**_plant(plants[n]), 'rule': rule['rule'], 'object': rule['title'],
                                      'norm_m': norm, 'distance_m': round(float(d), 2), 'layer': layer,
                                      'note': 'ближе нормы только к линии, которую фильтр счёл выноской подписи'})

    # охранные зоны: посадка по согласованию с владельцем сети
    approvals = []
    for zone in norms.get('protection_zones', []):
        part = [s for s in obstacles.get((zone['source'], zone['object']), []) if not s[2]]
        chosen = [n for n, (_, p) in enumerate(plants) if p['kind'] in zone['plantings']]
        if not part or not chosen:
            continue
        tree = STRtree([g for g, _, _ in part])
        found, distance = tree.query_nearest(points[chosen], max_distance=zone['distance'], return_distance=True)
        for (p, o), d in zip(found.T, distance):
            if d < zone['distance'] - TOLERANCE:
                approvals.append({**_plant(plants[chosen[int(p)]]), 'rule': zone['rule'], 'doc': zone['cite'],
                                  'object': zone['title'], 'zone_m': zone['distance'], 'distance_m': round(float(d), 2),
                                  'layer': part[int(o)][1], 'note': zone['note'],
                                  **({'assumption': zone['assumption']} if 'assumption' in zone else {})})

    outside = []
    if territory is not None and not territory.is_empty:
        shapely.prepare(territory)
        outside = [_plant(plants[n]) for n, (g, _) in enumerate(plants)
                   if not territory.covers(g) and territory.distance(g) > TOLERANCE]

    # пометка выноски имеет смысл, только если настоящих нарушений этого правила у растения нет
    real = {(v['id'], v['rule']) for v in violations}
    notes = [n for n in notes if (n['id'], n['rule']) not in real]
    for stat in by_rule.values():
        stat['objects'] = sorted(stat['objects'])
    return {
        'method': ('Расстояние от точки посадки (ось ствола дерева, центр куста) до ближайшего объекта каждого '
                   'нормированного класса — по исходным линиям слоёв, контурам поверхностей и площадок, без зон '
                   'разметки. Нормы — config/norms.json (743-ПП, табл. 3.6.1 и др.).'),
        'tolerance_m': TOLERANCE,
        'summary': {
            'plants': len(plants),
            'trees': sum(p['kind'] == 'tree' for _, p in plants),
            'shrubs': sum(p['kind'] == 'shrub' for _, p in plants),
            'violations': len(violations),
            'outside_territory': len(outside),
            'leader_notes': len(notes),
            'approvals': len({a['id'] for a in approvals}),
            'approvals_by_zone': {z['title']: sum(a['rule'] == z['rule'] for a in approvals)
                                  for z in norms.get('protection_zones', [])},
            'by_rule': by_rule,
        },
        'violations': sorted(violations, key=lambda v: v['distance_m'] - v['norm_m']),
        'outside_territory': outside,
        'approvals': approvals,
        'leader_notes': notes,
        'plants': [{**_plant(plants[n]), 'near': sorted(near[n], key=lambda e: e['distance_m'] - e['norm_m'])}
                   for n in range(len(plants))],
    }


def _plant(item):
    geometry, props = item
    return {'id': props.get('id'), 'structure': props.get('structure'), 'zone': props.get('zone'),
            'name': props.get('name'), 'kind': props['kind'], 'x': round(geometry.x, 2), 'y': round(geometry.y, 2)}
