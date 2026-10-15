"""Существующие насаждения: условные стволы в полосах деревьев (structures.row_trunks) и сохранение
существующих цветников (sections.build — деревья и кусты на них не сажаются).

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from types import SimpleNamespace

from shapely.geometry import LineString, Point, Polygon, box

from greening import sections, structures


def dash(x, y, layer='Полоса деревьев'):
    return SimpleNamespace(kind='tree', layer=layer, etype='LINE', geometry=LineString([(x - 0.2, y), (x + 0.2, y)]))


class RowTrunks(unittest.TestCase):
    def test_spacing_along_strip(self):
        # полоса из штрихов через 1 м на 40 м — стволы через ~5 м
        items = [dash(x, 0) for x in range(0, 41)]
        trunks = structures.row_trunks(items, box(-5, -5, 45, 5))
        self.assertGreaterEqual(len(trunks), 7)
        self.assertLessEqual(len(trunks), 10)
        for i, a in enumerate(trunks):
            for b in trunks[i + 1:]:
                self.assertGreaterEqual(a.distance(b), structures.TREE_ROW_STEP * 0.9 - 1e-9)

    def test_skips_existing_and_other_layers(self):
        items = [dash(x, 0) for x in range(0, 11)] + [dash(30, 0, layer='Отдельно стоящее дерево')]
        trunks = structures.row_trunks(items, box(-5, -5, 45, 5), existing=[Point(0, 0)])
        self.assertTrue(all(t.distance(Point(0, 0)) >= structures.TREE_ROW_STEP * 0.9 for t in trunks))
        self.assertTrue(all(t.x < 20 for t in trunks))


class ExistingFlowerbed(unittest.TestCase):
    def test_no_trees_or_shrubs_on_flowerbed(self):
        surfaces = {'lawn': box(0, 0, 20, 20), 'flowerbed': box(20, 0, 30, 20)}
        result, _ = sections.build([], surfaces, [], ([], Polygon()))
        on_bed = [s for s in result if s['surface'] == 'flowerbed']
        self.assertTrue(on_bed)
        for s in on_bed:
            self.assertEqual(s['allowed'], ['herbaceous'])
            self.assertIn('EXISTING-FLOWERBED', {r['rule'] for r in s['rules']})
        lawn = [s for s in result if s['surface'] == 'lawn']
        self.assertTrue(any('tree' in s['allowed'] for s in lawn))


if __name__ == '__main__':
    unittest.main()
