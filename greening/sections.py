"""Секции зелёных зон: газоны, разрезанные нормативными отступами и условиями места.

Считается по плиткам: каждая берёт из индекса только соседние объекты и обрезает их по своему окну,
поэтому объём работы растёт с площадью газонов, а не с размером всего чертежа.
"""

import json
import math
from collections import defaultdict
from pathlib import Path

import shapely
from shapely import STRtree
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from . import leaders
from .surfaces import GREEN

NORMS = Path(__file__).resolve().parent / 'config' / 'norms.json'
PLANTING_TYPES = ('tree', 'shrub')
# травянистые (газон, цветники, многолетники): в 743-ПП, табл. 3.6.1, отступов для них нет —
# разрешены на любой газонной поверхности, нормы деревьев и кустарников их не ограничивают
HERBACEOUS = 'herbaceous'
HERBACEOUS_SURFACES = ('lawn', 'flowerbed', 'ground')
SHAPE_TYPES = ('LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE', 'POINT', 'INSERT', 'HATCH')
# сторона плитки, м: крупные газоны режутся на плитки, секции потом склеиваются обратно
TILE = 60.0
# сегментов на четверть окружности у буферов: точнее не нужно, отступы в метрах
QUAD_SEGS = 4
# общая граница короче этого — касание, а не соседство, м
SHARED_EDGE = 0.01
# шум, который остаётся после склейки: площадь, м², и средняя ширина, м
NOISE_AREA = 0.05
NOISE_WIDTH = 0.05


def load_norms(path=NORMS):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _polygons(geometry):
    """Все непустые полигоны, в том числе из вложенных коллекций."""
    if geometry.geom_type == 'Polygon':
        return [] if geometry.is_empty else [geometry]
    return [p for part in getattr(geometry, 'geoms', []) for p in _polygons(part)]


def _parts(geometry):
    return list(getattr(geometry, 'geoms', [geometry]))


class Sources:
    """Геометрия объектов по источникам правил с пространственным индексом."""

    def __init__(self, items, surfaces, structures, trees, near, skip=frozenset()):
        """skip — индексы items, которые не объекты, а оформление (выноски и стрелки подписей сетей)."""
        shapely.prepare(near)
        self.surfaces, self.structures = surfaces, structures
        self.tree_points, self.tree_rows = trees
        self.by_kind = defaultdict(list)
        # неопознанные — только линии и точки: штриховки покрытий уже учтены как поверхности
        self.unknown = []
        for n, item in enumerate(items):
            if n not in skip and item.etype in SHAPE_TYPES and near.intersects(item.geometry):
                self.by_kind[item.kind].append(item.geometry)
                if item.kind is None and item.etype not in ('HATCH', 'INSERT'):
                    self.unknown.append(item.geometry)
        self.cache = {}

    def _shapes(self, source, obj):
        if source == 'layer':
            shapes = list(self.by_kind.get(obj, []))
            if obj == 'building_wall' and 'building' in self.surfaces:
                shapes += _parts(self.surfaces['building'])
            return shapes
        if source == 'surface':
            return _parts(self.surfaces[obj]) if obj in self.surfaces else []
        if source == 'structure':
            return [s['geometry'] for s in self.structures if s['type'] == obj]
        if source == 'trees':
            return [*self.tree_points, *_parts(self.tree_rows)]
        if source == 'unknown':
            return list(self.unknown)
        raise ValueError(f'Неизвестный источник правила: {source}')

    def indexed(self, source, obj):
        key = (source, obj)
        if key not in self.cache:
            shapes = [s for s in self._shapes(source, obj) if not s.is_empty]
            self.cache[key] = (shapes, STRtree(shapes) if shapes else None)
        return self.cache[key]

    def zone(self, source, obj, distance, area):
        """Полоса distance вокруг объектов источника, в пределах area."""
        shapes, index = self.indexed(source, obj)
        if index is None:
            return Polygon()
        minx, miny, maxx, maxy = area.bounds
        pad = distance + 0.5
        window = (minx - pad, miny - pad, maxx + pad, maxy + pad)
        found = index.query(box(*window))
        if not len(found):
            return Polygon()
        # обрезка по окну: края обрезки дальше distance от area и в зону не попадают
        clipped = [c for c in _clip([shapes[int(n)] for n in found], window) if not c.is_empty]
        if not clipped:
            return Polygon()
        return unary_union(shapely.buffer(clipped, distance, quad_segs=QUAD_SEGS)).intersection(area)


def _clip(shapes, window):
    try:
        return list(shapely.clip_by_rect(shapes, *window))
    except shapely.errors.GEOSException:
        # в исходных данных встречаются вырожденные контуры — пропускаем только их
        result = []
        for shape in shapes:
            try:
                result.append(shapely.clip_by_rect(shape, *window))
            except shapely.errors.GEOSException:
                continue
        return result


def _split(pieces, name, zone):
    """Каждый кусок делится на часть внутри зоны (получает метку name) и снаружи."""
    if zone.is_empty:
        return pieces
    shapely.prepare(zone)
    result = []
    for geometry, marks in pieces:
        if not zone.intersects(geometry):
            result.append((geometry, marks))
        elif zone.contains(geometry):
            result.append((geometry, marks | {name}))
        else:
            for part, extra in ((geometry.intersection(zone), {name}), (geometry.difference(zone), set())):
                part = unary_union(_polygons(part))
                if not part.is_empty:
                    result.append((part, marks | extra))
    return result


def _tiles(polygon):
    minx, miny, maxx, maxy = polygon.bounds
    if maxx - minx <= TILE and maxy - miny <= TILE:
        return [polygon]
    tiles = []
    for x in range(math.floor(minx / TILE), math.floor(maxx / TILE) + 1):
        for y in range(math.floor(miny / TILE), math.floor(maxy / TILE) + 1):
            part = shapely.clip_by_rect(polygon, x * TILE, y * TILE, (x + 1) * TILE, (y + 1) * TILE)
            tiles.extend(p for p in _polygons(part) if p.area > 0)
    return tiles


def build(items, surfaces, structures, trees, norms=None):
    """Секции: [{'id', 'geometry', 'area_m2', 'surface', 'allowed', 'tags', 'rules'}] и сводка."""
    norms = norms or load_norms()
    green_parts = [(surfaces[s], s) for s in GREEN if s in surfaces]
    if not green_parts:
        return [], {'green_m2': 0.0, 'sections': 0}
    green = unary_union([g for g, _ in green_parts])

    reach = max([r.get(t, 0) for r in norms['distance_rules'] for t in PLANTING_TYPES] +
                [c['distance'] for c in norms['context']])
    skip = leaders.find(items, {r['object'] for r in norms['distance_rules'] if r['source'] == 'layer'})
    sources = Sources(items, surfaces, structures, trees, green.buffer(reach, quad_segs=QUAD_SEGS), skip)
    rules = [(planting, rule) for rule in norms['distance_rules'] for planting in PLANTING_TYPES if planting in rule]

    # признаки куска: поверхность, запреты по типам посадок, условия места и сработавшие правила
    signed = defaultdict(list)
    notes = defaultdict(list)
    for geometry, surface in green_parts:
        for polygon in _polygons(geometry):
            for tile in _tiles(polygon):
                shapely.prepare(tile)
                pieces = [(tile, frozenset({'surface:' + surface}))]
                for planting, rule in rules:
                    zone = sources.zone(rule['source'], rule['object'], rule[planting], tile)
                    pieces = _split(pieces, f"rule:{planting}:{rule['object']}", zone)
                for context in norms['context']:
                    zone = sources.zone(context['source'], context['object'], context['distance'], tile)
                    if context.get('split', True):
                        pieces = _split(pieces, 'tag:' + context['tag'], zone)
                    elif not zone.is_empty:
                        notes[context['tag']].append(zone)
                for piece, marks in pieces:
                    signed[marks].append(piece)

    rule_info = {f"rule:{planting}:{rule['object']}": (planting, rule) for planting, rule in rules}

    # секция определяется тем, что важно для посадки: поверхность, что можно сажать, условия места;
    # конкретные правила — её свойство, а не повод резать газон ещё мельче
    groups = defaultdict(list)
    for marks, pieces in signed.items():
        rule_marks = frozenset(m for m in marks if m.startswith('rule:'))
        banned = frozenset(rule_info[m][0] for m in rule_marks)
        # где сажать нельзя ничего, условия места не нужны — такие куски склеиваются целиком
        tags = frozenset() if banned >= set(PLANTING_TYPES) else frozenset(m[4:] for m in marks if m.startswith('tag:'))
        key = (next(m[8:] for m in marks if m.startswith('surface:')), banned, tags)
        groups[key].extend((piece, rule_marks) for piece in pieces)

    sections = []
    for (surface, banned, tags), pieces in groups.items():
        for piece, marks in pieces:
            sections.append({
                'geometry': piece,
                'surface': surface,
                'allowed': [t for t in PLANTING_TYPES if t not in banned]
                           + ([HERBACEOUS] if surface in HERBACEOUS_SURFACES else []),
                'tags': set(tags),
                'rule_marks': set(marks),
                'narrowed': set(),
            })
    sections = _dissolve(sections)

    # сначала склейка полосок, потом проверка ширины: полоска в 20 см рядом с большой зоной
    # не «слишком узкая» — посадка стоит на соседнем месте и её просто перекрывает
    minimum = norms.get('min_section_area_m2', 1.0)
    widths = norms.get('min_planting_width_m', {})
    sections, absorbed = _absorb(sections, minimum)
    narrowed = _narrow(sections, widths, minimum)
    if narrowed:
        sections, more = _absorb(_dissolve(sections), minimum)
        absorbed += more
    sections, dropped = _drop_noise(sections)

    # пометки (колодец, ограда, неопознанный объект рядом) — у секций, которые их задевают
    for tag, zones in notes.items():
        zone = unary_union(zones)
        shapely.prepare(zone)
        for section in sections:
            if set(section['allowed']) & set(PLANTING_TYPES) and zone.intersects(section['geometry']):
                section['tags'].add(tag)

    for section in sections:
        section['area_m2'] = round(section['geometry'].area, 1)
        section['tags'] = sorted(section['tags'])
        section['rules'] = []
        for mark in sorted(section.pop('rule_marks')):
            planting, rule = rule_info[mark]
            section['rules'].append({'planting': planting, 'rule': rule['rule'], 'doc': rule['doc'],
                                     'object': rule['title'], 'distance_m': rule[planting],
                                     **({'assumption': rule['assumption']} if 'assumption' in rule else {})})
        for planting in sorted(section.pop('narrowed')):
            section['rules'].append({'planting': planting, 'rule': 'MIN-WIDTH', 'doc': 'config/norms.json',
                                     'object': 'Посадка не помещается по ширине', 'distance_m': widths[planting],
                                     'assumption': 'Минимальная ширина места под посадку — параметр модуля, не норматив'})

    sections.sort(key=lambda s: (-round(s['geometry'].centroid.y), s['geometry'].centroid.x))
    for number, section in enumerate(sections, 1):
        section['id'] = f'S{number:05d}'
    summary = {
        'green_m2': round(green.area, 1),
        'sections': len(sections),
        'narrowed_sections': narrowed,
        'absorbed_slivers': absorbed,
        'dropped_noise_m2': round(dropped, 2),
        'leaders_skipped': len(skip),
        'tags': {c['tag']: c['title'] for c in norms['context']},
        'planting_types': {
            'tree': 'Деревья: отступы по 743-ПП, табл. 3.6.1 (список в rules секции)',
            'shrub': 'Кустарники: отступы по 743-ПП, табл. 3.6.1 (список в rules секции)',
            HERBACEOUS: 'Газон, цветники, многолетники: в 743-ПП, табл. 3.6.1, отступов для травянистых нет',
        },
    }
    return sections, summary


def _key(section):
    return section['surface'], tuple(section['allowed']), frozenset(section['tags'])


def _dissolve(sections):
    """Склеивает соседние секции с одинаковыми поверхностью, разрешениями и условиями места."""
    groups = defaultdict(list)
    for section in sections:
        groups[_key(section)].append(section)
    result = []
    for (surface, allowed, tags), members in groups.items():
        probes = [(m['geometry'].representative_point(), m) for m in members]
        for polygon in _polygons(unary_union([m['geometry'] for m in members])):
            shapely.prepare(polygon)
            inside = [m for point, m in probes if polygon.contains(point)]
            result.append({
                'geometry': polygon, 'surface': surface, 'allowed': list(allowed), 'tags': set(tags),
                'rule_marks': set().union(*(m['rule_marks'] for m in inside)),
                'narrowed': set().union(*(m['narrowed'] for m in inside)),
            })
    return result


def _narrow(sections, widths, minimum):
    """Снимает тип посадки там, где не помещается круг его минимальной ширины. Только сужает разрешения.

    Секции меньше minimum не проверяются: это островки строже соседей, ширина у них не при чём.
    """
    changed = 0
    for section in sections:
        if section['geometry'].area < minimum:
            continue
        for planting in list(section['allowed']):
            width = widths.get(planting)
            if width and section['geometry'].buffer(-width / 2, quad_segs=QUAD_SEGS).is_empty:
                section['allowed'].remove(planting)
                section['narrowed'].add(planting)
                changed += 1
        if not set(section['allowed']) & set(PLANTING_TYPES):
            section['tags'] = set()
    return changed


def _absorb(sections, minimum):
    """Секции меньше minimum приклеиваются к соседу с не меньшими ограничениями, пока есть что клеить.

    Сосед выбирается по самой длинной общей границе, секция перенимает его признаки: разрешения
    соседа не шире своих, так что запрет не теряется. Мелкие клеятся первыми, в том числе к
    таким же мелким, — цепочки полосок собираются за несколько проходов.
    """
    absorbed = 0
    while True:
        index = STRtree([s['geometry'] for s in sections])
        gone, merged = set(), 0
        for n in sorted(range(len(sections)), key=lambda k: sections[k]['geometry'].area):
            piece = sections[n]
            geometry = piece['geometry']
            if n in gone or geometry.area >= minimum:
                continue
            # сосед с теми же разрешениями лучше более строгого: иначе полоска, где кустарник можно,
            # уходит в соседнюю «нельзя» и раздувает запрет; среди равных — самая длинная общая граница
            best, best_rank = None, None
            for m in index.query(geometry, predicate='intersects'):
                m = int(m)
                if m == n or m in gone or not set(sections[m]['allowed']) <= set(piece['allowed']):
                    continue
                length = geometry.boundary.intersection(sections[m]['geometry'].boundary).length
                rank = (len(sections[m]['allowed']), length)
                if length > SHARED_EDGE and (best_rank is None or rank > best_rank):
                    best, best_rank = m, rank
            if best is None:
                continue
            host = sections[best]
            joined = _polygons(unary_union([host['geometry'], geometry]))
            if not joined:
                continue
            host['geometry'] = max(joined, key=lambda p: p.area)
            gone.add(n)
            merged += 1
        sections = [s for k, s in enumerate(sections) if k not in gone]
        absorbed += merged
        if not merged:
            return sections, absorbed


def _drop_noise(sections):
    """Отбрасывает оставшиеся пылинки: меньше NOISE_AREA или уже NOISE_WIDTH (ширина ≈ 2·S/P)."""
    kept, dropped = [], 0.0
    for section in sections:
        geometry = section['geometry']
        if geometry.area < NOISE_AREA or 2 * geometry.area / geometry.length < NOISE_WIDTH:
            dropped += geometry.area
        else:
            kept.append(section)
    return kept, dropped
