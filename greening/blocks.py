"""Структуры территории: двор, сквер, бульвар, полоса вдоль улицы — целиком, вместе с дорожками.

Готовых границ таких структур в чертежах нет (проверено на обоих участках: «Объекты озеленения» пустой,
«Территории», «Граница улицы» — разрозненные линии, «Красных линий» у территории нет). Поэтому они
строятся из поверхностей: территория без проезжей части и зданий, разрезанная по узким перешейкам.

Перешеек — место уже NECK: тротуар, пересекающий въезд, дорожка между двумя дворами. Дорожки внутри
сквера окружены газоном и перешейками не считаются: разрез делает морфологическое «открытие»
(сжатие и расширение на NECK / 2), а сквер вместе с дорожками при этом остаётся одним куском.
Отрезанные перешейки прилипают к ядру, с которым у них самая длинная общая граница; куски,
не касающиеся ни одного ядра (узкие полосы вдоль проездов), — отдельные структуры.
"""

import shapely
from shapely import STRtree
from shapely.ops import unary_union

# перешеек уже этого разделяет структуры, м
NECK = 6.0
# ядро меньше этого — не структура, а часть перешейка, м²
MIN_CORE = 50.0
# кусок меньше этого — шум обрезки, м²
MIN_PIECE = 0.01
# граница ближе этого — общая, м
TOUCH = 0.01
# сегментов на четверть окружности у буферов открытия
QUAD_SEGS = 2
CUT_SURFACES = ('roadway', 'building')


def _polygons(geometry):
    return [p for p in getattr(geometry, 'geoms', [geometry]) if p.geom_type == 'Polygon' and not p.is_empty]


def build(territory, surfaces):
    """[{'geometry', 'core': bool}] — структуры территории, крупные сначала."""
    cut = unary_union([surfaces[s] for s in CUT_SURFACES if s in surfaces])
    rest = territory.difference(cut) if not cut.is_empty else territory
    opened = rest.buffer(-NECK / 2, quad_segs=QUAD_SEGS).buffer(NECK / 2, quad_segs=QUAD_SEGS).intersection(rest)
    cores = [p for p in _polygons(opened) if p.area >= MIN_CORE]
    pieces = [p for p in _polygons(rest.difference(unary_union(cores))) if p.area >= MIN_PIECE] if cores else _polygons(rest)

    groups = [[core] for core in cores]
    free = []
    index = STRtree(cores) if cores else None
    for piece in pieces:
        near = index.query(piece, predicate='dwithin', distance=TOUCH) if index is not None else []
        if not len(near):
            free.append(piece)
            continue
        band = piece.boundary
        best = max(near, key=lambda i: band.intersection(cores[int(i)].buffer(TOUCH)).length)
        groups[int(best)].append(piece)
    blocks = [{'geometry': unary_union(group), 'core': True} for group in groups]
    # свободные куски, касающиеся друг друга, — одна полоса
    if free:
        merged = unary_union([p.buffer(TOUCH) for p in free])
        for part in _polygons(merged):
            blocks.append({'geometry': unary_union([p for p in free if p.intersects(part)]), 'core': False})
    for block in blocks:
        shapely.prepare(block['geometry'])
    blocks.sort(key=lambda b: -b['geometry'].area)
    return blocks


def assign(blocks, geometries):
    """Номер структуры для каждой геометрии — по наибольшему перекрытию; -1, если не перекрывает ни одну."""
    index = STRtree([b['geometry'] for b in blocks])
    owners = []
    for geometry in geometries:
        hits = index.query(geometry, predicate='intersects')
        owners.append(int(max(hits, key=lambda i: blocks[int(i)]['geometry'].intersection(geometry).area))
                      if len(hits) else -1)
    return owners
