"""Удобство ухода за кустами (place.Access): подход с дорожки, не только с проезда, одиночный куст у края газона,
без невыкашиваемых полосок газона между кроной и краем."""
import unittest

import numpy as np
from shapely.geometry import Point, box

from greening import place


def access(**rules):
    config = place.load_config()
    config['access'] = {**config['access'], **rules}
    # газон 0..30 × 0..30, тротуар слева, проезд справа
    return place.Access(config, box(0, 0, 30, 30), box(-3, 0, 0, 30), box(30, 0, 36, 30))


class Access(unittest.TestCase):
    def test_path_reach(self):
        a = access(path_reach_m=8.0)
        self.assertTrue(a.ok(6, 15, 1.0))
        self.assertFalse(a.ok(12, 15, 1.0))
        self.assertTrue(access(path_reach_m=None, road_only_m=None).ok(12, 15, 1.0))

    def test_road_only(self):
        a = access(path_reach_m=None, road_only_m=3.0)
        # у проезда (крона ближе 1 м), тротуар далеко — подход только с проезда
        self.assertFalse(a.ok(29.0, 15, 1.0))
        self.assertTrue(a.ok(27.0, 15, 1.0))

    def test_lone_shrub_near_edge(self):
        a = access(path_reach_m=None, lone_edge_m=3.0)
        self.assertTrue(a.ok(2, 15, 1.0, lone=True))
        self.assertFalse(a.ok(15, 15, 1.0, lone=True))
        self.assertTrue(a.ok(15, 15, 1.0, lone=False))

    def test_mow_strip(self):
        a = access(edge_strip_max_m=0.2, mow_min_m=1.0)
        # крона 1 м: у края (полоска 0), полоска 0,5 м — нельзя, 1,5 м — можно
        self.assertTrue(a.mowable([(0.5, 15)], 1.0))
        self.assertFalse(a.mowable([(1.0, 15)], 1.0))
        self.assertTrue(a.mowable([(2.0, 15)], 1.0))
        # группа — по ближайшему к краю кусту
        self.assertFalse(a.mowable([(1.0, 15), (5, 15)], 1.0))
        self.assertTrue(a.mowable([(2.0, 15), (5, 15)], 1.0))
        # ближе к краю нормы не пускают (точка у края области выбора) — полоска допускается
        self.assertTrue(a.mowable([(1.0, 15)], 1.0, region=box(1.0, 0, 30, 30)))
        self.assertTrue(access(mow_min_m=None).mowable([(1.0, 15)], 1.0))

    def test_placer_keeps_shrubs_reachable(self):
        config = place.load_config()
        a = place.Access(config, box(0, 0, 30, 30), box(-3, 0, 0, 30), Point())
        p = place.Placer(config, [], np.random.default_rng(0), None, access=a)
        placed = p.fill([(box(0.5, 0.5, 29.5, 29.5), 1.0, 'solitary', {})], 'shrub', 'landscape', limit=40)[0]
        self.assertTrue(placed)
        for x, y, _ in placed:
            self.assertLessEqual(x, config['access']['path_reach_m'] + 1e-6)
            self.assertLessEqual(min(x, y, 30 - y), config['access']['lone_edge_m'] + 1e-6)


if __name__ == '__main__':
    unittest.main()
