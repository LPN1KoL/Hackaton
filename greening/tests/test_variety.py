"""Разнообразие посадок: миксбордер и второй вид смешанных групп в плане (variety.py), смешанные группы,
подлесок, ступенчатость по высоте, куртины миксбордера и полоса газона у лугового (place.py), хвойный акцент
и сезонные свойства (recommend.py, justify.py).

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import copy
import math
import unittest

import numpy as np
from shapely.geometry import LineString, Point, box, mapping, shape

from greening import catalog, justify, place, recommend, variety

PLANTS = catalog.load()
BY_NAME = {p['name']: p for p in PLANTS}


def zone(zid='z1', allowed=('tree', 'shrub', 'herbaceous'), area=200.0, **extra):
    return {'id': zid, 'patch': 'u1', 'allowed': list(allowed), 'area_m2': area, 'tags': [], 'rules': [],
            'near_ground': False, 'building_m': None, 'tree_capacity': 3, 'existing_trees': 0, 'sections': ['s1'],
            **extra}


def item(name, role, zid='z1'):
    plant = BY_NAME[name]
    return {'zone': zid, 'plant_id': plant['id'], 'role': role, 'name': plant['name'], 'group': plant['group'],
            'unit': 'м2' if plant['group'] in ('М', 'Г') else 'шт'}


def plan_of(*plantings, zones=None, kind='square'):
    return {'structures': [{'id': 'P0001', 'type': kind, 'zones': zones or [zone()], 'plantings': list(plantings)}]}


class Catalog(unittest.TestCase):
    def test_season_flags(self):
        self.assertEqual(justify.seasons(BY_NAME['Ель колючая']), ['зима'])
        self.assertIn('весна', justify.seasons(BY_NAME['Рябина обыкновенная']))
        self.assertIn('осень', justify.seasons(BY_NAME['Рябина обыкновенная']))
        # лиственница осенью сбрасывает хвою — не вечнозелёная
        self.assertNotIn('зима', justify.seasons(BY_NAME['Лиственница сибирская']))
        self.assertTrue(any(f.startswith('по сезонам') for f in justify.facts(BY_NAME['Сирень обыкновенная'])))

    def test_shrub_heights(self):
        shrubs = [p for p in PLANTS if p['group'] in ('КЛ', 'КХ')]
        self.assertTrue(all(any(f.startswith('h') and f[1:].isdigit() for f in p['flags']) for p in shrubs))


class Mixborder(unittest.TestCase):
    def test_companions_cover_seasons(self):
        plan = plan_of(item('Лилейник', 'цветник'), item('Газон обыкновенный (мятлик луговой, овсяница красная, '
                                                          'райграс пастбищный)', 'газон'))
        counts = variety.compose(plan)
        self.assertEqual(counts['mixborder'], 1)
        added = [p for p in plan['structures'][0]['plantings'] if 'mix_of' in p]
        self.assertEqual(len(added), place.load_config()['variety']['mixborder_species'] - 1)
        seasons = {s for p in added for s in justify.seasons(BY_NAME[p['name']])}
        self.assertTrue({'весна', 'осень'} <= seasons)
        self.assertTrue(all(p['mix_of'] == 0 and p['by'] == 'rules' and p['role'] == 'цветник' for p in added))

    def test_idempotent_and_off(self):
        plan = plan_of(item('Лилейник', 'цветник'))
        variety.compose(plan)
        once = copy.deepcopy(plan)
        variety.compose(plan)
        self.assertEqual(plan, once)
        config = place.load_config()
        config['variety']['mixborder_species'] = 1
        variety.compose(plan, config)
        self.assertEqual(len(plan['structures'][0]['plantings']), 1)

    def test_small_bed_stays_single(self):
        plan = plan_of(item('Лилейник', 'цветник'), zones=[zone(area=20.0)])
        self.assertEqual(variety.compose(plan)['mixborder'], 0)

    def test_drifts_split_patch(self):
        placer = place.Placer(place.load_config(), [], np.random.default_rng(0), None)
        bed = box(0, 0, 12, 4)
        parts = placer.drifts(bed, 3)
        self.assertEqual(len(parts), 3)
        self.assertAlmostEqual(sum(p.area for p in parts), bed.area, places=3)
        self.assertGreaterEqual(parts[0].area, parts[-1].area)


class MixedPartner(unittest.TestCase):
    def test_partner_of_other_height(self):
        plan = plan_of(item('Сирень обыкновенная', 'группа'), item('Спирея японская', 'группа', 'z2'),
                       zones=[zone(), zone('z2')])
        self.assertEqual(variety.compose(plan)['mixed'], 2)
        added = {p['zone']: p['name'] for p in plan['structures'][0]['plantings'] if 'mix_of' in p}
        self.assertEqual(added, {'z1': 'Спирея японская', 'z2': 'Сирень обыкновенная'})

    def test_no_partner_when_two_shrubs(self):
        plan = plan_of(item('Сирень обыкновенная', 'группа'), item('Спирея японская', 'группа'))
        self.assertEqual(variety.compose(plan)['mixed'], 0)


def access(walk):
    config = place.load_config()
    return config, place.Access(config, box(0, 0, 40, 30), walk, Point())


class Composition(unittest.TestCase):
    def test_mixed_group_low_in_front(self):
        # дорожка снизу (y < 0): низкий вид — ближе к ней
        config, a = access(box(0, -3, 40, 0))
        config['variety'] = {**config['variety'], 'mixed_share': 1.0, 'underplanting_share': 0}
        p = place.Placer(config, [], np.random.default_rng(1), None, access=a)
        region = box(0.5, 0.5, 39.5, 12)
        entries = [(region, 2.5, 'group', {'plant_id': 'tall', 'name': 'высокий'}),
                   (region, 1.0, 'group', {'plant_id': 'low', 'name': 'низкий'})]
        placed = p.fill(entries, 'shrub', 'landscape', limit=20, heights=[4.0, 1.0])
        mixed = [o for o in p.outlines if o['layout'] == 'mixed']
        self.assertTrue(mixed)
        gid = mixed[0]['group_id']
        tall = [y for x, y, q in placed[0] if q[3] == gid]
        low = [y for x, y, q in placed[1] if q[3] == gid]
        self.assertTrue(tall and low)
        self.assertLess(np.mean(low), np.mean(tall))

    def test_underplanting_around_tree(self):
        config, a = access(box(0, -3, 40, 0))
        config['variety'] = {**config['variety'], 'underplanting_share': 1.0, 'mixed_share': 0}
        p = place.Placer(config, [], np.random.default_rng(0), None, access=a)
        tree = p.solitary(box(5, 5, 35, 25), 1, 'tree', 6.0, 'landscape')[0]
        anchor = (tree[0], tree[1], 6.0, tree[2][3])
        placed = p.fill([(box(0.5, 0.5, 39.5, 29.5), 1.5, 'group', {'plant_id': 'k'})], 'shrub', 'landscape',
                        limit=7, heights=[2.0], anchors=[anchor])[0]
        self.assertTrue(any(o['layout'] == 'underplanting' for o in p.outlines))
        need = max(config['tree_shrub_min_m'], config['under_tree_share'] * 3.0)
        dists = [math.dist((x, y), tree[:2]) for x, y, _ in placed]
        self.assertTrue(all(d >= need - 1e-6 for d in dists))
        # дуга — со стороны дорожки
        self.assertLess(np.mean([y for x, y, _ in placed]), tree[1])

    def test_height_grading(self):
        config, a = access(box(0, -3, 40, 0))
        config['variety'] = {**config['variety'], 'mixed_share': 0, 'underplanting_share': 0, 'height_grading': 2.0}
        p = place.Placer(config, [], np.random.default_rng(0), None, access=a)
        region = box(0.5, 0.5, 39.5, 14)
        entries = [(region, 1.0, 'solitary', {'plant_id': 'low'}), (region, 1.0, 'solitary', {'plant_id': 'tall'})]
        p.access.rules = {}   # без правил ухода: одиночный куст не обязан стоять у края газона
        placed = p.fill(entries, 'shrub', 'landscape', limit=16, heights=[0.5, 4.0])
        self.assertLess(np.mean([y for _, y, _ in placed[0]]), np.mean([y for _, y, _ in placed[1]]))

    def test_off_keeps_result(self):
        config = place.load_config()
        off = copy.deepcopy(config)
        off['variety'] = {}
        results = []
        for c, heights in ((off, None), ({**config, 'variety': {k: 0 for k in config['variety']}}, [1.0, 3.0])):
            p = place.Placer(c, [], np.random.default_rng(3), None)
            entries = [(box(0, 0, 30, 20), 1.0, 'group', {'plant_id': 'a'}), (box(0, 0, 30, 20), 2.0, 'group', {'plant_id': 'b'})]
            results.append(p.fill(entries, 'shrub', 'landscape', limit=30, heights=heights))
        self.assertEqual(results[0], results[1])


class Lawns(unittest.TestCase):
    def test_meadow_with_edging_strip(self):
        walk, lawn = box(0, 0, 40, 3), box(0, 3, 40, 30)
        geojson = {'features': [
            {'geometry': mapping(walk), 'properties': {'layer': 'surface', 'surface': 'sidewalk'}},
            {'geometry': mapping(lawn), 'properties': {'layer': 'surface', 'surface': 'lawn'}},
            {'geometry': mapping(lawn), 'properties': {'layer': 'section', 'id': 's1', 'allowed': ['herbaceous']}},
        ]}
        plan = plan_of(item('Газон обыкновенный (мятлик луговой, овсяница красная, райграс пастбищный)', 'газон'),
                       item('Газон луговой (разнотравный: овсяница луговая, мятлик, тимофеевка, клевер белый)', 'газон'),
                       zones=[zone(allowed=('herbaceous',), area=lawn.area)])
        result = place.place(geojson, plan, log=lambda *a: None)
        lawns = {f['properties']['plant_id']: shape(f['geometry']) for f in result['features']
                 if f['properties']['layer'] == 'lawn'}
        border = place.load_config()['variety']['meadow_border_m']
        edging = lawns[BY_NAME['Газон обыкновенный (мятлик луговой, овсяница красная, райграс пастбищный)']['id']]
        self.assertAlmostEqual(edging.area, 40 * border, delta=2)
        self.assertLessEqual(edging.bounds[3], 3 + border + 0.05)
        self.assertGreater(sum(g.area for g in lawns.values()), lawn.area - 1)


class Evergreen(unittest.TestCase):
    def structure(self, *zones):
        return {'id': 'P0007', 'territory': 'Д', 'patches': [{'small': False, 'zones': list(zones)}]}

    def test_conifer_added(self):
        explained = {'z1': {'place': '', 'decision': 'Группа рябин.'}}
        kept = [item('Рябина обыкновенная', 'группа')]
        added = recommend._backfill_evergreen(self.structure(zone()), kept, PLANTS, explained)
        self.assertEqual(len(added), 1)
        plant = BY_NAME[added[0]['name']]
        self.assertTrue(recommend._evergreen(plant))
        self.assertEqual(added[0]['by'], 'rules')
        self.assertIn('вечнозелёный', explained['z1']['decision'])

    def test_not_added(self):
        structure = self.structure(zone())
        kept = [item('Ель колючая', 'солитер')]
        self.assertEqual(recommend._backfill_evergreen(structure, kept, PLANTS, {}), [])
        self.assertEqual(recommend._backfill_evergreen(structure, [], PLANTS, {}, 'Пожалуйста, без хвойных'), [])

    def test_shrub_when_no_tree_room(self):
        added = recommend._backfill_evergreen(self.structure(zone(tree_capacity=0)), [], PLANTS, {})
        self.assertEqual(BY_NAME[added[0]['name']]['group'], 'КХ')

    def test_near_playground_no_prickly(self):
        added = recommend._backfill_evergreen(self.structure(zone(tree_capacity=0, near_ground=True)), [], PLANTS, {})
        self.assertTrue(all('дп' not in BY_NAME[a['name']]['flags'] for a in added))


if __name__ == '__main__':
    unittest.main()
