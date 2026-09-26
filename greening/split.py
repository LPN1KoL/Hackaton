"""Нарезка разметки по структурам — подготовка к расстановке растений внутри структуры.

    python -m greening.split РАЗМЕТКА.greening.geojson [--plan ПЛАН.plan.json] [--margin 10] [--out ПАПКА]

На каждую структуру территории (двор, сквер, бульвар, полоса вдоль улицы — recommend.structures)
свой DXF и JSON. В DXF сама структура с дорожками, её газоны и зоны и окно вокруг неё: поверхности,
деревья, объекты и граница работ в пределах margin метров. Окна соседних структур перекрываются: одна дорожка попадает в несколько
файлов, чтобы у каждой структуры было её окружение. Координаты — исходные, чертежа: результат
расстановки ложится обратно в общий DXF без пересчёта.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import ezdxf
import shapely
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from shapely import STRtree
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import recommend
from .export import PRECISION, SECTION_LAYERS, SURFACE_COLORS, SURFACE_TRANSPARENCY, _area, _layer, _polygons

MARGIN = 10.0
# слои файла структуры; описание уходит в index.json, чтобы следующий шаг не угадывал
LAYERS = {
    'GRN_WINDOW': 'Граница окна: за ней чертёж обрезан, край окна — не объект',
    'GRN_TERRITORY': 'Граница работ',
    'GRN_SURF_*': 'Поверхности в окне (проезд, тротуар, здание, газон соседних участков…)',
    'GRN_OBJ_*': 'Объекты: урны, контейнерные площадки, детские площадки и т. п.',
    'GRN_TREES': 'Существующие деревья (сохраняются), круг r=0.5 м в точке ствола',
    'GRN_TREE_ROWS': 'Ряды существующих деревьев',
    'GRN_STRUCTURE': 'Контур структуры целиком, с дорожками и площадками',
    'GRN_PATCH': 'Контуры газонов структуры (u1…) — где идёт расстановка',
    'GRN_PATCH_ID': 'Номер газона (u1…) — как в JSON и в plan.json',
    'GRN_ZONE_*': 'Зоны структуры по разрешённым посадкам: TREE_SHRUB, TREE, SHRUB, HERBACEOUS, NONE',
    'GRN_ZONE_ID': 'Номер зоны (z1…) — как в JSON и в plan.json',
    'GRN_SEC_ID': 'Номер секции разметки (S…)',
}
ZONE_TEXT = 1.0
SECTION_TEXT = 0.5


class Context:
    """Всё, что рисуется вокруг структуры, с пространственными индексами."""

    def __init__(self, features):
        self.items = []
        for geometry, props in features:
            layer = props['layer']
            if layer == 'surface':
                name, color, fill = f"GRN_SURF_{props['surface'].upper()}", SURFACE_COLORS.get(props['surface'], 7), True
            elif layer == 'structure':
                name, color, fill = f"GRN_OBJ_{props['type'].upper()}", 6, True
            elif layer == 'tree':
                name, color, fill = 'GRN_TREES', 94, False
            elif layer == 'tree_row':
                name, color, fill = 'GRN_TREE_ROWS', 84, True
            elif layer == 'territory':
                name, color, fill, geometry = 'GRN_TERRITORY', 5, False, geometry.boundary
            else:
                continue
            for part in getattr(geometry, 'geoms', [geometry]):
                self.items.append((part, name, color, fill, props))
        self.index = STRtree([g for g, *_ in self.items])

    def near(self, window):
        return [self.items[i] for i in self.index.query(window, predicate='intersects')]


def _lines(msp, geometry, layer, **attribs):
    for line in getattr(geometry, 'geoms', [geometry]):
        if line.geom_type == 'LineString' and not line.is_empty:
            msp.add_lwpolyline(list(line.coords), dxfattribs={'layer': layer, **attribs})
        elif line.geom_type in ('Polygon', 'MultiPolygon', 'GeometryCollection'):
            _lines(msp, line.boundary if line.geom_type == 'Polygon' else line, layer, **attribs)


def _text(msp, text, at, height, layer):
    msp.add_text(text, height=height, dxfattribs={'layer': layer}).set_placement(
        (at.x, at.y), align=TextEntityAlignment.MIDDLE_CENTER)


def _zone_layer(zone):
    """Тот же выбор слоя, что у секций в export: по самому крупному разрешённому типу."""
    allowed = tuple(t for t in ('tree', 'shrub') if t in zone['allowed'])
    if not allowed and 'herbaceous' in zone['allowed']:
        allowed = ('herbaceous',)
    name, color = SECTION_LAYERS[allowed]
    return name.replace('GRN_SEC_', 'GRN_ZONE_'), color


def write_structure(structure, zone_shapes, sections, context, margin, path):
    """DXF одной структуры: окно с окружением, контур структуры, зоны и номера."""
    doc = ezdxf.new('R2018', setup=True)
    doc.units = units.M
    msp = doc.modelspace()
    window = structure['geometry'].buffer(margin, quad_segs=4)
    _lines(msp, window.boundary, _layer(doc, 'GRN_WINDOW', 8), linetype='DASHED')
    shapely.prepare(window)

    for geometry, name, color, fill, props in context.near(window):
        layer = _layer(doc, name, color)
        if geometry.geom_type == 'Point':
            radius = 0.5 if name == 'GRN_TREES' else 0.4
            msp.add_circle((geometry.x, geometry.y), radius, dxfattribs={'layer': layer})
            continue
        clipped = geometry if window.contains(geometry) else geometry.intersection(window)
        if clipped.is_empty:
            continue
        if fill and _polygons(clipped):
            _area(msp, clipped, layer, SURFACE_TRANSPARENCY if name.startswith('GRN_SURF_') else 0.3)
        else:
            _lines(msp, clipped, layer, **({'lineweight': 50} if name == 'GRN_TERRITORY' else {}))

    for polygon in _polygons(shapely.set_precision(structure['geometry'], PRECISION)):
        for ring in (polygon.exterior, *polygon.interiors):
            msp.add_lwpolyline(list(ring.coords)[:-1], close=True,
                               dxfattribs={'layer': _layer(doc, 'GRN_STRUCTURE', 2), 'lineweight': 35})
    patches, patch_ids = _layer(doc, 'GRN_PATCH', 3), _layer(doc, 'GRN_PATCH_ID', 3)
    for patch in structure['patches']:
        for polygon in _polygons(shapely.set_precision(patch['geometry'], PRECISION)):
            msp.add_lwpolyline(list(polygon.exterior.coords)[:-1], close=True, dxfattribs={'layer': patches})
            if polygon.area >= 20:
                _text(msp, patch['id'], polygon.representative_point(), ZONE_TEXT * 1.5, patch_ids)
    zone_ids, section_ids = _layer(doc, 'GRN_ZONE_ID', 7), _layer(doc, 'GRN_SEC_ID', 7)
    for zone in structure['zones']:
        name, color = _zone_layer(zone)
        geometry = zone_shapes[zone['id']]
        _area(msp, geometry, _layer(doc, name, color), 0.1, outline=False)
        for part in _polygons(geometry):
            if part.area >= 1:
                _text(msp, zone['id'], part.representative_point(), ZONE_TEXT, zone_ids)
        for section_id in zone['sections']:
            section = sections[section_id]
            if section.area >= 5:
                _text(msp, section_id, section.representative_point(), SECTION_TEXT, section_ids)
    doc.saveas(path)
    return window


def _record(structure, zone_shapes, window, plan):
    """JSON структуры: всё из recommend (место, газоны, зоны, нормы) + геометрия + ответ плана, если есть."""
    record = recommend.record_of(structure)
    record['territory'] = structure['territory']
    planned = {z['id']: z for z in plan.get('zones', [])} if plan else {}
    for zone in record['zones']:
        if zone['id'] in planned and 'explanation' in planned[zone['id']]:
            zone['explanation'] = planned[zone['id']]['explanation']
        zone['geometry'] = mapping(shapely.set_precision(zone_shapes[zone['id']], PRECISION))
    for item, patch in zip(record['patches'], structure['patches']):
        item['geometry'] = mapping(shapely.set_precision(patch['geometry'], PRECISION))
    record.update(window=list(window.bounds),
                  geometry=mapping(shapely.set_precision(structure['geometry'], PRECISION)))
    if plan:
        record['plan'] = {k: plan[k] for k in ('status', 'plantings', 'justification', 'rejected') if k in plan}
    return record


def split(geojson, out, plan=None, margin=MARGIN, log=print):
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    found = recommend.structures(geojson)
    context = Context(features)
    planned = {}
    if plan:
        planned = {s['id']: s for s in plan['structures']}
        # номера структур детерминированы разметкой; план от другой разметки не подмешиваем
        stale = [s['id'] for s in found if s['id'] in planned and abs(planned[s['id']]['area_m2'] - s['area_m2']) > 0.2]
        if stale or len(planned) != len(found):
            raise ValueError(f'План сделан по другой разметке (не совпали: {", ".join(stale[:5]) or "число структур"})')
    out.mkdir(parents=True, exist_ok=True)
    index = []
    for structure in found:
        zone_shapes = {z['id']: unary_union([sections[s] for s in z['sections']]) for z in structure['zones']}
        window = write_structure(structure, zone_shapes, sections, context, margin, out / f"{structure['id']}.dxf")
        record = _record(structure, zone_shapes, window, planned.get(structure['id']))
        (out / f"{structure['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding='utf-8')
        index.append({k: record[k] for k in ('id', 'type', 'name', 'place', 'area_m2', 'total_m2', 'window')}
                     | {'patches': len(record['patches']), 'zones': len(record['zones']),
                        'status': record.get('plan', {}).get('status')})
    summary = {'structures': len(index), 'margin_m': margin, 'layers': LAYERS,
               'coordinates': 'Система координат чертежа, метры', 'structures_list': index}
    (out / 'index.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return summary


def main():
    parser = argparse.ArgumentParser(description='DXF и JSON на каждую структуру территории')
    parser.add_argument('geojson', help='результат python -m greening (*.greening.geojson)')
    parser.add_argument('--plan', help='результат python -m greening.recommend (*.plan.json), необязательно')
    parser.add_argument('--margin', type=float, default=MARGIN, help=f'окно вокруг структуры, м (по умолчанию {MARGIN:g})')
    parser.add_argument('--out', help='папка (по умолчанию <имя>.structures рядом с разметкой)')
    args = parser.parse_args()

    source = Path(args.geojson)
    stem = source.name.replace('.greening.geojson', '')
    out = Path(args.out) if args.out else source.with_name(f'{stem}.structures')
    geojson = json.loads(source.read_text(encoding='utf-8'))
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8')) if args.plan else None
    summary = split(geojson, out, plan, args.margin)
    print(json.dumps({'out': str(out), 'structures': summary['structures'], 'margin_m': args.margin}, ensure_ascii=False))


if __name__ == '__main__':
    main()
