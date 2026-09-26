"""Выноски и стрелки на слоях сетей: оформление подписей, а не трассы.

Сети в генплане лежат в блоках вместе с подписями: от текста идёт полка и линия-выноска со стрелкой
к трассе. Все они на слое сети, и без фильтра по ним строился бы нормативный отступ.

Линии слоя собираются в граф по совпадающим концам. Выноска — путь от свободного конца (узел степени 1)
через узлы степени 2 до первого ветвления (острие на трассе), если у начала есть подпись того же слоя
или у острия — стрелка: два и больше коротких отрезка со свободным вторым концом (подпись бывает
в атрибутах блока, и тогда её не видно). Путь длиннее MAX_LENGTH — это уже трасса.
"""

from collections import defaultdict

from shapely import STRtree
from shapely.geometry import Point

# концы ближе этого — один узел, м
SNAP = 0.05
# свободный конец выноски не дальше этого от точки вставки подписи, м (подпись длиннее полки не бывает)
NEAR_TEXT = 1.5
# выноска с полкой длиннее этого не встречалась; длиннее — трасса, м
MAX_LENGTH = 15.0
# «перо» стрелки, м
ARROW = 1.5


def _key(point):
    return round(point[0] / SNAP), round(point[1] / SNAP)


def find(items, kinds):
    """Индексы элементов items классов kinds, которые являются выносками или стрелками подписей."""
    lines, texts = defaultdict(list), defaultdict(list)
    for n, item in enumerate(items):
        if item.etype in ('TEXT', 'MTEXT'):
            texts[(item.layer, item.source)].append(item.geometry)
        elif item.kind in kinds and item.geometry.geom_type == 'LineString' and not item.geometry.is_ring:
            lines[(item.layer, item.source)].append(n)

    found = set()
    for key, members in lines.items():
        text_index = STRtree(texts[key]) if key in texts else None
        nodes = defaultdict(list)          # узел → [(элемент, другой конец)]
        for n in members:
            coords = items[n].geometry.coords
            a, b = _key(coords[0]), _key(coords[-1])
            nodes[a].append((n, b))
            nodes[b].append((n, a))
        for start, edges in nodes.items():
            if len(edges) != 1:
                continue
            point = Point(start[0] * SNAP, start[1] * SNAP)
            labelled = text_index is not None and len(text_index.query(point, predicate='dwithin', distance=NEAR_TEXT))
            path, length, node, previous = [], 0.0, start, None
            while True:
                step = [(n, other) for n, other in nodes[node] if n != previous]
                if len(nodes[node]) > 2 or not step:
                    break
                n, other = step[0]
                path.append(n)
                length += items[n].geometry.length
                previous, node = n, other
                if length > MAX_LENGTH:
                    break
            if not path or length > MAX_LENGTH or len(nodes[node]) == 1:
                # путь до другого свободного конца — отдельный отрезок, не выноска
                continue
            feathers = [n for n, other in nodes[node]
                        if n not in path and len(nodes[other]) == 1 and items[n].geometry.length <= ARROW]
            if len(feathers) >= 2 or (labelled and path):
                found.update(path)
                found.update(feathers if len(feathers) >= 2 else ())
    return found
