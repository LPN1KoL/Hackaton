"""Рассадка растений, выбранных LLM, внутри зон — по нормам, правилам посадки и приёмам композиции.

    python -m greening.place РАЗМЕТКА.greening.geojson ПЛАН.plan.json [--seed 0] [--out ПАПКА]
    → *.placement.geojson (растения, контуры групп, пятна цветников и газона, сводка) и *.placement.dxf

Нормы. Нормативные отступы заложены в границы зон: секция пускает деревья, только если все отступы для
деревьев соблюдены (sections.py). Дерево ставится только в зону, где разрешены деревья, куст — где кусты.

Правила посадки (config/placement.json) — расстояния от кроны каждого вида:
- кусты одной группы — на 0,7 кроны (сажают плотнее взрослой кроны), живая изгородь — на треть кроны;
- разные группы, изгороди, одиночные кусты — край кроны к краю плюс зазор: группы не срастаются и читаются;
- куст не под кроной нового дерева, деревья — не ближе 0,8 кроны, от существующих стволов — отступ.

Композиция (разнообразие — от seed и стиля структуры):
- группы нечётные (3, 5, 7, 9); форма — треугольник, плотное пятно или вытянутая куртина по сотовой сетке
  с лёгким сдвигом; куртина вытянута вдоль ближайшего края газона (вдоль дорожки);
- центры групп и солитеры — в открытых местах, подальше от уже посаженного, с поправкой на стиль:
  регулярный (улицы) — ближе к краям и ровнее, пейзажный (дворы, скверы) — свободнее;
- изгородь и ряд — вдоль края зоны, обращённого к тротуару или проезду; не помещается — второй ряд
  в шахматном порядке;
- цветник — пятно округлой свободной формы в стороне от кустов; газон — остаток зоны.
Что не поместилось, попадает в сводку (placed < requested) — количество не подгоняется молча.
"""

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, MultiPoint, Point, mapping, shape
from shapely.ops import linemerge, unary_union

from . import catalog

CONFIG = Path(__file__).resolve().parent / 'config' / 'placement.json'
KIND = {'ДЛ': 'tree', 'ДХ': 'tree', 'КЛ': 'shrub', 'КХ': 'shrub', 'Л': 'shrub',
        'М': 'herbaceous', 'О': 'herbaceous', 'Б': 'herbaceous', 'Г': 'herbaceous'}
ROW_ROLES = ('рядовая посадка', 'живая изгородь')
GROUP_ROLES = ('группа', 'куртина')
AREA_UNITS = ('м2', 'м²', 'кв.м')
LAWN_GROUP = 'Г'
PATH_SURFACES = ('sidewalk', 'paving', 'sealed', 'roadway', 'playground', 'gravel')
# ячейка сетки поиска соседей, м
CELL = 8.0
# дальше этого соседи не проверяются (больше самого крупного нужного расстояния), м
REACH = 12.0
# координаты выводятся с точностью 1 см — проверка допускает такую погрешность, м
ROUNDING = 0.02
# точность подбора площади пятна цветника, м²
AREA_TOLERANCE = 0.5
SQRT3 = math.sqrt(3)


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _crown(plant, kind, config):
    for flag in plant['flags'] if plant else ():
        if flag.startswith('к') and flag[1:].isdigit():
            return float(flag[1:])
    return config['crown_default_m'][kind]


class Field:
    """Посаженное (и существующие деревья) с сеткой для быстрой проверки расстояний.

    Растение — (x, y, вид, крона, шаг, группа). Нужное расстояние между двумя растениями — required().
    """

    def __init__(self, config, existing):
        self.config = config
        self.grid = defaultdict(list)
        for point in existing:
            self._put((point.x, point.y, 'existing', 0.0, 0.0, None))

    def _put(self, entry):
        self.grid[(math.floor(entry[0] / CELL), math.floor(entry[1] / CELL))].append(entry)

    def _near(self, x, y, radius=REACH):
        cells = math.ceil(radius / CELL)
        cx, cy = math.floor(x / CELL), math.floor(y / CELL)
        for i in range(cx - cells, cx + cells + 1):
            for j in range(cy - cells, cy + cells + 1):
                yield from self.grid.get((i, j), ())

    def required(self, a, b):
        """Расстояние между растениями a и b (кортежи без координат: вид, крона, шаг, группа)."""
        kind, crown, spacing, group = a
        other, other_crown, other_spacing, other_group = b
        c = self.config
        if other == 'existing':
            return c['existing_tree_clearance_m'][kind]
        if kind == 'existing':
            return c['existing_tree_clearance_m'][other]
        if 'herbaceous' in (kind, other):
            return 0.0
        if group is not None and group == other_group:
            return max(spacing, other_spacing)
        if kind == other == 'tree':
            return max(spacing, other_spacing)
        if kind == other == 'shrub':
            return (crown + other_crown) / 2 + c['group_gap_m']
        tree_crown = crown if kind == 'tree' else other_crown
        return max(c['tree_shrub_min_m'], c['under_tree_share'] * tree_crown / 2)

    def ok(self, x, y, plant, tolerance=0.0):
        for ox, oy, *other in self._near(x, y):
            need = self.required(plant, tuple(other)) - tolerance
            if need > 0 and (ox - x) ** 2 + (oy - y) ** 2 < need * need:
                return False
        return True

    def clearance(self, x, y, kinds=('tree', 'shrub', 'existing'), radius=REACH):
        """Расстояние до ближайшего растения заданных видов (не дальше radius)."""
        best = radius
        for ox, oy, kind, *_ in self._near(x, y, radius):
            if kind in kinds:
                best = min(best, math.hypot(ox - x, oy - y))
        return best

    def add(self, x, y, plant):
        self._put((x, y, *plant))


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


def _polygons(geometry):
    return [p for p in getattr(geometry, 'geoms', [geometry]) if p.geom_type == 'Polygon' and not p.is_empty]


def _lines(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'LineString':
        return [geometry]
    if geometry.geom_type == 'MultiLineString':
        merged = linemerge(geometry)
        return list(getattr(merged, 'geoms', [merged]))
    return [g for part in getattr(geometry, 'geoms', []) for g in _lines(part)]


def _edge_angle(zone, x, y):
    """Направление ближайшего края зоны в точке (для ориентации куртин вдоль дорожек)."""
    ring = zone.boundary
    at = ring.project(Point(x, y))
    a, b = ring.interpolate(max(at - 1.0, 0)), ring.interpolate(min(at + 1.0, ring.length))
    return math.atan2(b.y - a.y, b.x - a.x)


def _hex_layout(n, spacing, aspect, angle, rng, jitter):
    """n точек сотовой сетки с шагом spacing внутри эллипса с соотношением осей aspect, повёрнутого на angle."""
    if n == 1:
        return [(0.0, 0.0)]
    if n == 3 and aspect < 1.3:
        # треугольник — классическая группа из трёх
        r = spacing / SQRT3
        base = rng.uniform(0, 2 * math.pi)
        pts = [(r * math.cos(base + k * 2 * math.pi / 3), r * math.sin(base + k * 2 * math.pi / 3)) for k in range(3)]
    else:
        ab = n * spacing * spacing * SQRT3 / 2 / math.pi
        a, b = math.sqrt(ab * aspect), math.sqrt(ab / aspect)
        span = int(max(a, b) / spacing) + 3
        lattice = [((i + 0.5 * (j % 2)) * spacing, j * spacing * SQRT3 / 2)
                   for i in range(-span, span + 1) for j in range(-span, span + 1)]
        # ближе к центру эллипса — раньше; сдвиг решётки делает группу несимметричной
        dx, dy = rng.uniform(-0.5, 0.5) * spacing, rng.uniform(-0.5, 0.5) * spacing
        lattice = [(x + dx, y + dy) for x, y in lattice]
        lattice.sort(key=lambda p: (p[0] / a) ** 2 + (p[1] / b) ** 2)
        pts = lattice[:n]
        mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        pts = [(x - mx, y - my) for x, y in pts]
    ca, sa = math.cos(angle), math.sin(angle)
    return [(x * ca - y * sa + rng.normal(0, jitter * spacing), x * sa + y * ca + rng.normal(0, jitter * spacing))
            for x, y in pts]


class Placer:
    def __init__(self, config, existing, rng, paths):
        self.config, self.rng = config, rng
        self.field = Field(config, existing)
        self.paths = paths
        self.group_seq = 0
        self.outlines = []          # контуры групп и изгородей для вывода

    # --- параметры вида -------------------------------------------------------------------------------

    def tree_spacing(self, crown, row=False):
        c = self.config
        base = c['tree_row_spacing_m'] if row else c['tree_spacing_m']
        return max(base, c['crown_share'] * crown)

    def shrub_spacing(self, crown, hedge=False):
        c = self.config
        if hedge:
            return min(max(c['hedge_share'] * crown, c['hedge_min_m']), c['hedge_max_m'])
        return max(c['shrub_share'] * crown, c['shrub_min_m'])

    def inset(self, zone, kind, crown):
        """Зона для центров: у кустов крона не нависает над краем (если зона от этого не пропадает)."""
        if kind != 'shrub':
            return zone
        depth = min(self.config['inset_share'] * crown, self.config['inset_max_m'])
        inner = zone.buffer(-depth)
        return inner if not inner.is_empty and inner.area > 0.3 * zone.area else zone

    def _new_group(self):
        self.group_seq += 1
        return self.group_seq

    def _take(self, x, y, plant, out):
        self.field.add(x, y, plant)
        out.append((x, y, plant))

    # --- солитеры --------------------------------------------------------------------------------------

    def solitary(self, zone, count, kind, crown, style):
        spacing = self.tree_spacing(crown) if kind == 'tree' else self.shrub_spacing(crown)
        placed = []
        shapely.prepare(zone)
        for _ in range(count):
            plant = (kind, crown, spacing, self._new_group())
            options = [(x, y) for x, y in _sample(zone, self.rng, self.config['candidates'])
                       if self.field.ok(x, y, plant)]
            if not options:
                break
            x, y = max(options, key=lambda p: self._open_score(zone, p, crown, style))
            self._take(x, y, plant, placed)
        return placed

    def _open_score(self, zone, p, crown, style):
        """Открытое место: дальше от посаженного (до разумного предела), крона внутри газона, чуть случайности;
        регулярный стиль тянет к краю, пейзажный — к середине."""
        x, y = p
        room = min(self.field.clearance(x, y), 2.5 * crown)
        edge = zone.boundary.distance(Point(x, y))
        inside = min(edge, crown / 2)
        pull = -edge if style == 'formal' else min(edge, 3 * crown)
        return room + 0.6 * inside + 0.15 * pull + self.rng.uniform(0, 0.25 * crown)

    # --- группы и куртины ------------------------------------------------------------------------------

    def _sizes(self, count, kind, crown):
        c = self.config['group_sizes']
        options = c['tree'] if kind == 'tree' else c['small'] if crown < 1.5 else c['medium'] if crown < 3 else c['large']
        sizes = []
        while count > 0:
            size = int(self.rng.choice(options))
            size = min(size, count)
            if size % 2 == 0 and size > 1:
                size -= 1                  # нечётные группы смотрятся естественнее
            sizes.append(max(size, 1))
            count -= sizes[-1]
        return sizes

    def groups(self, zone, count, kind, crown, style, meta):
        spacing = self.tree_spacing(crown) if kind == 'tree' else self.shrub_spacing(crown)
        placed = []
        shapely.prepare(zone)
        for size in self._sizes(count, kind, crown):
            group = self._try_group(zone, size, kind, crown, spacing, style, meta)
            while group is None and size > 1:
                size -= 2 if size > 2 else 1
                group = self._try_group(zone, size, kind, crown, spacing, style, meta)
            if group is None:
                break
            placed += group
        return placed

    def _try_group(self, zone, size, kind, crown, spacing, style, meta, attempts=4):
        """Группа целиком или ничего: центр — открытое место, форма — случайная из подходящих."""
        gid = self._new_group()
        plant = (kind, crown, spacing, gid)
        landscape = style == 'landscape'
        for _ in range(attempts):
            centres = [(x, y) for x, y in _sample(zone, self.rng, self.config['candidates'] // 2)]
            if not centres:
                return None
            centres.sort(key=lambda p: -self._open_score(zone, p, crown, style))
            for cx, cy in centres[:6]:
                drift = size >= 5 and self.rng.random() < (0.7 if landscape else 0.35)
                aspect = self.rng.uniform(1.8, 3.2) if drift else self.rng.uniform(1.0, 1.4)
                angle = _edge_angle(zone, cx, cy) + (self.rng.normal(0, 0.25) if landscape else 0.0)
                # решётка с запасом шага — лёгкий случайный сдвиг не сближает кусты сильнее шага
                jitter = 0.05 if landscape else 0.02
                layout = _hex_layout(size, spacing * (1 + 3 * jitter), aspect, angle, self.rng, jitter)
                pts = [(cx + dx, cy + dy) for dx, dy in layout]
                if min((math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]), default=spacing) < spacing:
                    continue
                if not all(zone.contains(Point(p)) for p in pts):
                    continue
                if not all(self.field.ok(x, y, plant) for x, y in pts):
                    continue
                out = []
                for x, y in pts:
                    self._take(x, y, plant, out)
                self._outline(out, crown, 'drift' if drift else 'group' if size > 1 else 'single', meta)
                return out
        return None

    def _outline(self, points, crown, layout, meta, line=None):
        if len(points) < 2 and line is None:
            return
        shape_ = (line.buffer(crown / 2 * 0.9, cap_style='round') if line is not None else
                  unary_union([Point(x, y).buffer(crown / 2 * 0.9, quad_segs=6) for x, y, _ in points])
                  .buffer(0.3, quad_segs=4).buffer(-0.3, quad_segs=4))
        self.outlines.append({'geometry': shape_, 'count': len(points), 'layout': layout,
                            'group_id': points[0][2][3] if points else None, **meta})

    # --- ряды и живые изгороди -------------------------------------------------------------------------

    def _guides(self, zone, offset):
        """Линии для ряда: край зоны со сдвигом внутрь; сначала участки у тротуара и проезда, потом прочие."""
        guides = []
        for part in sorted(_polygons(zone), key=lambda p: -p.area):
            inner = part.buffer(-offset)
            if inner.is_empty:
                continue
            for poly in _polygons(inner):
                ring = LineString(poly.exterior.coords)
                near = ring.intersection(self.paths.buffer(self.config['edge_reach_m'] + offset)) \
                    if self.paths is not None and not self.paths.is_empty else LineString()
                preferred = sorted(_lines(near), key=lambda l: -l.length)
                guides += [(l, True) for l in preferred if l.length > 2]
                corners = list(ring.simplify(0.5).coords)
                if len(corners) > 1:
                    a, b = max(zip(corners, corners[1:]), key=lambda s: math.dist(*s))
                    start = ring.project(Point(a))
                    guides.append((_rotate_ring(ring, start), False))
        return guides

    def row(self, zone, count, kind, crown, style, meta):
        hedge = kind == 'shrub'
        spacing = self.shrub_spacing(crown, hedge=True) if hedge else self.tree_spacing(crown, row=True)
        gid = self._new_group()
        plant = (kind, crown, spacing, gid)
        offset = min(crown / 2, self.config['inset_max_m']) if hedge else 1.0
        placed = []
        used_lines = []
        for guide, _ in self._guides(zone, offset):
            if len(placed) >= count:
                break
            line = placed_line = None
            for rank in (0, 1):
                # второй ряд изгороди — в шахматном порядке: внутрь на √3/2 шага, со сдвигом на полшага
                # (сотовая посадка — до соседей первого ряда ровно шаг)
                if rank == 1:
                    if not hedge or len(placed) >= count:
                        break
                    depth = spacing * SQRT3 / 2 * 1.01
                    shifted = guide.parallel_offset(depth, 'left')
                    if shifted.is_empty or not zone.buffer(-0.05).contains(shifted.interpolate(0.5, normalized=True)):
                        shifted = guide.parallel_offset(depth, 'right')
                    line = shifted if shifted.geom_type == 'LineString' and not shifted.is_empty else None
                    if line is None:
                        break
                else:
                    line = guide
                start = spacing / 2 if rank else 0.0
                steps = int((line.length - start) // spacing) + 1
                got = []
                for i in range(steps):
                    if len(placed) >= count:
                        break
                    p = line.interpolate(start + i * spacing)
                    if zone.contains(p) and self.field.ok(p.x, p.y, plant):
                        self._take(p.x, p.y, plant, placed)
                        got.append(p)
                if len(got) >= 2:
                    placed_line = LineString(got) if rank == 0 else placed_line
                    used_lines.append(LineString(got))
            if placed_line is None and not used_lines:
                continue
        if hedge and used_lines:
            self._outline(placed, crown, 'hedge', meta, line=unary_union(used_lines))
        return placed

    # --- цветники ---------------------------------------------------------------------------------------

    def patch(self, available, area):
        """Пятно площадью area свободной округлой формы (несколько сливающихся кругов) внутри available."""
        if available.is_empty or area <= 0:
            return None
        if area >= available.area - AREA_TOLERANCE:
            return available
        spots = _sample(available, self.rng, 12)
        if not spots:
            return None
        cx, cy = max(spots, key=lambda p: self.field.clearance(p[0], p[1], ('shrub', 'tree')))
        lobes = [(0.0, 0.0, 1.0)] + [(self.rng.normal(0, 0.6), self.rng.normal(0, 0.6), self.rng.uniform(0.5, 0.9))
                                    for _ in range(int(self.rng.integers(2, 5)))]
        stretch, angle = self.rng.uniform(1.0, 2.0), self.rng.uniform(0, 180)

        def blob(scale):
            shape_ = unary_union([Point(cx + dx * scale, cy + dy * scale).buffer(r * scale, quad_segs=8)
                                  for dx, dy, r in lobes])
            shape_ = affinity.rotate(affinity.scale(shape_, stretch, 1.0, origin=(cx, cy)), angle, origin=(cx, cy))
            return available.intersection(shape_.buffer(0.2 * scale).buffer(-0.2 * scale))

        low, high = 0.0, math.sqrt(available.area) + 1
        best = None
        for _ in range(30):
            scale = (low + high) / 2
            best = blob(scale)
            if abs(best.area - area) <= AREA_TOLERANCE:
                break
            low, high = (scale, high) if best.area < area else (low, scale)
        return best


def _rotate_ring(ring, start):
    """Кольцо как линия, начинающаяся с точки start (длина вдоль кольца)."""
    coords = list(ring.coords)[:-1]
    if not coords:
        return ring
    head = ring.interpolate(start)
    line = LineString(coords + [coords[0]])
    from shapely.ops import substring
    first = substring(line, start, line.length)
    second = substring(line, 0, start)
    parts = [p for p in (first, second) if p.geom_type == 'LineString' and p.length > 0]
    merged = linemerge(unary_union(parts)) if len(parts) > 1 else (parts[0] if parts else LineString([head, head]))
    return merged if merged.geom_type == 'LineString' else max(merged.geoms, key=lambda g: g.length)


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


def _style(structure, seed, config):
    """Стиль структуры: пейзажный или регулярный — случайно, с долей по типу структуры; тот же seed — тот же стиль."""
    share = config['landscape_share'].get(structure.get('type'), 0.5)
    roll = int(hashlib.md5(f"{structure['id']}:{seed}".encode()).hexdigest(), 16) % 1000 / 1000
    return 'landscape' if roll < share else 'formal'


def place(geojson, plan, seed=0, config=None, log=print):
    config = config or load_config()
    rng = np.random.default_rng(seed)
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    existing = [g for g, p in features if p['layer'] == 'tree']
    paths = unary_union([g for g, p in features if p['layer'] == 'surface' and p['surface'] in PATH_SURFACES])
    plants = {p['id']: p for p in catalog.load()}
    placer = Placer(config, existing, rng, paths)

    out, summary, styles = [], [], {}
    for structure in plan['structures']:
        style = styles[structure['id']] = _style(structure, seed, config)
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
                (areas if item.get('unit') in AREA_UNITS else points).append((item, kind))
            points.sort(key=lambda p: ORDER[(p[1], _mode(p[0].get('role')))])

            for item, kind in points:
                plant = plants.get(item.get('plant_id'))
                crown = _crown(plant, kind, config)
                count = int(round(_quantity(item)))
                mode = _mode(item.get('role'))
                meta = {**base, 'plant_id': item.get('plant_id'), 'name': item.get('name'), 'kind': kind,
                        'role': item.get('role'), 'crown_m': crown}
                if kind not in zone['allowed'] and kind != 'herbaceous':
                    placed = []
                else:
                    region = placer.inset(inner, kind, crown)
                    if mode == 'row':
                        placed = placer.row(inner, count, kind, crown, style, meta)
                    elif mode == 'group':
                        placed = placer.groups(region, count, kind, crown, style, meta)
                    else:
                        placed = placer.solitary(region, count, kind, crown, style)
                sizes = defaultdict(int)
                for _, _, p in placed:
                    sizes[p[3]] += 1
                for number, (x, y, p) in enumerate(placed, 1):
                    out.append({'type': 'Feature', 'geometry': mapping(Point(round(x, 2), round(y, 2))),
                                'properties': {'layer': 'plant', **meta, 'group': item.get('group'),
                                               'spacing_m': round(p[2], 2), 'group_id': p[3],
                                               'group_size': sizes[p[3]], 'number': number}})
                summary.append({**base, 'plant_id': item.get('plant_id'), 'name': item.get('name'), 'kind': kind,
                                'role': item.get('role'), 'unit': item.get('unit'),
                                'requested': count, 'placed': len(placed)})

            # цветники — пятна нужной площади в стороне от кустов, газон — что осталось
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

    for group in placer.outlines:
        g = group.pop('geometry')
        if g.is_empty:
            continue
        out.append({'type': 'Feature', 'geometry': mapping(shapely.set_precision(g, 0.01)),
                    'properties': {'layer': 'group', **group,
                                   'label': f"{group['name']} ×{group['count']}"}})

    short = [s for s in summary if s['unit'] not in AREA_UNITS and s['placed'] < s['requested']]
    totals = defaultdict(lambda: [0, 0])
    for s in summary:
        key = s['kind'] if s['unit'] not in AREA_UNITS else f"{s['kind']}_m2"
        totals[key][0] += s['requested']
        totals[key][1] += s['placed']
    return {
        'type': 'FeatureCollection',
        'metadata': {'seed': seed, 'config': {k: v for k, v in config.items() if not k.startswith('_')},
                     'styles': styles,
                     'summary': {'totals': {k: {'requested': round(v[0], 1), 'placed': round(v[1], 1)}
                                            for k, v in totals.items()},
                                 'short': len(short)},
                     'plantings': summary},
        'features': out,
    }


def verify(result, geojson, config=None):
    """Независимая проверка рассадки: каждое растение в секции, где разрешён его вид посадки, расстояния
    между растениями (по кроне, группе) и до существующих деревьев не меньше правил. Список нарушений."""
    config = config or load_config()
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = [(g, p) for g, p in features if p['layer'] == 'section']
    problems = []
    field = Field(config, [g for g, p in features if p['layer'] == 'tree'])
    plants = [(shape(f['geometry']), f['properties']) for f in result['features'] if f['properties']['layer'] == 'plant']
    index = shapely.STRtree([g for g, _ in sections])
    for point, props in plants:
        hits = [sections[int(i)] for i in index.query(point, predicate='intersects')]
        allowed = set().union(*[set(p['allowed']) for _, p in hits]) if hits else set()
        if props['kind'] != 'herbaceous' and props['kind'] not in allowed:
            problems.append(f"{props['structure']}/{props['zone']} {props['name']}: {props['kind']} вне разрешённой секции")
        plant = (props['kind'], props['crown_m'], props['spacing_m'], props.get('group_id'))
        if not field.ok(point.x, point.y, plant, ROUNDING):
            problems.append(f"{props['structure']}/{props['zone']} {props['name']}: ближе правил к соседу")
        field.add(point.x, point.y, plant)
    return problems


# слои рассадки: всё, что добавляет сервис, начинается с Greening (так договорились в команде),
# по видам — чтобы в CAD их можно было включать по отдельности
LAYERS = {'tree': ('Greening_Деревья', 94), 'shrub': ('Greening_Кустарники', 62),
          'herbaceous': ('Greening_Травянистые', 51), 'bed': ('Greening_Цветники', 221),
          'lawn': ('Greening_Газон', 3), 'spot': ('Greening_Кустарники_места', 62)}
LABEL_HEIGHT = 0.5


def add_layers(doc, result):
    """Рассадка на слоях Greening_*: деревья и одиночные кусты — круг кроны и точка ствола; группы и изгороди —
    один контур с подписью «вид ×N», места посадки — точки на отдельном слое; цветники и газон — заливки."""
    from ezdxf.enums import TextEntityAlignment

    from .export import _area, _layer

    msp = doc.modelspace()
    for feature in result['features']:
        props = feature['properties']
        geometry = shape(feature['geometry'])
        if props['layer'] == 'plant':
            in_group = props['kind'] == 'shrub' and (props.get('group_size', 1) > 1 or props.get('role') in ROW_ROLES)
            name, color = LAYERS['spot' if in_group else props['kind']]
            layer = _layer(doc, name, color)
            if in_group:
                msp.add_circle((geometry.x, geometry.y), 0.15, dxfattribs={'layer': layer})
            else:
                msp.add_circle((geometry.x, geometry.y), props['crown_m'] / 2, dxfattribs={'layer': layer})
                msp.add_point((geometry.x, geometry.y), dxfattribs={'layer': layer})
        elif props['layer'] == 'group':
            name, color = LAYERS[props['kind']]
            layer = _layer(doc, name, color)
            for poly in _polygons(geometry):
                msp.add_lwpolyline(list(poly.exterior.coords)[:-1], close=True, dxfattribs={'layer': layer})
            at = geometry.representative_point()
            msp.add_text(props['label'], height=LABEL_HEIGHT, dxfattribs={'layer': layer}).set_placement(
                (at.x, at.y), align=TextEntityAlignment.MIDDLE_CENTER)
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
    parser = argparse.ArgumentParser(description='Рассадка растений плана внутри зон')
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
