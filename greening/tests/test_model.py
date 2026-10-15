"""Модель рассадки (model.py) и режим «модель» в place.py: выбор только внутри разрешённой зоны.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from unittest import mock

import numpy as np
from shapely.geometry import Point, box

from greening import model, place


class Greedy:
    """Заглушка модели: чем правее точка, тем выше оценка — тянет растения за правый край зоны."""

    def __init__(self):
        self.calls = 0

    def __call__(self, kind, site, points, field):
        self.calls += 1
        return np.array([min(0.99, max(0.01, x / 100)) for x, _ in points])


class PlacerWithModel(unittest.TestCase):
    def setUp(self):
        self.config = place.load_config()
        self.config['model_temperature'] = 0.0
        # газон 0…100 м, а деревья разрешены только в левой половине
        self.lawn = box(0, 0, 100, 20)
        self.zone = box(0, 0, 50, 20)

    def placer(self, scorer):
        p = place.Placer(self.config, [], np.random.default_rng(0), None, scorer)
        p.site = model.Site(self.lawn, {})
        return p

    def test_stays_in_zone(self):
        scorer = Greedy()
        placed = self.placer(scorer).solitary(self.zone, 10, 'tree', 4.0, 'landscape')
        self.assertTrue(placed)
        self.assertGreater(scorer.calls, 0)
        for x, y, _ in placed:
            self.assertTrue(self.zone.covers(Point(x, y)), (x, y))
        # модель тянет вправо — первое дерево у правого края разрешённой зоны, а не за ним
        self.assertGreater(placed[0][0], 40)

    def test_spacing_kept(self):
        placed = self.placer(Greedy()).solitary(self.zone, 10, 'tree', 4.0, 'landscape')
        need = self.config['tree_unit_spacing_m']
        for i, (x, y, _) in enumerate(placed):
            for ox, oy, _ in placed[i + 1:]:
                self.assertGreaterEqual(np.hypot(x - ox, y - oy), need - 1e-6)

    def test_group_scored_by_all_members(self):
        # куртина из 5 кустов: модель тянет вправо — вся группа у правого края зоны и целиком внутри неё
        scorer = Greedy()
        placed = self.placer(scorer).groups(self.zone, 5, 'shrub', 1.5, 'landscape', {})
        self.assertEqual(len(placed), 5)
        for x, y, _ in placed:
            self.assertTrue(self.zone.covers(Point(x, y)), (x, y))
        self.assertGreater(np.mean([x for x, _, _ in placed]), 40)

    def test_threshold(self):
        self.config['model_min_score']['tree'] = 0.95
        self.assertEqual(self.placer(Greedy()).solitary(self.zone, 5, 'tree', 4.0, 'landscape'), [])


class Fallback(unittest.TestCase):
    def test_no_model(self):
        logs = []
        with mock.patch.object(model, 'load', return_value=None):
            result = place.place({'features': []}, {'structures': []}, mode='model', log=logs.append)
        self.assertEqual(result['metadata']['mode'], 'algorithm')
        self.assertEqual(result['metadata']['requested_mode'], 'model')
        self.assertIn('mode_note', result['metadata'])
        self.assertTrue(logs)

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            place.place({'features': []}, {'structures': []}, mode='magic')


class Features(unittest.TestCase):
    def test_shape_and_finite(self):
        site = model.Site(box(0, 0, 30, 10), {'sidewalk': box(0, 10, 30, 12), 'roadway': box(0, 12, 30, 20)},
                          trees=[(5, 5)])
        X = site.features([5, 15, 25], [5, 5, 5])
        self.assertEqual(X.shape, (3, len(model.FEATURES)))
        self.assertTrue(np.isfinite(X).all())
        # сама точка-дерево соседом не считается
        tree = X[0, model.FEATURES.index('tree')]
        self.assertAlmostEqual(tree, np.log1p(model.NEIGHBOUR_M))

    def test_net_learns(self):
        rng = np.random.default_rng(0)
        X = rng.normal(size=(400, len(model.FEATURES)))
        y = (X[:, 0] > 0).astype(float)
        net = model.Net(seed=0).fit(X, y, np.ones(len(y)), epochs=200)
        self.assertGreater(model.auc(net.prob(X), y), 0.95)


if __name__ == '__main__':
    unittest.main()
