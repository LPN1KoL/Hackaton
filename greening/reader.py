"""Чтение DXF: все объекты модели с раскрытием блоков, в метрах и с классом слоя."""

import math
from dataclasses import dataclass

import ezdxf
import ezdxf.recover
import numpy as np
import shapely
from ezdxf import units
from ezdxf.path import from_hatch, make_path
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon

from .classify import Classifier, base_name

# точность аппроксимации дуг и сплайнов, м
FLATTEN = 0.05
# объекты, совпадающие с такой точностью, — один объект (генплан обычно содержит геоподоснову)
DUPLICATE_PRECISION = 0.01
# правдоподобный размер генплана, м: меньше — ошибка единиц, больше — тоже
PLAUSIBLE_SIZE = (10.0, 100_000.0)

CURVES = ('LWPOLYLINE', 'POLYLINE', 'ARC', 'CIRCLE', 'ELLIPSE', 'SPLINE')
# штриховки нужны только там, где они означают площадь, а не условный знак
HATCH_KINDS = {'lawn_edge', 'flowerbed', 'building_wall', 'site_outline', 'building_school_kindergarten'}


@dataclass(slots=True)
class Item:
    kind: str | None      # класс слоя (config/layers.json); None — слой не распознан
    layer: str            # имя слоя без префикса листа
    etype: str
    geometry: object      # shapely; для TEXT и INSERT — точка вставки
    source: str           # 'plan' или 'base'
    text: str = ''
    block: str = ''
    surface: str = ''     # проектное покрытие для штриховок «_АБ ТР-ГЗН»


class ReadError(Exception):
    pass


def _scale(doc):
    try:
        factor = units.conversion_factor(doc.units, units.M) if doc.units else 1.0
    except (ValueError, TypeError, ZeroDivisionError):
        factor = 1.0
    return factor if math.isfinite(factor) and factor > 0 else 1.0


def _units(header_factor, items):
    """(множитель до метров, откуда он). Заголовку $INSUNITS верим, только если размер чертежа правдоподобен.

    Бывает, что в заголовке записаны миллиметры, а координаты — метры местной системы: тогда участок
    сжимается до метра. Размер считается по 1–99 перцентилям, чтобы легенда в стороне не мешала.
    """
    shapes = [i.geometry for i in items if not i.geometry.is_empty]
    if not shapes:
        return header_factor, 'header'
    bounds = shapely.bounds(shapes)
    size = max(np.percentile(bounds[:, 2], 99) - np.percentile(bounds[:, 0], 1),
               np.percentile(bounds[:, 3], 99) - np.percentile(bounds[:, 1], 1))
    if size <= 0 or PLAUSIBLE_SIZE[0] <= size * header_factor <= PLAUSIBLE_SIZE[1]:
        return header_factor, 'header'
    for factor, name in ((1.0, 'm'), (0.001, 'mm'), (0.01, 'cm')):
        if PLAUSIBLE_SIZE[0] <= size * factor <= PLAUSIBLE_SIZE[1]:
            return factor, f'guessed_{name}'
    return header_factor, 'header_implausible'


def _curve(entity):
    path = make_path(entity)
    points = [(p.x, p.y) for p in path.flattening(FLATTEN)]
    if path.is_closed and points and points[-1] != points[0]:
        points.append(points[0])
    if len(set(points)) < 2:
        return Point(points[0]) if points else None
    return LineString(points)


def _hatch(entity):
    area = Polygon()
    for path in from_hatch(entity):
        points = [(v.x, v.y) for v in path.flattening(FLATTEN)]
        if len(set(points)) >= 3:
            area = area.symmetric_difference(shapely.make_valid(Polygon(points)))
    return area if not area.is_empty else None


def _text(entity):
    if entity.dxftype() == 'MTEXT':
        return entity.plain_text().strip(), entity.dxf.insert
    return entity.dxf.text.strip(), entity.dxf.insert


def _entities(entities, inherited='0', stack=()):
    """(entity, слой) с раскрытием блоков; объекты блока на слое 0 берут слой вставки."""
    for entity in entities:
        layer = entity.dxf.layer
        if layer == '0':
            layer = inherited
        yield entity, layer
        if entity.dxftype() != 'INSERT':
            continue
        name = entity.dxf.name.casefold()
        if name in stack or entity.block() is None:
            continue
        inserts = entity.multi_insert() if entity.mcount > 1 else (entity,)
        for insert in inserts:
            try:
                children = list(insert.virtual_entities())
            except Exception:
                continue
            yield from _entities(children, layer, stack + (name,))


def read(path, source, classifier=None):
    """(объекты модели файла в виде Item, сведения о единицах)."""
    classifier = classifier or Classifier()
    try:
        doc, _ = ezdxf.recover.readfile(path)
    except Exception as exc:
        raise ReadError(f'Не удалось прочитать {path}: {exc}') from exc
    factor = _scale(doc)

    items = []
    for entity, layer in _entities(doc.modelspace()):
        etype = entity.dxftype()
        kind = classifier.kind(layer)
        if kind == 'ignore':
            continue
        name = base_name(layer)
        try:
            if etype == 'LINE':
                start, end = entity.dxf.start, entity.dxf.end
                geometry = LineString([(start.x, start.y), (end.x, end.y)]) if not start.isclose(end) else None
            elif etype in CURVES:
                geometry = _curve(entity)
            elif etype == 'POINT':
                geometry = Point(entity.dxf.location.x, entity.dxf.location.y)
            elif etype in ('TEXT', 'MTEXT'):
                text, at = _text(entity)
                if text:
                    items.append(Item(kind, name, etype, Point(at.x, at.y), source, text=text))
                continue
            elif etype == 'INSERT':
                at = entity.dxf.insert
                block = base_name(entity.dxf.name)
                block_kind = classifier.kind(block)
                if block_kind not in (None, 'ignore'):
                    items.append(Item(block_kind, name, etype, Point(at.x, at.y), source, block=block))
                continue
            elif etype == 'HATCH':
                surface = classifier.hatch_surface(layer)
                if surface is None and kind not in HATCH_KINDS:
                    continue
                geometry = _hatch(entity)
                if geometry is not None:
                    items.append(Item(kind, name, etype, geometry, source, surface=surface or ''))
                continue
            else:
                continue
        except Exception:
            # битые сущности пропускаем: одна кривая сплайн-линия не должна ронять разбор
            continue
        if geometry is not None:
            items.append(Item(kind, name, etype, geometry, source))

    factor, basis = _units(factor, items)
    if factor != 1.0:
        scaled = affinity.scale
        for item in items:
            item.geometry = scaled(item.geometry, factor, factor, origin=(0, 0))
    return items, {'insunits': doc.header.get('$INSUNITS', 0), 'scale_to_m': factor, 'basis': basis}


def deduplicate(items):
    """Убирает объекты с тем же слоем, типом и геометрией (до 1 см); первым остаётся объект плана."""
    if not items:
        return items, 0
    keys = shapely.normalize(shapely.set_precision([i.geometry for i in items], DUPLICATE_PRECISION))
    seen, unique = set(), []
    for item, geometry in zip(items, keys):
        key = (item.layer, item.etype, item.text, item.block, geometry.wkb)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique, len(items) - len(unique)
