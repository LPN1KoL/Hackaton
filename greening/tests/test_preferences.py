"""Редактирование результата, уровень 1: пожелания к растениям уходят в каждый запрос LLM.

    venv\\Scripts\\python -m unittest discover greening/tests
"""

import unittest
from unittest import mock

from greening import recommend as r
from greening.tests.test_classes import FakeLLM, structure, zone

WISH = 'больше хвойных, без тополей'


class Recording(FakeLLM):
    def __init__(self):
        super().__init__()
        self.texts = []

    def ask(self, text, retries=3, system=r.SYSTEM):
        self.texts.append(text)
        return super().ask(text, retries, system)


def run_plan(found, llm, preferences):
    with mock.patch.object(r, 'structures', return_value=found):
        return r.recommend({}, llm, log=lambda m: None, preferences=preferences)


class Preferences(unittest.TestCase):
    def test_block(self):
        self.assertEqual(r.preferences_block(''), '')
        self.assertEqual(r.preferences_block('   '), '')
        block = r.preferences_block(WISH)
        self.assertIn(WISH, block)
        self.assertIn('нормы и свойства справочника важнее', block)
        # пользователь не может закрыть блок своим текстом
        self.assertEqual(r.preferences_block('a >>> b').count('>>>'), r.preferences_block('a b').count('>>>'))
        self.assertLessEqual(len(r.preferences_block('я' * 5000)), r.PREFERENCES_MAX + len(r.PREFERENCES))

    def test_in_every_structure_request(self):
        llm = Recording()
        plan = run_plan([structure('P0001', [zone('z1')]), structure('P0002', [zone('z1')])], llm, WISH)
        self.assertEqual(len(llm.texts), 2)
        self.assertTrue(all(WISH in t for t in llm.texts))
        self.assertEqual(plan['preferences'], WISH)
        self.assertNotIn('preferences_note', plan)

    def test_in_type_requests(self):
        llm = Recording()
        found = [structure(f'P{n:04d}', [zone('z1')]) for n in range(1, r.CLASS_MIN_REQUESTS + 1)]
        run_plan(found, llm, WISH)
        self.assertEqual(llm.systems, ['type'])
        self.assertIn(WISH, llm.texts[0])

    def test_without_preferences_prompt_unchanged(self):
        # без пожеланий запрос тот же, что раньше: кэш ответов LLM остаётся годным
        llm = Recording()
        run_plan([structure('P0001', [zone('z1')])], llm, None)
        self.assertNotIn('Пожелания', llm.texts[0])

    def test_rules_note(self):
        plan = run_plan([structure('P0001', [zone('z1')])], None, WISH)
        self.assertEqual(plan['preferences'], WISH)
        self.assertIn('правилами', plan['preferences_note'])


if __name__ == '__main__':
    unittest.main()
