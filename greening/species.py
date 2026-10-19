"""Нормы по видам растений (species_rules в config/norms.json) — сверх отступов табл. 3.6.1, общих для всех
деревьев и кустарников.

- distance — вид не ближе distance м к объекту (у теплотрасс: 623-ПП (МГСН 1.02-02), п. 4.2.8). Подбор
  (recommend) заменяет вид кодом, если места дальше нормы в зоне нет; рассадка (place) не ставит вид ближе;
  проверка отступов (clearance) сверяет готовую рассадку с исходной геометрией.
- tag — вид не рекомендуется в зоне с этим условием места (у спортивной площадки: п. 4.12.13.2) — подбор
  заменяет его кодом.

Вид выбирает LLM; эти нормы — по названию вида, поэтому проверяет их код, а не запрос (дешевле и без нового кэша).
"""

import re
from functools import cached_property

from shapely.ops import unary_union

from .sections import load_norms

# зона годится виду, если места дальше нормы в ней не меньше, м²: дерево — крона 5 м, куст — малая группа
ROOM_M2 = {'tree': 20.0, 'shrub': 4.0}
KIND = {'ДЛ': 'tree', 'ДХ': 'tree', 'КЛ': 'shrub', 'КХ': 'shrub', 'Л': 'shrub'}


def _name(text):
    return (text or '').lower().replace('ё', 'е').strip()


def load(norms=None):
    rules = (norms or load_norms()).get('species_rules', [])
    return [{**r, 'match': re.compile(r['names'], re.I) if r.get('names') else None,
             'flags': set(r.get('flags', []))} for r in rules]


def matches(rule, name, flags=()):
    """Вид подпадает под правило: по началу названия или свойству справочника (flags)."""
    return bool(rule['match'] and rule['match'].search(_name(name))) or bool(rule['flags'] & set(flags))


class Restrictions:
    """Нормы по видам с геометрией объектов. obstacles — clearance.collect (Result.obstacles, obstacles.pkl):
    {(источник, объект): [(геометрия, слой, выноска)]}; выноски подписей отступа не дают."""

    def __init__(self, obstacles=None, norms=None):
        self.rules = load(norms)
        self.shapes = {}
        for rule in self.rules:
            if 'distance' in rule:
                key = (rule['source'], rule['object'])
                if key not in self.shapes:
                    self.shapes[key] = unary_union([g for g, _, leader in (obstacles or {}).get(key, []) if not leader])
        self._buffers = {}

    @cached_property
    def distance_rules(self):
        return [r for r in self.rules if 'distance' in r and not self.shapes[(r['source'], r['object'])].is_empty]

    def forbidden(self, rule):
        """Полоса, где вид правила сажать нельзя."""
        if rule['rule'] not in self._buffers:
            self._buffers[rule['rule']] = self.shapes[(rule['source'], rule['object'])].buffer(rule['distance'])
        return self._buffers[rule['rule']]

    def applying(self, name, flags=()):
        return [r for r in self.distance_rules if matches(r, name, flags)]

    def cut(self, region, name, flags=()):
        """Область для центров посадки вида без полос, где его сажать нельзя."""
        for rule in self.applying(name, flags):
            if region.intersects(self.forbidden(rule)):
                region = region.difference(self.forbidden(rule))
        return region

    def room(self, geometry):
        """{правило: место в зоне дальше нормы, м²} — по правилам, чья полоса задевает зону."""
        return {r['rule']: round(geometry.difference(self.forbidden(r)).area, 1)
                for r in self.distance_rules if geometry.intersects(self.forbidden(r))}

    def annotate(self, structures, geojson):
        """У зон структур — species_room (для unfit): по секциям разметки."""
        if not self.distance_rules:
            return
        from shapely.geometry import shape
        sections = {f['properties']['id']: shape(f['geometry']) for f in geojson['features']
                    if f['properties']['layer'] == 'section'}
        for structure in structures:
            for zone in structure['zones']:
                geometry = unary_union([sections[s] for s in zone['sections'] if s in sections])
                room = self.room(geometry)
                if room:
                    zone['species_room'] = room


def unfit(plant, zone, rules=None):
    """Почему вид не годится зоне по нормам видов (текст для обоснования замены) или None."""
    kind = KIND.get(plant['group'])
    if kind is None:
        return None
    tags = {t for t, share in zone.get('tag_share', {}).items() if share > 0} | set(zone.get('tags', []))
    for rule in rules if rules is not None else RULES:
        if not matches(rule, plant['name'], plant['flags']):
            continue
        if 'tag' in rule and rule['tag'] in tags:
            return f"{rule['why']} ({rule['cite']})"
        room = zone.get('species_room', {}).get(rule['rule'])
        if 'distance' in rule and room is not None and room < ROOM_M2[kind]:
            return (f"{rule['why']} ({rule['cite']}), а дальше {rule['distance']:g} м от "
                    f"{rule['object_title']} в зоне места почти нет ({room:g} м²)")
    return None


def notes(plant, zone, rules=None):
    """Строки обоснования: нормы видов, которые вид соблюдает в этой зоне только за счёт рассадки."""
    lines = []
    for rule in rules if rules is not None else RULES:
        if 'distance' in rule and rule['rule'] in zone.get('species_room', {}) \
                and matches(rule, plant['name'], plant['flags']):
            lines.append(f"{rule['why'][0].upper()}{rule['why'][1:]} ({rule['cite']}): "
                         f"полоса {rule['distance']:g} м у {rule['object_title']} для этого вида из рассадки исключена")
    return lines


RULES = load()
