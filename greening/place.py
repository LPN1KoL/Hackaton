"""Случайная рассадка растений, выбранных LLM, внутри зон — MVP перед ML-расстановкой.

    python -m greening.place РАЗМЕТКА.greening.geojson ПЛАН.plan.json [--seed 0] [--out ПАПКА]
    → *.placement.geojson (точки растений, пятна цветников и газонов, сводка) и *.placement.dxf

Нормативные отступы уже заложены в границы зон: секция пускает деревья, только если все отступы
для деревьев соблюдены (sections.py), поэтому дерево ставится только в зону, где разрешены деревья,
кустарник — где разрешены кустарники. Сверх норм соблюдаются правила рассадки из config/placement.json:
расстояния между новыми растениями (по кроне), от стволов существующих деревьев, шаг ряда.

Как ставится каждая роль из плана:
- солитер — по одному, в место, дальше всего от уже посаженного (из нескольких случайных);
- группа, куртина (шт) — кластеры по group_size вокруг случайных центров;
- рядовая посадка, живая изгородь — вдоль самой длинной стороны зоны, с отступом от края, шагом ряда;
- цветник, куртина (м²) — пятно нужной площади вокруг случайной точки;
- газон — оставшаяся часть зоны.
Что не поместилось, попадает в сводку (placed < requested) — количество не подгоняется молча.
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import substring, unary_union

from . import catalog

CONFIG = Path(__file__).resolve().parent / 'config' / 'placement.json'
KIND = {'ДЛ': 'tree', 'ДХ': 'tree', 'КЛ': 'shrub', 'КХ': 'shrub', 'Л': 'shrub',
        'М': 'herbaceous', 'О': 'herbaceous', 'Б': 'herbaceous', 'Г': 'herbaceous'}
ROW_ROLES = ('рядовая посадка', 'живая изгородь')
GROUP_ROLES = ('группа', 'куртина')
AREA_UNITS = ('м2', 'м²', 'кв.м')
LAWN_GROUP = 'Г'
# ячейка сетки поиска соседей, м (больше самого крупного шага)
CELL = 10.0
# координаты выводятся с точностью 1 см — проверка допускает такую погрешность, м
ROUNDING = 0.02
# точность подбора радиуса пятна цветника, м²
AREA_TOLERANCE = 0.5


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _crown(plant, kind, config):
    for flag in plant['flags'] if plant else ():
        if flag.startswith('к') and flag[1:].isdigit():
            return float(flag[1:])
    return config['crown_default_m'][kind]


class Field:
    """Уже посаженное (и существующие деревья) с сеткой для быстрой проверки расстояний."""

    def __init__(self, config, existing):
        self.config = config
        self.grid = defaultdict(list)
        for point in existing:
            self._put(point.x, point.y, 'existing', 0.0)

    def _put(self, x, y, kind, gap):
        self.grid[(math.floor(x / CELL), math.floor(y / CELL))].append((x, y, kind, gap))

    def _near(self, x, y, radius):
        cells = math.ceil(radius / CELL)
        cx, cy = math.floor(x / CELL), math.floor(y / CELL)
        for i in range(cx - cells, cx + cells + 1):
            for j in range(cy - cells, cy + cells + 1):
                yield from self.grid.get((i, j), ())

    def required(self, kind, gap, other_kind, other_gap):
        if other_kind == 'existing':
            return self.config['existing_tree_clearance_m'][kind]
        if kind == other_kind:
            return max(gap, other_gap)
        pair = '|'.join(sorted((kind, other_kind), key=('tree', 'shrub', 'herbaceous').index))
        return self.config['cross_spacing_m'].get(pair, 0.0)

    def ok(self, x, y, kind, gap, tolerance=0.0):
        reach = max(gap, max(self.config['existing_tree_clearance_m'].values()), 2.0)
        for ox, oy, other_kind, other_gap in self._near(x, y, reach):
            need = self.required(kind, gap, other_kind, other_gap) - tolerance
            if need > 0 and (ox - x) ** 2 + (oy - y) ** 2 < need * need:
                return False
        return True

    def clearance(self, x, y, radius=15.0):
        """Расстояние до ближайшего растения (для солитеров — чем дальше, тем лучше)."""
        best = radius
        for ox, oy, _, _ in self._near(x, y, radius):
            best = min(best, math.hypot(ox - x, oy - y))
        return best

    def add(self, x, y, kind, gap):
        self._put(x, y, kind, gap)


def _sample(polygon, rng, count):
    """До count случайных точек внутри polygon (равномерно)."""
    if polygon.is_empty or polygon.area <= 0:
        return []
    minx, miny, maxx, maxy = polygon.bounds
    share = polygon.area / max((maxx - minx) * (maxy - miny), 1e-9)
    n = int(count / max(share, 0.02)) + 8
    xs, ys = rng.uniform(minx, maxx, n), rng.uniform(miny, maxy, n)
    inside = shapely.contains_xy(polygon, xs, ys)
    return list(zip(xs[inside], ys[inside]))[:count]


class Placer:
    def __init__(self, config, existing, rng):
        self.config, self.rng = config, rng
        self.field = Field(config, existing)

    def gap(self, kind, role, crown):
        if role in ROW_ROLES:
            return self.config['row_spacing_m'][kind]
        spacing = self.config['spacing_m'][kind]
        return max(spacing, crown * self.config['crown_share']) if kind == 'tree' else spacing

    def _take(self, x, y, kind, gap, out):
        self.field.add(x, y, kind, gap)
        out.append((x, y))

    def solitary(self, zone, count, kind, gap):
        placed = []
        shapely.prepare(zone)
        for _ in range(count):
            options = [(x, y) for x, y in _sample(zone, self.rng, self.config['candidates'])
                       if self.field.ok(x, y, kind, gap)]
            if not options:
                break
            x, y = max(options, key=lambda p: self.field.clearance(*p))
            self._take(x, y, kind, gap, placed)
        return placed

    def groups(self, zone, count, kind, gap):
        size = self.config['group_size'][kind]
        placed = []
        shapely.prepare(zone)
        while len(placed) < count:
            centre = self.solitary(zone, 1, kind, gap)
            if not centre:
                break
            placed += centre
            cx, cy = centre[0]
            members, radius, misses = 1, gap * 1.5, 0
            while members < size and len(placed) < count and misses < 6:
                angles = self.rng.uniform(0, 2 * math.pi, self.config['candidates'])
                radii = radius * np.sqrt(self.rng.uniform(0, 1, self.config['candidates']))
                xs, ys = cx + radii * np.cos(angles), cy + radii * np.sin(angles)
                inside = shapely.contains_xy(zone, xs, ys)
                spot = next(((x, y) for x, y, i in zip(xs, ys, inside) if i and self.field.ok(x, y, kind, gap)), None)
                if spot is None:
                    radius *= 1.3
                    misses += 1
                    continue
                self._take(*spot, kind, gap, placed)
                members += 1
        return placed

    def row(self, zone, count, kind, gap):
        """Ряд вдоль самой длинной стороны зоны с отступом от края; не больше одного обхода контура."""
        placed = []
        inset = self.config['row_inset_m'][kind]
        for part in sorted(_polygons(zone), key=lambda p: -p.area):
            inner = part.buffer(-inset)
            inner = max(_polygons(inner), key=lambda p: p.area) if not inner.is_empty else part
            ring = LineString(inner.exterior.coords)
            if ring.length < gap:
                continue
            corners = list(ring.simplify(0.5).coords)
            a, b = max(zip(corners, corners[1:]), key=lambda s: math.dist(*s))
            start = ring.project(Point(a))
            steps = int(ring.length // gap)
            for i in range(steps):
                if len(placed) >= count:
                    return placed
                point = ring.interpolate((start + i * gap) % ring.length)
                if self.field.ok(point.x, point.y, kind, gap):
                    self._take(point.x, point.y, kind, gap, placed)
        return placed

    def patch(self, available, area):
        """Пятно площадью area внутри available: круг вокруг случайной точки, радиус подбирается."""
        if available.is_empty or area <= 0:
            return None
        if area >= available.area - AREA_TOLERANCE:
            return available
        spots = _sample(available, self.rng, 1)
        if not spots:
            return None
        centre = Point(spots[0])
        low, high = 0.0, math.sqrt(available.area) * 2 + 1
        best = None
        for _ in range(30):
            radius = (low + high) / 2
            best = available.intersection(centre.buffer(radius, quad_segs=8))
            if abs(best.area - area) <= AREA_TOLERANCE:
                break
            low, high = (radius, high) if best.area < area else (low, radius)
        return best


def _polygons(geometry):
    return [p for p in getattr(geometry, 'geoms', [geometry]) if p.geom_type == 'Polygon' and not p.is_empty]


def _quantity(item):
    try:
        return float(item.get('quantity') or 0)
    except (TypeError, ValueError):
        return 0.0


# порядок внутри зоны: крупное раньше мелкого, ряды раньше групп (им нужен край зоны)
ORDER = {('tree', 'row'): 0, ('tree', 'solitary'): 1, ('tree', 'group'): 2,
         ('shrub', 'row'): 3, ('shrub', 'group'): 4, ('shrub', 'solitary'): 5,
         ('herbaceous', 'row'): 6, ('herbaceous', 'group'): 7, ('herbaceous', 'solitary'): 8}


def _mode(role):
    if role in ROW_ROLES:
        return 'row'
    if role in GROUP_ROLES:
        return 'group'
    return 'solitary'


def place(geojson, plan, seed=0, config=None, log=print):
    config = config or load_config()
    rng = np.random.default_rng(seed)
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    existing = [g for g, p in features if p['layer'] == 'tree']
    plants = {p['id']: p for p in catalog.load()}
    placer = Placer(config, existing, rng)

    out, summary = [], []
    for structure in plan['structures']:
        zones = {z['id']: z for z in structure['zones']}
        by_zone = defaultdict(list)
        for item in structure.get('plantings', []):
            if item.get('zone') in zones:
                by_zone[item['zone']].append(item)
        for zone_id, items in by_zone.items():
            zone = zones[zone_id]
            geometry = unary_union([sections[s] for s in zone['sections'] if s in sections])
            if geometry.is_empty:
                continue
            base = {'structure': structure['id'], 'zone': zone_id, 'patch': zone.get('patch')}
            # край зоны — нормативная граница: точки ставятся чуть внутрь, чтобы округление их не вынесло
            inner = geometry.buffer(-ROUNDING)
            points, areas = [], []
            for item in items:
                kind = KIND.get(item.get('group'), 'herbaceous')
                if item.get('unit') in AREA_UNITS:
                    areas.append((item, kind))
                else:
                    points.append((item, kind))
            points.sort(key=lambda p: ORDER[(p[1], _mode(p[0].get('role')))])

            for item, kind in points:
                plant = plants.get(item.get('plant_id'))
                crown = _crown(plant, kind, config)
                gap = placer.gap(kind, item.get('role'), crown)
                count = int(round(_quantity(item)))
                mode = _mode(item.get('role'))
                if kind not in zone['allowed'] and not (kind == 'herbaceous'):
                    placed = []
                elif mode == 'row':
                    placed = placer.row(inner, count, kind, gap)
                elif mode == 'group':
                    placed = placer.groups(inner, count, kind, gap)
                else:
                    placed = placer.solitary(inner, count, kind, gap)
                for number, (x, y) in enumerate(placed, 1):
                    out.append({'type': 'Feature', 'geometry': mapping(Point(round(x, 2), round(y, 2))),
                                'properties': {'layer': 'plant', **base, 'plant_id': item.get('plant_id'),
                                               'name': item.get('name'), 'group': item.get('group'), 'kind': kind,
                                               'role': item.get('role'), 'crown_m': crown, 'spacing_m': gap,
                                               'number': number}})
                summary.append({**base, 'plant_id': item.get('plant_id'), 'name': item.get('name'), 'kind': kind,
                                'role': item.get('role'), 'unit': item.get('unit'),
                                'requested': count, 'placed': len(placed)})

            # цветники — пятна нужной площади, газон — что осталось; несколько газонов: меньшие пятнами
            taken = []
            lawns = sorted((a for a in areas if a[0].get('group') == LAWN_GROUP), key=lambda a: _quantity(a[0]))
            beds = [a for a in areas if a[0].get('group') != LAWN_GROUP]
            for index, (item, kind) in enumerate(beds + lawns):
                available = geometry.difference(unary_union(taken)) if taken else geometry
                last_lawn = item.get('group') == LAWN_GROUP and index == len(beds) + len(lawns) - 1
                polygon = available if last_lawn else placer.patch(available, _quantity(item))
                area = round(polygon.area, 1) if polygon is not None and not polygon.is_empty else 0.0
                if area:
                    taken.append(polygon)
                    out.append({'type': 'Feature',
                                'geometry': mapping(shapely.set_precision(polygon, 0.01)),
                                'properties': {'layer': 'lawn' if item.get('group') == LAWN_GROUP else 'bed', **base,
                                               'plant_id': item.get('plant_id'), 'name': item.get('name'),
                                               'group': item.get('group'), 'kind': kind, 'role': item.get('role'),
                                               'area_m2': area}})
                summary.append({**base, 'plant_id': item.get('plant_id'), 'name': item.get('name'), 'kind': kind,
                                'role': item.get('role'), 'unit': item.get('unit'),
                                'requested': round(_quantity(item), 1), 'placed': area})

    short = [s for s in summary if s['unit'] not in AREA_UNITS and s['placed'] < s['requested']]
    totals = defaultdict(lambda: [0, 0])
    for s in summary:
        key = s['kind'] if s['unit'] not in AREA_UNITS else f"{s['kind']}_m2"
        totals[key][0] += s['requested']
        totals[key][1] += s['placed']
    return {
        'type': 'FeatureCollection',
        'metadata': {'seed': seed, 'config': {k: v for k, v in config.items() if not k.startswith('_')},
                     'summary': {'totals': {k: {'requested': round(v[0], 1), 'placed': round(v[1], 1)}
                                            for k, v in totals.items()},
                                 'short': len(short)},
                     'plantings': summary},
        'features': out,
    }


def verify(result, geojson, config=None):
    """Независимая проверка рассадки: каждое растение в своей зоне, в зоне разрешён его вид посадки,
    расстояния между растениями и до существующих деревьев не меньше правил. Возвращает список нарушений."""
    config = config or load_config()
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: (g, p) for g, p in features if p['layer'] == 'section'}
    problems = []
    field = Field(config, [g for g, p in features if p['layer'] == 'tree'])
    plants = [(shape(f['geometry']), f['properties']) for f in result['features'] if f['properties']['layer'] == 'plant']
    index = shapely.STRtree([g for g, _ in sections.values()])
    geoms = list(sections.values())
    for point, props in plants:
        hits = [geoms[int(i)] for i in index.query(point, predicate='intersects')]
        allowed = set().union(*[set(p['allowed']) for _, p in hits]) if hits else set()
        if props['kind'] != 'herbaceous' and props['kind'] not in allowed:
            problems.append(f"{props['structure']}/{props['zone']} {props['name']}: {props['kind']} вне разрешённой секции")
        if not field.ok(point.x, point.y, props['kind'], props['spacing_m'], ROUNDING):
            problems.append(f"{props['structure']}/{props['zone']} {props['name']}: ближе правил к соседу")
        field.add(point.x, point.y, props['kind'], props['spacing_m'])
    return problems


# слои рассадки: всё, что добавляет сервис, начинается с Greening (так договорились в команде),
# по видам — чтобы в CAD их можно было включать по отдельности
LAYERS = {'tree': ('Greening_Деревья', 94), 'shrub': ('Greening_Кустарники', 62),
          'herbaceous': ('Greening_Травянистые', 51), 'bed': ('Greening_Цветники', 221),
          'lawn': ('Greening_Газон', 3)}


def add_layers(doc, result):
    """Рассадка на слоях Greening_*: деревья и кустарники — круг кроны и точка ствола, цветники и газон — заливки."""
    from .export import _area, _layer

    msp = doc.modelspace()
    for feature in result['features']:
        props = feature['properties']
        geometry = shape(feature['geometry'])
        if props['layer'] == 'plant':
            name, color = LAYERS[props['kind']]
            layer = _layer(doc, name, color)
            msp.add_circle((geometry.x, geometry.y), props['crown_m'] / 2, dxfattribs={'layer': layer})
            msp.add_point((geometry.x, geometry.y), dxfattribs={'layer': layer})
        else:
            name, color = LAYERS[props['layer']]
            _area(msp, geometry, _layer(doc, name, color), 0.6 if props['layer'] == 'lawn' else 0.3)


def write_dxf(result, path, base=None):
    """Отдельный DXF с рассадкой или, если задан base, она же поверх копии чертежа."""
    import ezdxf
    import ezdxf.recover
    from ezdxf import units

    if base:
        doc, _ = ezdxf.recover.readfile(base)
    else:
        doc = ezdxf.new('R2018', setup=True)
        doc.units = units.M
    add_layers(doc, result)
    doc.saveas(path)


def main():
    parser = argparse.ArgumentParser(description='Случайная рассадка растений плана внутри зон (MVP)')
    parser.add_argument('geojson', help='результат python -m greening (*.greening.geojson)')
    parser.add_argument('plan', help='результат python -m greening.recommend (*.plan.json)')
    parser.add_argument('--seed', type=int, default=0, help='зерно случайности: тот же seed — та же рассадка')
    parser.add_argument('--out', help='папка (по умолчанию рядом с разметкой)')
    args = parser.parse_args()

    source = Path(args.geojson)
    stem = source.name.replace('.greening.geojson', '')
    out = Path(args.out) if args.out else source.parent
    geojson = json.loads(source.read_text(encoding='utf-8'))
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    result = place(geojson, plan, args.seed)
    problems = verify(result, geojson)
    result['metadata']['summary']['violations'] = len(problems)
    (out / f'{stem}.placement.geojson').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    write_dxf(result, out / f'{stem}.placement.dxf')
    for problem in problems[:20]:
        print(problem, file=sys.stderr)
    print(json.dumps({'out': str(out / f'{stem}.placement.geojson'), **result['metadata']['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
