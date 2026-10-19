"""Поверхности и секции: широкий асфальт — проезжая часть, на неизвестной поверхности посадки не назначаются,
у края асфальта — отступ тротуара (пилотные улицы, 19.10).

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from unittest import mock
from collections import Counter

from shapely.geometry import box

from greening import sections, surfaces
from greening.classify import Classifier


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
