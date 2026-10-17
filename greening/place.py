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

Разнообразие (config variety; 0 или null — прежний результат):
- смешанная группа кустов — два вида зоны на общей сотовой сетке, низкий ближе к дорожке (mixed_share);
- подлесок — группа кустов дугой у нового дерева по краю кроны, лицом к дорожке (underplanting_share);
- ступенчатость — низкие виды зоны ближе к дорожке, высокие дальше (height_grading, штраф к оценке места);
- миксбордер — пятно цветника делится на куртины видов (спутники с mix_of добавляет variety.compose);
- луговой газон в глубине, обыкновенный — полосой meadow_border_m вдоль дорожек.

Удобство ухода (Access, config access; кустарники, оба режима): куст не дальше path_reach_m от тротуара или
площадки; у проезжей части — только если рядом дорожка (иначе подход с проезда); одиночный куст — у края газона,
а не посреди него; между кроной и краем газона нет полоски, которую не выкосить (кроме изгородей и мест, где
ближе к краю не пускают нормы).

Количество определяет рассадка, а не LLM (LLM выбирает только виды, приём посадки и пишет обоснование).
Зона заполняется выбранными приёмами по очереди, пока есть место, — с просветами, а не впритык:
- деревья — пока встают: между отдельными деревьями и группами tree_unit_spacing_m (кроны не смыкаются);
- ряд деревьев и живая изгородь — вдоль края у тротуара или проезда (нет такого — вдоль длинной стороны), в один ряд;
- кустарники — пока их кроны не займут shrub_cover_share зоны (остальное — газон и цветник);
- цветник — bed_share зоны; газон — остаток.
apply_counts() записывает получившиеся количества в план.

Режим рассадки (mode): 'algorithm' — места выбирает эвристика (_open_score: открытое место, стиль);
'model' — обученная модель (model.py): из тех же допустимых точек (внутри зоны, где вид разрешён нормами,
и с соблюдением расстояний между растениями) берётся точка, которую модель оценила выше, — как ставят
проектировщики. Ряды и изгороди в обоих режимах идут вдоль края у дорожки. Модели нет — алгоритм.
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
from shapely.ops import linemerge, substring, unary_union

from . import catalog
from . import model as learned

CONFIG = Path(__file__).resolve().parent / 'config' / 'placement.json'
KIND = {'ДЛ': 'tree', 'ДХ': 'tree', 'КЛ': 'shrub', 'КХ': 'shrub', 'Л': 'shrub',
        'М': 'herbaceous', 'О': 'herbaceous', 'Б': 'herbaceous', 'Г': 'herbaceous'}
ROW_ROLES = ('рядовая посадка', 'живая изгородь')
GROUP_ROLES = ('группа', 'куртина')
AREA_UNITS = ('м2', 'м²', 'кв.м')
LAWN_GROUP = 'Г'
PATH_SURFACES = ('sidewalk', 'paving', 'sealed', 'roadway', 'playground', 'gravel')
# откуда подходят к посадкам для ухода (проезжая часть — нет)
WALK_SURFACES = ('sidewalk', 'paving', 'sealed', 'playground', 'gravel')
# газон для выкашивания: его край — край, у которого не должно оставаться узкой полоски
GREEN_SURFACES = ('lawn', 'flowerbed', 'ground')
# точка не дальше этого от края своей области — ближе к краю газона куст не пускают нормы или отступ кроны, м
EDGE_SLACK = 0.15
# ячейка сетки поиска соседей, м
CELL = 8.0
# дальше этого соседи не проверяются (больше самого крупного нужного расстояния), м
REACH = 12.0
# координаты выводятся с точностью 1 см — проверка допускает такую погрешность, м
ROUNDING = 0.02
# полоса обыкновенного газона у дорожек при луговом меньше этого — не выделяется, м²
MIN_EDGING_M2 = 1.0
# точность подбора площади пятна цветника, м²
AREA_TOLERANCE = 0.5
SQRT3 = math.sqrt(3)
# шаг сетки запасного поиска места, когда случайные точки не подошли (узкие и мелкие зоны), м
GRID_STEP = 1.0


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _crown(plant, kind, config):
    """Крона проектного возраста: справочная (взрослая) — не больше crown_cap_m, нет данных — crown_default_m."""
    cap = config.get('crown_cap_m', {}).get(kind)
    for flag in plant['flags'] if plant else ():
        if flag.startswith('к') and flag[1:].isdigit():
            return min(float(flag[1:]), cap) if cap else float(flag[1:])
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
            return max(spacing, other_spacing, c['tree_unit_spacing_m'])
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


class _Distance:
    """Расстояние от точки до линий (края поверхностей): линии режутся на короткие куски в STRtree."""

    CHUNK = 16

    def __init__(self, geometry):
        pieces = []
        for line in _lines(geometry.boundary if geometry.geom_type.endswith('Polygon') else geometry):
            coords = list(line.coords)
            for i in range(0, len(coords) - 1, self.CHUNK):
                pieces.append(LineString(coords[i:i + self.CHUNK + 1]))
        self.tree = shapely.STRtree(pieces) if pieces else None

    def __call__(self, x, y):
        if self.tree is None:
            return math.inf
        _, distance = self.tree.query_nearest(Point(x, y), return_distance=True, all_matches=False)
        return float(distance[0])

    def nearest(self, x, y):
        """Ближайшая точка линий (x, y) или None, если линий нет."""
        if self.tree is None:
            return None
        index = self.tree.query_nearest(Point(x, y), all_matches=False)[0]
        line = self.tree.geometries[int(index)]
        q = line.interpolate(line.project(Point(x, y)))
        return q.x, q.y


class Access:
    """Удобство ухода за кустами (config access, см. placement.json): подход с дорожки, не только с проезда,
    одиночный куст — у края газона, без невыкашиваемых полосок газона между кроной и краем."""

    def __init__(self, config, green, walk, road):
        self.rules = config.get('access') or {}
        self.edge = _Distance(green)
        self.walk = _Distance(walk)
        self.road = _Distance(road)

    def ok(self, x, y, crown, lone=False):
        """Подход: дорожка недалеко, не только с проезда; одиночный куст — у края газона."""
        r, rules = crown / 2, self.rules
        reach, road_only, lone_edge = rules.get('path_reach_m'), rules.get('road_only_m'), rules.get('lone_edge_m')
        walk = self.walk(x, y) if reach is not None or road_only is not None else None
        if reach is not None and walk > reach:
            return False
        if road_only is not None and walk > road_only and self.road(x, y) - r < 1.0:
            return False
        return not (lone and lone_edge is not None and self.edge(x, y) > lone_edge)

    def front(self, x, y):
        """Направление (угол) от точки к ближайшей дорожке — лицевая сторона посадки; None — дорожек нет."""
        q = self.walk.nearest(x, y)
        if q is None or math.dist(q, (x, y)) < 1e-6:
            return None
        return math.atan2(q[1] - y, q[0] - x)

    def mowable(self, points, crown, region=None):
        """Между кронами группы (или одного куста) и краем газона нет полоски, которую не выкосить: ближайший
        к краю куст либо у самого края, либо не ближе mow_min_m. region — область, где выбирались точки: если
        ближе к краю куст не пускают нормы (точка у края области), полоска допускается."""
        strip, mow = self.rules.get('edge_strip_max_m') or 0.0, self.rules.get('mow_min_m')
        if mow is None or not points:
            return True
        gaps = [self.edge(x, y) - crown / 2 for x, y in points]
        gap = min(gaps)
        if not strip < gap < mow:
            return True
        x, y = points[gaps.index(gap)]
        return region is not None and region.boundary.distance(Point(x, y)) <= EDGE_SLACK


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


def _grid(polygon, step=GRID_STEP):
    """Узлы сетки step внутри polygon — полный перебор места для мелких и узких зон."""
    if polygon.is_empty or polygon.area <= 0:
        return []
    minx, miny, maxx, maxy = polygon.bounds
    xs, ys = np.meshgrid(np.arange(minx + step / 2, maxx, step), np.arange(miny + step / 2, maxy, step))
    xs, ys = xs.ravel(), ys.ravel()
    inside = shapely.contains_xy(polygon, xs, ys)
    return list(zip(xs[inside], ys[inside]))


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
    def __init__(self, config, existing, rng, paths, scorer=None, access=None):
        self.config, self.rng = config, rng
        self.field = Field(config, existing)
        self.paths = paths
        # удобство ухода (Access): None — не проверяется
        self.access = access
        self.group_seq = 0
        self.outlines = []          # контуры групп и изгородей для вывода
        # режим «модель»: оценка мест обученной моделью; site — газон текущей зоны (model.Site)
        self.scorer = scorer
        self.site = None
        # ступенчатость по высоте для текущей записи fill: (место вида по высоте 0…1, вес, глубина, м) или None
        self.grade = None

    def _rank(self, kind, zone, points, crown, style):
        """Оценки точек: модель (лог-вероятность с шумом Гумбеля — разнообразие от seed) или эвристика."""
        if self.scorer is not None and self.site is not None:
            p = self.scorer(kind, self.site, points, self.field)
            noise = self.rng.gumbel(size=len(points)) * self.config['model_temperature']
            return list(np.log(np.clip(p, 1e-6, 1)) + noise - self._grading_share(points)), list(p)
        return [self._open_score(zone, q, crown, style) for q in points], None

    def _grading(self, x, y):
        """Ступенчатость по высоте (variety.height_grading): штраф за отход от своей полосы — низкие виды
        у дорожки, высокие дальше от неё (до grading_depth_m). Метры, умноженные на вес."""
        if self.grade is None or self.access is None:
            return 0.0
        rank, weight, depth = self.grade
        walk = self.access.walk(x, y)
        if not math.isfinite(walk):
            return 0.0
        return weight * abs(min(walk, depth) - rank * depth)

    def _grading_share(self, points):
        """Тот же штраф в долях глубины — для лог-оценок модели."""
        if self.grade is None:
            return 0.0
        return np.array([self._grading(x, y) for x, y in points]) / self.grade[2]

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

    # удобство ухода — только у кустарников (деревья держат нормы и просветы)
    def reachable(self, x, y, kind, crown, lone=False):
        return kind != 'shrub' or self.access is None or self.access.ok(x, y, crown, lone)

    def mowable(self, points, kind, crown, region):
        return kind != 'shrub' or self.access is None or self.access.mowable(points, crown, region)

    def convenient(self, x, y, kind, crown, region):
        """Одиночное растение: подход и выкашивание вокруг."""
        return self.reachable(x, y, kind, crown, True) and self.mowable([(x, y)], kind, crown, region)

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
                       if self.field.ok(x, y, plant) and self.convenient(x, y, kind, crown, zone)]
            if not options:
                # случайные точки промахнулись — перебор по сетке, чтобы не терять место в узкой зоне
                options = [(x, y) for x, y in _grid(zone)
                           if self.field.ok(x, y, plant) and self.convenient(x, y, kind, crown, zone)]
            if not options:
                break
            scores, probs = self._rank(kind, zone, options, crown, style)
            best = int(np.argmax(scores))
            # модель считает все оставшиеся места неподходящими — солитер не ставится
            if probs is not None and probs[best] < self.config['model_min_score'][kind]:
                break
            x, y = options[best]
            self._take(x, y, plant, placed)
        return placed

    def _open_score(self, zone, p, crown, style):
        """Открытое место: дальше от посаженного (до разумного предела), крона внутри газона, чуть случайности;
        регулярный стиль тянет к краю, пейзажный — к середине."""
        x, y = p
        room = min(self.field.clearance(x, y), 2.5 * crown)
        edge = zone.boundary.distance(Point(x, y))
        inside = min(edge, crown / 2)
        # к середине — до трёх крон от края, к краю — без предела
        weight = self.config['edge_pull'][style]
        pull = weight * (min(edge, 3 * crown) if weight >= 0 else edge)
        return room + 0.6 * inside + pull + self.rng.uniform(0, 0.25 * crown) - self._grading(x, y)

    # --- группы и куртины ------------------------------------------------------------------------------

    def _options(self, kind, crown):
        c = self.config['group_sizes']
        return c['tree'] if kind == 'tree' else c['small'] if crown < 1.5 else c['medium'] if crown < 3 else c['large']

    def _sizes(self, count, kind, crown):
        options = self._options(kind, crown)
        sizes = []
        while count > 0:
            size = int(self.rng.choice(options))
            if size > count:
                # остаток меньше выбранного размера — нечётным: так группа смотрится естественнее;
                # сам выбранный размер (параметр рассадки) соблюдается, даже чётный
                size = count
                if size % 2 == 0 and size > 1:
                    size -= 1
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

    def fill(self, entries, kind, style, budget=math.inf, limit=math.inf, heights=None, anchors=None):
        """Заполнение зоны солитерами и группами нескольких видов по очереди (виды перемешиваются), пока
        встают и пока не исчерпан budget — площадь крон, м², и limit — число растений. Одиночных растений
        вида — не больше solitary_max (параметр рассадки). entries — [(зона для центров, крона, режим, meta)].

        Разнообразие (config variety, только кустарники): heights — высота вида по записям: низкие виды
        ближе к дорожке, высокие дальше (height_grading), часть групп — смешанные из двух видов (mixed_share);
        anchors — новые деревья зоны [(x, y, крона, группа)]: часть групп встаёт у них подлеском
        (underplanting_share). Возвращает посаженное по каждой записи."""
        placed = [[] for _ in entries]
        active = list(range(len(entries)))
        solitary_left = self.config['solitary_max'].get(kind)
        solitary_left = math.inf if solitary_left is None else solitary_left
        variety = self.config.get('variety') or {}
        shrubs = kind == 'shrub'
        grading = variety.get('height_grading') or 0.0
        ranks = _ranks(heights) if shrubs and heights and grading else {}
        mixed = (variety.get('mixed_share') or 0.0) if shrubs and heights else 0.0
        anchors = list(anchors or []) if shrubs else []
        under = (variety.get('underplanting_share') or 0.0) if anchors else 0.0
        regions = {}
        count = 0
        while active and budget > 0 and count < limit:
            for i in list(active):
                if i not in active:
                    continue
                region, crown, mode, meta = entries[i]
                self.grade = (ranks[i], grading, variety.get('grading_depth_m', 6.0)) if i in ranks else None
                got, extra = [], {}
                if mode == 'group':
                    size = int(min(int(self.rng.choice(self._options(kind, crown))), limit - count))
                    # случайный выбор приёма — только когда он включён: без него рассадка прежняя
                    roll = self.rng.random() if mixed or under else 1.0
                    if roll < under and anchors:
                        got = self.underplant(region, anchors.pop(0), size, crown, style, meta) or []
                    elif roll < under + mixed:
                        j = _partner(i, entries, heights, active)
                        if j is not None:
                            key = (min(i, j), max(i, j))
                            if key not in regions:
                                regions[key] = region.intersection(entries[j][0])
                            got, extra = self.mixed_group(regions[key], i, j, entries, heights, style, limit - count)
                    if not got:
                        got = self.groups(region, size, kind, crown, style, meta)
                elif solitary_left > 0:
                    got = self.solitary(region, 1, kind, crown, style)
                    solitary_left -= len(got)
                if not got:
                    active.remove(i)
                    continue
                placed[i] += got
                count += len(got)
                budget -= len(got) * math.pi * (crown / 2) ** 2
                for j, more in extra.items():
                    placed[j] += more
                    count += len(more)
                    budget -= len(more) * math.pi * (entries[j][1] / 2) ** 2
                if budget <= 0 or count >= limit:
                    break
        self.grade = None
        return placed

    def underplant(self, region, anchor, size, crown, style, meta):
        """Подлесок: группа кустов дугой у нового дерева — по краю его кроны, не ближе к стволу, чем пускают
        правила (under_tree_share, tree_shrub_min_m), лицом к дорожке. Не встаёт целиком — пробует меньше."""
        tx, ty, tree_crown, tree_group = anchor
        c = self.config
        spacing = self.shrub_spacing(crown)
        radius = max(c['tree_shrub_min_m'], c['under_tree_share'] * tree_crown / 2) + 3 * ROUNDING
        # шаг по дуге: хорда не короче шага посадки; дуга — не больше полукруга
        step = 2 * math.asin(min(1.0, spacing / (2 * radius))) * 1.02
        size = min(size, int(math.pi / step) + 1)
        front = self.access.front(tx, ty) if self.access is not None else None
        if front is None:
            front = self.rng.uniform(0, 2 * math.pi)
        turns = (0.0, math.pi / 3, -math.pi / 3, 2 * math.pi / 3, -2 * math.pi / 3, math.pi)
        gid = self._new_group()
        plant = ('shrub', crown, spacing, gid)
        for n in range(size, 1, -1):
            span = step * (n - 1)
            for turn in turns:
                start = front + turn - span / 2
                pts = [(tx + radius * math.cos(start + k * step), ty + radius * math.sin(start + k * step)) for k in range(n)]
                if not all(region.contains(Point(q)) for q in pts) or not all(self.field.ok(x, y, plant) for x, y in pts):
                    continue
                if not all(self.reachable(x, y, 'shrub', crown) for x, y in pts) or not self.mowable(pts, 'shrub', crown, region):
                    continue
                out = []
                for x, y in pts:
                    self._take(x, y, plant, out)
                self._outline(out, crown, 'underplanting', {**meta, 'tree_group': tree_group})
                return out
        return None

    def mixed_group(self, region, i, j, entries, heights, style, limit=math.inf):
        """Смешанная группа двух видов кустов на общей сотовой сетке (шаг — по крупному виду): ближе к дорожке —
        низкий вид, дальше — высокий. Возвращает (посаженное записи i, {j: посаженное записи j}) или ([], {})."""
        tall, low = (i, j) if heights[i] >= heights[j] else (j, i)
        crowns = {tall: entries[tall][1], low: entries[low][1]}
        spacing = max(self.shrub_spacing(crowns[tall]), self.shrub_spacing(crowns[low]))
        # размер — как у обычной группы мелкого из видов; низких — половина с округлением вверх (их ряд спереди)
        total = int(min(self.rng.choice(self._options('shrub', min(crowns.values()))), limit))
        if total < 3 or region.is_empty:
            return [], {}
        gid = self._new_group()
        plants = {k: ('shrub', crowns[k], spacing, gid) for k in (tall, low)}
        shapely.prepare(region)
        while total >= 3:
            found = self._mixed_at(region, tall, low, total, crowns, spacing, plants, entries, style)
            if found is not None:
                return found[i], {j: found[j]}
            total -= 2 if total > 4 else 1
        return [], {}

    def _mixed_at(self, region, tall, low, total, crowns, spacing, plants, entries, style):
        counts = {low: (total + 1) // 2, tall: total // 2}
        jitter = self.config['group_jitter'][style]
        for _ in range(2):
            centres = _sample(region, self.rng, self.config['candidates'] // 2)
            if not centres:
                return None
            scores, _ = self._rank('shrub', region, centres, crowns[tall], style)
            for n in sorted(range(len(centres)), key=lambda n: -scores[n])[:6]:
                cx, cy = centres[n]
                aspect = self.rng.uniform(1.3, 2.4)
                layout = _hex_layout(total, spacing * (1 + 3 * jitter), aspect, _edge_angle(region, cx, cy), self.rng, jitter)
                pts = [(cx + dx, cy + dy) for dx, dy in layout]
                if min((math.dist(a, b) for m, a in enumerate(pts) for b in pts[m + 1:]), default=spacing) < spacing:
                    continue
                if not all(region.contains(Point(q)) for q in pts):
                    continue
                # лицевая сторона — к дорожке, нет её — к ближайшему краю газона
                front = self.access.front(cx, cy) if self.access is not None else None
                if front is None:
                    q = region.boundary.interpolate(region.boundary.project(Point(cx, cy)))
                    front = math.atan2(q.y - cy, q.x - cx)
                fx, fy = math.cos(front), math.sin(front)
                pts.sort(key=lambda q: -((q[0] - cx) * fx + (q[1] - cy) * fy))
                parts = {low: pts[:counts[low]], tall: pts[counts[low]:]}
                if not all(self.field.ok(x, y, plants[k]) for k in parts for x, y in parts[k]):
                    continue
                if not all(self.reachable(x, y, 'shrub', crowns[k]) for k in parts for x, y in parts[k]) \
                        or not all(self.mowable(parts[k], 'shrub', crowns[k], region) for k in parts):
                    continue
                out = {tall: [], low: []}
                for k in (tall, low):
                    for x, y in parts[k]:
                        self._take(x, y, plants[k], out[k])
                mix = [{'plant_id': entries[k][3].get('plant_id'), 'name': entries[k][3].get('name'), 'count': len(out[k])}
                       for k in (tall, low)]
                self._outline(out[tall] + out[low], crowns[tall], 'mixed',
                              {**entries[tall][3], 'mix': mix,
                               'label': ' + '.join(f"{m['name']} ×{m['count']}" for m in mix)})
                return out
        return None

    def _try_group(self, zone, size, kind, crown, spacing, style, meta, attempts=4):
        """Группа целиком или ничего. Алгоритм: центр — открытое место, первая подошедшая форма.
        Модель: варианты положения и формы оцениваются по всем растениям группы (средняя оценка модели)."""
        gid = self._new_group()
        plant = (kind, crown, spacing, gid)
        if self.scorer is not None and self.site is not None:
            return self._try_group_model(zone, size, kind, crown, spacing, style, meta, plant, attempts)
        for _ in range(attempts):
            centres = [(x, y) for x, y in _sample(zone, self.rng, self.config['candidates'] // 2)]
            if not centres:
                return None
            centres.sort(key=lambda p: -self._open_score(zone, p, crown, style))
            for cx, cy in centres[:6]:
                found = self._layout_at(zone, cx, cy, size, spacing, style, plant)
                if found is not None:
                    return self._place_group(found, plant, crown, meta)
        return None

    def _try_group_model(self, zone, size, kind, crown, spacing, style, meta, plant, attempts):
        """Варианты группы в разных местах зоны; выбирается тот, где выше средняя оценка модели по всем
        растениям группы (с шумом Гумбеля — разнообразие от seed). Ниже model_min_score — группа не ставится."""
        per_attempt = self.config['model_group_variants']
        for _ in range(attempts):
            centres = _sample(zone, self.rng, self.config['candidates'] // 2)
            if not centres:
                return None
            variants = []
            for cx, cy in centres[:per_attempt]:
                found = self._layout_at(zone, cx, cy, size, spacing, style, plant)
                if found is not None:
                    variants.append(found)
            if not variants:
                continue
            points = [q for pts, _ in variants for q in pts]
            probs = self.scorer(kind, self.site, points, self.field)
            means, at = [], 0
            for pts, _ in variants:
                means.append(float(np.mean(probs[at:at + len(pts)])))
                at += len(pts)
            noise = self.rng.gumbel(size=len(variants)) * self.config['model_temperature']
            grading = np.array([np.mean(self._grading_share(pts)) for pts, _ in variants]) if self.grade else 0.0
            best = int(np.argmax(np.log(np.clip(means, 1e-6, 1)) + noise - grading))
            if means[best] < self.config['model_min_score'][kind]:
                return None
            return self._place_group(variants[best], plant, crown, meta)
        return None

    def _layout_at(self, zone, cx, cy, size, spacing, style, plant):
        """Форма группы вокруг центра (случайная из подходящих): (точки, вытянутая ли) или None, если не встаёт."""
        landscape = style == 'landscape'
        drift = size >= 5 and self.rng.random() < self.config['drift_share'][style]
        aspect = self.rng.uniform(1.8, 3.2) if drift else self.rng.uniform(1.0, 1.4)
        angle = _edge_angle(zone, cx, cy) + (self.rng.normal(0, 0.25) if landscape else 0.0)
        # решётка с запасом шага — лёгкий случайный сдвиг не сближает кусты сильнее шага
        jitter = self.config['group_jitter'][style]
        layout = _hex_layout(size, spacing * (1 + 3 * jitter), aspect, angle, self.rng, jitter)
        pts = [(cx + dx, cy + dy) for dx, dy in layout]
        if min((math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1:]), default=spacing) < spacing:
            return None
        if not all(zone.contains(Point(p)) for p in pts):
            return None
        if not all(self.field.ok(x, y, plant) for x, y in pts):
            return None
        if not all(self.reachable(x, y, plant[0], plant[1], len(pts) == 1) for x, y in pts) \
                or not self.mowable(pts, plant[0], plant[1], zone):
            return None
        return pts, drift

    def _place_group(self, found, plant, crown, meta):
        pts, drift = found
        out = []
        for x, y in pts:
            self._take(x, y, plant, out)
        self._outline(out, crown, 'drift' if drift else 'group' if len(pts) > 1 else 'single', meta)
        return out

    def _outline(self, points, crown, layout, meta, line=None):
        if len(points) < 2 and line is None:
            return
        shape_ = (line.buffer(crown / 2 * 0.9, cap_style='round') if line is not None else
                  unary_union([Point(x, y).buffer(p[1] / 2 * 0.9, quad_segs=6) for x, y, p in points])
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
                guides += [(l, True, l.length) for l in preferred if l.length > 2]
                corners = list(ring.simplify(0.5).coords)
                if len(corners) > 1:
                    a, b = max(zip(corners, corners[1:]), key=lambda s: math.dist(*s))
                    start = ring.project(Point(a))
                    guides.append((_rotate_ring(ring, start), False, math.dist(a, b)))
        return guides

    def row(self, zone, count, kind, crown, style, meta, limit=None):
        """Ряд или изгородь. count=None — заполнение: в один ряд вдоль всех краёв у тротуара и проезда,
        нет таких — вдоль длинной стороны зоны; limit — не больше стольких растений (параметр рассадки)."""
        hedge = kind == 'shrub'
        spacing = self.shrub_spacing(crown, hedge=True) if hedge else self.tree_spacing(crown, row=True)
        gid = self._new_group()
        plant = (kind, crown, spacing, gid)
        offset = min(crown / 2, self.config['inset_max_m']) if hedge else 1.0
        guides = self._guides(zone, offset)
        fill = count is None
        if fill:
            count = math.inf if limit is None else limit
            preferred = [g for g in guides if g[1]]
            guides = preferred or [(substring(g[0], 0, g[2]), False, g[2]) for g in guides[:1]]
        placed = []
        used_lines = []
        for guide, _, _ in guides:
            if len(placed) >= count:
                break
            line = placed_line = None
            for rank in ((0,) if fill else (0, 1)):
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
                    if zone.contains(p) and self.field.ok(p.x, p.y, plant) \
                            and self.reachable(p.x, p.y, kind, crown):
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

    def drifts(self, polygon, count):
        """Миксбордер: пятно цветника делится на count куртин свободной формы — ячейки Вороного от разнесённых
        по пятну точек. Крупнейшая — первому виду (выбранному для цветника); не вышло — None вместо куртины."""
        if count < 2 or polygon is None or polygon.is_empty:
            return [polygon] + [None] * (count - 1)
        points = _sample(polygon, self.rng, 120)
        if len(points) < count:
            return [polygon] + [None] * (count - 1)
        seeds = [points[0]]
        while len(seeds) < count:
            seeds.append(max(points, key=lambda q: min(math.dist(q, s) for s in seeds)))
        cells = shapely.voronoi_polygons(MultiPoint(seeds), extend_to=polygon.envelope.buffer(10))
        parts = [polygon.intersection(cell) for cell in cells.geoms]
        parts = sorted((q for q in parts if not q.is_empty and q.area > 0), key=lambda q: -q.area)
        return parts + [None] * (count - len(parts))


def _ranks(heights):
    """Место вида по высоте среди видов зоны: 0 — самый низкий, 1 — самый высокий; один вид — без ступеней."""
    levels = sorted(set(heights))
    if len(levels) < 2:
        return {}
    return {i: levels.index(h) / (len(levels) - 1) for i, h in enumerate(heights)}


def _partner(i, entries, heights, active):
    """Второй вид для смешанной группы: другая групповая посадка кустов зоны, сильнее всего отличная по высоте."""
    options = [j for j in active if j != i and entries[j][2] == 'group'
               and entries[j][3].get('plant_id') != entries[i][3].get('plant_id')]
    return max(options, key=lambda j: abs(heights[j] - heights[i]), default=None)


def _height(plant, crown):
    """Высота взрослого растения по справочнику (hN), нет данных — по кроне."""
    for flag in plant['flags'] if plant else ():
        if flag.startswith('h') and flag[1:].isdigit():
            return float(flag[1:])
    return crown


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
    if config.get('style_mode') in ('landscape', 'formal'):
        return config['style_mode']
    share = config['landscape_share'].get(structure.get('type'), 0.5)
    roll = int(hashlib.md5(f"{structure['id']}:{seed}".encode()).hexdigest(), 16) % 1000 / 1000
    return 'landscape' if roll < share else 'formal'


MODES = ('algorithm', 'model')
KEPT_BED_NOTE = 'Здесь существующий цветник — он сохраняется, новая посадка на его месте не устраивается.'
NOT_FOUND_NOTE = ('Рассадка не нашла места: зона уже занята другими посадками и существующими деревьями '
                  'с нужными просветами, свободное место неудобно для ухода (далеко от дорожек, подход только '
                  'с проезда) или исчерпан предел, заданный параметрами рассадки.')
AUTO_NOTES = (KEPT_BED_NOTE, NOT_FOUND_NOTE,
              'Рассадка не нашла места: зона уже занята другими посадками и существующими деревьями '
              'с нужными просветами (или исчерпан предел, заданный параметрами рассадки).',
              'Рассадка не нашла места: зона уже занята другими посадками и существующими деревьями с нужными просветами.')


def place(geojson, plan, seed=0, config=None, log=print, mode='algorithm', model=None):
    """mode — 'algorithm' или 'model' (см. начало модуля); model — model.Model, по умолчанию из greening/models."""
    config = config or load_config()
    rng = np.random.default_rng(seed)
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    existing = [g for g, p in features if p['layer'] == 'tree']
    paths = unary_union([g for g, p in features if p['layer'] == 'surface' and p['surface'] in PATH_SURFACES])
    surfaces = {p['surface']: g for g, p in features if p['layer'] == 'surface'}
    # край газона — по поверхностям чертежа: в объединении секций дыры от отброшенных обрезков
    green = unary_union([g for s, g in surfaces.items() if s in GREEN_SURFACES]) or unary_union(list(sections.values()))
    walks = unary_union([g for s, g in surfaces.items() if s in WALK_SURFACES])
    access = Access(config, green, walks, surfaces.get('roadway', Point()))
    variety = config.get('variety') or {}
    meadows = set(variety.get('meadow_lawns') or ())
    # существующие цветники сохраняются: новые цветники и газон их не накрывают
    old_beds = unary_union([g for g, p in features if p['layer'] == 'surface' and p['surface'] == 'flowerbed'])
    plants = {p['id']: p for p in catalog.load()}
    # mode ниже переиспользуется для приёма посадки — режим рассадки хранится отдельно
    used, requested, note = mode, mode, None
    if mode not in MODES:
        raise ValueError(f'Неизвестный режим рассадки: {mode}')
    scorer = None
    if mode == 'model':
        model = model or learned.load()
        if model is None:
            used, note = 'algorithm', 'Модели рассадки нет (greening/models/placement.npz) — места выбраны алгоритмом'
            log(note)
        else:
            # существующие деревья в признаках модели — вместе с условными стволами полос (так она и училась)
            scorer = Scorer(model, surfaces, existing)
    placer = Placer(config, existing, rng, paths, scorer, access)

    out, summary, styles = [], [], {}
    for structure in plan['structures']:
        style = styles[structure['id']] = _style(structure, seed, config)
        zones = {z['id']: z for z in structure['zones']}
        by_zone = defaultdict(list)
        for index, item in enumerate(structure.get('plantings', [])):
            if item.get('zone') in zones:
                by_zone[item['zone']].append((index, item))
        for zone_id, items in by_zone.items():
            zone = zones[zone_id]
            geometry = unary_union([sections[s] for s in zone['sections'] if s in sections])
            if geometry.is_empty:
                continue
            base = {'structure': structure['id'], 'zone': zone_id, 'patch': zone.get('patch')}
            # край зоны — нормативная граница: точки ставятся чуть внутрь, чтобы округление их не вынесло
            inner = geometry.buffer(-ROUNDING)
            if scorer is not None:
                # модель смотрит на весь газон зоны (u1…), а выбирает только внутри зоны
                same = [z for z in structure['zones'] if z.get('patch') == zone.get('patch')] if zone.get('patch') else [zone]
                parts = [(unary_union([sections[s] for s in z['sections'] if s in sections]), z['allowed']) for z in same]
                patch = unary_union([g for g, _ in parts])
                placer.site = scorer.site((structure['id'], zone.get('patch') or zone_id), patch, parts)
            points, areas = [], []
            for index, item in items:
                kind = KIND.get(item.get('group'), 'herbaceous')
                (areas if kind == 'herbaceous' else points).append((index, item, kind))
            points.sort(key=lambda p: ORDER[(p[2], _mode(p[1].get('role')))])

            # деревья, потом кустарники: сначала ряды и изгороди вдоль края, потом зона заполняется
            # солитерами и группами выбранных видов по очереди
            results = {}
            crowns = defaultdict(float)          # площадь крон по виду посадки, м²
            # предел новых деревьев в зоне (параметр рассадки): ряды тоже считаются
            per_100 = config.get('tree_max_per_100m2')
            tree_left = math.inf if per_100 is None else math.floor(per_100 * geometry.area / 100)
            for kind in ('tree', 'shrub'):
                entries, heights = [], []
                # новые деревья зоны — места для подлеска из кустов (variety.underplanting_share)
                anchors = [(x, y, p[1], p[3]) for _, _, placed in results.values() for x, y, p in placed
                           if p[0] == 'tree'] if kind == 'shrub' else None
                for index, item, k in points:
                    if k != kind:
                        continue
                    crown = _crown(plants.get(item.get('plant_id')), kind, config)
                    mode = _mode(item.get('role'))
                    meta = {**base, 'plant_id': item.get('plant_id'), 'name': item.get('name'), 'kind': kind,
                            'role': item.get('role'), 'crown_m': crown}
                    if kind not in zone['allowed']:
                        results[index] = (meta, item, [])
                    elif mode == 'row':
                        cap = None if kind != 'tree' or tree_left == math.inf else max(0, tree_left)
                        results[index] = (meta, item, placer.row(inner, None, kind, crown, style, meta, cap) if cap != 0 else [])
                        crowns[kind] += len(results[index][2]) * math.pi * (crown / 2) ** 2
                        if kind == 'tree':
                            tree_left -= len(results[index][2])
                    else:
                        entries.append((index, item, (placer.inset(inner, kind, crown), crown, mode, meta)))
                        heights.append(_height(plants.get(item.get('plant_id')), crown))
                budget = math.inf
                if kind == 'shrub':
                    # кустарники — только доля зоны: остальное остаётся газоном и цветником
                    budget = config['shrub_cover_share'][style] * max(0.0, geometry.area - crowns['tree']) - crowns['shrub']
                filled = placer.fill([e for _, _, e in entries], kind, style, budget,
                                     max(0, tree_left) if kind == 'tree' else math.inf, heights, anchors)
                for (index, item, entry), placed in zip(entries, filled):
                    results[index] = (entry[3], item, placed)
                    crowns[kind] += len(placed) * math.pi * (entry[1] / 2) ** 2

            # размер группы — по всем её растениям (в смешанной группе их два вида)
            sizes = defaultdict(int)
            for _, _, placed in results.values():
                for _, _, p in placed:
                    sizes[p[3]] += 1
            for index, (meta, item, placed) in sorted(results.items()):
                for number, (x, y, p) in enumerate(placed, 1):
                    out.append({'type': 'Feature', 'geometry': mapping(Point(round(x, 2), round(y, 2))),
                                'properties': {'layer': 'plant', **meta, 'group': item.get('group'),
                                               'spacing_m': round(p[2], 2), 'group_id': p[3],
                                               'group_size': sizes[p[3]], 'number': number}})
                summary.append({**base, 'index': index, 'plant_id': item.get('plant_id'), 'name': item.get('name'),
                                'kind': meta['kind'], 'role': item.get('role'), 'unit': 'шт', 'placed': len(placed)})

            # цветники — пятна bed_share зоны в стороне от кустов, газон — что осталось (несколько газонов — поровну);
            # миксбордер (виды цветника с mix_of) — одно пятно, поделённое на куртины видов
            taken = []
            lawns = [a for a in areas if a[1].get('group') == LAWN_GROUP]
            mixes = defaultdict(list)
            for a in areas:
                if a[1].get('group') != LAWN_GROUP:
                    mixes[a[1].get('mix_of', a[0])].append(a)
            # луговой газон в глубине, обыкновенный — полосой вдоль дорожек (variety.meadow_border_m)
            border = variety.get('meadow_border_m')
            meadow = [a for a in lawns if a[1].get('plant_id') in meadows]
            edging = [a for a in lawns if a[1].get('plant_id') not in meadows]
            strip = None
            if border and meadow and edging and not walks.is_empty:
                lawns = edging[:1] + [a for a in lawns if a is not edging[0]]
                strip = walks.intersection(geometry.buffer(border + 1)).buffer(border)
            units = list(mixes.values()) + [[a] for a in lawns]
            bed_area = min(config['bed_share'] * geometry.area, config['bed_max_m2']) / max(len(mixes), 1)
            free = geometry.difference(old_beds) if not old_beds.is_empty and old_beds.intersects(geometry) else geometry
            kept_bed = free.area < 0.5 * geometry.area
            for n, unit in enumerate(units):
                available = free.difference(unary_union(taken)) if taken else free
                lawn = unit[0][1].get('group') == LAWN_GROUP
                last = n == len(units) - 1 and (lawn or not lawns)
                target = available.area / (len(units) - n) if lawn else bed_area
                polygon = None
                if strip is not None and unit[0] is lawns[0] and not last:
                    polygon = available.intersection(strip)
                    polygon = polygon if polygon.area >= MIN_EDGING_M2 else None
                if polygon is None:
                    polygon = available if last else placer.patch(available, target)
                parts = placer.drifts(polygon, len(unit)) if len(unit) > 1 else [polygon]
                for (index, item, kind), part in zip(unit, parts):
                    area = round(part.area, 1) if part is not None and not part.is_empty else 0.0
                    if area:
                        taken.append(part)
                        out.append({'type': 'Feature',
                                    'geometry': mapping(shapely.set_precision(part, 0.01)),
                                    'properties': {'layer': 'lawn' if lawn else 'bed', **base,
                                                   'plant_id': item.get('plant_id'), 'name': item.get('name'),
                                                   'group': item.get('group'), 'kind': kind, 'role': item.get('role'),
                                                   'area_m2': area}})
                    summary.append({**base, 'index': index, 'plant_id': item.get('plant_id'), 'name': item.get('name'),
                                    'kind': kind, 'role': item.get('role'), 'unit': 'м2', 'placed': area,
                                    **({'note': KEPT_BED_NOTE} if kept_bed and not area else {})})

    # сквозной номер растения: по нему точка в DXF, проверка отступов и обоснование ссылаются друг на друга
    for number, feature in enumerate((f for f in out if f['properties']['layer'] == 'plant'), 1):
        feature['properties'] = {'id': f'R{number:05d}', **feature['properties']}

    for group in placer.outlines:
        g = group.pop('geometry')
        if g.is_empty:
            continue
        out.append({'type': 'Feature', 'geometry': mapping(shapely.set_precision(g, 0.01)),
                    'properties': {'layer': 'group', **group,
                                   'label': group.get('label') or f"{group['name']} ×{group['count']}"}})

    totals = defaultdict(float)
    for s in summary:
        totals[s['kind'] if s['unit'] == 'шт' else f"{s['kind']}_m2"] += s['placed']
    return {
        'type': 'FeatureCollection',
        'metadata': {'seed': seed, 'config': {k: v for k, v in config.items() if not k.startswith('_')},
                     'mode': used, 'requested_mode': requested, **({'mode_note': note} if note else {}),
                     **({'model': _model_card(scorer.model)} if scorer is not None else {}),
                     'styles': styles,
                     'summary': {'totals': {k: {'placed': round(v, 1)} for k, v in totals.items()}},
                     'plantings': summary},
        'features': out,
    }


class Scorer:
    """Оценка мест моделью: газон с окружением и существующими деревьями (model.Site) кэшируется.
    Расстояния до уже посаженного модель не учитывает — их держат правила (Field)."""

    def __init__(self, model, surfaces, existing):
        self.model = model
        self.surfaces = surfaces
        self.existing = _PointIndex([g for g in existing])
        self.sites = {}

    def site(self, key, patch, zones=()):
        """zones — [(геометрия зоны газона, allowed)]: где какой вид запрещён нормами (признак модели)."""
        if key not in self.sites:
            patch = learned.polygonal(patch)
            near = patch.buffer(learned.CONTEXT_M)
            self.sites[key] = learned.Site(patch, {k: g.intersection(near) for k, g in self.surfaces.items()},
                                           self.existing.points_near(near), learned.blocked_parts(zones))
        return self.sites[key]

    def __call__(self, kind, site, points, field):
        return self.model.score(kind, site, [p[0] for p in points], [p[1] for p in points])


class _PointIndex:
    """Точки существующих деревьев с индексом: какие лежат в области."""

    def __init__(self, points):
        self.points = points
        self.index = shapely.STRtree(points) if points else None

    def points_near(self, area):
        if self.index is None:
            return []
        return [(self.points[int(i)].x, self.points[int(i)].y) for i in self.index.query(area, predicate='intersects')]


def _model_card(model):
    kinds = model.meta.get('kinds', {})
    return {kind: {'auc_model': k.get('auc_model_mean'), 'auc_algorithm': k.get('auc_algorithm_mean'),
                   'positives_project': k.get('positives_project'), 'objects': len(k.get('objects_with_project', []))}
            for kind, k in kinds.items()}


def _rigid(old, new, tolerance=0.02):
    """Сдвиг с поворотом, переводящий точки old в new (по центрам, наименьшие квадраты): (угол в градусах,
    центр old, центр new) или None, если точки сдвинуты не как одно целое."""
    n = len(old)
    ox, oy = sum(x for x, _ in old) / n, sum(y for _, y in old) / n
    nx, ny = sum(x for x, _ in new) / n, sum(y for _, y in new) / n
    cross = sum((a - ox) * (d - ny) - (b - oy) * (c - nx) for (a, b), (c, d) in zip(old, new))
    dot = sum((a - ox) * (c - nx) + (b - oy) * (d - ny) for (a, b), (c, d) in zip(old, new))
    angle = math.atan2(cross, dot) if n > 1 else 0.0
    cos, sin = math.cos(angle), math.sin(angle)
    for (a, b), (c, d) in zip(old, new):
        x, y = a - ox, b - oy
        if math.hypot(x * cos - y * sin + nx - c, x * sin + y * cos + ny - d) > tolerance:
            return None
    return math.degrees(angle), (ox, oy), (nx, ny)


def apply_moves(result, moves):
    """Перемещения растений пользователем (редактирование, уровень 3): moves — {номер растения: (x, y)}.
    Растение остаётся в своей посадке и зоне (structure, zone), у него ставится moved. Контур группы: вся группа
    сдвинута или повёрнута как одно целое — контур сдвигается и поворачивается вместе с ней, иначе строится
    заново по кронам. Нормы здесь не проверяются — это делают verify() и проверка отступов после.
    Возвращает число перемещённых растений."""
    plants = {f['properties'].get('id'): f for f in result['features'] if f['properties']['layer'] == 'plant'}
    before = {pid: tuple(f['geometry']['coordinates']) for pid, f in plants.items()}
    touched = set()
    moved = 0
    for pid, (x, y) in moves.items():
        feature = plants.get(pid)
        if feature is None:
            continue
        ox, oy = feature['geometry']['coordinates']
        if abs(ox - x) < 0.005 and abs(oy - y) < 0.005:
            continue
        feature['geometry'] = mapping(Point(round(x, 2), round(y, 2)))
        feature['properties']['moved'] = True
        touched.add(feature['properties'].get('group_id'))
        moved += 1
    members = defaultdict(list)
    for pid, feature in plants.items():
        members[feature['properties'].get('group_id')].append(pid)
    for feature in result['features']:
        props = feature['properties']
        if props['layer'] != 'group' or props.get('group_id') not in touched:
            continue
        group = members[props['group_id']]
        motion = _rigid([before[pid] for pid in group], [tuple(plants[pid]['geometry']['coordinates']) for pid in group])
        if motion:
            angle, (ox, oy), (nx, ny) = motion
            geometry = affinity.rotate(shape(feature['geometry']), angle, origin=(ox, oy))
            geometry = affinity.translate(geometry, nx - ox, ny - oy)
        else:
            # крона — своя у каждого растения: в смешанной группе два вида
            geometry = unary_union([shape(plants[pid]['geometry']).buffer(plants[pid]['properties']['crown_m'] / 2 * 0.9,
                                                                          quad_segs=6) for pid in group])                 .buffer(0.3, quad_segs=4).buffer(-0.3, quad_segs=4)
        feature['geometry'] = mapping(shapely.set_precision(geometry, 0.01))
    result['metadata']['moved'] = result['metadata'].get('moved', 0) + moved
    return moved


def apply_counts(plan, result):
    """Количества в план — по итогам рассадки (их определяет алгоритм, а не LLM): у посадки quantity и unit."""
    placed = {(s['structure'], s['index']): s for s in result['metadata']['plantings']}
    for structure in plan['structures']:
        for index, item in enumerate(structure.get('plantings', [])):
            s = placed.get((structure['id'], index))
            unit = s['unit'] if s else item.get('unit', 'шт')
            value = s['placed'] if s else 0
            item['unit'], item['quantity'] = unit, (int(value) if unit == 'шт' else round(float(value), 1))
            # пометки прежней рассадки снимаются: план мог прийти из прошлой версии (пересчёт по параметрам)
            item['warnings'] = [w for w in item.get('warnings', []) if w not in AUTO_NOTES]
            if not value:
                item['warnings'].append((s or {}).get('note') or NOT_FOUND_NOTE)
    return plan


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
    parser.add_argument('--placement', choices=MODES, default='algorithm',
                        help='как выбирать места: algorithm — эвристика, model — обученная модель')
    args = parser.parse_args()

    source = Path(args.geojson)
    stem = source.name.replace('.greening.geojson', '')
    out = Path(args.out) if args.out else source.parent
    geojson = json.loads(source.read_text(encoding='utf-8'))
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    from . import variety
    variety.compose(plan)
    result = place(geojson, plan, args.seed, mode=args.placement)
    problems = verify(result, geojson)
    Path(args.plan).write_text(json.dumps(apply_counts(plan, result), ensure_ascii=False), encoding='utf-8')
    result['metadata']['summary']['violations'] = len(problems)
    (out / f'{stem}.placement.geojson').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    write_dxf(result, out / f'{stem}.placement.dxf')
    for problem in problems[:20]:
        print(problem, file=sys.stderr)
    print(json.dumps({'out': str(out / f'{stem}.placement.geojson'), **result['metadata']['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
