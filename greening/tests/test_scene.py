"""3D-сцена (scene.py): здания с этажностью из подписей, породы существующих деревьев, вид растений.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from types import SimpleNamespace

from shapely.geometry import LineString, Point, Polygon, box, mapping

from greening import scene


def item(kind, layer, geometry, etype='LWPOLYLINE', text=''):
    return SimpleNamespace(kind=kind, layer=layer, etype=etype, geometry=geometry, source='base', text=text)


def walls(x0, y0, x1, y1, layer='Здания'):
    """Дом из отдельных отрезков, как в топосъёмке."""
    pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return [item('building_wall', layer, LineString(pts[i:i + 2])) for i in range(4)]


def feature(geometry, **props):
    return {'type': 'Feature', 'geometry': mapping(geometry), 'properties': props}


class Context(unittest.TestCase):
    territory = box(0, 0, 100, 100)

    def test_floors_from_label_and_porch(self):
        items = walls(110, 10, 140, 25) + [item('building_wall', 'Здания', Point(120, 15), 'TEXT', '9')]
        items += walls(110, 30, 113, 32, layer='Крыльца')
        ctx = scene.context(items, self.territory, {})
        found = {round(g.area): (floors, layer) for g, floors, layer in ctx['buildings']}
        self.assertEqual(found[450], (9, 'Здания'))
        self.assertEqual(found[6], (None, 'Крыльца'))
        heights = {round(g.area): scene._height(f, layer, scene.load_config()) for g, f, layer in ctx['buildings']}
        self.assertAlmostEqual(heights[450], 9 * 3.0 + 1.2)
        self.assertLess(heights[6], 1)

    def test_big_unlabelled_face_outside_is_yard(self):
        # кольцо дома вокруг двора: двор — грань без подписи, домом он не становится
        items = walls(110, 0, 160, 50) + walls(115, 5, 155, 45)
        items.append(item('building_wall', 'Здания', Point(112, 25), 'TEXT', '5'))
        ctx = scene.context(items, self.territory, {})
        self.assertEqual(len(ctx['buildings']), 1)
        self.assertEqual(ctx['buildings'][0][1], 5)

    def test_species_labels_only(self):
        items = [item('lawn_edge', 'Леса и газоны', Point(50, 50), 'TEXT', t) for t in ('КЛЕН', 'ГАЗОН', '0.40')]
        ctx = scene.context(items, self.territory, {})
        self.assertEqual([t for _, t in ctx['tree_labels']], ['КЛЕН'])


class Existing(unittest.TestCase):
    def test_crowns_do_not_overlap(self):
        # деревья в 3 м друг от друга: кроны не шире промежутка; одиночное — своего размера
        geojson = {'features': [feature(box(0, 0, 100, 100), layer='territory')]
                   + [feature(Point(x, 50), layer='tree') for x in (40, 43)]
                   + [feature(Point(80, 80), layer='tree', row=True)]}
        data = scene.build(geojson)
        crowns = [e[4] for e in data['existing']]
        self.assertEqual(len(crowns), 3)
        self.assertTrue(all(c <= 3 + 0.05 for c in crowns[:2]))  # +0,05 — округление до 0,1 м
        self.assertEqual(data['existing'][2][3], 1)


class Build(unittest.TestCase):
    def test_scene(self):
        territory = box(0, 0, 100, 100)
        geojson = {'features': [
            feature(territory, layer='territory'),
            feature(box(10, 10, 40, 40), layer='surface', surface='lawn'),
            feature(box(110, 10, 140, 25), layer='building', floors=9, source='Здания'),
            feature(Point(20, 20), layer='tree'),
            feature(Point(21, 21), layer='tree_label', text='БЕРЕЗА'),
        ]}
        placement = {'features': [
            feature(Point(30, 30), layer='plant', plant_id='Д046', name='Рябина обыкновенная', group='ДЛ',
                    kind='tree', crown_m=5, id='R00001', structure='P0001', role='солитер'),
            feature(box(12, 12, 20, 20), layer='lawn', plant_id='Т002', group='Г', kind='herbaceous',
                    name='Газон теневыносливый (овсяница красная, мятлик лесной)'),
        ]}
        data = scene.build(geojson, placement)
        self.assertEqual(data['origin'], [50, 50])
        self.assertEqual(data['buildings'][0]['h'], 28.2)
        self.assertFalse(data['buildings'][0]['in'])
        birch = data['profiles'][data['existing'][0][2]]
        self.assertEqual(birch['shape'], 'birch')
        self.assertTrue(birch['existing'])
        rowan = data['profiles'][data['plants'][0][2]]
        self.assertEqual(rowan['shape'], 'small_tree')
        # «теневынОСЛИВый» — газон, а не слива
        lawn = data['profiles'][data['lawns'][0]['s']]
        self.assertIn('lawn', lawn)
        self.assertNotIn('shape', lawn)
        self.assertEqual(data['plants'][0][:2], [-20, -20])


if __name__ == '__main__':
    unittest.main()
