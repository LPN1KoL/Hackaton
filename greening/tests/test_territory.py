"""Территория (territory.resolve): копия «Границы работ» без подосновы (лист, схема) отбрасывается.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from types import SimpleNamespace

from shapely.geometry import LineString, Point

from greening import territory


def item(geometry, kind, etype='LWPOLYLINE'):
    return SimpleNamespace(kind=kind, layer='слой', etype=etype, geometry=geometry, source='plan', text='', block='',
                           surface='')


def square(x, y, size=100):
    return item(LineString([(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]), 'work_boundary')


def base(x, y, size=100, step=5):
    """Подоснова внутри квадрата: бортовые камни через step м."""
    return [item(LineString([(x + 1, y + k), (x + size - 1, y + k)]), 'kerb') for k in range(1, size, step)]


class Territory(unittest.TestCase):
    def test_copy_without_base_is_dropped(self):
        # Харьковская: та же граница в 4,4 км от участка, внутри только она сама
        items = [square(0, 0), square(4400, 0)] + base(0, 0) + [item(Point(4450, 50), 'label', 'TEXT')]
        area, info = territory.resolve(items)
        self.assertAlmostEqual(area.area, 100 * 100)
        self.assertLess(area.bounds[2], 200)
        self.assertEqual(len(info['dropped_parts']), 1)
        self.assertAlmostEqual(info['area_m2'], 10000.0)

    def test_parts_with_base_are_kept(self):
        # Грузинская, Багрицкого: несколько частей, у всех подоснова — территория целиком
        items = [square(0, 0), square(150, 0, 60)] + base(0, 0) + base(150, 0, 60)
        area, info = territory.resolve(items)
        self.assertAlmostEqual(area.area, 100 * 100 + 60 * 60)
        self.assertNotIn('dropped_parts', info)

    def test_single_part_unchanged(self):
        area, info = territory.resolve([square(0, 0)])
        self.assertAlmostEqual(area.area, 10000.0)
        self.assertNotIn('dropped_parts', info)


if __name__ == '__main__':
    unittest.main()
