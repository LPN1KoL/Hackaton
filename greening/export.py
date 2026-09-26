"""Выгрузка результата: GeoJSON для следующих шагов и DXF со слоями для просмотра."""

import io
import json

import ezdxf
import ezdxf.recover
from ezdxf import units
import shapely
from ezdxf.enums import TextEntityAlignment
from ezdxf.lldxf import const
from shapely.geometry import mapping

# координаты — в системе чертежа, в метрах; сантиметров достаточно
PRECISION = 0.01

SURFACE_COLORS = {
    'building': 30, 'roadway': 8, 'sidewalk': 9, 'paving': 42, 'playground': 6, 'sealed': 252,
    'gravel': 40, 'flowerbed': 221, 'lawn': 3, 'ground': 33, 'unknown': 1,
}
# секции — почти сплошной заливкой поверх поверхностей, поверхности — бледнее, как подложка
SECTION_TRANSPARENCY = 0.1
SURFACE_TRANSPARENCY = 0.5
SECTION_LAYERS = {
    ('tree', 'shrub'): ('GRN_SEC_TREE_SHRUB', 94),
    ('tree',): ('GRN_SEC_TREE', 84),
    ('shrub',): ('GRN_SEC_SHRUB', 62),
    ('herbaceous',): ('GRN_SEC_HERBACEOUS', 51),
    (): ('GRN_SEC_NONE', 11),
}


def _section_layer(section):
    """Слой по самому крупному разрешённому типу: травянистые рядом с деревьями цвет не меняют."""
    allowed = tuple(t for t in ('tree', 'shrub') if t in section['allowed'])
    if not allowed and 'herbaceous' in section['allowed']:
        allowed = ('herbaceous',)
    return SECTION_LAYERS[allowed]


def to_geojson(result):
    features = []

    def add(geometry, properties):
        # контур, схлопнувшийся при округлении до сантиметра, не выпускаем
        rounded = shapely.set_precision(geometry, PRECISION)
        if not rounded.is_empty:
            features.append({'type': 'Feature', 'geometry': mapping(rounded), 'properties': properties})

    add(result.territory, {'layer': 'territory', **result.territory_info})
    for surface, geometry in result.surfaces.items():
        add(geometry, {'layer': 'surface', 'surface': surface, 'area_m2': round(geometry.area, 1)})
    for number, structure in enumerate(result.structures, 1):
        geometry = structure['geometry']
        add(geometry, {'layer': 'structure', 'id': f'T{number:04d}', 'type': structure['type'],
                       'label': structure['label'], 'method': structure['method'], 'area_m2': round(geometry.area, 1),
                       'outside': structure.get('outside', False)})
    for point in result.trees:
        add(point, {'layer': 'tree'})
    if not result.tree_rows.is_empty:
        add(result.tree_rows, {'layer': 'tree_row', 'area_m2': round(result.tree_rows.area, 1)})
    for section in result.sections:
        add(section['geometry'], {'layer': 'section', **{k: v for k, v in section.items() if k != 'geometry'}})
    return {
        'type': 'FeatureCollection',
        'metadata': {
            'coordinates': 'Система координат чертежа, метры',
            'summary': result.summary,
            'layers': result.layers,
            'timings_s': result.timings,
        },
        'features': features,
    }


def write_geojson(result, path):
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(to_geojson(result), stream, ensure_ascii=False)


def _polygons(geometry):
    if geometry.geom_type == 'Polygon':
        return [] if geometry.is_empty else [geometry]
    return [p for p in getattr(geometry, 'geoms', []) if p.geom_type == 'Polygon']


def _layer(doc, name, color):
    if name not in doc.layers:
        doc.layers.add(name, color=color)
    return name


def _area(msp, geometry, layer, transparency=0.5, outline=True):
    """Полигоны как штриховка (с контуром, если outline); дыры — внутренними контурами."""
    for polygon in _polygons(shapely.set_precision(geometry, PRECISION)):
        # у add_hatch свой параметр color (по умолчанию 7) перекрывает dxfattribs — передаём «по слою» им
        hatch = msp.add_hatch(color=const.BYLAYER, dxfattribs={'layer': layer})
        hatch.paths.add_polyline_path(list(polygon.exterior.coords)[:-1], is_closed=True,
                                      flags=const.BOUNDARY_PATH_EXTERNAL)
        for ring in polygon.interiors:
            hatch.paths.add_polyline_path(list(ring.coords)[:-1], is_closed=True)
        hatch.transparency = transparency
        if outline:
            msp.add_lwpolyline(list(polygon.exterior.coords)[:-1], close=True, dxfattribs={'layer': layer})


def add_layers(doc, result, labels=True, outlines=True):
    """Добавляет в документ слои GRN_*: территория, поверхности, структуры, деревья, секции.

    outlines=False — только заливки: в превью обводка толще узкой полосы газона и ложится на тротуар.
    Деревья и их ряды рисуются в пределах территории: запас за границей нужен только для расчёта.
    """
    msp = doc.modelspace()
    inside = result.territory
    shapely.prepare(inside)
    layer = _layer(doc, 'GRN_TERRITORY', 5)
    for polygon in _polygons(result.territory):
        for ring in (polygon.exterior, *polygon.interiors):
            msp.add_lwpolyline(list(ring.coords)[:-1], close=True, dxfattribs={'layer': layer, 'lineweight': 50})

    for surface, geometry in result.surfaces.items():
        _area(msp, geometry, _layer(doc, f'GRN_SURF_{surface.upper()}', SURFACE_COLORS.get(surface, 7)), SURFACE_TRANSPARENCY, outlines)

    for structure in result.structures:
        layer = _layer(doc, f"GRN_STRUCT_{structure['type'].upper()}", 6)
        geometry = structure['geometry']
        if geometry.geom_type == 'Point':
            msp.add_circle((geometry.x, geometry.y), 0.4, dxfattribs={'layer': layer})
        else:
            _area(msp, geometry, layer, 0.3, outlines)
        if labels:
            at = geometry.representative_point()
            msp.add_text(structure['type'], height=0.8, dxfattribs={'layer': layer}).set_placement(
                (at.x, at.y), align=TextEntityAlignment.MIDDLE_CENTER)

    layer = _layer(doc, 'GRN_TREES', 94)
    for point in result.trees:
        if inside.contains(point):
            msp.add_circle((point.x, point.y), 0.5, dxfattribs={'layer': layer})
    rows = result.tree_rows.intersection(inside)
    if not rows.is_empty:
        _area(msp, rows, _layer(doc, 'GRN_TREE_ROWS', 84), 0.5, outlines)

    ids = _layer(doc, 'GRN_SEC_ID', 7)
    for section in result.sections:
        name, color = _section_layer(section)
        _area(msp, section['geometry'], _layer(doc, name, color), SECTION_TRANSPARENCY, outlines)
        if labels and section['area_m2'] >= 20:
            at = section['geometry'].representative_point()
            msp.add_text(section['id'], height=0.6, dxfattribs={'layer': ids}).set_placement(
                (at.x, at.y), align=TextEntityAlignment.MIDDLE_CENTER)


def dxf_bytes(result):
    """DXF только со слоями разметки для превью: заливки без обводок."""
    doc = ezdxf.new('R2018', setup=True)
    doc.units = units.M
    add_layers(doc, result, outlines=False)
    stream = io.StringIO()
    doc.write(stream)
    return doc.encode(stream.getvalue())


def write_dxf(result, path, base=None):
    """Отдельный DXF только со слоями разметки или, если задан base, они же поверх копии чертежа."""
    if base:
        doc, _ = ezdxf.recover.readfile(base)
    else:
        doc = ezdxf.new('R2018', setup=True)
        doc.units = units.M
    add_layers(doc, result)
    doc.saveas(path)
