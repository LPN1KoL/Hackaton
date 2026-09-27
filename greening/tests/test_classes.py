"""Классы мест в подборе LLM (recommend.py): группировка зон, раскладка по структурам, порог включения.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import re
import unittest
from unittest import mock

from greening import catalog, recommend as r

PLANTS = catalog.load()
BY_ID = {p['id']: p for p in PLANTS}


def _plant(groups, need=(), avoid=()):
    return next(p['id'] for p in PLANTS if p['group'] in groups and (p['where'] == '*' or 'Т' in p['where'])
                and set(need) <= p['flags'] and not set(avoid) & p['flags'])


TREE = _plant(('ДЛ',), avoid=('дп', 'шк', 'г'))
TREE_2 = next(p['id'] for p in PLANTS if p['group'] == 'ДЛ' and p['where'] == '*' and p['id'] != TREE
              and not {'дп', 'шк', 'г'} & p['flags'])
TREE_DP = _plant(('ДЛ', 'ДХ'), need=('дп',), avoid=('шк', 'г'))
SHRUB = _plant(('КЛ',), avoid=('дп', 'г'))


def zone(zid, allowed=('tree', 'shrub', 'herbaceous'), tags=(), capacity=3, building=None, ground=False, area=100.0):
    z = {'id': zid, 'allowed': list(allowed), 'tags': list(tags), 'tag_share': {t: 1.0 for t in tags},
         'area_m2': area, 'place': 'участок между зданием и проездом; рядом: урна', 'rules': [], 'sides': {'roadway': 1.0},
         'building_m': building, 'near_ground': ground, 'existing_trees': 0}
    if 'tree' in allowed:
        z['tree_capacity'] = capacity
    if not allowed:
        z['empty_reason'] = 'нормативные отступы исключают посадки'
    return z


def structure(sid, zones, kind='street'):
    area = sum(z['area_m2'] for z in zones)
    place = 'участок, окружённый проездом'
    patch = {'id': 'u1', 'small': False, 'zones': zones, 'area_m2': area, 'place': place, 'sides': {'roadway': 1.0},
             'objects': {}, 'existing_trees': 0, 'mean_width_m': 5.0, 'zone_ids': [z['id'] for z in zones]}
    return {'id': sid, 'type': kind, 'name': r.TYPES[kind][0], 'territory': r.TYPES[kind][1], 'patches': [patch],
            'zones': zones, 'place': place, 'existing_trees': 0, 'area_m2': area, 'total_m2': area * 2,
            'sides': {'roadway': 1.0}, 'objects': {}, 'composition': {'lawn': 0.5, 'sidewalk': 0.5},
            'size_m': [20, 10], 'mean_width_m': 5.0, 'near_playground': False, 'playground_share': 0.0,
            'tag_titles': {'roadside': 'у проезжей части'}}


class FakeLLM:
    """Отвечает на каждую запрошенную зону или класс; виды — заданные тестом."""

    def __init__(self, variants=(TREE, TREE_2), fail_types=False):
        self.requests, self.systems, self.variants, self.fail_types = 0, [], list(variants), fail_types

    def ask(self, text, retries=3, system=r.SYSTEM):
        self.requests += 1
        self.systems.append('type' if system is r.SYSTEM_TYPE else 'structure')
        if system is r.SYSTEM_TYPE:
            if self.fail_types:
                raise RuntimeError('LLM не ответила: тест')
            m = re.search(r'(?:только для классов:?|не решены классы) ([c\d, ]+)', text)
            ids = m.group(1).replace(' ', '').split(',') if m else re.findall(r'^(c\d+):', text, re.M)
            return {'classes': [{'class': c, 'place': 'места', 'decision': f'решение {c}',
                                 'plantings': [{'plant_ids': self.variants, 'role': 'группа', 'reason': 'тень и шум'}]}
                                for c in ids], 'justification': 'замысел типа', '_usage': {'prompt_tokens': 10}}
        m = re.search(r'(?:только для зон:?|не объяснены зоны) ([z\d, ]+)', text)
        ids = m.group(1).replace(' ', '').split(',') if m else re.findall(r'^(z\d+):', text, re.M)
        return {'plantings': [{'zone': z, 'plant_id': TREE, 'role': 'солитер', 'reason': 'тень'} for z in ids],
                'zones': [{'zone': z, 'place': 'место', 'decision': 'решение'} for z in ids],
                'justification': 'замысел', '_usage': {'prompt_tokens': 10}}


class ClassKey(unittest.TestCase):
    def test_same_conditions_share_class(self):
        self.assertEqual(r._class_key(zone('z1', tags=('roadside', 'manhole_nearby'))),
                         r._class_key(zone('z2', tags=('roadside',), area=5)))

    def test_conditions_that_change_choice_split_classes(self):
        base = r._class_key(zone('z1'))
        for other in (zone('z2', building=4.0), zone('z3', ground=True), zone('z4', capacity=0),
                      zone('z5', tags=('existing_trees',)), zone('z6', allowed=('shrub', 'herbaceous'))):
            self.assertNotEqual(base, r._class_key(other), other['id'])

    def test_building_distance_ignored_without_trees(self):
        self.assertEqual(r._class_key(zone('z1', allowed=('shrub',), building=3.0)),
                         r._class_key(zone('z2', allowed=('shrub',))))


class Classes(unittest.TestCase):
    def setUp(self):
        self.members = [structure('P0001', [zone('z1'), zone('z2', allowed=())]),
                        structure('P0002', [zone('z1'), zone('z2', ground=True)])]
        self.classes = r._classes(self.members)

    def test_empty_zones_left_to_code(self):
        self.assertEqual(sum(len(c['zones']) for c in self.classes), 3)
        self.assertEqual(len(self.classes), 2)

    def test_prompt_lists_classes_conditions_and_catalog(self):
        text = r.type_prompt('street', self.members, self.classes, PLANTS)
        self.assertIn('c1: зон 2 в 2 структурах', text)
        self.assertIn(r.NEAR_GROUND, text)
        self.assertIn('Структур 2', text)
        self.assertIn(catalog.header(), text)
        self.assertIn(f'\n{TREE}|', text)

    def test_from_classes_rotates_variants_and_explains_every_zone(self):
        answer = FakeLLM().ask(r.type_prompt('street', self.members, self.classes, PLANTS), system=r.SYSTEM_TYPE)
        first = r._from_classes(self.members[0], 0, self.classes, answer, PLANTS)
        second = r._from_classes(self.members[1], 1, self.classes, answer, PLANTS)
        self.assertEqual(first['plantings'][0]['plant_id'], TREE)
        self.assertEqual(second['plantings'][0]['plant_id'], TREE_2)
        self.assertEqual({z['zone'] for z in first['zones']}, {'z1', 'z2'})
        self.assertEqual(next(z for z in first['zones'] if z['zone'] == 'z2')['by'], 'code')
        self.assertEqual(first['zones'][0]['place'], self.members[0]['zones'][0]['place'])
        self.assertIn('замысел типа', first['justification'])

    def test_unfit_variant_skipped_near_playground(self):
        answer = FakeLLM(variants=(TREE_DP, TREE)).ask(
            r.type_prompt('street', self.members, self.classes, PLANTS), system=r.SYSTEM_TYPE)
        result = r._from_classes(self.members[1], 0, self.classes, answer, PLANTS)
        near = [p for p in result['plantings'] if p['zone'] == 'z2']
        self.assertEqual(near[0]['plant_id'], TREE)
        kept, rejected = r._check(result, self.members[1], PLANTS)
        self.assertFalse(rejected)
        self.assertTrue(all(k['norms'] for k in kept))

    def test_one_flowerbed_per_structure(self):
        herb = ('herbaceous',)
        member = structure('P0001', [zone('z1', allowed=herb, area=200), zone('z2', allowed=herb, area=100),
                                     zone('z3', allowed=herb, area=30)])
        classes = r._classes([member])
        perennial = next(p['id'] for p in PLANTS if p['group'] == 'М')
        answer = {'classes': [{'class': 'c1', 'decision': 'Газон и цветник.', 'plantings': [
            {'plant_ids': [perennial], 'role': 'цветник', 'reason': 'цвет'}]}]}
        result = r._from_classes(member, 0, classes, answer, PLANTS)
        roles = {(p['zone'], p['role']) for p in result['plantings']}
        self.assertEqual(roles, {('z1', 'цветник'), ('z2', 'газон'), ('z3', 'газон')})
        notes = {z['zone']: z['decision'] for z in result['zones']}
        self.assertEqual(notes['z1'], 'Газон и цветник.')
        self.assertIn('самом крупном', notes['z2'])
        self.assertIn(f'меньше {r.BED_MIN_M2:g}', notes['z3'])
        self.assertFalse(any('bed_note' in z for z in result['zones']))

    def test_missing_class_leaves_zone_unexplained(self):
        result = r._from_classes(self.members[0], 0, self.classes, {'classes': []}, PLANTS)
        self.assertEqual([z['zone'] for z in result['zones']], ['z2'])

    def test_ask_type_in_parts(self):
        llm = FakeLLM()
        members = [structure(f'P{n:04d}', [zone('z1', area=n + 1, tags=t, capacity=c)])
                   for n, (t, c) in enumerate([((), 3), (('roadside',), 3), (('existing_trees',), 0),
                                               (('along_path',), 1), (('slope',), 2)])]
        classes = r._classes(members)
        with mock.patch.object(r, 'CLASSES_PER_REQUEST', 2):
            answer = r.ask_type(llm, 'street', members, classes, PLANTS, log=lambda m: None)
        self.assertEqual(set(r._class_answers(answer)), {c['id'] for c in classes})
        self.assertEqual(llm.requests, 3)


class Threshold(unittest.TestCase):
    def run_plan(self, found, llm):
        with mock.patch.object(r, 'structures', return_value=found):
            return r.recommend({}, llm, log=lambda m: None)

    def many(self, count):
        return [structure(f'P{n:04d}', [zone('z1', tags=('roadside',) if n % 2 else ()), zone('z2', allowed=('herbaceous',))],
                          kind='street' if n % 3 else 'yard') for n in range(1, count + 1)]

    def test_few_requests_stay_per_structure(self):
        llm = FakeLLM()
        plan = self.run_plan(self.many(r.CLASS_MIN_REQUESTS - 1), llm)
        self.assertEqual(llm.requests, r.CLASS_MIN_REQUESTS - 1)
        self.assertEqual({s['scope'] for s in plan['structures']}, {'structure'})
        self.assertEqual(plan['summary']['by_type'], 0)

    def test_many_requests_go_by_type(self):
        llm = FakeLLM()
        plan = self.run_plan(self.many(r.CLASS_MIN_REQUESTS), llm)
        self.assertEqual(llm.requests, 2)  # полосы вдоль улиц и дворы
        self.assertEqual({s['scope'] for s in plan['structures']}, {'type'})
        summary = plan['summary']
        self.assertEqual(summary['zones_explained'], summary['zones'])
        self.assertEqual(summary['ok'], r.CLASS_MIN_REQUESTS)
        self.assertEqual(summary['usage']['prompt_tokens'], 20)  # расход — один раз на тип
        trees = {p['plant_id'] for s in plan['structures'] for p in s['plantings'] if p['group'] in r.GROUPS['tree']}
        self.assertEqual(trees, {TREE, TREE_2})

    def test_large_structure_asked_alone(self):
        found = self.many(r.CLASS_MIN_REQUESTS)
        found.insert(0, structure('P0000', [zone(f'z{n}') for n in range(1, r.SEPARATE_ZONES + 2)], kind='square'))
        llm = FakeLLM()
        plan = self.run_plan(found, llm)
        self.assertEqual(plan['structures'][0]['scope'], 'structure')
        self.assertEqual(llm.systems.count('structure'), 2)  # 31 зона — две части
        self.assertEqual(plan['summary']['zones_explained'], plan['summary']['zones'])

    def test_type_failure_falls_back_to_rules(self):
        plan = self.run_plan(self.many(r.CLASS_MIN_REQUESTS), FakeLLM(fail_types=True))
        self.assertEqual({s['status'] for s in plan['structures']}, {'rules'})
        self.assertTrue(all('llm_error' in s for s in plan['structures']))
        self.assertNotIn('scope', plan['structures'][0])

    def test_without_llm_rules_per_structure(self):
        plan = self.run_plan(self.many(r.CLASS_MIN_REQUESTS), None)
        self.assertEqual(plan['summary']['requests'], 0)
        self.assertEqual({s['status'] for s in plan['structures']}, {'rules'})


if __name__ == '__main__':
    unittest.main()
