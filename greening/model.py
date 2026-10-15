"""Модель рассадки: оценка, насколько точка газона подходит для дерева или куста, — по тому, как сажают
проектировщики (посадочные чертежи пилотного набора) и как деревья уже стоят в городе.

Модель не решает, где сажать можно: это делают нормы (sections.py). Она получает только точки,
которые уже лежат в зоне, разрешённой для этого вида посадки, и прошли правила расстояний
(place.Field), и выбирает среди них. Поэтому выйти за разрешённую зону она не может; verify()
и проверка отступов (clearance.py) остаются как есть.

Признаки точки — только из входа (подосновы), не из ответа: расстояния до края газона, дорожек, проезда,
здания и существующих деревьев, до части газона, где этот вид нельзя по нормам (коридор сети, отступ
от стены), ширина и площадь газона, выравнивание ближайшего дерева вдоль края.
Другие посадки проекта в признаки не входят: иначе модель учится «сажай рядом с посадкой» по самому ответу
(на отложенном объекте это давало AUC 0,999 у кустов), а при рассадке такого соседа ещё нет.
Расстояния между новыми растениями задают правила рассадки (place.Field).
Модель на numpy (без внешних библиотек), своя на деревья и на кусты: для деревьев — перцептрон
с одним скрытым слоем (16), для кустов — логистическая регрессия; так вышло лучше всего на отложенных
объектах (сравнение вариантов — в placement.json, holdout). Учится только на посадках проектировщиков:
существующие деревья растут кучно и тянули модель не туда — они остаются признаком (вход), не примером.
Вес каждого объекта выровнен: иначе Олимпийская (9 тыс. кустов) задавала бы всё.
Веса и нормировка — greening/models/placement.npz, описание обучения — placement.json рядом.

Обучение: python -m greening.model train D:\\Projects\\greening_data\\samples
"""

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Point, shape
from shapely.ops import unary_union

MODELS = Path(__file__).resolve().parent / 'models'
WEIGHTS = MODELS / 'placement.npz'
# окружение, которое видит модель: в примерах обучения поверхности вырезаны в этом радиусе от газона, м
CONTEXT_M = 15.0
# соседние растения дальше этого не влияют, м
NEIGHBOUR_M = 12.0
PATH_SURFACES = ('sidewalk', 'paving', 'sealed', 'gravel', 'playground')
FEATURES = ('edge', 'path', 'road', 'building', 'tree', 'trees_10m', 'no_tree', 'no_shrub', 'width', 'area', 'depth',
            'align')
KINDS = ('tree', 'shrub')


# --- признаки -----------------------------------------------------------------------------------------

def polygonal(geometry):
    """Только площадные части геометрии (после make_valid бывают линии и точки)."""
    geometry = shapely.make_valid(geometry)
    parts = [g for g in shapely.get_parts(geometry) if g.geom_type in ('Polygon', 'MultiPolygon') and not g.is_empty]
    return unary_union(parts) if parts else shapely.Polygon()


def _distance(geometry, xs, ys, cap):
    if geometry is None or geometry.is_empty:
        return np.full(len(xs), cap)
    return np.minimum(shapely.distance(geometry, shapely.points(xs, ys)), cap)


class Site:
    """Газон и его окружение: всё, что нужно для признаков точек этого газона."""

    def __init__(self, patch, surfaces, trees=(), blocked=None):
        """patch — полигон газона; surfaces — {вид поверхности: геометрия}; trees — существующие деревья [(x, y)];
        blocked — {'tree' | 'shrub': части газона, где этот вид запрещён нормами}."""
        self.patch = patch
        self.edge = patch.boundary
        shapely.prepare(self.edge)
        near = patch.buffer(CONTEXT_M)
        pick = lambda kinds: unary_union([g for k, g in surfaces.items() if k in kinds and g is not None]).intersection(near)
        self.path = pick(PATH_SURFACES)
        self.road = pick(('roadway',))
        self.building = pick(('building',))
        for g in (self.path, self.road, self.building):
            shapely.prepare(g)
        self.width = min(2 * patch.area / max(patch.length, 1e-6), 40.0)
        self.area = math.log1p(patch.area)
        # глубина газона — самое большое расстояние от края внутри (по сетке): нормирует «насколько внутри»
        minx, miny, maxx, maxy = patch.bounds
        step = max(0.5, math.sqrt(patch.area) / 30)
        gx, gy = np.meshgrid(np.arange(minx, maxx, step), np.arange(miny, maxy, step))
        inside = shapely.contains_xy(patch, gx.ravel(), gy.ravel())
        depth = shapely.distance(self.edge, shapely.points(gx.ravel()[inside], gy.ravel()[inside]))
        self.depth = max(float(np.nanmax(depth)) if len(depth) and np.isfinite(depth).any() else 0.0, 0.25)
        self.trees = list(trees)
        self.blocked = {k: (blocked or {}).get(k) for k in KINDS}
        for g in self.blocked.values():
            if g is not None:
                shapely.prepare(g)

    def features(self, xs, ys):
        """Матрица признаков (n, len(FEATURES))."""
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        edge = _distance(self.edge, xs, ys, CONTEXT_M)
        tree_d, tree_n, align = _neighbours(xs, ys, self.trees, self.edge)
        return np.column_stack([
            np.log1p(edge),
            np.log1p(_distance(self.path, xs, ys, CONTEXT_M)),
            np.log1p(_distance(self.road, xs, ys, CONTEXT_M)),
            np.log1p(_distance(self.building, xs, ys, CONTEXT_M)),
            np.log1p(tree_d), tree_n,
            np.log1p(_distance(self.blocked['tree'], xs, ys, CONTEXT_M)),
            np.log1p(_distance(self.blocked['shrub'], xs, ys, CONTEXT_M)),
            np.full(len(xs), math.log1p(self.width)), np.full(len(xs), self.area),
            np.minimum(edge / self.depth, 1.0), align,
        ])


def _neighbours(xs, ys, points, edge):
    """Расстояние до ближайшего соседа (не дальше NEIGHBOUR_M), число соседей в 10 м и выравнивание:
    |cos| угла между направлением на ближайшего соседа и краем газона (1 — сосед вдоль края, как в ряду)."""
    n = len(xs)
    if not len(points):
        return np.full(n, NEIGHBOUR_M), np.zeros(n), np.zeros(n)
    pts = np.asarray(points, float)
    dx = pts[None, :, 0] - xs[:, None]
    dy = pts[None, :, 1] - ys[:, None]
    d = np.hypot(dx, dy)
    # точка, совпадающая с соседом (та же посадка из другого источника), соседом не считается
    d[d < 0.3] = np.inf
    nearest = d.argmin(axis=1)
    best = np.minimum(d[np.arange(n), nearest], NEIGHBOUR_M)
    count = (d < 10.0).sum(axis=1).astype(float)
    align = np.zeros(n)
    if edge is not None:
        close = best < NEIGHBOUR_M
        if close.any():
            idx = np.where(close)[0]
            heading = np.arctan2(dy[idx, nearest[idx]], dx[idx, nearest[idx]])
            along = np.array([_edge_heading(edge, xs[i], ys[i]) for i in idx])
            align[idx] = np.abs(np.cos(heading - along))
    return best, count, align


def _edge_heading(edge, x, y):
    at = edge.project(Point(x, y))
    a, b = edge.interpolate(max(at - 1.0, 0)), edge.interpolate(min(at + 1.0, edge.length))
    return math.atan2(b.y - a.y, b.x - a.x)


# --- модель -------------------------------------------------------------------------------------------

class Net:
    """Перцептрон F → H1 → H2 → 1 (ReLU, сигмоида) с нормировкой входа."""

    def __init__(self, sizes=(len(FEATURES), 16, 1), seed=0, params=None):
        if params is not None:
            self.p = {k: np.asarray(v, float) for k, v in params.items()}
            return
        rng = np.random.default_rng(seed)
        self.p = {'mean': np.zeros(sizes[0]), 'std': np.ones(sizes[0])}
        for i, (a, b) in enumerate(zip(sizes, sizes[1:])):
            self.p[f'W{i}'] = rng.normal(0, math.sqrt(2 / a), (a, b))
            self.p[f'b{i}'] = np.zeros(b)

    def _layers(self):
        return sum(1 for k in self.p if k.startswith('W'))

    def forward(self, X, keep=False):
        h = (X - self.p['mean']) / self.p['std']
        cache = [h]
        for i in range(self._layers()):
            z = h @ self.p[f'W{i}'] + self.p[f'b{i}']
            h = np.maximum(z, 0) if i < self._layers() - 1 else z
            cache.append(h)
        logit = h[:, 0]
        return (logit, cache) if keep else logit

    def prob(self, X):
        return 1 / (1 + np.exp(-np.clip(self.forward(X), -30, 30)))

    def fit(self, X, y, w, epochs=200, lr=3e-3, l2=1e-3, batch=4096, seed=0, log=None):
        rng = np.random.default_rng(seed)
        self.p['mean'], self.p['std'] = X.mean(axis=0), X.std(axis=0) + 1e-6
        names = [k for k in self.p if k[0] in 'Wb']
        m = {k: np.zeros_like(self.p[k]) for k in names}
        v = {k: np.zeros_like(self.p[k]) for k in names}
        step = 0
        w = w / w.mean()
        for epoch in range(epochs):
            order = rng.permutation(len(X))
            for start in range(0, len(X), batch):
                idx = order[start:start + batch]
                logit, cache = self.forward(X[idx], keep=True)
                p = 1 / (1 + np.exp(-np.clip(logit, -30, 30)))
                grad = ((p - y[idx]) * w[idx] / len(idx))[:, None]
                grads = {}
                for i in reversed(range(self._layers())):
                    grads[f'W{i}'] = cache[i].T @ grad + l2 * self.p[f'W{i}']
                    grads[f'b{i}'] = grad.sum(axis=0)
                    if i:
                        grad = (grad @ self.p[f'W{i}'].T) * (cache[i] > 0)
                step += 1
                for k in names:
                    m[k] = 0.9 * m[k] + 0.1 * grads[k]
                    v[k] = 0.999 * v[k] + 0.001 * grads[k] ** 2
                    self.p[k] -= lr * (m[k] / (1 - 0.9 ** step)) / (np.sqrt(v[k] / (1 - 0.999 ** step)) + 1e-8)
            if log and (epoch + 1) % 100 == 0:
                p = self.prob(X)
                loss = -np.mean(w * (y * np.log(p + 1e-9) + (1 - y) * np.log(1 - p + 1e-9)))
                log(f'  эпоха {epoch + 1}: потери {loss:.4f}')
        return self


class Model:
    """Обученные сети по видам посадки; score(kind, site, xs, ys) → вероятность «здесь сажают»."""

    def __init__(self, nets, meta=None):
        self.nets, self.meta = nets, meta or {}

    @classmethod
    def load(cls, path=WEIGHTS):
        path = Path(path)
        if not path.exists():
            return None
        data = np.load(path)
        nets = {}
        for kind in KINDS:
            params = {k.split('/', 1)[1]: data[k] for k in data.files if k.startswith(kind + '/')}
            if params:
                nets[kind] = Net(params=params)
        meta_path = path.with_suffix('.json')
        meta = json.loads(meta_path.read_text(encoding='utf-8')) if meta_path.exists() else {}
        return cls(nets, meta)

    def save(self, path=WEIGHTS):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, **{f'{kind}/{k}': v for kind, net in self.nets.items() for k, v in net.p.items()})
        path.with_suffix('.json').write_text(json.dumps(self.meta, ensure_ascii=False, indent=1), encoding='utf-8')

    def score(self, kind, site, xs, ys):
        net = self.nets.get(kind)
        if net is None or not len(xs):
            return np.full(len(xs), 0.5)
        return net.prob(site.features(xs, ys))


_loaded = {}


def load(path=WEIGHTS):
    """Модель из файла (один раз на процесс) или None, если её нет."""
    key = str(path)
    if key not in _loaded:
        _loaded[key] = Model.load(path)
    return _loaded[key]


# --- данные для обучения -----------------------------------------------------------------------------

# случайных точек газона на одну посадку и не меньше одной на столько м²
NEGATIVES_PER_POSITIVE = 3
NEGATIVE_AREA_M2 = 25.0
# отрицательная точка не ближе этого к любой посадке своего вида, м
NEGATIVE_GAP = {'tree': 2.0, 'shrub': 0.8}
# шаг точек вдоль изгороди и внутри пятна группы кустов, м; вес пятна или изгороди — как у стольких кустов
SHRUB_STEP = 1.0
SHRUB_AREA_WEIGHT = 5.0


def _surfaces(sample):
    out = {}
    for kind, parts in sample['context']['surfaces'].items():
        # части вырезаны с округлением до сантиметра и бывают невалидны — make_valid перед объединением
        geoms = [shapely.make_valid(shape(g)) for g in parts]
        out[kind] = shapely.union_all(geoms, grid_size=0.01) if geoms else None
    return out


def _shrub_points(targets, patch):
    """Кусты проекта точками: одиночные — как есть; пятна групп — сетка SHRUB_STEP, изгороди — шаг вдоль линии.
    Возвращает [(x, y, вес)]: суммарный вес пятна или изгороди не больше SHRUB_AREA_WEIGHT."""
    out = [(t['x'], t['y'], 1.0) for t in targets.get('shrubs', [])]
    for item in targets.get('shrub_groups', []):
        g = shape(item['geometry']).intersection(patch)
        if g.is_empty or g.area < 0.5:
            continue
        minx, miny, maxx, maxy = g.bounds
        gx, gy = np.meshgrid(np.arange(minx, maxx, SHRUB_STEP), np.arange(miny, maxy, SHRUB_STEP))
        inside = shapely.contains_xy(g, gx.ravel(), gy.ravel())
        pts = list(zip(gx.ravel()[inside], gy.ravel()[inside])) or [(g.centroid.x, g.centroid.y)]
        out += [(x, y, min(1.0, SHRUB_AREA_WEIGHT / len(pts))) for x, y in pts]
    for item in targets.get('hedges', []):
        g = shape(item['geometry']).intersection(patch.buffer(0.3))
        for line in getattr(g, 'geoms', [g]):
            if line.geom_type != 'LineString' or line.length < 1:
                continue
            steps = np.arange(0, line.length, SHRUB_STEP)
            pts = [line.interpolate(s) for s in steps]
            out += [(p.x, p.y, min(1.0, SHRUB_AREA_WEIGHT / len(pts))) for p in pts]
    return out


def sample_rows(sample, rng):
    """Строки обучения из одного примера-газона: {kind: (X, y, w, source)}."""
    patch = polygonal(shape(sample['geometry']))
    if patch.is_empty or patch.area < 20:
        return {}
    shapely.prepare(patch)
    surfaces = _surfaces(sample)
    existing = [tuple(p) for p in sample['context']['existing_trees']]
    targets = sample.get('targets') or {}
    project_trees = [(t['x'], t['y']) for t in targets.get('trees', []) if patch.buffer(0.5).covers(Point(t['x'], t['y']))]
    shrubs = _shrub_points(targets, patch)
    # соседи в признаках — только существующие деревья (вход), посадки проекта — ответ
    site = Site(patch, surfaces, existing, blocked_parts(
        [(shape(z['geometry']), z['allowed']) for z in sample.get('zones', []) if z.get('geometry')]))
    out = {}
    positives = {
        'tree': [(x, y, 1.0, 'project') for x, y in project_trees],
        'shrub': [(x, y, w, 'project') for x, y, w in shrubs],
    }
    for kind, pos in positives.items():
        if not pos:
            continue
        n_neg = max(NEGATIVES_PER_POSITIVE * len(pos), int(patch.area / NEGATIVE_AREA_M2))
        neg = _random_points(patch, rng, n_neg, [(p[0], p[1]) for p in pos], NEGATIVE_GAP[kind])
        rows_X, rows_y, rows_w, rows_s = [], [], [], []
        X = site.features([p[0] for p in pos], [p[1] for p in pos])
        rows_X += list(X); rows_y += [1.0] * len(pos); rows_w += [p[2] for p in pos]; rows_s += [p[3] for p in pos]
        if neg:
            xs, ys = zip(*neg)
            X = site.features(xs, ys)
            pos_w = sum(p[2] for p in pos)
            rows_X += list(X); rows_y += [0.0] * len(neg); rows_w += [pos_w / len(neg)] * len(neg)
            rows_s += ['negative'] * len(neg)
        out[kind] = (np.array(rows_X), np.array(rows_y), np.array(rows_w), np.array(rows_s))
    return out


def blocked_parts(zones):
    """[(геометрия зоны, allowed)] → {'tree' | 'shrub': объединение зон, где этот вид не разрешён}."""
    out = {}
    for kind in KINDS:
        parts = [polygonal(g) for g, allowed in zones if kind not in allowed]
        out[kind] = unary_union(parts) if parts else None
    return out


def _random_points(polygon, rng, count, avoid, gap):
    minx, miny, maxx, maxy = polygon.bounds
    out = []
    tree = STRtree([Point(p) for p in avoid]) if avoid else None
    for _ in range(20):
        xs, ys = rng.uniform(minx, maxx, count * 3), rng.uniform(miny, maxy, count * 3)
        ok = shapely.contains_xy(polygon, xs, ys)
        for x, y in zip(xs[ok], ys[ok]):
            if tree is not None and len(tree.query(Point(x, y), predicate='dwithin', distance=gap)):
                continue
            out.append((x, y))
            if len(out) >= count:
                return out
    return out


def dataset(samples_dir, log=print):
    """Все примеры-газоны → {kind: (X, y, w, source, object)}."""
    samples_dir = Path(samples_dir)
    rng = np.random.default_rng(0)
    parts = {k: [] for k in KINDS}
    files = sorted(samples_dir.glob('*/patch_*.json'))
    for n, path in enumerate(files):
        sample = json.loads(path.read_text(encoding='utf-8'))
        for kind, (X, y, w, s) in sample_rows(sample, rng).items():
            parts[kind].append((X, y, w, s, np.full(len(y), sample['object'], dtype=object)))
        if log and (n + 1) % 200 == 0:
            log(f'  примеров {n + 1} из {len(files)}')
    data = {}
    for kind, chunks in parts.items():
        if chunks:
            data[kind] = tuple(np.concatenate([c[i] for c in chunks]) for i in range(5))
    return data


# --- оценка ------------------------------------------------------------------------------------------

def auc(scores, labels):
    """Площадь под ROC: вероятность, что посадка проектировщика оценена выше случайной точки газона."""
    scores, labels = np.asarray(scores), np.asarray(labels)
    pos, neg = scores[labels == 1], scores[labels == 0]
    if not len(pos) or not len(neg):
        return None
    ranks = np.argsort(np.argsort(np.concatenate([pos, neg]))) + 1
    return float((ranks[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def baseline(X):
    """Эвристика алгоритма (place._open_score): дальше от соседей и края газона — лучше."""
    tree = np.expm1(X[:, FEATURES.index('tree')])
    edge = np.expm1(X[:, FEATURES.index('edge')])
    return np.minimum(tree, 10) + 0.6 * np.minimum(edge, 2) + 0.15 * np.minimum(edge, 12)


# скрытые слои сети по виду посадки: () — логистическая регрессия
HIDDEN = {'tree': (16,), 'shrub': ()}


def balanced(w, obj, y):
    """Веса так, чтобы у каждого объекта положительные и отрицательные весили одинаково в сумме."""
    w = w.copy()
    for o in set(obj):
        for label in (0, 1):
            mask = (obj == o) & (y == label)
            if mask.any():
                w[mask] /= w[mask].sum()
    return w


def train(samples_dir, out=WEIGHTS, log=print):
    data = dataset(samples_dir, log)
    meta = {'features': list(FEATURES), 'samples': str(samples_dir), 'hidden': {k: list(v) for k, v in HIDDEN.items()},
            'kinds': {}}
    nets = {}
    for kind, (X, y, w, source, obj) in data.items():
        project_objects = sorted(set(obj[source == 'project']))
        log(f'{kind}: строк {len(y)}, посадок проекта {int((source == "project").sum())} '
            f'(объектов {len(project_objects)})')
        sizes = (X.shape[1], *HIDDEN[kind], 1)
        # проверка на отложенном объекте: обучение без него, оценка — на его посадках проекта
        folds = []
        for held in project_objects:
            keep = obj != held
            test = (obj == held) & ((source == 'project') | (source == 'negative'))
            if not keep.any() or y[test].sum() == 0:
                continue
            net = Net(sizes, seed=1).fit(X[keep], y[keep], balanced(w[keep], obj[keep], y[keep]))
            folds.append({'object': held, 'positives': int(y[test].sum()),
                          'auc_model': round(auc(net.prob(X[test]), y[test]), 3),
                          'auc_algorithm': round(auc(baseline(X[test]), y[test]), 3)})
            log(f"  отложен {held}: AUC модели {folds[-1]['auc_model']}, эвристики алгоритма {folds[-1]['auc_algorithm']}")
        nets[kind] = Net(sizes, seed=1).fit(X, y, balanced(w, obj, y), log=log)
        meta['kinds'][kind] = {
            'rows': int(len(y)), 'positives_project': int((source == 'project').sum()),
            'objects_with_project': project_objects,
            'holdout': folds,
            'auc_model_mean': round(float(np.mean([f['auc_model'] for f in folds])), 3) if folds else None,
            'auc_algorithm_mean': round(float(np.mean([f['auc_algorithm'] for f in folds])), 3) if folds else None,
        }
    model = Model(nets, meta)
    model.save(out)
    log(f'модель записана: {out}')
    return model


def main():
    parser = argparse.ArgumentParser(description='Модель рассадки: обучение на примерах greening_data')
    sub = parser.add_subparsers(dest='command', required=True)
    t = sub.add_parser('train', help='обучить на samples/<объект>/patch_*.json')
    t.add_argument('samples', help='папка samples из greening_data')
    t.add_argument('--out', default=str(WEIGHTS))
    args = parser.parse_args()
    if args.command == 'train':
        train(args.samples, args.out, log=lambda m: print(m, file=sys.stderr, flush=True))


if __name__ == '__main__':
    main()
