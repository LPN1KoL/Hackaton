"""Территория работ: контур из слоёв «Граница работ» с закрытием мелких разрывов."""

from collections import Counter

from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

# разрывы между концами линий границы до такой длины считаются недочерчиванием, м
MAX_GAP = 2.0
# запас вокруг всех объектов, если границы нет совсем, м
FALLBACK_MARGIN = 5.0


def _lines(geometry):
    if geometry.geom_type == 'LineString':
        return [geometry]
    if geometry.geom_type == 'Polygon':
        return [LineString(geometry.exterior.coords), *(LineString(r.coords) for r in geometry.interiors)]
    if hasattr(geometry, 'geoms'):
        return [line for part in geometry.geoms for line in _lines(part)]
    return []


def _close_gaps(lines, max_gap):
    """Соединяет висящие концы, у которых ровно один сосед ближе max_gap и взаимно."""
    network = unary_union(lines)
    segments = [network] if network.geom_type == 'LineString' else list(getattr(network, 'geoms', []))
    degree = Counter(p for s in segments for p in (s.coords[0], s.coords[-1]))
    ends = [Point(p) for p, n in degree.items() if n == 1]
    if not ends:
        return network, 0
    index = STRtree(ends)
    near = {}
    for i, point in enumerate(ends):
        near[i] = [int(j) for j in index.query(point.buffer(max_gap)) if j != i and point.distance(ends[j]) <= max_gap]
    links = []
    for i, candidates in near.items():
        if len(candidates) == 1 and near[candidates[0]] == [i] and i < candidates[0]:
            links.append(LineString([ends[i], ends[candidates[0]]]))
    return unary_union([network, *links]), len(links)


def _area(lines, max_gap):
    """Полигон из линий: грани с чётностью вложенности (внутренние замкнутые контуры — дыры)."""
    network, links = _close_gaps(lines, max_gap)
    area = Polygon()
    for face in polygonize(network):
        area = area.symmetric_difference(Polygon(face.exterior))
    return area, links


def resolve(items, max_gap=MAX_GAP):
    """(territory, info). Порядок: «Граница работ» → «Граница заказа» → охват всех объектов."""
    for kind, method in (('work_boundary', 'work_boundary'), ('order_boundary', 'order_boundary')):
        lines = [line for i in items if i.kind == kind for line in _lines(i.geometry)]
        if not lines:
            continue
        area, links = _area(lines, max_gap)
        if not area.is_empty and area.area > 1.0:
            return area, {'method': method, 'gaps_closed': links, 'area_m2': round(area.area, 1)}

    shapes = [i.geometry for i in items if i.etype not in ('TEXT', 'MTEXT', 'INSERT')]
    if not shapes:
        raise ValueError('В чертеже нет геометрии')
    area = unary_union(shapes).envelope.buffer(FALLBACK_MARGIN, join_style='mitre')
    return area, {'method': 'extent', 'gaps_closed': 0, 'area_m2': round(area.area, 1),
                  'note': 'Границы работ нет: взят охват всех объектов'}
