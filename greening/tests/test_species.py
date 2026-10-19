"""Нормы по видам (species.py): МГСН 1.02-02, п. 4.2.8 — породы у теплотрасс, п. 4.12.13.2 — у спортивных площадок;
замена вида кодом в подборе и полоса, исключённая из рассадки."""
import unittest

from shapely.geometry import LineString, Point, box

from greening import catalog, clearance, recommend, sections, species

PLANTS = {p['name']: p for p in catalog.load()}
# теплосеть по оси x = 0
HEAT = {('layer', 'heating_network'): [(LineString([(0, -50), (0, 50)]), 'Теплосеть', False)]}


def zone(**extra):
    return {'id': 'z1', 'allowed': ['tree', 'shrub', 'herbaceous'], 'tags': [], 'tag_share': {}, 'rules': [],
            'building_m': None, 'near_ground': False, **extra}


class Matching(unittest.TestCase):
    def test_names(self):
        rules = {r['rule']: r for r in species.RULES}
        near, far = rules['MGSN-10202-4.2.8-2M'], rules['MGSN-10202-4.2.8-4M']
        self.assertTrue(species.matches(near, 'Клён остролистный'))
        self.assertTrue(species.matches(near, 'Сирень обыкновенная'))
        self.assertTrue(species.matches(far, 'Дерен белый/сибирский'))
        self.assertTrue(species.matches(far, 'Берёза повислая/плакучая'))
        self.assertFalse(species.matches(far, 'Спирея березолистная'))
        sport = rules['MGSN-10202-4.12.13.2-SPECIES']
        self.assertTrue(species.matches(sport, 'Ива белая'))
        # мужские клоны тополя пуха не дают
        self.assertFalse(species.matches(sport, 'Тополь бальзамический (муж. клоны)'))
        self.assertTrue(species.matches(sport, 'Рябина Арнольда', {'пд'}))


class Geometry(unittest.TestCase):
    def test_cut(self):
        r = species.Restrictions(HEAT)
        region = box(0, 0, 10, 10)
        self.assertAlmostEqual(r.cut(region, 'Дерен белый/сибирский').area, 60, delta=0.5)
        self.assertAlmostEqual(r.cut(region, 'Клен остролистный').area, 80, delta=0.5)
        self.assertEqual(r.cut(region, 'Спирея японская').area, 100)
        self.assertEqual(set(r.room(region)), {'MGSN-10202-4.2.8-2M', 'MGSN-10202-4.2.8-4M'})
        # нет теплосети — правил с геометрией нет, область прежняя
        self.assertEqual(species.Restrictions({}).cut(region, 'Дерен белый/сибирский').area, 100)

    def test_clearance(self):
        placement = {'features': [
            {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [3, 0]},
             'properties': {'layer': 'plant', 'kind': 'shrub', 'name': 'Дерен белый/сибирский', 'id': 'R00001'}},
            {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [3, 5]},
             'properties': {'layer': 'plant', 'kind': 'shrub', 'name': 'Спирея японская', 'id': 'R00002'}}]}
        check = clearance.check(placement, HEAT, sections.load_norms())
        ids = [(v['id'], v['rule']) for v in check['violations']]
        self.assertIn(('R00001', 'MGSN-10202-4.2.8-4M'), ids)
        self.assertNotIn('R00002', [i for i, _ in ids])


class Choice(unittest.TestCase):
    def test_unfit(self):
        dogwood = PLANTS['Дерен белый/сибирский']
        self.assertIsNone(species.unfit(dogwood, zone()))
        self.assertIsNone(species.unfit(dogwood, zone(species_room={'MGSN-10202-4.2.8-4M': 50.0})))
        self.assertIn('4.2.8', species.unfit(dogwood, zone(species_room={'MGSN-10202-4.2.8-4M': 1.0})))
        rowan = PLANTS['Рябина Арнольда']
        self.assertIn('4.12.13.2', species.unfit(rowan, zone(tags=['near_sport_ground'])))
        self.assertIsNone(species.unfit(rowan, zone()))

    def test_replaced_in_check(self):
        dogwood = PLANTS['Дерен белый/сибирский']
        z = zone(species_room={'MGSN-10202-4.2.8-4M': 1.0})
        structure = {'zones': [z], 'territory': 'Д'}
        answer = {'plantings': [{'zone': 'z1', 'plant_id': dogwood['id'], 'role': 'группа', 'reason': 'текст LLM'}]}
        kept, rejected = recommend._check(answer, structure, catalog.load())
        self.assertEqual(len(kept), 1)
        self.assertNotEqual(kept[0]['plant_id'], dogwood['id'])
        self.assertEqual(kept[0]['by'], 'rules')
        self.assertEqual(kept[0]['substituted_from'], dogwood['name'])
        self.assertIn(kept[0]['group'], ('КЛ', 'КХ'))
        self.assertIn('4.2.8', kept[0]['reason'])
        self.assertIsNone(species.unfit(PLANTS[kept[0]['name']], z))
        self.assertEqual(rejected[0]['replaced_by'], kept[0]['name'])

    def test_kept_with_note(self):
        maple = PLANTS['Клен остролистный']
        z = zone(species_room={'MGSN-10202-4.2.8-2M': 200.0})
        kept, _ = recommend._check({'plantings': [{'zone': 'z1', 'plant_id': maple['id'], 'role': 'солитер'}]},
                                   {'zones': [z], 'territory': 'Д'}, catalog.load())
        self.assertEqual(kept[0]['plant_id'], maple['id'])
        self.assertTrue(any('4.2.8' in line for line in kept[0]['norms']))
        self.assertTrue(any('примечание 1' in line for line in kept[0]['norms']))


if __name__ == '__main__':
    unittest.main()
