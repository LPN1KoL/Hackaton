"""Структуры территории: площадки по подписям, малые формы, существующие деревья и их ряды."""

import math
import re

import shapely
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union

from .surfaces import EDGE_KINDS, LABEL_KINDS

# куски условного знака дерева ближе этого расстояния — одно дерево, м
TREE_MERGE = 0.8
# полоса вокруг пунктира «Полоса деревьев», м
TREE_ROW_HALF_WIDTH = 1.5
# радиус поиска линий вокруг подписи площадки, м
LABEL_SEARCH = 40.0
# контур площадки, если замкнутой грани вокруг подписи не нашлось, м
LABEL_FALLBACK_RADIUS = 5.0
MAX_STRUCTURE_AREA = 5000.0
MIN_STRUCTURE_AREA = 10.0
# площадка обычно не больше этого; грань крупнее — скорее двор вокруг неё, м²
TYPICAL_AREA = 1500.0
# подпись бывает вынесена за контур площадки не дальше этого, м
LABEL_OFFSET = 6.0
# разрывы пунктирной границы площадки, м; закрываются только в окне GAP_SEARCH вокруг подписи
GAP = 1.2
GAP_SEARCH = 25.0
# площадки за границей работ тоже нужны: от края детской площадки нормируется отступ деревьев,
# условие «у детской площадки» действует на 10 м. Подпись ищется до LABEL_REACH от территории
# (столько же захватывает pipeline.MARGIN), площадка остаётся, если её контур ближе KEEP_REACH
LABEL_REACH = 15.0
KEEP_REACH = 10.0

POINT_OBJECTS = (
    (r'урн', 'urn'), (r'контейнер', 'container_site'), (r'вело', 'bike_parking'),
    (r'столбик|полусфер', 'bollard'), (r'скам', 'bench'), (r'маф', 'small_architecture'),
)


def _extend(line, length):
    """Линия, продлённая на length с обоих концов вдоль крайних отрезков."""
    coords = list(line.coords)
    if len(coords) < 2:
        return line
    (x0, y0), (x1, y1) = coords[0][:2], coords[1][:2]
    (xa, ya), (xb, yb) = coords[-2][:2], coords[-1][:2]
    first = math.hypot(x1 - x0, y1 - y0) or 1.0
    last = math.hypot(xb - xa, yb - ya) or 1.0
    start = (x0 - (x1 - x0) / first * length, y0 - (y1 - y0) / first * length)
    end = (xb + (xb - xa) / last * length, yb + (yb - ya) / last * length)
    return LineString([start, *[c[:2] for c in coords[1:-1]], end] if len(coords) > 2 else [start, end])


def _faces(near, gaps):
    """Замкнутые грани из линий. gaps — ещё и с закрытыми разрывами до GAP: границы площадок часто
    пунктирные, штрихи продлеваются на GAP / 2 с концов и при объединении сшиваются в сплошную линию."""
    if gaps:
        near = [_extend(line, GAP / 2) for line in near]
    faces = list(polygonize(unary_union(near)))
    return [f for f in faces if MIN_STRUCTURE_AREA <= f.area <= MAX_STRUCTURE_AREA]


def _pick(faces, point):
    """Наименьшая небольшая грань с подписью внутри, иначе ближайшая до LABEL_OFFSET."""
    small = [f for f in faces if f.area <= TYPICAL_AREA]
    inside = [f for f in small if f.contains(point)]
    if inside:
        return min(inside, key=lambda f: f.area)
    nearby = [f for f in small if f.distance(point) <= LABEL_OFFSET]
    return min(nearby, key=lambda f: (round(f.distance(point), 1), f.area)) if nearby else None


def _outline(point, lines_index, lines):
    """Контур площадки у подписи: наименьшая грань, в которой стоит подпись, а если такой нет
    (подпись вынесена за контур) — ближайшая грань не дальше LABEL_OFFSET."""
    near = [lines[int(n)] for n in lines_index.query(point.buffer(LABEL_SEARCH), predicate='intersects')]
    if not near:
        return None
    faces = _faces(near, gaps=False)
    found = _pick(faces, point)
    if found is None:
        close = [lines[int(n)] for n in lines_index.query(point.buffer(GAP_SEARCH), predicate='intersects')]
        found = _pick(_faces(close, gaps=True), point) if close else None
    if found is None:
        inside = [f for f in faces if f.contains(point)]
        found = min(inside, key=lambda f: f.area) if inside else None
    return found


def _labelled(items, territory, classifier):
    lines = [i.geometry for i in items if i.kind in EDGE_KINDS and i.geometry.geom_type == 'LineString']
    index = STRtree(lines) if lines else None
    reach = territory.buffer(LABEL_REACH)
    shapely.prepare(reach)
    found = []
    for item in items:
        if item.etype not in ('TEXT', 'MTEXT') or item.kind not in LABEL_KINDS:
            continue
        match = classifier.label(item.text)
        if not match or not match[0] or not reach.intersects(item.geometry):
            continue
        outline = _outline(item.geometry, index, lines) if index else None
        method = 'outline'
        if outline is None:
            outline, method = item.geometry.buffer(LABEL_FALLBACK_RADIUS), 'label_radius'
        if outline.distance(territory) > KEEP_REACH:
            continue
        found.append({'type': match[0], 'label': item.text, 'geometry': outline, 'method': method,
                      'outside': not territory.intersects(outline)})
    # одна площадка бывает подписана дважды — оставляем по одной на контур;
    # «СПЕЦ. ПОКРЫТИЕ» внутри подписанной площадки — её покрытие, а не отдельная структура
    unique, typed = {}, set()
    for record in found:
        key = shapely.normalize(record['geometry']).wkb
        unique.setdefault((record['type'], key), record)
        if record['type'] != 'special_surface':
            typed.add(key)
    return [r for (kind, key), r in unique.items() if not (kind == 'special_surface' and key in typed)]


def _point_objects(items, territory):
    found = []
    for item in items:
        if item.etype != 'INSERT' or not territory.intersects(item.geometry):
            continue
        name = item.block.casefold()
        kind = next((k for pattern, k in POINT_OBJECTS if re.search(pattern, name)), None)
        if kind:
            found.append({'type': kind, 'label': item.block.strip('_ '), 'geometry': item.geometry, 'method': 'block'})
    return found


def trees(items, territory):
    """(точки деревьев, полигон рядов деревьев) в пределах территории с запасом."""
    area = territory.buffer(TREE_ROW_HALF_WIDTH * 2)
    shapely.prepare(area)
    single, rows = [], []
    for item in items:
        if item.kind != 'tree' or item.etype in ('TEXT', 'MTEXT'):
            continue
        centre = item.geometry.centroid
        if centre.is_empty or not area.contains(centre):
            continue
        # «Полоса деревьев» — пунктир из мелких штрихов, остальное — знаки отдельных деревьев
        if 'полос' in item.layer.casefold():
            rows.append(centre)
        else:
            single.append(centre)
    points = []
    if single:
        blobs = unary_union(shapely.buffer(single, TREE_MERGE / 2))
        points = [blob.centroid for blob in getattr(blobs, 'geoms', [blobs])]
    row_area = unary_union(shapely.buffer(rows, TREE_ROW_HALF_WIDTH)) if rows else Point().buffer(0)
    return points, row_area


def find(items, territory, classifier):
    """Список структур: {'type', 'label', 'geometry', 'method'}."""
    return _labelled(items, territory, classifier) + _point_objects(items, territory)
