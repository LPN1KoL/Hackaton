"""Данные интерактивного просмотра результата (view.json для core/static/core/viewer.js).

Поверхности, существующие деревья, структуры с газонами и зонами (место, что закрыло деревья
и кустарники, объяснение из плана), посадки и рассадка — каждая точка ссылается на свою посадку.
"""

import re

from shapely.geometry import shape
from shapely.ops import unary_union

from . import catalog, place, recommend
from .split import MARGIN

SIMPLIFY = 0.05
SURFACES = ('roadway', 'sidewalk', 'paving', 'sealed', 'gravel', 'playground', 'building',
            'lawn', 'flowerbed', 'ground', 'unknown')
PLANTING_WORDS = {'tree': 'деревья', 'shrub': 'кустарники'}
# радиус точки посадочного места куста в группе на карте, м
GROUP_SPOT = 0.25


def _path(geometry):
    """SVG path; ось y переворачивается, чтобы текст не зеркалился."""
    parts = []
    geometry = geometry.simplify(SIMPLIFY)
    for polygon in getattr(geometry, 'geoms', [geometry]):
        if polygon.geom_type != 'Polygon' or polygon.is_empty:
            continue
        for ring in (polygon.exterior, *polygon.interiors):
            coords = list(ring.coords)[:-1]
            if len(coords) >= 3:
                parts.append('M' + 'L'.join(f'{x:.2f} {-y:.2f}' for x, y in coords) + 'Z')
    return ''.join(parts)


def _kind(allowed):
    if 'tree' in allowed and 'shrub' in allowed:
        return 'tree_shrub'
    for kind in ('tree', 'shrub', 'herbaceous'):
        if kind in allowed:
            return kind
    return 'none'


def _short(title):
    return re.split(r'[,(]', title)[0].strip().lower()


def _blocked_text(blocked):
    """«деревья: кабель связи 2 м — 80%, газопровод 1.5 м — 40% (743-ПП, табл. 3.6.1)» по каждому закрытому
    типу посадки; нормы одного документа — вместе, документ — после них."""
    lines = []
    for planting, rules in blocked.items():
        by_doc = {}
        for r in rules:
            by_doc.setdefault(r.get('doc', ''), []).append(f"{_short(r['object'])} {r['distance_m']:g} м — {round(r['share'] * 100)}%")
        if by_doc:
            lines.append(f"{PLANTING_WORDS[planting]}: " + '; '.join(
                ', '.join(items) + (f' ({doc})' if doc else '') for doc, items in by_doc.items()))
    return lines


def _labels(geometry, minimum):
    return [[round(q.x, 2), round(-q.y, 2)] for part in getattr(geometry, 'geoms', [geometry])
            if part.area >= minimum for q in [part.representative_point()]]


def build(geojson, plan=None, placement=None):
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    planned = {s['id']: s for s in plan['structures']} if plan else {}
    territory = unary_union([g for g, p in features if p['layer'] == 'territory'])
    minx, miny, maxx, maxy = territory.buffer(5).bounds
    data = {
        'view': [minx, -maxy, maxx - minx, maxy - miny],
        'territory': _path(territory),
        'surfaces': [{'kind': p['surface'], 'd': _path(g)} for g, p in features
                     if p['layer'] == 'surface' and p['surface'] in SURFACES],
        'trees': [[round(g.x, 2), round(-g.y, 2)] for g, p in features if p['layer'] == 'tree'],
        # у существующих деревьев в подоснове только значок ствола — крона рисуется условной
        'existing_crown': place.load_config().get('existing_crown_m', 6.0),
        'rows': ''.join(_path(g) for g, p in features if p['layer'] == 'tree_row'),
        'objects': [],
        'structures': [],
        'plants': [],
        'beds': [],
        'lawns': [],
        'groups': [],
    }
    refs = {}
    for g, p in features:
        if p['layer'] == 'structure':
            at = g if g.geom_type == 'Point' else g.representative_point()
            data['objects'].append({'type': recommend.OBJECTS.get(p['type'], p['type']),
                                    'x': round(at.x, 2), 'y': round(-at.y, 2),
                                    'd': '' if g.geom_type == 'Point' else _path(g)})

    for structure in recommend.structures(geojson):
        answer = planned.get(structure['id'], {})
        if answer and abs(answer.get('area_m2', 0) - structure['area_m2']) > 0.2:
            answer = {}
        explained = {z['id']: z.get('explanation', {}) for z in answer.get('zones', [])}
        plantings, details = {}, []
        for number, item in enumerate(answer.get('plantings', [])):
            plantings.setdefault(item['zone'], []).append(
                f"{item.get('name', item['plant_id'])}"
                + (f" — {item['quantity']} {item.get('unit', '')}".rstrip() if item.get('quantity') is not None else ''))
            # полное объяснение посадки: текст LLM (reason) и то, что пишет код (norms, facts, warnings)
            details.append({'i': number, 'zone': item['zone'], 'name': item.get('name', item.get('plant_id')),
                            'group': item.get('group'), 'role': item.get('role'), 'quantity': item.get('quantity'),
                            'unit': item.get('unit'), 'reason': item.get('reason', ''), 'norms': item.get('norms', []),
                            'facts': item.get('facts', []), 'warnings': item.get('warnings', []),
                            'by': item.get('by', 'llm'), 'placed': 0})
            refs[(structure['id'], item['zone'], item.get('plant_id'), item.get('role'))] = (structure['id'], number)
        zones = []
        for zone in structure['zones']:
            geometry = unary_union([sections[s] for s in zone['sections']])
            zones.append({'id': zone['id'], 'patch': zone['patch'], 'kind': _kind(zone['allowed']),
                          'area': zone['area_m2'], 'place': zone.get('place', ''), 'reason': zone.get('empty_reason', ''),
                          'tags': [structure['tag_titles'].get(t, t) for t in zone['tags']],
                          'blocked': _blocked_text(zone.get('blocked_by', {})),
                          'decision': explained.get(zone['id'], {}).get('decision', ''),
                          'plantings': plantings.get(zone['id'], []),
                          'explanation': explained.get(zone['id'], {}).get('place', ''),
                          'sections': len(zone['sections']), 'd': _path(geometry), 'labels': _labels(geometry, 2)})
        patches = [{'id': p['id'], 'd': _path(p['geometry']), 'labels': _labels(p['geometry'], 20),
                    'area': p['area_m2'], 'place': p['place'], 'small': p['small']} for p in structure['patches']]
        w = structure['geometry'].buffer(MARGIN).bounds
        data['structures'].append({
            'id': structure['id'], 'name': structure['name'], 'place': structure['place'],
            'area': structure['area_m2'], 'total': structure['total_m2'],
            'composition': recommend._composition_text(structure['composition']),
            'trees': structure['existing_trees'],
            'status': answer.get('status', ''), 'justification': answer.get('justification', ''),
            'window': [w[0], -w[3], w[2] - w[0], w[3] - w[1]],
            'outline': _path(structure['geometry']), 'patches': patches, 'zones': zones,
            'plantings': details, 'type': structure['type'],
        })

    # рассадка: каждая точка и пятно ссылаются на свою посадку (структура, номер) — по клику её объяснение
    by_id = {s['id']: s for s in data['structures']}
    for f in (placement or {}).get('features', []):
        p = f['properties']
        g = shape(f['geometry'])
        sid, number = refs.get((p['structure'], p['zone'], p.get('plant_id'), p.get('role')), (p['structure'], -1))
        if number >= 0 and p['layer'] != 'group':
            item = by_id[sid]['plantings'][number]
            item['placed'] = round(item['placed'] + (1 if p['layer'] == 'plant' else p.get('area_m2', 0)), 1)
        if p['layer'] == 'plant':
            # куст в группе или изгороди — точка посадочного места: крону показывает контур группы
            in_group = p['kind'] == 'shrub' and (p.get('group_size', 1) > 1 or p.get('role') in ('живая изгородь', 'рядовая посадка'))
            radius = GROUP_SPOT if in_group else round(p['crown_m'] / 2, 2)
            data['plants'].append([round(g.x, 2), round(-g.y, 2), radius, p['kind'],
                                   f"{p['name']} ({p['role']}), {p['structure']}/{p['zone']}", sid, number])
        elif p['layer'] == 'group':
            data['groups'].append({'d': _path(g), 't': f"{p['label']} — {p['role']}", 's': sid, 'i': number,
                                   'label': p['label'], 'at': _labels(g, 0)[:1]})
        else:
            data['beds' if p['layer'] == 'bed' else 'lawns'].append(
                {'d': _path(g), 't': f"{p['name']}: {p['area_m2']} м²", 's': sid, 'i': number})
    return data


# подписи — как в карточках просмотра (core/static/core/viewer.js)
KIND_NAMES = {'tree_shrub': 'деревья и кустарники', 'tree': 'деревья', 'shrub': 'кустарники',
              'herbaceous': 'только травянистые', 'none': 'ничего'}
GROUP_NAMES = {'ДЛ': 'дерево лиственное', 'ДХ': 'дерево хвойное', 'КЛ': 'кустарник лиственный',
               'КХ': 'кустарник хвойный', 'Л': 'лиана', 'М': 'многолетник', 'О': 'однолетник', 'Б': 'луковичные', 'Г': 'газон'}


def _decided(item, status):
    if item.get('by') == 'code':
        return 'код'
    return 'правила' if item.get('by') == 'rules' or status == 'rules' else 'LLM'


def report(data, plan):
    """Обоснование для выгрузки (justification.json): то же, что показывают карточки просмотра, без геометрии.

    data — результат build(); plan — plan.json (из него — отклонённые посадки и источник подбора)."""
    names = {p['id']: p['name'] for p in catalog.load()}
    planned = {s['id']: s for s in plan['structures']}
    structures = []
    for s in data['structures']:
        record = planned.get(s['id'], {})
        status = record.get('status', s['status'])
        structures.append({
            'структура': s['id'], 'название': s['name'], 'место': s['place'],
            'всего_м2': s['total'], 'газоны_м2': s['area'], 'состав': s['composition'],
            'существующих_деревьев': s['trees'], 'замысел': s['justification'],
            'посадки': [{
                'зона': p['zone'], 'растение': p['name'], 'группа': GROUP_NAMES.get(p['group'], p['group']),
                'приём': p['role'], 'количество': p['placed'], 'единица': 'шт' if p['unit'] == 'шт' else 'м²',
                'обоснование': p['reason'], 'нормы': p['norms'], 'свойства': p['facts'],
                'предупреждения': p['warnings'], 'решено': _decided(p, status),
            } for p in s['plantings']],
            'зоны': [{
                'зона': z['id'], 'газон': z['patch'], 'площадь_м2': z['area'], 'можно_сажать': KIND_NAMES[z['kind']],
                'место': z['explanation'] or z['place'], 'решение': z['decision'], 'условия': z['tags'],
                'почему_без_посадок': z['reason'], 'что_закрыло_деревья_и_кустарники': z['blocked'],
                'посадки': z['plantings'],
            } for z in s['zones']],
            'отклонено': [{
                'зона': r.get('zone'), 'растение': names.get(r.get('plant_id'), r.get('plant_id')),
                'приём': r.get('role'), 'причина': r['rejected'],
                **({'заменено_на': r['replaced_by']} if r.get('replaced_by') else {}),
            } for r in record.get('rejected', [])],
        })
    return {'подбор': 'LLM' if plan.get('source') == 'llm' else 'правила', 'структуры': structures}
