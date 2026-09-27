"""Подбор растений LLM по структурам участка с обоснованием.

    python -m greening.recommend РАЗМЕТКА.greening.geojson [--out plan.json] [--model M] [--limit N]

Структура — кусок территории целиком, вместе с дорожками: двор, сквер, бульвар, полоса вдоль улицы
(blocks.py). Внутри неё газоны u1, u2… (связные зелёные участки из секций разметки, их разделяют
дорожки и площадки), а в газонах — зоны по разрешённым посадкам. LLM получает структуру целиком:
описание, газоны, зоны с нормами и отобранную часть справочника (config/plants.txt), — подбирает
единый ассортимент на всю структуру и объясняет каждую зону. Если так вышло бы не меньше
CLASS_MIN_REQUESTS запросов, по структурам спрашиваются только крупные (больше SEPARATE_ZONES зон), остальные — по типу (все полосы вдоль улиц, все дворы…): их зоны сводятся
в классы мест (разрешённые посадки + условия, от которых зависит выбор вида), LLM решает каждый класс
один раз, код раскладывает решение по зонам, чередуя взаимозаменяемые виды по структурам. Доступ —
OpenAI-совместимый API из .env (BASE_URL, API_KEY, необязательно LLM_MODEL).
"""

import argparse
import hashlib
import math
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import shape
from shapely.ops import unary_union

from . import blocks, catalog, justify, place, rules

# укладывается в ограничение цены аккаунта прокси (≤100 ₽ за 1 млн токенов); Sonnet туда не проходит
DEFAULT_MODEL = 'google/gemini-3.1-pro-preview'
ENV = Path(__file__).resolve().parent.parent / '.env'
# газоны меньше этого — просто газон, без подбора LLM, м²
MIN_STRUCTURE_M2 = 20.0
# секции ближе этого друг к другу — одна структура (щели от округления координат), м
JOIN = 0.05
PLAYGROUND_DISTANCE = 10.0
# структура — «территория с площадкой», если площадка в ней (эта доля площади площадки внутри)
# и рядом с площадкой (ближе PLAYGROUND_DISTANCE) не меньше PLAYGROUND_SHARE её газонов
PLAYGROUND_INSIDE = 0.5
PLAYGROUND_SHARE = 0.3
# условия зоны: растения с «дп» (ядовитые, колючие) сюда нельзя
GROUND_TAGS = ('near_playground', 'near_sport_ground')
TYPES = {
    'street': ('полоса вдоль улицы', 'Т'),
    'playground': ('территория с детской площадкой', 'Д'),
    'yard': ('придомовая территория', 'Д'),
    'square': ('сквер', 'К'),
    'plaza': ('пешеходная зона с озеленением', 'П'),
    'lawn': ('озеленённая территория', 'К'),
}
HARD_SURFACES = ('sidewalk', 'paving', 'sealed', 'gravel', 'playground')
SURFACE_NAMES = {
    'lawn': 'газон', 'flowerbed': 'цветник', 'ground': 'грунт', 'unknown': 'не определено',
    'sidewalk': 'тротуары и дорожки', 'paving': 'мощение', 'sealed': 'твёрдое покрытие',
    'gravel': 'гравий', 'playground': 'площадки',
}
GROUPS = {'tree': ('ДЛ', 'ДХ'), 'shrub': ('КЛ', 'КХ', 'Л'), 'herbaceous': ('М', 'О', 'Б', 'Г')}
PLANTING_NAMES = {'tree': 'деревья', 'shrub': 'кустарники', 'herbaceous': 'травянистые'}
PLANTING_ORDER = ('tree', 'shrub')
# вместимость зоны для новых деревьев: шаг между ними и отступ от старых стволов — как в config/placement.json
# (tree_spacing_m, existing_tree_clearance_m.tree); сетка поиска мест, м
_PLACEMENT = place.load_config()
TREE_SPACING = _PLACEMENT['tree_unit_spacing_m']
EXISTING_CLEARANCE = _PLACEMENT['existing_tree_clearance_m']['tree']
ROOM_STEP = 1.0
# газон для мелких участков, которые не отправляются в LLM: обыкновенный и теневыносливый
LAWN, SHADE_LAWN = 'Т001', 'Т002'
SHADE_TAGS = ('existing_trees', 'near_building')
# граница участка ближе этого к объекту — участок с ним граничит, м
TOUCH = 0.5
# объекты (урны, контейнерные площадки) ближе этого — «рядом», м
NEAR_OBJECT = 3.0
# соседи границы: порядок — приоритет, когда кусок границы близок сразу к двум объектам;
# формы: именительный, творительный («между проездом и …»), родительный («вдоль проезда»)
SIDES = {
    'building': ('здание', 'зданием', 'здания'),
    'playground': ('площадка', 'площадкой', 'площадки'),
    'roadway': ('проезд', 'проездом', 'проезда'),
    'sidewalk': ('тротуар или дорожка', 'тротуаром или дорожкой', 'тротуара или дорожки'),
    'paving': ('мощение', 'мощением', 'мощения'),
    'sealed': ('площадка с твёрдым покрытием', 'площадкой с твёрдым покрытием', 'площадки с твёрдым покрытием'),
    'gravel': ('гравийное покрытие', 'гравийным покрытием', 'гравийного покрытия'),
    'zones': ('другие зоны этого газона', 'другими зонами газона', 'других зон газона'),
    'edge': ('граница работ', 'границей работ', 'границы работ'),
    'other': ('не определено по чертежу', None, None),
}
OBJECTS = {
    'urn': 'урна', 'container_site': 'контейнерная площадка', 'bike_parking': 'велопарковка',
    'bollard': 'столбики', 'bench': 'скамья', 'small_architecture': 'МАФ',
    'playground_children': 'детская площадка', 'playground_sport': 'спортивная площадка',
    'special_surface': 'площадка со спецпокрытием', 'stands': 'трибуна', 'cafe': 'летнее кафе',
    'garage': 'гараж', 'service_building': 'хозпостройка', 'dog_area': 'вольер', 'exposition': 'экспозиция',
    'parking': 'парковка',
}
UNKNOWN_EMPTY = ('поверхность не определена по чертежу (нет штриховки и подписи): посадки не назначаются, '
                 'место остаётся как есть до уточнения')

SYSTEM = """Ты ландшафтный архитектор, проектируешь озеленение Москвы по 743-ПП.
Подбери посадки для одной структуры территории целиком — двора, сквера, бульвара, полосы вдоль улицы. Газоны внутри структуры разделены дорожками и площадками, но это одно место: ассортимент, ритм и композиция должны быть общими для всей структуры (повторяющиеся виды, связанные группы, единый стиль), а не подбираться для каждого газона отдельно. Правила:
- только растения из справочника по id; в зону — только разрешённые в ней группы (деревья ДЛ/ДХ, кустарники КЛ/КХ/Л, травянистые М/О/Б/Г);
- соблюдай свойства: дп не рядом с детскими и спорт. площадками; шк не ближе 10 м от жилых домов (не в зоны с условием «у здания»); г не у проезжей части; пл не рядом с плодовыми; учитывай свет (с/п/т) и условия зоны;
- зоны «у проезжей части» — устойчивые к соли и газам; «под кронами существующих деревьев» — теневыносливые; «вдоль дорожки» — без колючих и не загораживающие проход;
- в каждой зоне с разрешёнными травянистыми предложи покрытие (газон или цветник);
- деревья — основной ярус озеленения: в каждой зоне, где разрешены деревья и есть место под новые (указано в описании), выбери дерево и приём (солитер, группа, рядовая посадка); тень от существующих деревьев не причина отказываться — выбери теневыносливые или компактные виды; кустарники — второй ярус;
- в зоны ближе 10 м к зданию (расстояние дано в описании) — только компактные деревья без широкой кроны;
- ты выбираешь виды и приём посадки, а количество и точную расстановку считает алгоритм по площади, нормам и просветам между растениями; поэтому нигде не называй количество растений и площади посадок;
- ассортимент «о» (основной) предпочтительнее «д» и «п».
Обоснование — убедительное и конкретное, для заказчика ДПиООС: социальная польза (кто пользуется местом, безопасность, тень, шум, пыль), экономика (стоимость, долговечность, приживаемость), обслуживание (уход, стрижка, полив, уборка листвы), экология и эстетика по сезонам. Ссылайся только на нормы, переданные в описании, не выдумывай пункты.
Объясни каждую зону из описания (кроме мелких газонов, решённых без подбора) — и ту, где сажаешь, и ту, где ничего не сажаешь или ничего нельзя. Для каждой: что это за место простыми словами по описанию окружения («полоса между проездом и тротуаром», «карман между дорожками», «газон у торца дома») и почему здесь такое решение. Если в зоне нельзя ничего — объясни почему (причина дана в описании) и чем место остаётся.
В тексте (reason, justification, zones) не используй коды справочника — ни id, ни буквы свойств и групп: только названия растений и обычные слова. Номера зон (z1…) в тексте тоже не упоминай.
Ответ — только JSON, в "zones" по одной записи на каждую зону, в "justification" — замысел для структуры целиком:
{"plantings":[{"zone":"z1","plant_id":"Д032","role":"солитер|группа|рядовая посадка|живая изгородь|куртина|цветник|газон","reason":"почему это растение здесь"}],"zones":[{"zone":"z1","place":"что это за место","decision":"что здесь и почему, 1–3 предложения"}],"justification":"обоснование решения по структуре целиком, 5–8 предложений"}"""

SYSTEM_TYPE = """Ты ландшафтный архитектор, проектируешь озеленение Москвы по 743-ПП.
Подбери единый ассортимент для всех небольших структур одного типа на участке — например, всех полос вдоль улиц или всех придомовых территорий. Их зоны сведены в классы мест: в классе — зоны с одинаковыми разрешёнными посадками и условиями, решение класса применяется ко всем его зонам. Правила:
- только растения из справочника по id; в класс — только разрешённые в нём группы (деревья ДЛ/ДХ, кустарники КЛ/КХ/Л, травянистые М/О/Б/Г);
- соблюдай свойства: дп не рядом с детскими и спорт. площадками; шк не ближе 10 м от жилых домов; г не у проезжей части; пл не рядом с плодовыми; учитывай свет (с/п/т) и условия класса;
- «у проезжей части» — устойчивые к соли и газам; «под кронами существующих деревьев» — теневыносливые; «вдоль дорожки» — без колючих и не загораживающие проход;
- в каждом классе с разрешёнными травянистыми предложи покрытие (газон или цветник);
- деревья — основной ярус: в каждом классе, где разрешены деревья и есть место под новые, выбери дерево и приём (солитер, группа, рядовая посадка); тень от существующих деревьев не причина отказываться; кустарники — второй ярус;
- в классы «ближе 10 м к зданию» — только компактные деревья без широкой кроны;
- для каждого приёма дай 2–3 взаимозаменяемых вида в "plant_ids": алгоритм чередует их по структурам, чтобы территория не была однообразной; каждый вариант должен подходить всем условиям класса;
- ассортимент общий на тип: одни и те же виды повторяются в разных классах, где подходят;
- количество и расстановку считает алгоритм; нигде не называй количество растений и площади посадок;
- ассортимент «о» (основной) предпочтительнее «д» и «п».
Обоснование — убедительное и конкретное, для заказчика ДПиООС: социальная польза (кто пользуется местом, безопасность, тень, шум, пыль), экономика (стоимость, долговечность, приживаемость), обслуживание (уход, стрижка, полив, уборка листвы), экология и эстетика по сезонам. "reason" — о приёме и свойствах, общих для вариантов; если называешь растения, называй все варианты. Ссылайся только на нормы из описания, не выдумывай пункты.
Объясни каждый класс: что это за места простыми словами и почему такое решение.
В тексте не используй коды справочника — ни id, ни буквы свойств и групп — и номера классов (c1…).
Ответ — только JSON, в "classes" по одной записи на каждый класс из описания:
{"classes":[{"class":"c1","plantings":[{"plant_ids":["Д032","Д011"],"role":"солитер|группа|рядовая посадка|живая изгородь|куртина|цветник|газон","reason":"почему этот приём и эти виды здесь"}],"place":"что это за места","decision":"что здесь и почему, 1–3 предложения"}],"justification":"замысел для всех структур этого типа, 4–6 предложений"}"""


def load_env(path=ENV):
    env = {}
    if Path(path).exists():
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    env.update({k: v for k, v in os.environ.items() if k in ('BASE_URL', 'API_KEY', 'LLM_MODEL')})
    return env


def _classify(structure, zones):
    """Тип структуры по составу, окружению и форме; буква — колонка разрешений справочника."""
    area = structure['area_m2'] or 1
    share = defaultdict(float)
    for zone in zones:
        for tag in zone['tags']:
            share[tag] += zone['area_m2'] / area
    hard = sum(structure['composition'].get(s, 0) for s in HARD_SURFACES)
    if structure['playground_share'] >= PLAYGROUND_SHARE:
        return 'playground'
    if not structure['core'] or (share['roadside'] > 0.3 and structure['mean_width_m'] < 6):
        return 'street'
    if hard > 0.6:
        return 'plaza'
    if share['near_building'] > 0.4:
        return 'yard'
    if area > 1500:
        return 'square'
    return 'lawn'


class Surroundings:
    """Что окружает участок: доли его границы по соседям и объекты рядом — чтобы LLM могла назвать место."""

    def __init__(self, features):
        self.sides = {}
        for kind in SIDES:
            parts = [part for g, p in features if p['layer'] == 'surface' and p['surface'] == kind
                     for part in getattr(g, 'geoms', [g])]
            if parts:
                self.sides[kind] = (STRtree(parts), parts)
        edges = [g.boundary for g, p in features if p['layer'] == 'territory']
        if edges:
            self.sides['edge'] = (STRtree(edges), edges)
        self.objects = [(g, p['type']) for g, p in features if p['layer'] == 'structure']
        self.object_index = STRtree([g for g, _ in self.objects]) if self.objects else None
        self.surfaces = [(part, p['surface']) for g, p in features if p['layer'] == 'surface'
                         for part in getattr(g, 'geoms', [g])]
        self.surface_index = STRtree([g for g, _ in self.surfaces])

    def composition(self, geometry):
        """{поверхность: доля площади} внутри geometry."""
        areas = Counter()
        for i in self.surface_index.query(geometry, predicate='intersects'):
            part, kind = self.surfaces[int(i)]
            areas[kind] += part.intersection(geometry).area
        total = sum(areas.values()) or 1
        return {k: round(v / total, 2) for k, v in areas.most_common() if v / total >= 0.01}

    def inside(self, geometry):
        """Объекты внутри geometry: {тип: число}."""
        found = Counter()
        if self.object_index is not None:
            for i in self.object_index.query(geometry, predicate='intersects'):
                found[self.objects[int(i)][1]] += 1
        return dict(found)

    def describe(self, geometry, zones=None):
        """{'sides': {вид: доля границы}, 'objects': {тип: число}}; zones — остальные зоны того же участка."""
        line = geometry.boundary
        total = line.length
        shares = {}
        clip = geometry.buffer(TOUCH * 2)
        for kind in SIDES:
            if kind == 'other' or line.is_empty:
                continue
            if kind == 'zones':
                near = [zones] if zones is not None and not zones.is_empty else []
            elif kind in self.sides:
                tree, parts = self.sides[kind]
                near = [parts[i] for i in tree.query(line, predicate='dwithin', distance=TOUCH)]
            else:
                near = []
            if not near:
                continue
            band = unary_union([g.intersection(clip) for g in near]).buffer(TOUCH)
            length = line.intersection(band).length
            if length > 0:
                shares[kind] = length / total
                line = line.difference(band)
        if total and line.length / total > 0.01:
            shares['other'] = line.length / total
        objects = Counter()
        if self.object_index is not None:
            for i in self.object_index.query(geometry, predicate='dwithin', distance=NEAR_OBJECT):
                objects[self.objects[i][1]] += 1
        return {'sides': {k: round(v, 2) for k, v in sorted(shares.items(), key=lambda kv: -kv[1])},
                'objects': dict(objects)}


def _place(around, width, area):
    """Короткое название места по окружению: «полоса ~3 м между проездом и тротуаром или дорожкой»."""
    strip = width < 4
    shape_name = f'полоса шириной ~{width:g} м' if strip else ('небольшой участок' if area < 100 else 'участок')
    main = [k for k, v in around['sides'].items() if v >= 0.2 and k not in ('other', 'zones')]
    if len(main) >= 2:
        text = f'{shape_name} между {SIDES[main[0]][1]} и {SIDES[main[1]][1]}'
    elif main and around['sides'][main[0]] >= 0.7 and not strip:
        text = f'{shape_name}, окружённый {SIDES[main[0]][1]}'
    elif main:
        text = f'{shape_name} вдоль {SIDES[main[0]][2]}'
    elif around['sides'].get('zones', 0) >= 0.5:
        text = f'{shape_name} внутри газона' if strip else 'внутренняя часть газона'
    else:
        text = shape_name
    objects = [OBJECTS.get(t, t) + (f' ×{n}' if n > 1 else '') for t, n in around['objects'].items()]
    return text + (f'; рядом: {", ".join(objects)}' if objects else '')


def _sides_text(around):
    return ', '.join(f'{SIDES[k][0]} {round(v * 100)}%' for k, v in around['sides'].items())


def _empty_reason(zone):
    """Почему в зоне ничего нельзя — пишется кодом, LLM только пересказывает."""
    if zone['surface'].get('unknown', 0) >= 0.5:
        return UNKNOWN_EMPTY
    rules = _rules_text(zone['rules'])
    return 'нормативные отступы исключают посадки' + (f': {rules}' if rules else '')


def _zones(members, buildings, around=None, trees=None):
    """Секции структуры → зоны по разрешённым посадкам; условия места с долей площади.

    building_m — расстояние от зоны до ближайшего здания (для широких крон), None — зданий нет.
    around — Surroundings: у зоны появляются соседи границы и название места.
    trees — STRtree существующих деревьев: у зоны с деревьями — их число и ориентир вместимости новых.
    """
    groups = defaultdict(list)
    shapes = defaultdict(list)
    for geometry, props in members:
        groups[tuple(props['allowed'])].append(props)
        shapes[tuple(props['allowed'])].append(geometry)
    merged = {allowed: unary_union(parts) for allowed, parts in shapes.items()}
    zones = []
    for allowed, props in sorted(groups.items(), key=lambda item: -sum(p['area_m2'] for p in item[1])):
        near = None if buildings.is_empty else round(merged[allowed].distance(buildings), 1)
        area = sum(p['area_m2'] for p in props)
        surface = Counter()
        for p in props:
            surface[p['surface']] += p['area_m2'] / area
        place = {}
        if around is not None:
            geometry = merged[allowed]
            others = unary_union([g for a, g in merged.items() if a != allowed])
            described = around.describe(geometry, others)
            pieces = len(getattr(geometry.buffer(JOIN), 'geoms', [None]))
            width = round(2 * geometry.area / geometry.length, 1) if geometry.length else 0
            place = {'sides': described['sides'], 'objects': described['objects'], 'parts': pieces,
                     'place': _place(described, width, area) + (f'; частей: {pieces}' if pieces > 1 else '')}
        tags = Counter()
        for p in props:
            for tag in p['tags']:
                tags[tag] += p['area_m2']
        rules = {}
        for p in props:
            for rule in p['rules']:
                rules[(rule['planting'], rule['object'])] = rule
        # что закрыло деревья и кустарники: нормы с долей площади зоны, где они сработали
        blocked = {}
        for planting in PLANTING_ORDER:
            if planting in allowed:
                continue
            covered = Counter()
            for p in props:
                for key in {(r['object'], r['distance_m'], r['doc']) for r in p['rules'] if r['planting'] == planting}:
                    covered[key] += p['area_m2']
            blocked[planting] = [{'object': o, 'distance_m': d, 'doc': doc, 'share': round(a / area, 2)}
                                 for (o, d, doc), a in covered.most_common()]
        zones.append({
            'allowed': list(allowed),
            'area_m2': round(area, 1),
            'tags': [tag for tag, a in tags.most_common() if a / area >= 0.1],
            'tag_share': {tag: round(a / area, 2) for tag, a in tags.most_common()},
            'rules': sorted(({'planting': r['planting'], 'object': r['object'], 'distance_m': r['distance_m'],
                              'doc': r['doc']} for r in rules.values()),
                            key=lambda r: (r['planting'], r['distance_m'], r['object'])),
            'sections': sorted(p['id'] for p in props),
            'building_m': near,
            'surface': {k: round(v, 2) for k, v in surface.most_common()},
            'blocked_by': blocked,
            'near_ground': any(tags.get(t) for t in GROUND_TAGS),
            **_tree_room(merged[allowed], area, allowed, trees),
            **place,
        })
    for number, zone in enumerate(zones, 1):
        zone['id'] = f'z{number}'
        if not zone['allowed']:
            zone['empty_reason'] = _empty_reason(zone)
    return zones


def _tree_room(geometry, area, allowed, trees):
    """Существующие деревья в зоне и вместимость новых: жадная расстановка по сетке ROOM_STEP с шагом
    TREE_SPACING между новыми и не ближе EXISTING_CLEARANCE к старым стволам — те же правила, что у рассадки."""
    near = [trees.geometries[int(i)] for i in trees.query(geometry.buffer(EXISTING_CLEARANCE), predicate='intersects')] \
        if trees is not None else []
    existing = sum(1 for t in near if geometry.buffer(1.0).contains(t))
    if 'tree' not in allowed:
        return {'existing_trees': existing}
    minx, miny, maxx, maxy = geometry.bounds
    xs = np.arange(minx + ROOM_STEP / 2, maxx, ROOM_STEP)
    ys = np.arange(miny + ROOM_STEP / 2, maxy, ROOM_STEP)
    gx, gy = np.meshgrid(xs, ys)
    inside = shapely.contains_xy(geometry, gx.ravel(), gy.ravel())
    placed = []
    for x, y in zip(gx.ravel()[inside], gy.ravel()[inside]):
        if any((x - t.x) ** 2 + (y - t.y) ** 2 < EXISTING_CLEARANCE ** 2 for t in near):
            continue
        if any((x - px) ** 2 + (y - py) ** 2 < TREE_SPACING ** 2 for px, py in placed):
            continue
        placed.append((x, y))
    return {'existing_trees': existing, 'tree_capacity': len(placed)}


def _patches(sections, buildings, around, trees, playgrounds):
    """Газоны: связные зелёные участки из секций (щели от округления координат не разделяют)."""
    blobs = unary_union([g.buffer(JOIN) for g, _ in sections])
    blobs = list(getattr(blobs, 'geoms', [blobs]))
    blob_index = STRtree(blobs)
    members = defaultdict(list)
    for geometry, props in sections:
        owner = next(int(b) for b in blob_index.query(geometry.representative_point(), predicate='within'))
        members[owner].append((geometry, props))
    tree_index = STRtree(trees) if trees else None
    patches = []
    for parts in members.values():
        geometry = unary_union([g for g, _ in parts])
        area = sum(p['area_m2'] for _, p in parts)
        width = round(2 * geometry.area / geometry.length, 1) if geometry.length else 0
        described = around.describe(geometry)
        patches.append({
            'geometry': geometry, 'area_m2': round(area, 1), 'mean_width_m': width,
            'sides': described['sides'], 'objects': described['objects'], 'place': _place(described, width, area),
            'existing_trees': len(tree_index.query(geometry, predicate='contains')) if tree_index is not None else 0,
            'near_playground': any(geometry.distance(g) <= PLAYGROUND_DISTANCE for g in playgrounds),
            'small': area < MIN_STRUCTURE_M2,
            'zones': _zones(parts, buildings, around, tree_index),
        })
    return patches


def _composition_text(composition):
    return ', '.join(f'{SURFACE_NAMES.get(k, k)} {round(v * 100)}%' for k, v in composition.items()
                     if k in SURFACE_NAMES)


def structures(geojson):
    """Структуры территории с газонами и зонами: [{'id','type','name','territory','area_m2','total_m2',
    'geometry','patches','zones',…}]. area_m2 — площадь газонов, total_m2 — структуры целиком."""
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = [(g, p) for g, p in features if p['layer'] == 'section' and not g.is_empty]
    tag_titles = geojson['metadata']['summary'].get('tags', {})
    playgrounds = [g for g, p in features if p['layer'] == 'structure'
                   and p['type'] in ('playground_children', 'playground_sport')]
    trees = [g for g, p in features if p['layer'] == 'tree']
    surfaces = {p['surface']: g for g, p in features if p['layer'] == 'surface'}
    buildings = surfaces.get('building', unary_union([]))
    territory = unary_union([g for g, p in features if p['layer'] == 'territory'])
    around = Surroundings(features)

    patches = _patches(sections, buildings, around, trees, playgrounds)
    found = blocks.build(territory, surfaces)
    owners = blocks.assign(found, [p['geometry'] for p in patches])
    grouped = defaultdict(list)
    for patch, owner in zip(patches, owners):
        if owner < 0:
            # газон за пределами всех структур (край территории) — сам себе структура
            found.append({'geometry': patch['geometry'], 'core': False})
            owner = len(found) - 1
        grouped[owner].append(patch)

    result = []
    for owner, members in grouped.items():
        geometry = found[owner]['geometry']
        members.sort(key=lambda p: -p['area_m2'])
        zones = []
        for number, patch in enumerate(members, 1):
            patch['id'] = f'u{number}'
            for zone in patch['zones']:
                zone['patch'] = patch['id']
                zones.append(zone)
        for number, zone in enumerate(zones, 1):
            zone['id'] = f'z{number}'
        for patch in members:
            patch['zone_ids'] = [z['id'] for z in patch['zones']]
        area = sum(p['area_m2'] for p in members)
        width = round(2 * geometry.area / geometry.length, 1) if geometry.length else 0
        described = around.describe(geometry)
        minx, miny, maxx, maxy = geometry.bounds
        green = unary_union([p['geometry'] for p in members])
        own = [g for g in playgrounds if g.intersection(geometry).area >= PLAYGROUND_INSIDE * g.area]
        share = (green.intersection(unary_union([g.buffer(PLAYGROUND_DISTANCE) for g in own])).area / green.area
                 if own and green.area else 0.0)
        structure = {
            'area_m2': round(area, 1), 'total_m2': round(geometry.area, 1), 'playground_share': round(share, 2),
            'size_m': [round(maxx - minx), round(maxy - miny)], 'mean_width_m': width,
            'sides': described['sides'], 'objects': around.inside(geometry), 'core': found[owner]['core'],
            'composition': around.composition(geometry),
            'place': _place({'sides': described['sides'], 'objects': {}}, width, geometry.area),
            'existing_trees': sum(p['existing_trees'] for p in members),
            'near_playground': any(p['near_playground'] for p in members),
            'patches': members, 'zones': zones, 'geometry': geometry,
            'green': green, 'tag_titles': tag_titles,
        }
        kind = _classify(structure, zones)
        structure.update(type=kind, name=TYPES[kind][0], territory=TYPES[kind][1])
        result.append(structure)
    result.sort(key=lambda s: -s['area_m2'])
    for number, structure in enumerate(result, 1):
        structure['id'] = f'P{number:04d}'
    return result


def prompt(structure, plants):
    """Текст запроса: структура целиком, её газоны и зоны, отобранные строки справочника."""
    titles = structure['tag_titles']
    lines = [f"Структура {structure['id']}: {structure['name']}, общая площадь {structure['total_m2']} м² "
             f"({_composition_text(structure['composition'])}), газоны {structure['area_m2']} м², "
             f"габарит {structure['size_m'][0]}×{structure['size_m'][1]} м, "
             f"существующих деревьев {structure['existing_trees']} (сохраняются).",
             f"Место: {structure['place']}. Граница структуры: {_sides_text(structure)}."]
    inside = [OBJECTS.get(t, t) + (f' ×{n}' if n > 1 else '') for t, n in structure['objects'].items()]
    if inside:
        lines.append('Внутри: ' + ', '.join(inside) + '.')
    groups = set()
    small = [p for p in structure['patches'] if p['small']]
    for patch in structure['patches']:
        if patch['small']:
            continue
        lines.append(f"\nГазон {patch['id']}: {patch['area_m2']} м², {patch['place']}; "
                     f"граница: {_sides_text(patch)}; существующих деревьев {patch['existing_trees']}.")
        for zone in patch['zones']:
            allowed = ', '.join(PLANTING_NAMES[a] for a in zone['allowed']) or f"ничего — {zone['empty_reason']}"
            tags = '; '.join(f"{titles.get(t, t)} ({round(zone['tag_share'][t] * 100)}%)" for t in zone['tags'])
            trees = ''
            if 'tree' in zone['allowed']:
                building = f", до здания {zone['building_m']:g} м" if zone.get('building_m') is not None else ''
                room = f"место под новые есть (~{zone['tree_capacity']})" if zone['tree_capacity'] else 'места под новые нет'
                trees = (f"; деревья: {room}, уже стоит {zone['existing_trees']}{building}")
            lines.append(f"{zone['id']}: {zone['area_m2']} м², можно: {allowed}" + trees + (f"; условия: {tags}" if tags else ''))
            lines.append(f"  место: {zone['place']}; граница: {_sides_text(zone)}")
            if zone['rules'] and zone['allowed']:
                lines.append('  нормы: ' + _rules_text(zone['rules']))
            for planting in zone['allowed']:
                groups.update(GROUPS[planting])
    if small:
        lines.append(f"\nМелкие газоны (меньше {MIN_STRUCTURE_M2:g} м², решены без подбора — газон): "
                     f"{len(small)} шт., всего {round(sum(p['area_m2'] for p in small), 1)} м².")
    # «дп» убираются из справочника, только если у площадок все зоны, где можно сажать деревья и кустарники
    planted = [z for p in structure['patches'] if not p['small'] for z in p['zones']
               if set(z['allowed']) & set(PLANTING_ORDER)]
    near_all = bool(planted) and all(z['near_ground'] for z in planted)
    rows = catalog.select(plants, groups, structure['territory'], near_all)
    text = '\n'.join(lines)
    return text + '\n\n' + catalog.header() + '\n' + '\n'.join(rows) if rows else text


def _rules_text(rules):
    """«деревья: тротуар 0.7, газопровод 1.5 (743-ПП, табл. 3.6.1); кустарники: …» — коротко для запроса."""
    parts = []
    for planting in ('tree', 'shrub'):
        by_doc = defaultdict(list)
        for rule in rules:
            if rule['planting'] == planting:
                short = re.split(r'[,(]', rule['object'])[0].strip().lower()
                by_doc[rule['doc']].append(f"{short} {rule['distance_m']:g}")
        if by_doc:
            parts.append(PLANTING_NAMES[planting] + ': ' + '; '.join(
                f"{', '.join(items)} ({doc})" for doc, items in by_doc.items()))
    return ' | '.join(parts)


class LLM:
    def __init__(self, env, model=None, cache_dir=None):
        if not env.get('BASE_URL') or not env.get('API_KEY'):
            raise RuntimeError('В .env нет BASE_URL или API_KEY')
        self.url = env['BASE_URL'].rstrip('/') + '/chat/completions'
        self.key = env['API_KEY']
        self.model = model or env.get('LLM_MODEL') or DEFAULT_MODEL
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)
        # запросов за прогон, вместе с ответами из кэша
        self.requests = 0
        self._lock = threading.Lock()

    def ask(self, text, retries=3, system=SYSTEM):
        """JSON-ответ модели; одинаковые запросы берутся из кэша, чтобы не платить дважды."""
        with self._lock:
            self.requests += 1
        key = hashlib.sha256(f'{self.model}\n{system}\n{text}'.encode()).hexdigest()[:24]
        cached = self.cache / f'{key}.json' if self.cache else None
        if cached and cached.exists():
            # из кэша — без расхода токенов: в сводке считаются только настоящие запросы
            answer = json.loads(cached.read_text(encoding='utf-8'))
            answer['_usage'] = {'cached_requests': 1}
            return answer
        # json_object — модель не ломает JSON кавычками в тексте; max_tokens — длинный ответ не обрезается
        body = {'model': self.model, 'temperature': 0.3, 'max_tokens': MAX_TOKENS,
                'response_format': {'type': 'json_object'},
                'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}]}
        last = None
        for attempt in range(retries):
            try:
                request = urllib.request.Request(self.url, data=json.dumps(body).encode(), method='POST', headers={
                    'Authorization': f'Bearer {self.key}', 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request, timeout=300) as response:
                    reply = json.load(response)
                content = reply['choices'][0]['message']['content']
                answer = _parse(content)
                answer['_usage'] = reply.get('usage', {})
                if cached:
                    cached.write_text(json.dumps(answer, ensure_ascii=False), encoding='utf-8')
                return answer
            except urllib.error.HTTPError as exc:
                # текст ошибки прокси объясняет причину (лимит цены, нет модели), без него 422 ничего не говорит
                last = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
                if exc.code in (400, 401, 403, 404, 422):
                    break
                time.sleep(2 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
                last = exc
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f'LLM не ответила: {last}')


def _parse(content):
    """JSON из ответа: модели иногда оборачивают его в ```json … ```."""
    match = re.search(r'\{.*\}', content, re.S)
    if not match:
        raise ValueError('В ответе нет JSON')
    return json.loads(match.group(0))


def _check(answer, structure, plants):
    """Проверяет посадки и добавляет нормативное обоснование по зоне (norms), свойства (facts)
    и противоречия текста справочнику (warnings). Отбрасывает посадки с несуществующим id,
    группой, не разрешённой в зоне, или нарушающие нормы (широкая крона у здания, детская площадка)."""
    by_id = {p['id']: p for p in plants}
    zones = {z['id']: z for z in structure['zones']}
    kept, rejected = [], []
    for item in answer.get('plantings', []):
        plant, zone = by_id.get(item.get('plant_id')), zones.get(item.get('zone'))
        reason = None
        if plant is None:
            reason = 'нет в справочнике'
        elif zone is None:
            reason = 'нет такой зоны'
        elif not any(plant['group'] in GROUPS[a] for a in zone['allowed']):
            reason = 'группа не разрешена в зоне'
        elif zone['near_ground'] and 'дп' in plant['flags']:
            reason = 'нельзя у детских площадок'
        lines = []
        if not reason:
            lines, reason = justify.norms(plant, zone, structure)
            # широкая крона у здания — не повод оставить зону без дерева: компактное дерево той же роли
            if reason and reason.startswith('широкая крона'):
                substitute = _compact_tree(zone, structure, plants, answer)
                if substitute is not None:
                    rejected.append({**item, 'rejected': reason, 'replaced_by': substitute['name']})
                    item = {**item, 'plant_id': substitute['id'], 'substituted_from': plant['name'],
                            'reason': f"{item.get('reason', '')} Заменено на {substitute['name']}: у {plant['name'].lower()} "
                                      f"широкая крона, а до здания меньше 10 м.".strip()}
                    plant = substitute
                    lines, reason = justify.norms(plant, zone, structure)
        if reason:
            rejected.append({**item, 'rejected': reason})
        else:
            item = {k: v for k, v in item.items() if k != 'quantity'}
            item['unit'] = 'м2' if plant['group'] in GROUPS['herbaceous'] else 'шт'
            kept.append({**item, 'name': plant['name'], 'group': plant['group'], 'norms': lines,
                         'facts': justify.facts(plant), 'warnings': justify.warnings(item.get('reason', ''), plant)})
    return kept, rejected


def _compact_tree(zone, structure, plants, answer=None):
    """Дерево без широкой кроны, подходящее зоне: сначала из выбранных LLM для этой структуры, потом по правилам."""
    by_id = {p['id']: p for p in plants}
    chosen = [by_id.get(i.get('plant_id')) for i in (answer or {}).get('plantings', [])]
    candidates = [p for p in chosen if p and p['group'] in GROUPS['tree'] and 'шк' not in p['flags']]
    candidates += sorted((p for p in plants if p['group'] in GROUPS['tree'] and 'шк' not in p['flags']),
                         key=lambda p: (-rules._score(p, 'roadside' in zone['tags'], bool(set(zone['tags']) & set(rules.SHADE_TAGS))), p['id']))
    return next((p for p in candidates if rules._fits(p, zone, structure['territory'])), None)


def _backfill_trees(structure, kept, plants, explained):
    """Деревья — основной ярус: зона, где деревья разрешены и есть место под новые, а LLM дерево не выбрала,
    получает дерево правилами — вид из палитры структуры, иначе компактный по правилам. Количество
    считает рассадка. Возвращает добавленные посадки."""
    by_id = {p['id']: p for p in plants}
    trees = [i for i in kept if i.get('group') in GROUPS['tree'] and i.get('plant_id') in by_id]
    with_trees = {i['zone'] for i in trees}
    palette = [by_id[i['plant_id']] for i in trees]
    added = []
    for patch in structure['patches']:
        if patch['small']:
            continue
        for zone in patch['zones']:
            if 'tree' not in zone['allowed'] or zone.get('tree_capacity', 0) < 1 or zone['id'] in with_trees:
                continue
            plant = next((p for p in palette if 'шк' not in p['flags'] and rules._fits(p, zone, structure['territory'])),
                         None) or _compact_tree(zone, structure, plants)
            if plant is None:
                continue
            lines, reason = justify.norms(plant, zone, structure)
            if reason:
                continue
            source = 'вид, уже выбранный для этой структуры' if plant in palette else 'компактный вид, подходящий условиям места'
            added.append({'zone': zone['id'], 'plant_id': plant['id'],
                          'role': 'группа' if zone['tree_capacity'] >= 3 else 'солитер', 'unit': 'шт', 'by': 'rules',
                          'name': plant['name'], 'group': plant['group'],
                          'reason': f"Дерево добавлено правилами: в зоне можно сажать деревья и есть место под новые "
                                    f"(уже стоит {zone['existing_trees']}), а деревья — основной ярус озеленения; "
                                    f"{plant['name']} — {source}.",
                          'norms': lines, 'facts': justify.facts(plant), 'warnings': []})
            note = f" Дополнительно: {plant['name'].lower()} (дерево добавлено правилами, в зоне есть место)."
            if zone['id'] in explained:
                explained[zone['id']] = {**explained[zone['id']], 'decision': explained[zone['id']]['decision'] + note}
    return added


def _explain(answer, structure):
    """Объяснения мест по зонам из ответа LLM: {id зоны: {'place', 'decision'}}."""
    explained = {}
    for item in answer.get('zones', []):
        if isinstance(item, dict) and item.get('zone') and item.get('decision'):
            explained[item['zone']] = {'place': item.get('place', ''), 'decision': item['decision'],
                                       **{k: item[k] for k in ('by', 'class') if k in item}}
    return explained


def _lawn(zones, structure, plants):
    """Мелкие газоны без LLM: газон там, где разрешены травянистые, и объяснение каждой зоны кодом."""
    by_id = {p['id']: p for p in plants}
    plantings, explained = [], {}
    note = f"газон меньше {MIN_STRUCTURE_M2:g} м², ассортимент не подбирается"
    for zone in zones:
        if 'herbaceous' not in zone['allowed']:
            reason = zone.get('empty_reason') or 'травянистые здесь не разрешены, а деревья и кустарники на таком малом участке не размещаются'
            explained[zone['id']] = {'place': zone['place'], 'decision': f'Без посадок: {reason}.', 'by': 'code'}
            continue
        shade = any(t in zone['tags'] for t in SHADE_TAGS)
        plant = by_id[SHADE_LAWN if shade else LAWN]
        reason = (f"{note[0].upper() + note[1:]}: сплошной газон"
                  + (' теневыносливый — место в тени здания или крон' if shade else '') + '.')
        lines, _ = justify.norms(plant, zone, structure)
        plantings.append({'zone': zone['id'], 'plant_id': plant['id'], 'role': 'газон', 'unit': 'м2', 'reason': reason, 'name': plant['name'], 'group': plant['group'],
                          'norms': lines, 'facts': justify.facts(plant), 'warnings': [], 'by': 'code'})
        explained[zone['id']] = {'place': zone['place'], 'decision': f"Газон: {note}.", 'by': 'code'}
    return plantings, explained


STRUCTURE_FIELDS = ('id', 'type', 'name', 'place', 'sides', 'objects', 'composition', 'area_m2', 'total_m2',
                    'size_m', 'mean_width_m', 'existing_trees', 'near_playground', 'playground_share')
PATCH_FIELDS = ('id', 'area_m2', 'place', 'sides', 'objects', 'existing_trees', 'mean_width_m', 'small', 'zone_ids')
ZONE_FIELDS = ('id', 'patch', 'allowed', 'area_m2', 'surface', 'place', 'sides', 'objects', 'parts', 'tags', 'rules',
               'blocked_by', 'near_ground', 'building_m', 'existing_trees', 'tree_capacity', 'sections', 'empty_reason')


def record_of(structure):
    """Структура без геометрии — для plan.json и файлов split."""
    record = {k: structure[k] for k in STRUCTURE_FIELDS}
    record['patches'] = [{k: p[k] for k in PATCH_FIELDS} for p in structure['patches']]
    record['zones'] = [{k: z[k] for k in ZONE_FIELDS if k in z} for z in structure['zones']]
    return record


FOLLOW_UPS = 2
# зон больше этого — структура спрашивается частями: длинный ответ модели ломается или обрезается
ZONES_PER_REQUEST = 30
MAX_TOKENS = 32000
FIRST_PART = """

Зон много, поэтому ответ по частям. Сейчас верни посадки и объяснения только для зон: {zones};
остальные запрошу следующими сообщениями. "justification" — замысел для всей структуры."""
FOLLOW_UP = """

Продолжение по этой структуре: ещё не объяснены зоны {zones}. Верни JSON в том же формате только для них:
посадки ("plantings", если в зоне что-то сажаешь) и объяснение каждой ("zones"); "justification" — пустая строка.{chosen}"""
CHOSEN = """
Для структуры уже выбраны: {names}. Держись этого ассортимента — у структуры должен быть единый стиль."""


def _chosen(answer):
    names = []
    for item in answer.get('plantings', []):
        name = item.get('plant_id')
        if name and name not in names:
            names.append(name)
    return CHOSEN.format(names=', '.join(names)) if names else ''


def _missing(answer, structure):
    """Зоны без объяснения, кроме мелких газонов (их закрывает код)."""
    explained = _explain(answer, structure)
    return [z['id'] for p in structure['patches'] if not p['small'] for z in p['zones'] if z['id'] not in explained]


def _merge(answer, extra):
    merged = dict(answer)
    merged['plantings'] = answer.get('plantings', []) + extra.get('plantings', [])
    merged['zones'] = answer.get('zones', []) + extra.get('zones', [])
    usage = Counter({k: v for k, v in answer.get('_usage', {}).items() if isinstance(v, int)})
    usage.update({k: v for k, v in extra.get('_usage', {}).items() if isinstance(v, int)})
    merged['_usage'] = dict(usage)
    return merged


# структура больше этого числа зон спрашивается отдельно (композиция продумывается целиком);
# меньшие — через классы мест, один запрос на тип структуры
SEPARATE_ZONES = 30
# классы мест включаются, только если по структурам вышло бы не меньше стольких запросов:
# на небольшом участке запрос на каждую структуру дешёв, а объяснения по зонам — точнее
CLASS_MIN_REQUESTS = 15
# цветник в режиме классов — только в зоне не меньше этого (как MIN_ZONE['bed'] в rules.py), м²
BED_MIN_M2 = rules.MIN_ZONE['bed']
# классов в одном ответе: длиннее ответ ломается, как и по зонам
CLASSES_PER_REQUEST = 40
# условия места, от которых зависит выбор вида; люки, заборы, сети уже учтены нормами разметки
# (площадки — отдельным признаком near_ground: запрет «дп» действует при любой доле площади у площадки)
CLASS_TAGS = ('roadside', 'existing_trees', 'near_building', 'under_power_line', 'along_path', 'slope')
NEAR_GROUND = 'рядом детская или спортивная площадка (без ядовитых и колючих)'
FIRST_CLASSES = """

Классов много, поэтому ответ по частям. Сейчас верни решения только для классов: {classes};
остальные запрошу следующими сообщениями. "justification" — замысел для всего типа."""
FOLLOW_CLASSES = """

Продолжение: ещё не решены классы {classes}. Верни JSON в том же формате только для них; "justification" — пустая строка.{chosen}"""
CHOSEN_TYPE = """
Для этого типа уже выбраны: {names}. Держись этого ассортимента — у всех структур типа должен быть единый стиль."""


def _class_key(zone):
    """Что определяет выбор вида: разрешённые посадки, значимые условия, место под деревья, близость здания."""
    tree = 'tree' in zone['allowed']
    near = zone.get('building_m') is not None and zone['building_m'] < justify.WIDE_CROWN_M
    return (tuple(sorted(zone['allowed'])), tuple(t for t in CLASS_TAGS if t in zone['tags']),
            tree and bool(zone.get('tree_capacity')), tree and near, zone['near_ground'])


def _classes(members):
    """Классы мест по структурам одного типа: [{'id': 'c1', 'key', 'zones': [(структура, зона)]}], крупные сначала.
    Зоны, где ничего нельзя, в классы не входят — их объясняет код по нормам."""
    found = defaultdict(list)
    for structure in members:
        for patch in structure['patches']:
            if patch['small']:
                continue
            for zone in patch['zones']:
                if zone['allowed']:
                    found[_class_key(zone)].append((structure, zone))
    ordered = sorted(found.items(), key=lambda kv: -sum(z['area_m2'] for _, z in kv[1]))
    return [{'id': f'c{n}', 'key': key, 'zones': zones} for n, (key, zones) in enumerate(ordered, 1)]


def _generic_place(text):
    """Место без размеров и соседних объектов — чтобы одинаковые места считались вместе."""
    text = text.split(';')[0]
    return re.sub(r' шириной ~[\d.]+ м', '', text)


def type_prompt(kind, members, classes, plants):
    """Запрос по типу: сводка по структурам типа, классы мест и справочник один раз."""
    titles = members[0]['tag_titles']
    places = Counter(_generic_place(s['place']) for s in members)
    lines = [f"Тип территории: {TYPES[kind][0]}. Структур {len(members)}, газоны всего "
             f"{round(sum(s['area_m2'] for s in members), 1)} м², существующих деревьев "
             f"{sum(s['existing_trees'] for s in members)} (сохраняются).",
             'Где находятся: ' + '; '.join(f'{p} ×{n}' for p, n in places.most_common(5)) + '.']
    groups = set()
    for cls in classes:
        allowed, tags, room, near, ground = cls['key']
        zones = [z for _, z in cls['zones']]
        areas = [z['area_m2'] for z in zones]
        structures_in = len({s['id'] for s, _ in cls['zones']})
        head = (f"\n{cls['id']}: зон {len(zones)} в {structures_in} структурах, {round(sum(areas), 1)} м² "
                f"(от {min(areas):g} до {max(areas):g} м²); можно: " + ', '.join(PLANTING_NAMES[a] for a in allowed))
        if 'tree' in allowed:
            head += '; деревья: ' + ('место под новые есть' if room else 'места под новые нет')
            head += ', ближе 10 м к зданию — только компактные' if near else ''
        conditions = [titles.get(t, t) for t in tags] + ([NEAR_GROUND] if ground else [])
        if conditions:
            head += '; условия: ' + ', '.join(conditions)
        lines.append(head)
        typical = Counter(_generic_place(z['place']) for z in zones)
        lines.append('  места: ' + '; '.join(f'{p} ×{n}' for p, n in typical.most_common(3)))
        norms = Counter(_rules_text(z['rules']) for z in zones if z['rules'])
        if norms:
            lines.append('  нормы (чаще всего): ' + norms.most_common(1)[0][0])
        for planting in allowed:
            groups.update(GROUPS[planting])
    rows = catalog.select(plants, groups, TYPES[kind][1])
    text = '\n'.join(lines)
    return text + '\n\n' + catalog.header() + '\n' + '\n'.join(rows) if rows else text


def _class_answers(answer):
    """{id класса: запись ответа} — только классы с решением."""
    return {item['class']: item for item in answer.get('classes', [])
            if isinstance(item, dict) and item.get('class') and item.get('decision')}


def _merge_type(answer, extra):
    merged = {'classes': answer.get('classes', []) + extra.get('classes', []),
              'justification': answer.get('justification') or extra.get('justification', '')}
    usage = Counter({k: v for k, v in answer.get('_usage', {}).items() if isinstance(v, int)})
    usage.update({k: v for k, v in extra.get('_usage', {}).items() if isinstance(v, int)})
    merged['_usage'] = dict(usage)
    return merged


def _chosen_type(answer):
    names = []
    for item in answer.get('classes', []):
        for planting in item.get('plantings', []) if isinstance(item, dict) else []:
            for name in planting.get('plant_ids') or []:
                if name not in names:
                    names.append(name)
    return CHOSEN_TYPE.format(names=', '.join(names)) if names else ''


def ask_type(llm, kind, members, classes, plants, log=print):
    """Решения по классам мест одного типа; крупный тип — частями, пропущенные классы — дозапросом."""
    text = type_prompt(kind, members, classes, plants)
    wanted = [c['id'] for c in classes]
    answer = {}
    for attempt in range(math.ceil(len(wanted) / CLASSES_PER_REQUEST) + FOLLOW_UPS):
        missing = [c for c in wanted if c not in _class_answers(answer)]
        if not missing:
            break
        batch = missing[:CLASSES_PER_REQUEST]
        if not attempt:
            suffix = '' if len(wanted) <= CLASSES_PER_REQUEST else FIRST_CLASSES.format(classes=', '.join(batch))
        else:
            log(f"{TYPES[kind][0]}: дозапрос по {len(batch)} классам из {len(missing)}")
            suffix = FOLLOW_CLASSES.format(classes=', '.join(batch), chosen=_chosen_type(answer))
        answer = _merge_type(answer, llm.ask(text + suffix, system=SYSTEM_TYPE))
    return answer


def _one_bed(plantings, zones, structure, by_id):
    """Цветник класса — один на структуру, в самой крупной зоне не меньше BED_MIN_M2: решение класса
    ложится на все его зоны, и иначе цветником стала бы каждая полоска газона (дорогой уход).
    В остальных зонах вместо цветника — газон, причина дописывается в объяснение зоны."""
    area = {z['id']: z for p in structure['patches'] for z in p['zones']}
    beds = [p for p in plantings if p['role'] == 'цветник']
    fit = [p for p in beds if area[p['zone']]['area_m2'] >= BED_MIN_M2]
    keep = max(fit, key=lambda p: area[p['zone']]['area_m2'])['zone'] if fit else None
    explained = {z['zone']: z for z in zones}
    for bed in beds:
        if bed['zone'] == keep:
            continue
        plantings.remove(bed)
        zone = area[bed['zone']]
        if not any(p['zone'] == zone['id'] and p['role'] == 'газон' for p in plantings):
            lawn = by_id[SHADE_LAWN if set(zone['tags']) & set(SHADE_TAGS) else LAWN]
            plantings.append({'zone': zone['id'], 'plant_id': lawn['id'], 'role': 'газон', 'class': bed['class'],
                              'reason': 'Газон вместо цветника: цветник в структуре один, на самом крупном участке.'})
        why = (f'зона меньше {BED_MIN_M2:g} м²' if zone['area_m2'] < BED_MIN_M2
               else 'цветник в структуре один — на самом крупном подходящем участке')
        note = explained.get(zone['id'])
        if note and 'цветник' not in note.get('bed_note', ''):
            note['decision'] += f' Цветник здесь не устраивается ({why}) — газон.'
            note['bed_note'] = 'цветник'
    for note in zones:
        note.pop('bed_note', None)


def _from_classes(structure, index, classes, answer, plants):
    """Ответ по одной структуре из решений классов — в формате SYSTEM, чтобы дальше работали те же проверки.
    index — номер структуры в типе: с него начинается чередование взаимозаменяемых видов."""
    by_id = {p['id']: p for p in plants}
    decided = _class_answers(answer)
    owner = {id(z): c['id'] for c in classes for _, z in c['zones']}
    plantings, zones = [], []
    for patch in structure['patches']:
        if patch['small']:
            continue
        for zone in patch['zones']:
            if not zone['allowed']:
                zones.append({'zone': zone['id'], 'place': zone['place'], 'by': 'code',
                              'decision': f"Без посадок: {zone['empty_reason']}."})
                continue
            cls = owner[id(zone)]
            item = decided.get(cls)
            if item is None:
                continue
            for planting in item.get('plantings', []):
                if not isinstance(planting, dict):
                    continue
                variants = planting.get('plant_ids') or [planting.get('plant_id')]
                known = [v for v in variants if v in by_id]
                if known:
                    shift = index % len(known)
                    order = known[shift:] + known[:shift]
                    fits = [v for v in order if rules._fits(by_id[v], zone, structure['territory'])
                            and not (zone['near_ground'] and 'дп' in by_id[v]['flags'])]
                    pick = fits[0] if fits else order[0]
                else:
                    pick = variants[0]  # отбросит _check: «нет в справочнике»
                plantings.append({'zone': zone['id'], 'plant_id': pick, 'role': planting.get('role', ''),
                                  'reason': planting.get('reason', ''), 'class': cls})
            zones.append({'zone': zone['id'], 'place': zone['place'], 'decision': item['decision'],
                          'by': 'class', 'class': cls})
    _one_bed(plantings, zones, structure, by_id)
    common = answer.get('justification', '')
    note = (f"Структура решена по общему ассортименту для всех структур типа «{structure['name']}» на участке: "
            f"зоны с одинаковыми условиями получают одно решение, виды чередуются между структурами.")
    return {'plantings': plantings, 'zones': zones, 'justification': f'{common} {note}'.strip()}


def recommend(geojson, llm, limit=None, workers=5, log=print, progress=None):
    """progress(готово, всего) — после каждой структуры, отправленной в LLM.

    llm=None — подбор правилами (rules.py) для всех структур; если LLM не ответила по структуре,
    для неё тоже работают правила, а ошибка остаётся в записи (llm_error)."""
    plants = catalog.load()
    found = structures(geojson)
    # в LLM идут все структуры, где есть газон не меньше порога, даже если сажать нельзя ничего:
    # место всё равно нужно объяснить; мелкие газоны закрываются кодом
    asked = [s for s in found if any(not p['small'] for p in s['patches'])]
    if limit:
        asked = asked[:limit]
    ids = {s['id'] for s in asked}

    def run(structure):
        if llm is None:
            return [(structure, rules.answer(structure, plants), None)]
        text = prompt(structure, plants)
        wanted = _missing({}, structure)
        try:
            first = text if len(wanted) <= ZONES_PER_REQUEST else \
                text + FIRST_PART.format(zones=', '.join(wanted[:ZONES_PER_REQUEST]))
            answer = llm.ask(first)
            # крупная структура — частями по ZONES_PER_REQUEST; пропущенные моделью зоны — дозапросом
            for _ in range(math.ceil(len(wanted) / ZONES_PER_REQUEST) + FOLLOW_UPS):
                missing = _missing(answer, structure)
                if not missing:
                    break
                batch = missing[:ZONES_PER_REQUEST]
                log(f"{structure['id']}: дозапрос по {len(batch)} зонам из {len(missing)}")
                extra = llm.ask(text + FOLLOW_UP.format(zones=', '.join(batch), chosen=_chosen(answer)))
                answer = _merge(answer, extra)
        except RuntimeError as exc:
            log(f"{structure['id']}: {exc} — подбор правилами")
            return [(structure, rules.answer(structure, plants), str(exc))]
        log(f"{structure['id']} ({structure['name']}, {structure['area_m2']} м²): готово")
        return [(structure, answer, None)]

    def run_type(kind, members):
        classes = _classes(members)
        try:
            answer = ask_type(llm, kind, members, classes, plants, log)
        except RuntimeError as exc:
            log(f"{TYPES[kind][0]}: {exc} — подбор правилами")
            return [(s, rules.answer(s, plants), str(exc)) for s in members]
        log(f"{TYPES[kind][0]} ({len(members)} структур, {len(classes)} классов мест): готово")
        usage = answer.pop('_usage', {})
        results = [(s, _from_classes(s, n, classes, answer, plants), None) for n, s in enumerate(members)]
        # расход токенов — один раз на тип, а не на каждую структуру
        if results:
            results[0][1]['_usage'] = usage
        return results

    # без LLM, на небольшом участке и крупные структуры — по одной; остальные — один запрос на тип
    per_structure = sum(max(1, math.ceil(len(_missing({}, s)) / ZONES_PER_REQUEST)) for s in asked)
    by_classes = llm is not None and per_structure >= CLASS_MIN_REQUESTS
    separate = [s for s in asked if not by_classes or len(_missing({}, s)) > SEPARATE_ZONES]
    log(f"структур для подбора {len(asked)}, по структурам вышло бы запросов {per_structure}: "
        + ('крупные — по структурам, остальные — по классам мест' if by_classes else 'все по структурам'))
    alone = {s['id'] for s in separate}
    by_type = defaultdict(list)
    for structure in asked:
        if structure['id'] not in alone:
            by_type[structure['type']].append(structure)
    scope = {s['id']: 'structure' for s in separate}
    scope.update({s['id']: 'type' for members in by_type.values() for s in members})

    answers = {}
    if progress:
        progress(0, len(asked))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        # типы — первыми: в них больше структур
        futures = [pool.submit(run_type, kind, members) for kind, members in by_type.items()]
        futures += [pool.submit(run, s) for s in separate]
        done = 0
        for future in as_completed(futures):
            for structure, answer, error in future.result():
                answers[structure['id']] = (answer, error)
                done += 1
            if progress:
                progress(done, len(asked))
    except BaseException:
        # progress может прервать подбор (задачу бросили): ещё не начатые структуры не запускаются,
        # уже отправленные запросы дорабатывают в фоне (их ответы попадут в кэш)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()

    output, usage = [], Counter()
    for structure in found:
        record = record_of(structure)
        small = [z for p in structure['patches'] if p['small'] for z in p['zones']]
        plantings, explained = _lawn(small, structure, plants)
        if all(p['small'] for p in structure['patches']):
            record.update(status='small', justification=f"{structure['place'][0].upper() + structure['place'][1:]}: "
                                                        f"газоны {structure['area_m2']} м², все меньше {MIN_STRUCTURE_M2:g} м², "
                                                        'решение принято по разрешённым посадкам без LLM.')
        elif structure['id'] not in ids:
            record.update(status='skipped', note='Не запрашивалась (limit)')
        else:
            answer, error = answers[structure['id']]
            usage.update({k: v for k, v in answer.pop('_usage', {}).items() if isinstance(v, int)})
            kept, rejected = _check(answer, structure, plants)
            explained.update(_explain(answer, structure))
            kept += _backfill_trees(structure, kept, plants, explained)
            plantings = kept + plantings
            # rules — подбор правилами: LLM не передана или не ответила (тогда причина в llm_error)
            record.update(status='rules' if llm is None or error else 'ok',
                          justification=answer.get('justification', ''))
            if llm is not None and not error:
                # structure — запрос по структуре, type — решение по классам мест для всего типа
                record['scope'] = scope[structure['id']]
            if error:
                record['llm_error'] = error
            if rejected:
                record['rejected'] = rejected
        record['plantings'] = plantings
        for zone in record['zones']:
            if zone['id'] in explained:
                zone['explanation'] = explained[zone['id']]
        missing = [z['id'] for z in record['zones'] if 'explanation' not in z]
        if missing:
            record['unexplained_zones'] = missing
        output.append(record)
    zones = [z for r in output for z in r['zones']]
    # название модели в результат не пишется (решение пользователя): прокси подставляет разные модели
    return {'source': 'llm' if llm is not None else 'rules', 'structures': output,
            'summary': {'structures': len(found), 'asked': len(asked),
                        'requests': llm.requests if llm is not None else 0,
                        'by_type': sum(v == 'type' for v in scope.values()),
                        'ok': sum(r.get('status') == 'ok' for r in output),
                        'rules': sum(r.get('status') == 'rules' for r in output),
                        'small': sum(r.get('status') == 'small' for r in output),
                        'patches': sum(len(r['patches']) for r in output),
                        'zones': len(zones), 'zones_explained': sum('explanation' in z for z in zones),
                        'unexplained_m2': round(sum(z['area_m2'] for z in zones if 'explanation' not in z), 1),
                        'usage': dict(usage)}}


def main():
    parser = argparse.ArgumentParser(description='Подбор растений LLM по структурам территории')
    parser.add_argument('geojson', help='результат python -m greening (*.greening.geojson)')
    parser.add_argument('--out', help='куда записать JSON (по умолчанию рядом с разметкой)')
    parser.add_argument('--model', help=f'модель (по умолчанию LLM_MODEL из .env или {DEFAULT_MODEL})')
    parser.add_argument('--limit', type=int, help='запросить только N крупнейших структур')
    parser.add_argument('--workers', type=int, default=5)
    parser.add_argument('--rules', action='store_true', help='без LLM: подбор правилами (rules.py)')
    args = parser.parse_args()

    source = Path(args.geojson)
    out = Path(args.out) if args.out else source.with_name(source.name.replace('.greening.geojson', '') + '.plan.json')
    geojson = json.loads(source.read_text(encoding='utf-8'))
    llm = None if args.rules else LLM(load_env(), args.model, cache_dir=out.parent / '.llm_cache')
    result = recommend(geojson, llm, args.limit, args.workers, log=lambda m: print(m, file=sys.stderr, flush=True))
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'out': str(out), **result['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
