"""Служебные слои результата в DXF: зоны допустимости, номера зон с причинами запретов, нарушения отступов.

Всё на отдельных слоях Greening_*: исходные слои не трогаются (п. 5 ТЗ — «зоны допустимости, служебные
пометки… размещаются только на отдельном слое»). Номер зоны в DXF — тот же, что в plan.json и обосновании.
"""

from collections import defaultdict

from ezdxf.enums import MTextEntityAlignment
from shapely.geometry import shape
from shapely.ops import unary_union

from . import justify
from .export import _area, _layer

KIND_NAMES = {'tree': 'деревья', 'shrub': 'кустарники', 'herbaceous': 'травянистые'}
# зоны допустимости по тому, что разрешено нормами; цвета — как у секций разметки
ZONE_LAYERS = {
    ('tree', 'shrub'): ('Greening_Зоны_Деревья_Кустарники', 94),
    ('tree',): ('Greening_Зоны_Деревья', 84),
    ('shrub',): ('Greening_Зоны_Кустарники', 62),
    ('herbaceous',): ('Greening_Зоны_Травянистые', 51),
    (): ('Greening_Зоны_Запрет', 11),
}
LABELS = ('Greening_Зоны_Номера', 7)
VIOLATIONS = ('Greening_Нарушения', 1)
# посадка в охранной зоне сети — по согласованию с её владельцем
APPROVALS = ('Greening_Согласование', 30)
APPROVAL_HEIGHT = 0.3
# заливка зон почти прозрачная: под ней должны читаться подоснова и рассадка
ZONE_TRANSPARENCY = 0.75
LABEL_HEIGHT = 0.5
# ширина подписи зоны, м: длинные строки переносятся
LABEL_WIDTH = 12.0
# в подписи зоны — не больше стольких норм на тип посадки (остальные — в обосновании)
LABEL_RULES = 3


def _zone_key(allowed):
    key = tuple(t for t in ('tree', 'shrub') if t in allowed)
    return key or (('herbaceous',) if 'herbaceous' in allowed else ())


def _label(structure_id, zone):
    """«P0001 z5 / можно: … / деревья нельзя — 743-ПП, табл. 3.6.1: теплосеть 2 м, газопровод 1,5 м»."""
    lines = [f"{structure_id} {zone['id']}"]
    allowed = [KIND_NAMES[k] for k in ('tree', 'shrub', 'herbaceous') if k in zone['allowed']]
    lines.append('можно: ' + (', '.join(allowed) if allowed else 'ничего'))
    for kind, rules in (zone.get('blocked_by') or {}).items():
        if not rules:
            continue
        top = sorted(rules, key=lambda r: -r.get('share', 0))[:LABEL_RULES]
        by_doc = defaultdict(list)
        for r in top:
            by_doc[justify.cite(r['object'], r['doc'])].append(f"{r['object'].lower()} {r['distance_m']:g} м")
        text = '; '.join(f"{doc}: {', '.join(objects)}" for doc, objects in by_doc.items())
        more = len(rules) - len(top)
        lines.append(f"{KIND_NAMES.get(kind, kind)} нельзя — {text}" + (f' (+{more})' if more > 0 else ''))
    return '\\P'.join(lines)


def add_layers(doc, geojson, plan=None, check=None, fills=True, labels=True):
    """Зоны допустимости (заливки по секциям), подписи зон плана, нарушения из проверки отступов."""
    msp = doc.modelspace()
    sections = {}
    by_key = defaultdict(list)
    for feature in geojson['features']:
        props = feature['properties']
        if props['layer'] != 'section':
            continue
        geometry = shape(feature['geometry'])
        sections[props['id']] = geometry
        by_key[_zone_key(props['allowed'])].append(geometry)

    if fills:
        for key, parts in by_key.items():
            name, color = ZONE_LAYERS[key]
            _area(msp, unary_union(parts), _layer(doc, name, color), ZONE_TRANSPARENCY, outline=False)

    if labels and plan:
        layer = _layer(doc, *LABELS)
        for structure in plan['structures']:
            for zone in structure['zones']:
                parts = [sections[s] for s in zone.get('sections', []) if s in sections]
                if not parts:
                    continue
                at = unary_union(parts).representative_point()
                attribs = {'layer': layer, 'char_height': LABEL_HEIGHT, 'width': LABEL_WIDTH}
                text = msp.add_mtext(_label(structure['id'], zone), dxfattribs=attribs)
                text.set_location((at.x, at.y), attachment_point=MTextEntityAlignment.MIDDLE_CENTER)

    if check and check.get('approvals'):
        layer = _layer(doc, *APPROVALS)
        for a in check['approvals']:
            msp.add_circle((a['x'], a['y']), 0.4, dxfattribs={'layer': layer})
            text = msp.add_mtext(f"{a['id']}: согласование — {a['object'].lower()} ({a['distance_m']:g} м)",
                                 dxfattribs={'layer': layer, 'char_height': APPROVAL_HEIGHT})
            text.set_location((a['x'] + 0.45, a['y']), attachment_point=MTextEntityAlignment.MIDDLE_LEFT)

    if check and check.get('violations'):
        layer = _layer(doc, *VIOLATIONS)
        for v in check['violations']:
            msp.add_circle((v['x'], v['y']), max(v['norm_m'], 0.5), dxfattribs={'layer': layer})
            text = msp.add_mtext(f"{v['id']}: {v['object'].lower()} {v['distance_m']:g} м < {v['norm_m']:g} м\\P{v['doc']}",
                                 dxfattribs={'layer': layer, 'char_height': LABEL_HEIGHT})
            text.set_location((v['x'], v['y']), attachment_point=MTextEntityAlignment.BOTTOM_LEFT)
