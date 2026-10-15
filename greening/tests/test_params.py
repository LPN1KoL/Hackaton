"""Параметры рассадки (редактирование результата, уровень 2): проверка значений, применение к конфигу
и действие на рассадку в обоих режимах.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import math
import unittest

import numpy as np
from shapely.geometry import box

from greening import model, params, place


class Clean(unittest.TestCase):
    def test_valid(self):
        got = params.clean({'shrub_share': '0.5', 'seed': 7, 'placement': 'model', 'group_sizes_small': '3, 5,7',
                            'solitary_max_tree': '', 'style_mode': 'formal'})
        self.assertEqual(got, {'shrub_share': 0.5, 'seed': 7, 'placement': 'model', 'group_sizes_small': [3, 5, 7],
                               'solitary_max_tree': None, 'style_mode': 'formal'})

    def test_errors(self):
        with self.assertRaises(params.ParamError) as ctx:
            params.clean({'shrub_share': 5, 'unknown': 1, 'seed': 'abc', 'group_sizes_tree': '0, 3',
                          'placement': 'magic', 'tree_spacing_m': ''})
        self.assertEqual(set(ctx.exception.errors),
                         {'shrub_share', 'unknown', 'seed', 'group_sizes_tree', 'placement', 'tree_spacing_m'})

    def test_apply_and_changed(self):
        config, mode, seed = params.apply(params.clean({'drift_landscape': 0.1, 'placement': 'model', 'seed': 3,
                                                        'tree_unit_spacing_m': 8}))
        self.assertEqual(config['drift_share']['landscape'], 0.1)
        self.assertEqual((mode, seed), ('model', 3))
        # значение по умолчанию не считается изменением, режим и зерно — всегда
        self.assertEqual(set(params.changed({'tree_unit_spacing_m': 8, 'seed': 3})), {'seed'})
        self.assertEqual(params.changed({'seed': 0, 'placement': 'model'}), {'placement': 'model'})
        # исходный конфиг не трогается
        self.assertEqual(place.load_config()['drift_share']['landscape'], 0.7)

    def test_schema(self):
        schema = params.schema(default_placement='model')
        keys = {p['key'] for p in schema['params']}
        self.assertEqual(keys, set(params.BY_KEY))
        by = {p['key']: p for p in schema['params']}
        self.assertEqual(by['placement']['default'], 'model')
        self.assertEqual(by['shrub_share']['default'], place.load_config()['shrub_share'])
        self.assertTrue({p['group'] for p in schema['params']} <= {g['key'] for g in schema['groups']})


class Effect(unittest.TestCase):
    """Параметры меняют рассадку одинаково в обоих режимах: каркас общий, модель лишь выбирает места."""

    def run_fill(self, overrides, scorer=None, kind='tree', mode='solitary', limit=math.inf):
        config, _, _ = params.apply(params.clean(overrides))
        config['model_temperature'] = 0.0
        p = place.Placer(config, [], np.random.default_rng(0), None, scorer)
        zone = box(0, 0, 60, 30)
        if scorer is not None:
            p.site = model.Site(zone, {})
        return p.fill([(zone, 4.0 if kind == 'tree' else 1.5, mode, {})], kind, 'landscape', limit=limit)[0]

    def scorer(self):
        return lambda kind, site, points, field: np.full(len(points), 0.6)

    def test_solitary_max(self):
        for scorer in (None, self.scorer()):
            self.assertEqual(len(self.run_fill({'solitary_max_tree': 2}, scorer)), 2)
            self.assertEqual(len(self.run_fill({'solitary_max_tree': 0}, scorer)), 0)
            self.assertGreater(len(self.run_fill({}, scorer)), 2)

    def test_spacing(self):
        for scorer in (None, self.scorer()):
            dense = len(self.run_fill({'tree_unit_spacing_m': 5}, scorer))
            sparse = len(self.run_fill({'tree_unit_spacing_m': 15}, scorer))
            self.assertGreater(dense, sparse)

    def test_limit(self):
        for scorer in (None, self.scorer()):
            self.assertEqual(len(self.run_fill({}, scorer, limit=3)), 3)
            got = self.run_fill({'group_sizes_tree': '5'}, scorer, mode='group', limit=4)
            self.assertLessEqual(len(got), 4)

    def test_group_sizes(self):
        got = self.run_fill({'group_sizes_small': '3', 'shrub_share': 0.7}, kind='shrub', mode='group', limit=3)
        self.assertEqual(len(got), 3)

    def test_style_forced(self):
        config, _, _ = params.apply(params.clean({'style_mode': 'formal'}))
        self.assertEqual(place._style({'id': 'P0001', 'type': 'square'}, 0, config), 'formal')
        config, _, _ = params.apply(params.clean({'style_mode': 'auto', 'landscape_share_square': 1.0}))
        self.assertEqual(place._style({'id': 'P0001', 'type': 'square'}, 0, config), 'landscape')


if __name__ == '__main__':
    unittest.main()


class Moves(unittest.TestCase):
    """Перемещения растений (уровень 3): точки сдвигаются, контур группы — целиком или заново по кронам."""

    def placement(self):
        from shapely.geometry import mapping, Point
        from shapely.ops import unary_union
        plant = lambda pid, x, gid: {'type': 'Feature', 'geometry': mapping(Point(x, 0)), 'properties': {
            'layer': 'plant', 'id': pid, 'group_id': gid, 'kind': 'shrub', 'crown_m': 1.0}}
        outline = unary_union([Point(x, 0).buffer(0.45) for x in (0, 1)])
        return {'metadata': {}, 'features': [plant('R00001', 0, 7), plant('R00002', 1, 7), plant('R00003', 5, 8),
                                             {'type': 'Feature', 'geometry': mapping(outline),
                                              'properties': {'layer': 'group', 'group_id': 7}}]}

    def test_whole_group_shifts_outline(self):
        from shapely.geometry import shape
        result = self.placement()
        self.assertEqual(place.apply_moves(result, {'R00001': (0, 10), 'R00002': (1, 10), 'R00009': (0, 0)}), 2)
        outline = shape(result['features'][3]['geometry'])
        self.assertAlmostEqual(outline.centroid.y, 10, places=1)
        self.assertTrue(result['features'][0]['properties']['moved'])
        self.assertNotIn('moved', result['features'][2]['properties'])
        self.assertEqual(result['metadata']['moved'], 2)

    def test_single_member_rebuilds_outline(self):
        from shapely.geometry import shape, Point
        result = self.placement()
        place.apply_moves(result, {'R00002': (4, 0)})
        outline = shape(result['features'][3]['geometry'])
        self.assertTrue(outline.covers(Point(4, 0)) and outline.covers(Point(0, 0)))
        self.assertFalse(outline.covers(Point(1, 0)))


class EvenSizes(unittest.TestCase):
    def test_even_size_kept(self):
        config, _, _ = params.apply(params.clean({'group_sizes_small': '4'}))
        p = place.Placer(config, [], np.random.default_rng(0), None)
        self.assertEqual(p._sizes(8, 'shrub', 1.0), [4, 4])
        # остаток меньше выбранного размера — нечётный
        self.assertEqual(p._sizes(6, 'shrub', 1.0), [4, 1, 1])
