"""Проверка отступов по исходной геометрии (clearance.py) и разрез секции по зоне (sections._cut).

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from types import SimpleNamespace

from shapely.geometry import LineString, Point, Polygon, box, mapping

from greening import clearance, sections

NORMS = {'distance_rules': [
    {'object': 'gas_pipeline', 'source': 'layer', 'tree': 1.5, 'rule': 'GAS', 'doc': '743-ПП, табл. 3.6.1', 'title': 'Газопровод'},
    {'object': 'roadway', 'source': 'surface', 'tree': 2.0, 'shrub': 1.0, 'rule': 'ROAD', 'doc': '743-ПП, табл. 3.6.1', 'title': 'Край проезжей части'},
],
    'protection_zones': [
        {'object': 'gas_pipeline', 'source': 'layer', 'distance': 3.0, 'plantings': ['tree', 'shrub'], 'rule': 'GAS-ZONE',
         'cite': 'охранная зона', 'title': 'Охранная зона газопровода', 'note': 'по согласованию'},
    ]}


def item(kind, geometry, layer='Газопровод'):
    return SimpleNamespace(kind=kind, layer=layer, etype='LINE', geometry=geometry)


def plant(pid, kind, x, y):
    return {'type': 'Feature', 'geometry': mapping(Point(x, y)),
            'properties': {'layer': 'plant', 'id': pid, 'kind': kind, 'structure': 'P0001', 'zone': 'z1', 'name': pid}}


class Clearance(unittest.TestCase):
    def setUp(self):
        gas = LineString([(0, 0), (20, 0)])
        leader = LineString([(0, 10), (5, 10)])
        items = [item('gas_pipeline', gas), item('gas_pipeline', leader)]
        surfaces = {'roadway': box(0, -10, 20, -5)}
        self.obstacles = clearance.collect(items, surfaces, [], NORMS, skip={1})
        self.placement = {'features': [
            plant('R1', 'tree', 5, 1.0),     # 1 м от газопровода при норме 1,5
            plant('R2', 'tree', 5, 3.0),     # дальше нормы
            plant('R3', 'shrub', 5, 0.2),    # для кустарников у газопровода нормы нет
            plant('R4', 'tree', 2, 10.5),    # у выноски — пометка, не нарушение
            plant('R5', 'shrub', 5, -4.5),   # 0,5 м от проезжей части при норме 1
            plant('R6', 'tree', 50, 50),     # за территорией
        ]}

    def test_violations(self):
        report = clearance.check(self.placement, self.obstacles, NORMS, territory=box(-1, -11, 21, 12))
        found = {(v['id'], v['rule']) for v in report['violations']}
        self.assertEqual(found, {('R1', 'GAS'), ('R5', 'ROAD')})
        r1 = next(v for v in report['violations'] if v['id'] == 'R1')
        self.assertAlmostEqual(r1['distance_m'], 1.0)
        self.assertEqual(r1['layer'], 'Газопровод')
        self.assertEqual([n['id'] for n in report['leader_notes']], ['R4'])
        self.assertEqual([p['id'] for p in report['outside_territory']], ['R6'])
        self.assertEqual(report['summary']['violations'], 2)

    def test_approvals(self):
        report = clearance.check(self.placement, self.obstacles, NORMS)
        # охранная зона 3 м: R1 (1 м), R3 (0,2 м); R2 на 3 м — на границе, не в зоне; выноска зону не даёт
        self.assertEqual(sorted(a['id'] for a in report['approvals']), ['R1', 'R3'])
        self.assertEqual(report['summary']['approvals'], 2)

    def test_near_objects(self):
        report = clearance.check(self.placement, self.obstacles, NORMS)
        near = {p['id']: p['near'] for p in report['plants']}
        self.assertEqual([e['rule'] for e in near['R2']], ['GAS'])
        self.assertEqual(near['R2'][0]['distance_m'], 3.0)
        # кустарник: только нормы для кустарников
        self.assertEqual({e['rule'] for e in near['R3']}, set())
        self.assertEqual(near['R6'], [])


class Cut(unittest.TestCase):
    def test_parts_cover_piece(self):
        piece = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        zone = LineString([(0, 5), (10, 5.000000001)]).buffer(1)
        inside, outside = sections._cut(piece, zone)
        self.assertAlmostEqual(inside.area + outside.area, piece.area, places=4)
        self.assertLess(inside.intersection(outside).area, 1e-6)


if __name__ == '__main__':
    unittest.main()
