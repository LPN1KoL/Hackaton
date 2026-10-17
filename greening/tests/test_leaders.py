"""Фильтр выносок (leaders.find): выноска — только с подписью на полке; стрелка без подписи — участок трубы.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from types import SimpleNamespace

from shapely.geometry import LineString, Point

from greening import leaders

LAYER = 'Канализация самотёчная'


def line(*points, kind='sewer'):
    return SimpleNamespace(kind=kind, layer=LAYER, etype='LWPOLYLINE', geometry=LineString(points), source='base')


def text(x, y, value='148.39лот.'):
    return SimpleNamespace(kind='sewer', layer=LAYER, etype='TEXT', geometry=Point(x, y), source='base', text=value)


def arrow(x, y):
    """Два «пера» стрелки с остриём в (x, y)."""
    return [line((x, y), (x - 0.9, y - 0.3)), line((x, y), (x - 0.9, y + 0.3))]


def trunk():
    """Трасса через колодец (0, 0): два участка, сходящихся в узле."""
    return [line((-20, 0), (0, 0)), line((0, 0), (20, 0))]


class Leaders(unittest.TestCase):
    def test_flow_arrow_without_label_is_pipe(self):
        # выпуск в колодец со стрелкой направления стока, подписи нет — это труба
        items = trunk() + [line((-8, 6), (0, 0))] + arrow(0, 0) + [text(-15, 3)]
        found = leaders.find(items, {'sewer'})
        self.assertNotIn(2, found)

    def test_label_at_bend(self):
        # полка (-6, 4) → (-2, 4) с текстом у излома, наклонная к колодцу со стрелкой
        items = trunk() + [line((-6, 4), (-2, 4), (0, 0))] + arrow(0, 0) + [text(-2.3, 4.45)]
        found = leaders.find(items, {'sewer'})
        self.assertEqual(found, {2, 3, 4})

    def test_label_at_free_end(self):
        items = trunk() + [line((-6, 4), (0, 0))] + [text(-6.2, 4.4)]
        self.assertEqual(leaders.find(items, {'sewer'}), {2})

    def test_long_path_is_pipe(self):
        items = trunk() + [line((-30, 10), (0, 0))] + arrow(0, 0) + [text(-30, 10.4)]
        self.assertEqual(leaders.find(items, {'sewer'}), set())


if __name__ == '__main__':
    unittest.main()
