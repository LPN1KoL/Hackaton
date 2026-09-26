"""Перенос рассадки из GeoJSON на чертёж DXF."""

import io

import ezdxf.recover

# всё, что добавляет сервис, — на одном слое поверх чертежа;
# деревья и кустарники различаются цветом и размером кружка
LAYER = 'Посадки'
# координаты GeoJSON — в системе чертежа, в метрах
KINDS = {
    'tree': {'color': 3, 'radius': 2.5},
    'shrub': {'color': 92, 'radius': 0.75},
}


class PlantingError(Exception):
    pass


def apply_planting(stream, planting):
    """Возвращает байты DXF: исходный чертёж плюс кружки посадок на слое LAYER."""
    try:
        doc, _ = ezdxf.recover.read(stream)
    except Exception as exc:
        raise PlantingError('Не удалось разобрать файл: %s' % exc) from exc

    if LAYER not in doc.layers:
        doc.layers.add(LAYER, color=3)

    msp = doc.modelspace()
    for feature in planting.get('features') or []:
        geometry = feature.get('geometry') or {}
        if geometry.get('type') != 'Point':
            continue
        kind = KINDS.get((feature.get('properties') or {}).get('plant_type'), KINDS['shrub'])
        x, y = geometry['coordinates'][:2]
        msp.add_circle((x, y), kind['radius'], dxfattribs={'layer': LAYER, 'color': kind['color']})

    out = io.StringIO()
    doc.write(out)
    return out.getvalue().encode(doc.output_encoding)
