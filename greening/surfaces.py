"""Карта поверхностей территории: здания, покрытия, газоны; остаток — по граням линий и подписям."""

from collections import Counter, defaultdict

import shapely
from shapely import STRtree
from shapely.geometry import MultiPoint, Polygon
from shapely.ops import polygonize, unary_union

# при наложении побеждает тип левее: здание важнее штриховки газона, проезд — тротуара
PRIORITY = ('building', 'roadway', 'sidewalk', 'paving', 'playground', 'sealed', 'gravel',
            'flowerbed', 'lawn', 'ground', 'unknown')
# где можно сажать: газоны и то, про что чертёж ничего не говорит — неизвестное не запрещается,
# а попадает в секции с surface='unknown', чтобы решение принимали следующие шаги
GREEN = ('lawn', 'flowerbed', 'ground', 'unknown')
BUILDING_KINDS = ('building_wall', 'building_school_kindergarten')
# линии, по которым делится остаток территории без штриховок
EDGE_KINDS = ('kerb', 'street_edge', 'carriageway_edge', 'lawn_edge', 'vegetation_edge', 'site_outline',
              'fence', 'stairs_ramp', 'sidewalk_path', *BUILDING_KINDS)
# подписи покрытий берутся только с этих слоёв: «А» на слое сетей — не асфальт
LABEL_KINDS = (None, 'label', 'site_outline', 'lawn_edge', 'vegetation_edge', 'street_edge', 'kerb',
               'flowerbed', 'sidewalk_path', *BUILDING_KINDS)
# грань без подписи — здание, если такая доля её контура идёт по линиям зданий
BUILDING_EDGE_SHARE = 0.6
WALL_SNAP = 0.2
# грани меньше этой площади — щели между линиями, м²
MIN_FACE = 0.5


def _polygons(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [p for p in getattr(geometry, 'geoms', []) if p.geom_type == 'Polygon']


def _buildings(items):
    shapes = []
    for item in items:
        if item.kind not in BUILDING_KINDS:
            continue
        geometry = item.geometry
        if item.etype == 'HATCH':
            shapes.extend(_polygons(geometry))
        elif geometry.geom_type == 'LineString' and geometry.is_closed and len(geometry.coords) >= 4:
            polygon = Polygon(geometry.coords)
            if polygon.is_valid and polygon.area > 1.0:
                shapes.append(polygon)
    return unary_union(shapes) if shapes else Polygon()


def _labelled_faces(items, area, classifier):
    """Делит area по линиям-границам и назначает граням тип по подписям внутри."""
    if area.is_empty:
        return []
    shapely.prepare(area)
    lines = [i.geometry for i in items if i.kind in EDGE_KINDS and i.geometry.geom_type == 'LineString']
    lines = [line for line in lines if area.intersects(line)]
    lines.append(area.boundary)
    # граница area входит в разбиение, поэтому грань целиком внутри или целиком снаружи
    faces = [f for f in polygonize(unary_union(lines))
             if f.area >= MIN_FACE and area.contains(f.representative_point())]
    if not faces:
        return []

    labels = []
    for item in items:
        if item.etype in ('TEXT', 'MTEXT') and item.kind in LABEL_KINDS:
            match = classifier.label(item.text)
            if match and match[1]:
                labels.append((item.geometry, match[1], item.text))
    index = STRtree(faces)
    votes, points = defaultdict(Counter), defaultdict(list)
    for point, surface, _ in labels:
        for n in index.query(point, predicate='within'):
            votes[int(n)][surface] += 1
            points[int(n)].append((point, surface))

    # дома в топосъёмке часто нарисованы отдельными отрезками, а не замкнутым контуром
    walls = [i.geometry for i in items if i.kind in BUILDING_KINDS and i.geometry.geom_type == 'LineString']
    wall_index = STRtree(walls) if walls else None

    result = []
    for n, face in enumerate(faces):
        if votes[n] and len({s in GREEN for s in votes[n]}) > 1:
            # и газон, и покрытие в одной грани: линии не замкнулись, грань склеила разное —
            # делим её между подписями, каждой достаётся ближайшее к ней место
            result.extend((piece, surface, 'label_split') for piece, surface in _split_by_labels(face, points[n]))
            continue
        if votes[n]:
            # при разных покрытиях в одной грани берём тип с высшим приоритетом
            surface, source = min(votes[n], key=PRIORITY.index), 'labelled'
        elif wall_index is not None and _wall_share(face, walls, wall_index) >= BUILDING_EDGE_SHARE:
            surface, source = 'building', 'walls'
        else:
            surface, source = 'unknown', 'unlabelled'
        result.append((face, surface, source))
    return result


def _split_by_labels(face, labels):
    """Делит грань на ячейки Вороного вокруг подписей; ячейка получает тип своей подписи."""
    unique = {}
    for point, surface in labels:
        unique.setdefault((round(point.x, 2), round(point.y, 2)), (point, surface))
    if len(unique) == 1:
        (point, surface), = unique.values()
        return [(face, surface)]
    cells = shapely.voronoi_polygons(MultiPoint([p for p, _ in unique.values()]), extend_to=face)
    pieces = []
    for cell in cells.geoms:
        owner = next((s for p, s in unique.values() if cell.contains(p)), None)
        part = cell.intersection(face)
        if owner and not part.is_empty:
            pieces.append((part, owner))
    return pieces


def _wall_share(face, walls, index):
    """Доля контура грани, вдоль которой идут линии стен."""
    ring = face.exterior
    band = ring.buffer(WALL_SNAP)
    # длины кусков стен в полосе вдоль контура; наложения редки — дубликаты уже убраны
    along = sum(walls[int(n)].intersection(band).length for n in index.query(band, predicate='intersects'))
    return min(along / ring.length, 1.0)


def build(items, territory, classifier):
    """{тип: геометрия} — непересекающиеся полигоны внутри территории, и сводка по источникам."""
    layers = defaultdict(list)
    sources = Counter()

    buildings = _buildings(items).intersection(territory)
    if not buildings.is_empty:
        layers['building'].append(buildings)
        sources['building_outline'] += 1

    for item in items:
        if item.etype != 'HATCH':
            continue
        surface = item.surface
        if not surface and item.kind == 'lawn_edge':
            surface = 'lawn'
        elif not surface and item.kind == 'flowerbed':
            surface = 'flowerbed'
        if surface:
            layers[surface].append(item.geometry)
            sources['hatch'] += 1

    covered = unary_union([g for shapes in layers.values() for g in shapes]).intersection(territory)
    rest = territory.difference(covered)
    for face, surface, source in _labelled_faces(items, rest, classifier):
        layers[surface].append(face)
        sources['face_' + source] += 1

    result, taken = {}, Polygon()
    for surface in PRIORITY:
        if surface not in layers:
            continue
        shape = unary_union(layers[surface]).intersection(territory).difference(taken)
        shape = unary_union(_polygons(shape))
        if not shape.is_empty:
            result[surface] = shape
            taken = taken.union(shape)
    return result, dict(sources)
