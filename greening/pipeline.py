"""Разбор генплана: территория → поверхности → структуры → секции зелёных зон."""

import time
from collections import Counter
from dataclasses import dataclass, field

import shapely
from shapely import STRtree

from . import reader, sections, structures, surfaces, territory
from .classify import Classifier

# объекты дальше этого от территории не нужны: самый большой отступ — 10 м от школ
MARGIN = 15.0


@dataclass
class Result:
    territory: object
    territory_info: dict
    surfaces: dict
    structures: list
    trees: list
    tree_rows: object
    sections: list
    summary: dict
    layers: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)


def _layer_report(items, area):
    """Какие слои у территории распознаны и какие нет — чтобы пополнять config/layers.json."""
    shapely.prepare(area)
    kinds, unknown = Counter(), Counter()
    for item in items:
        if item.etype in ('TEXT', 'MTEXT') or not area.intersects(item.geometry):
            continue
        kinds[item.kind or 'unknown'] += 1
        if item.kind is None and not item.surface:
            unknown[item.layer] += 1
    return {'by_kind': dict(kinds.most_common()), 'unknown_layers': dict(unknown.most_common(40))}


def parse(plan, geobase=None, log=None):
    """Разбирает генплан (и геоподоснову, если есть) и возвращает Result."""
    log = log or (lambda message: None)
    timings, started = {}, time.monotonic()

    def lap(name):
        nonlocal started
        now = time.monotonic()
        timings[name] = round(now - started, 1)
        log(f'{name}: {timings[name]} с')
        started = now

    classifier = Classifier()
    items, plan_units = reader.read(plan, 'plan', classifier)
    units = {'plan': plan_units}
    removed = 0
    if geobase:
        base_items, units['geobase'] = reader.read(geobase, 'base', classifier)
        items, removed = reader.deduplicate(items + base_items)
    lap('чтение DXF')

    area, info = territory.resolve(items)
    info.update(duplicates_removed=removed, units=units)
    index = STRtree([i.geometry for i in items])
    local = [items[int(n)] for n in index.query(area.buffer(MARGIN))]
    del items, index
    lap('территория')

    surface_map, surface_sources = surfaces.build(local, area, classifier)
    lap('поверхности')

    found = structures.find(local, area, classifier)
    tree_points, tree_rows = structures.trees(local, area)
    lap('структуры')

    section_list, summary = sections.build(local, surface_map, found, (tree_points, tree_rows))
    lap('секции')

    summary.update(
        territory_m2=round(area.area, 1),
        surfaces_m2={k: round(g.area, 1) for k, g in surface_map.items()},
        surface_sources=surface_sources,
        structures=dict(Counter(s['type'] for s in found)),
        trees=len(tree_points),
        tree_rows_m2=round(tree_rows.area, 1),
        allowed_m2={
            '+'.join(key) or 'none': round(sum(s['area_m2'] for s in section_list if tuple(s['allowed']) == key), 1)
            for key in {tuple(s['allowed']) for s in section_list}
        },
    )
    return Result(area, info, surface_map, found, tree_points, tree_rows, section_list, summary,
                  _layer_report(local, area), timings)
