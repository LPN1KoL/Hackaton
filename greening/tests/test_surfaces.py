"""Поверхности и секции: широкий асфальт — проезжая часть, на неизвестной поверхности посадки не назначаются,
у края асфальта — отступ тротуара (пилотные улицы, 19.10).

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from unittest import mock
from collections import Counter

from shapely.geometry import LineString, box

from greening import pipeline, sections, surfaces
from greening.classify import Classifier
from greening.reader import Item


class WideSealed(unittest.TestCase):
    def test_wide_asphalt_becomes_roadway(self):
        # проезд 8 м и дорожка 2 м, обе подписаны «А»
        result = {'sealed': box(0, 0, 100, 8).union(box(40, 8, 42, 30)), 'lawn': box(0, 8, 40, 30)}
        surfaces._wide_sealed_as_roadway(result, Classifier().sealed_road_width, Counter())
        self.assertAlmostEqual(result['roadway'].area, 800, delta=1)
        self.assertAlmostEqual(result['sealed'].area, 44, delta=1)

    def test_narrow_asphalt_stays(self):
        result = {'sealed': box(0, 0, 100, 3)}
        surfaces._wide_sealed_as_roadway(result, 6.0, Counter())
        self.assertNotIn('roadway', result)
        self.assertAlmostEqual(result['sealed'].area, 300)


class OghExport(unittest.TestCase):
    """Выгрузка паспорта ОГХ (236285, 236287 — файлы проверки организаторов, 20.10): граница — BOUND,
    газоны — замкнутые контуры LAWN без штриховок и подписей."""

    def test_layer_classes(self):
        c = Classifier()
        self.assertEqual(c.kind('BOUND'), 'work_boundary')
        self.assertEqual(c.kind('ogh_BOUND'), 'ignore')
        self.assertEqual(c.kind('PLANT_NUM_TXT'), 'ignore')
        self.assertEqual(c.kind('TREE'), 'tree')
        self.assertEqual(c.kind('08TREE'), 'tree')
        self.assertEqual(c.kind('DOM'), 'building_wall')
        self.assertEqual(c.kind('Столб ограждения'), 'fence')
        self.assertEqual(c.hatch_surface('2.Асфальт'), 'sealed')

    def test_lawn_outline_is_lawn(self):
        territory = box(0, 0, 50, 50)
        lawn = Item('lawn_edge', 'LAWN', 'LWPOLYLINE', LineString(box(10, 10, 30, 30).exterior.coords), 'plan')
        result, sources = surfaces.build([lawn], territory, Classifier())
        self.assertAlmostEqual(result['lawn'].area, 400, delta=0.1)
        self.assertEqual(sources['outline'], 1)

    def test_open_line_is_only_an_edge(self):
        territory = box(0, 0, 50, 50)
        line = Item('lawn_edge', 'LAWN', 'LWPOLYLINE', LineString([(10, 10), (30, 10), (30, 30)]), 'plan')
        result, _ = surfaces.build([line], territory, Classifier())
        self.assertNotIn('lawn', result)


class Demolition(unittest.TestCase):
    """СПОЗУ (2026-07-21 - СПОЗУ, 20.10): дом под снос в подоснове ещё стоит — его стены и крыльца не учитываются,
    стена нового дома, пересекающая контур сноса, остаётся."""

    def test_layer_classes(self):
        c = Classifier()
        self.assertEqual(c.kind('_ГП_Сносимые здания'), 'demolition_area')
        self.assertEqual(c.kind('_ГП_Экспликация сущ зданий для 2000'), 'ignore')
        self.assertEqual(c.kind('_ГП_Граница ГПЗУ'), 'order_boundary')

    def test_drop_demolished(self):
        outline = Item('demolition_area', 'Сносимые здания', 'LWPOLYLINE', LineString(box(0, 0, 10, 40).exterior.coords), 'plan')
        old_wall = Item('building_wall', 'Части зданий', 'LINE', LineString([(0, 5), (10, 5)]), 'base')
        porch = Item('building_wall', 'Крыльца', 'LINE', LineString([(10, 10), (12, 10)]), 'base')
        new_wall = Item('building_wall', 'Здание', 'LWPOLYLINE', LineString([(5, 20), (30, 20)]), 'plan')
        cable = Item('power_comm_cable', 'Кабель', 'LINE', LineString([(1, 1), (9, 1)]), 'base')
        kept, removed = pipeline._drop_demolished([outline, old_wall, porch, new_wall, cable])
        self.assertEqual(removed, 2)
        self.assertEqual([i.layer for i in kept], ['Сносимые здания', 'Здание', 'Кабель'])


class Sections(unittest.TestCase):
    def test_unknown_surface_has_no_plantings(self):
        found, _ = sections.build([], {'unknown': box(0, 0, 30, 30), 'lawn': box(30, 0, 60, 30)}, [], ([], box(0, 0, 0, 0)))
        by_surface = {s['surface']: s['allowed'] for s in found}
        self.assertEqual(by_surface['unknown'], [])
        self.assertIn('tree', by_surface['lawn'])

    def test_join_keeps_holes(self):
        # GEOS вернул объединение без дыры хозяина (Измайловская) — такой результат не принимается
        host = box(0, 0, 20, 20).difference(box(5, 5, 10, 10))
        piece = box(20, 0, 20.01, 1)
        real = sections.shapely.union
        calls = []

        def faulty(a, b, grid_size=None):
            calls.append(grid_size)
            return real(box(0, 0, 20, 20), b) if grid_size is None else real(a, b, grid_size=grid_size)

        with mock.patch.object(sections.shapely, 'union', faulty):
            joined = sections._join(host, piece)
        self.assertEqual(calls, [None, sections.GRID])
        self.assertAlmostEqual(joined.area, host.area + piece.area, places=2)

    def test_sealed_edge_keeps_walk_distance(self):
        found, _ = sections.build([], {'sealed': box(0, 0, 10, 3), 'lawn': box(0, 3, 10, 20)}, [], ([], box(0, 0, 0, 0)))
        trees = [s['geometry'] for s in found if 'tree' in s['allowed']]
        self.assertTrue(trees)
        self.assertGreaterEqual(min(g.bounds[1] for g in trees), 3 + 0.7 - 0.01)


if __name__ == '__main__':
    unittest.main()
