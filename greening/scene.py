"""3D-сцена результата: scene.json для отдельной страницы просмотра (core/static/core/scene.js).

Два шага. При разметке (`context`) из исходного чертежа берётся то, чего нет в зонах: здания целиком
(и за границей работ, до context_m) с этажностью по подписям слоя зданий — «9», «14», и подписи пород
существующих деревьев («БЕРЕЗА», «ТОПОЛЬ»). Это слои geojson `building` и `tree_label`.
По запросу страницы (`build`) — сцена из разметки, рассадки и просмотра: поверхности, здания с высотой,
существующие и новые растения с видом (форма, цвет листвы по сезонам, цветение — config/scene.json),
цветники, газоны. Координаты — от центра территории, метры, ось y — на север, как в чертеже.
Сцена строится только по запросу и кэшируется рядом с результатом: на основной конвейер не влияет.
"""

import json
import math
import re
from pathlib import Path

import shapely
from shapely import STRtree
from shapely.geometry import Polygon, mapping, shape
from shapely.ops import polygonize, unary_union

from . import catalog

CONFIG = Path(__file__).resolve().parent / 'config' / 'scene.json'
BUILDING_KINDS = ('building_wall', 'building_school_kindergarten')
# этажность в топосъёмке — число внутри контура здания, иногда с буквой назначения («9», «5Н», «14Ж»)
FLOORS = re.compile(r'^(\d{1,2})\s*[А-Яа-яA-Za-z]{0,2}$')
SPECIES_LABEL = re.compile(r'^[А-ЯЁа-яё]{3,}$')
# без подписи большая грань за границей работ — скорее двор, окружённый домом, чем дом
UNLABELLED_MAX_M2 = 400
SIMPLIFY = 0.1
HERBACEOUS = ('М', 'О', 'Б')
SURFACES = ('roadway', 'sidewalk', 'paving', 'sealed', 'gravel', 'playground', 'lawn', 'flowerbed', 'ground', 'unknown')


def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _polygons(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    return [p for p in getattr(geometry, 'geoms', []) if p.geom_type == 'Polygon']


# --- при разметке -------------------------------------------------------------------------------

def context(items, territory, surface_map, config=None):
    """{'buildings': [(Polygon, этажей | None, слой)], 'tree_labels': [(Point, текст)]} вокруг территории."""
    config = config or load_config()
    near = territory.buffer(config['context_m'])
    shapely.prepare(near)
    lines, floors, labels = [], [], []
    species = [re.compile(r['match'], re.I) for r in config['existing_labels']]
    for item in items:
        g = item.geometry
        if item.kind in BUILDING_KINDS:
            if item.etype in ('TEXT', 'MTEXT'):
                m = FLOORS.match(item.text.strip())
                if m and 1 <= int(m[1]) <= config['max_floors'] and near.contains(g):
                    floors.append((g, int(m[1])))
            elif not near.intersects(g):
                continue
            elif g.geom_type == 'LineString':
                lines.append((g, item.layer))
            elif item.etype == 'HATCH':
                lines.extend((p.exterior, item.layer) for p in _polygons(g))
        elif item.etype in ('TEXT', 'MTEXT') and item.kind in ('lawn_edge', 'tree', 'vegetation_edge'):
            text = item.text.strip()
            if SPECIES_LABEL.match(text) and any(r.search(text) for r in species)                     and territory.distance(g) < config['existing_label_radius_m']:
                labels.append((g, text))
    buildings = []
    if lines:
        faces = [f for f in polygonize(unary_union([g for g, _ in lines])) if f.area >= 1.0]
        line_index = STRtree([g for g, _ in lines])
        floor_index = STRtree([p for p, _ in floors]) if floors else None
        inside = surface_map.get('building', Polygon())
        shapely.prepare(territory)
        for face in faces:
            count = max((floors[int(n)][1] for n in floor_index.query(face, predicate='contains')), default=None) \
                if floor_index is not None else None
            if count is None:
                part = face.intersection(territory).area
                if part > face.area * 0.5:
                    # на территории здание уже определено разметкой (подписи, доля стен)
                    if face.intersection(inside).area < part * 0.5:
                        continue
                elif face.area > UNLABELLED_MAX_M2:
                    continue
            buildings.append((face, count, _dominant_layer(face, lines, line_index)))
        covered = unary_union([f for f, _, _ in buildings]) if buildings else Polygon()
        rest = inside.difference(covered.buffer(0.05))
        buildings.extend((p, None, '') for p in _polygons(rest) if p.area >= 1.0)
    return {'buildings': buildings, 'tree_labels': labels}


def _dominant_layer(face, lines, index):
    """Слой, по линиям которого идёт большая часть контура: крыльцо, навес или само здание."""
    edge = face.boundary.buffer(0.05)
    lengths = {}
    for n in index.query(edge):
        g, layer = lines[int(n)]
        lengths[layer] = lengths.get(layer, 0) + g.intersection(edge).length
    return max(lengths, key=lengths.get) if lengths else ''


# --- по запросу страницы ------------------------------------------------------------------------

class Profiles:
    """Вид растения → внешний вид (форма, цвета, высота и крона взрослого). Индекс — номер в списке сцены."""

    def __init__(self, config):
        self.config = config
        self.rules = [(re.compile(r['match'], re.I), r) for r in config['plants']]
        self.catalog = {p['id']: p for p in catalog.load()}
        self.items, self.index = [], {}

    def _look(self, name, group):
        # правило газона — только для газонов, цветочное — для травянистых, остальные — для деревьев и кустов
        category = 'lawn' if group == 'Г' else 'foliage' if group in HERBACEOUS else 'shape'
        for pattern, rule in self.rules:
            kind = 'lawn' if 'lawn' in rule else 'foliage' if 'foliage' in rule else 'shape'
            if kind == category and pattern.search(name):
                return {k: v for k, v in rule.items() if k != 'match'}
        return dict(self.config['group_defaults'].get(group, self.config['group_defaults']['КЛ']))

    def get(self, key, name, group, flags=(), defaults=None, force=None):
        if key in self.index:
            return self.index[key]
        look = self._look(name, group)
        for flag in flags:
            if re.fullmatch(r'h\d+', flag):
                look['h'] = int(flag[1:])
            elif re.fullmatch(r'к\d+', flag):
                look['k'] = int(flag[1:])
        for k, v in (defaults or {}).items():
            look.setdefault(k, v)
        look.update(force or {}, name=name, group=group)
        self.index[key] = len(self.items)
        self.items.append(look)
        return self.index[key]

    def plant(self, plant_id, name, group):
        entry = self.catalog.get(plant_id, {})
        return self.get(plant_id, entry.get('name', name), entry.get('group', group), entry.get('flags', ()))

    def existing(self, text):
        """Порода по подписи подосновы; существующее дерево — взрослое (h, k по умолчанию)."""
        default = self.config['existing_default']
        for rule in self.config['existing_labels']:
            if re.search(rule['match'], text, re.I):
                return self.get('existing:' + rule['name'], rule['name'], 'ДЛ', (),
                                {'h': default['h'], 'k': default['k'], 'existing': True})
        return None

    def existing_default(self, shape='round'):
        """Порода не подписана: условное лиственное дерево заданной формы."""
        return self.get('existing:' + shape, self.config['existing_default']['name'], 'ДЛ', (),
                        force={**self.config['existing_default'], 'shape': shape, 'existing': True})


def _ring(coords, ox, oy):
    flat = []
    for x, y in list(coords)[:-1]:
        flat += [round(x - ox, 2), round(y - oy, 2)]
    return flat


def _poly(geometry, ox, oy, simplify=SIMPLIFY):
    """[[внешнее кольцо, дыры…], …] — плоские списки x, y."""
    out = []
    for p in _polygons(geometry.simplify(simplify) if simplify else geometry):
        if p.is_empty or len(p.exterior.coords) < 4:
            continue
        out.append([_ring(p.exterior.coords, ox, oy)] + [_ring(r.coords, ox, oy) for r in p.interiors if len(r.coords) >= 4])
    return out


def _height(count, layer, config):
    if count:
        return round(count * config['floor_m'] + config['roof_m'], 1)
    for rule in config['building_heights']:
        if re.search(rule['match'], layer or '', re.I):
            return rule['h']
    return config['building_default_h']


def _existing(features, profiles, config, ox, oy):
    """[[x, y, профиль, ряд, крона, высота]] — существующие деревья. Размер разный (молодые, средние,
    крупные), крона не шире промежутка до соседнего дерева: в подоснове они часто стоят через 3–5 м."""
    trees = [g for g, p in features if p['layer'] == 'tree']
    rows = [bool(p.get('row')) for g, p in features if p['layer'] == 'tree']
    labels = [(g, p['text']) for g, p in features if p['layer'] == 'tree_label']
    label_index = STRtree([g for g, _ in labels]) if labels else None
    tree_index = STRtree(trees) if trees else None
    sizes, shapes = config['existing_sizes'], config['existing_shapes']
    out = []
    for n, g in enumerate(trees):
        kind = None
        if label_index is not None:
            m = int(label_index.nearest(g))
            if labels[m][0].distance(g) <= config['existing_label_radius_m']:
                kind = profiles.existing(labels[m][1])
        h = _hash(g.x, g.y)
        acc, size = 0, sizes[-1]
        for size in sizes:
            acc += size['share']
            if h < acc:
                break
        if kind is None:
            shape = size.get('shape') or shapes[int(_hash(g.y, g.x) * len(shapes)) % len(shapes)]
            kind = profiles.existing_default(shape)
        p = profiles.items[kind]
        crown = p['k'] * size['crown'] * (0.9 + _hash(g.x + 1, g.y) * 0.2)
        height = p['h'] * size['height'] * (0.9 + _hash(g.x, g.y + 1) * 0.2)
        near = [g.distance(trees[int(m)]) for m in tree_index.query(g, predicate='dwithin', distance=crown)
                if int(m) != n]
        if near:
            cap = max(config['existing_min_crown_m'], min(near) * config['existing_spacing'])
            if crown > cap:
                height *= (cap / crown) ** 0.5
                crown = cap
        out.append([round(g.x - ox, 2), round(g.y - oy, 2), kind, 1 if rows[n] else 0, round(crown, 1), round(height, 1)])
    return out


def _hash(x, y):
    """Устойчивое «случайное» 0…1 по координатам: одно и то же дерево — одного размера при каждой сборке."""
    v = math.sin(x * 12.9898 + y * 78.233) * 43758.5453
    return v - math.floor(v)


def build(geojson, placement=None, view=None, config=None):
    config = config or load_config()
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    territory = unary_union([g for g, p in features if p['layer'] == 'territory'])
    c = territory.centroid
    ox, oy = round(c.x), round(c.y)
    profiles = Profiles(config)

    surfaces = {p['surface']: _poly(g, ox, oy) for g, p in features
                if p['layer'] == 'surface' and p['surface'] in SURFACES}
    buildings = [{'p': _poly(g, ox, oy, 0.05), 'h': _height(p.get('floors'), p.get('source', ''), config),
                  'f': p.get('floors'), 'in': bool(g.intersects(territory)),
                  'water': bool(re.search('фонтан', p.get('source', ''), re.I))}
                 for g, p in features if p['layer'] == 'building']
    if not buildings:
        # разметка до появления слоя building: только здания на территории, высота условная
        buildings = [{'p': _poly(part, ox, oy, 0.05), 'h': config['building_default_h'], 'f': None, 'in': True}
                     for g, p in features if p['layer'] == 'surface' and p['surface'] == 'building'
                     for part in _polygons(g)]

    existing = _existing(features, profiles, config, ox, oy)

    plants, beds, lawns, groups = [], [], [], []
    for f in (placement or {}).get('features', []):
        p = f['properties']
        g = shape(f['geometry'])
        kind = profiles.plant(p.get('plant_id'), p.get('name', ''), p.get('group', ''))
        if p['layer'] == 'plant':
            # [x, y, профиль, крона проектная, номер растения, структура, группа, роль]
            plants.append([round(g.x - ox, 2), round(g.y - oy, 2), kind, p.get('crown_m', 2.0), p.get('id'),
                           p.get('structure'), p.get('group_id'), p.get('role', '')])
        elif p['layer'] == 'bed':
            beds.append({'p': _poly(g, ox, oy), 's': kind, 'a': p.get('area_m2')})
        elif p['layer'] == 'lawn':
            lawns.append({'p': _poly(g, ox, oy), 's': kind})
        elif p['layer'] == 'group' and p.get('role') == 'живая изгородь':
            groups.append({'p': _poly(g, ox, oy), 's': kind})

    objects = [{'type': p['type'], 'p': _poly(g, ox, oy)} for g, p in features
               if p['layer'] == 'structure' and g.geom_type != 'Point'
               and p['type'] in ('playground_children', 'playground_sport', 'special_surface', 'dog_area')]
    points = [{'type': p['type'], 'x': round(g.x - ox, 2), 'y': round(g.y - oy, 2)} for g, p in features
              if p['layer'] == 'structure' and g.geom_type == 'Point']

    structures, notes = [], {}
    for s in (view or {}).get('structures', []):
        x, y, w, h = s['window']
        # окно в просмотре — с перевёрнутой осью y (SVG)
        structures.append({'id': s['id'], 'name': s['name'], 'x': round(x + w / 2 - ox, 1),
                           'y': round(-(y + h / 2) - oy, 1), 'w': round(w, 1), 'h': round(h, 1)})
        notes[s['id']] = [{'name': d['name'], 'role': d.get('role') or '', 'reason': (d.get('reason') or '')[:400]}
                          for d in s.get('plantings', [])]
    # у растения — номер посадки из просмотра: в view.json плоский список plants с теми же номерами R…
    refs = {row[7]: row[6] for row in (view or {}).get('plants', []) if len(row) > 7}
    for row in plants:
        row.append(refs.get(row[4], -1))

    minx, miny, maxx, maxy = territory.bounds
    return {
        'v': 1,
        'origin': [ox, oy],
        'extent': [round(minx - ox, 1), round(miny - oy, 1), round(maxx - ox, 1), round(maxy - oy, 1)],
        'territory': _poly(territory, ox, oy),
        'surfaces': surfaces,
        'buildings': buildings,
        'existing': existing,
        'profiles': profiles.items,
        'ratio': config['ratio'],
        'plants': plants,
        'beds': beds,
        'lawns': lawns,
        'hedges': groups,
        'objects': objects,
        'points': points,
        'structures': structures,
        'plantings': notes,
    }


def context_from_dxf(geojson, sources, config=None):
    """Окружение для разметки, сделанной до появления слоя building: заново читает исходные DXF
    (десятки секунд — один раз на задачу, результат идёт в scene.json)."""
    from . import reader
    from .classify import Classifier

    classifier = Classifier()
    items = []
    for path, source in sources:
        items.extend(reader.read(path, source, classifier)[0])
    if len(sources) > 1:
        items = reader.deduplicate(items)[0]
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    territory = unary_union([g for g, p in features if p['layer'] == 'territory'])
    surface_map = {p['surface']: g for g, p in features if p['layer'] == 'surface'}
    return context(items, territory, surface_map, config)


def fresh(path):
    """Сцена новее настроек вида и кода сборки — иначе её пора пересобрать."""
    path = Path(path)
    return path.is_file() and path.stat().st_mtime >= max(CONFIG.stat().st_mtime, Path(__file__).stat().st_mtime)


def write(out_dir, sources=()):
    """Сцена по файлам результата задачи; None, если разметки нет. sources — [(путь DXF, 'plan'|'base')]
    для старой разметки без зданий окружения: прочитанное из DXF кэшируется в scene_context.json."""
    out_dir = Path(out_dir)
    if not (out_dir / 'greening.geojson').exists():
        return None
    read = lambda name: json.loads((out_dir / name).read_text(encoding='utf-8')) if (out_dir / name).exists() else None
    geojson = read('greening.geojson')
    if not any(f['properties']['layer'] == 'building' for f in geojson['features']):
        extra = read('scene_context.json')
        if extra is None and sources:
            ctx = context_from_dxf(geojson, sources)
            extra = [{'type': 'Feature', 'geometry': mapping(geometry),
                      'properties': {'layer': 'building', 'floors': floors, 'source': source}}
                     for geometry, floors, source in ctx['buildings']]
            extra += [{'type': 'Feature', 'geometry': mapping(point), 'properties': {'layer': 'tree_label', 'text': text}}
                      for point, text in ctx['tree_labels']]
            (out_dir / 'scene_context.json').write_text(json.dumps(extra, ensure_ascii=False), encoding='utf-8')
        geojson['features'].extend(extra or [])
    data = build(geojson, read('placement.geojson'), read('view.json'))
    path = out_dir / 'scene.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp.replace(path)
    return path


if __name__ == '__main__':
    import sys
    print(write(sys.argv[1]))
