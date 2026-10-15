"""Параметры рассадки, которые пользователь меняет при редактировании результата (уровень 2).

Каждый параметр — значение из config/placement.json (путь path), с пределами и подписью. Одно описание
служит и проверке (apply), и форме на сайте (schema → GET /params/). Нормы параметрами не меняются:
растения встают только в зоны, где их вид разрешён, verify() и проверка отступов остаются.

mode — в каком режиме рассадки параметр действует: both — в обоих (каркас рассадки общий: зоны, правила
расстояний, группы, изгороди, бюджет кустов), model — только при выборе мест моделью, algorithm — только
эвристикой. Режим и seed — тоже параметры (placement, seed).
"""

import copy

from . import place

GROUPS = (
    ('mode', 'Режим'),
    ('density', 'Плотность и просветы'),
    ('groups', 'Группы'),
    ('amount', 'Сколько сажать'),
    ('existing', 'Существующие деревья'),
    ('style', 'Стиль'),
    ('model', 'Модель'),
    ('algorithm', 'Алгоритм'),
)

# key — имя в API; path — путь в placement.json (None — не конфиг); kind — float | int | sizes | choice
SPEC = [
    {'key': 'placement', 'group': 'mode', 'kind': 'choice', 'path': None, 'mode': 'both', 'default': None,
     'choices': [['algorithm', 'алгоритм'], ['model', 'модель']],
     'title': 'Места посадки', 'help': 'Кто выбирает точки среди разрешённых нормами: эвристика или обученная модель'},
    {'key': 'seed', 'group': 'mode', 'kind': 'int', 'path': None, 'mode': 'both', 'default': 0, 'min': 0, 'max': 999999,
     'title': 'Seed', 'help': 'Число, от которого зависят случайные решения рассадки: тот же seed — тот же результат, другой — другой вариант при тех же параметрах'},

    {'key': 'shrub_share', 'group': 'density', 'kind': 'float', 'path': ['shrub_share'], 'mode': 'both',
     'min': 0.4, 'max': 1.5, 'step': 0.05, 'unit': '× кроны',
     'title': 'Кучность кустов в группе', 'help': 'Шаг между кустами одной группы в долях кроны: меньше — плотнее'},
    {'key': 'hedge_share', 'group': 'density', 'kind': 'float', 'path': ['hedge_share'], 'mode': 'both',
     'min': 0.2, 'max': 1.0, 'step': 0.05, 'unit': '× кроны',
     'title': 'Шаг живой изгороди', 'help': 'Шаг куста в изгороди в долях кроны'},
    {'key': 'hedge_max_m', 'group': 'density', 'kind': 'float', 'path': ['hedge_max_m'], 'mode': 'both',
     'min': 0.3, 'max': 2.0, 'step': 0.1, 'unit': 'м',
     'title': 'Шаг изгороди не больше', 'help': 'Предел шага изгороди для крупных кустов'},
    {'key': 'tree_unit_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_unit_spacing_m'], 'mode': 'both',
     'min': 4, 'max': 25, 'step': 0.5, 'unit': 'м',
     'title': 'Просвет между деревьями и группами', 'help': 'Между отдельными деревьями, группами и рядами: больше — разреженнее'},
    {'key': 'tree_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_spacing_m'], 'mode': 'both',
     'min': 3, 'max': 12, 'step': 0.5, 'unit': 'м',
     'title': 'Шаг деревьев в группе', 'help': 'Не меньше 0,8 кроны вида'},
    {'key': 'tree_row_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_row_spacing_m'], 'mode': 'both',
     'min': 3, 'max': 15, 'step': 0.5, 'unit': 'м',
     'title': 'Шаг деревьев в ряду', 'help': '743-ПП, табл. 3.6.2: однорядная посадка — 5–6 м'},
    {'key': 'group_gap_m', 'group': 'density', 'kind': 'float', 'path': ['group_gap_m'], 'mode': 'both',
     'min': 0.2, 'max': 5, 'step': 0.1, 'unit': 'м',
     'title': 'Зазор между группами кустов', 'help': 'Край кроны к краю кроны: меньше — группы сливаются'},

    {'key': 'group_sizes_small', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'small'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Количество в группах: мелкие кусты (крона < 1,5 м)', 'help': 'Сколько растений в одной группе: рассадка выбирает из отмеченных; нечётные смотрятся естественнее'},
    {'key': 'group_sizes_medium', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'medium'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Количество в группах: средние кусты (крона 1,5–3 м)', 'help': 'Сколько растений в одной группе: рассадка выбирает из отмеченных; нечётные смотрятся естественнее'},
    {'key': 'group_sizes_large', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'large'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Количество в группах: крупные кусты (крона > 3 м)', 'help': 'Сколько растений в одной группе: рассадка выбирает из отмеченных; нечётные смотрятся естественнее'},
    {'key': 'group_sizes_tree', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'tree'], 'mode': 'both',
     'min': 1, 'max': 15, 'title': 'Количество в группах: деревья', 'help': 'Сколько растений в одной группе: рассадка выбирает из отмеченных; нечётные смотрятся естественнее'},
    {'key': 'drift_landscape', 'group': 'groups', 'kind': 'float', 'path': ['drift_share', 'landscape'], 'mode': 'both',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Вытянутые куртины (пейзажный стиль)',
     'help': 'Доля групп от 5 растений, вытянутых вдоль края, — остальные компактные'},
    {'key': 'drift_formal', 'group': 'groups', 'kind': 'float', 'path': ['drift_share', 'formal'], 'mode': 'both',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Вытянутые куртины (регулярный стиль)',
     'help': 'Доля групп от 5 растений, вытянутых вдоль края'},
    {'key': 'jitter_landscape', 'group': 'groups', 'kind': 'float', 'path': ['group_jitter', 'landscape'], 'mode': 'both',
     'min': 0, 'max': 0.15, 'step': 0.01, 'unit': '× шага', 'title': 'Неровность групп (пейзажный стиль)',
     'help': 'Случайный сдвиг растений в группе: 0 — ровная сетка'},
    {'key': 'jitter_formal', 'group': 'groups', 'kind': 'float', 'path': ['group_jitter', 'formal'], 'mode': 'both',
     'min': 0, 'max': 0.15, 'step': 0.01, 'unit': '× шага', 'title': 'Неровность групп (регулярный стиль)',
     'help': 'Случайный сдвиг растений в группе'},

    {'key': 'solitary_max_tree', 'group': 'amount', 'kind': 'int', 'path': ['solitary_max', 'tree'], 'mode': 'both',
     'min': 0, 'max': 200, 'optional': True, 'title': 'Одиночных деревьев на зону, не больше',
     'help': 'Пусто — без предела (сколько встанет); 0 — без солитеров'},
    {'key': 'solitary_max_shrub', 'group': 'amount', 'kind': 'int', 'path': ['solitary_max', 'shrub'], 'mode': 'both',
     'min': 0, 'max': 500, 'optional': True, 'title': 'Одиночных кустов на зону, не больше',
     'help': 'Пусто — без предела'},
    {'key': 'tree_max_per_100m2', 'group': 'amount', 'kind': 'float', 'path': ['tree_max_per_100m2'], 'mode': 'both',
     'min': 0, 'max': 5, 'step': 0.1, 'optional': True, 'unit': 'на 100 м²', 'title': 'Деревьев не больше',
     'help': 'Предел новых деревьев на 100 м² зоны (ряды тоже считаются); пусто — без предела'},
    {'key': 'shrub_cover_landscape', 'group': 'amount', 'kind': 'float', 'path': ['shrub_cover_share', 'landscape'],
     'mode': 'both', 'min': 0, 'max': 0.8, 'step': 0.05, 'unit': 'доля зоны',
     'title': 'Насыщенность кустарником (пейзажный)', 'help': 'Какую долю зоны занимают кроны кустов в группах'},
    {'key': 'shrub_cover_formal', 'group': 'amount', 'kind': 'float', 'path': ['shrub_cover_share', 'formal'],
     'mode': 'both', 'min': 0, 'max': 0.8, 'step': 0.05, 'unit': 'доля зоны',
     'title': 'Насыщенность кустарником (регулярный)', 'help': 'Какую долю зоны занимают кроны кустов в группах'},
    {'key': 'bed_share', 'group': 'amount', 'kind': 'float', 'path': ['bed_share'], 'mode': 'both',
     'min': 0, 'max': 0.5, 'step': 0.01, 'unit': 'доля зоны', 'title': 'Цветник', 'help': 'Доля зоны под цветник'},
    {'key': 'bed_max_m2', 'group': 'amount', 'kind': 'float', 'path': ['bed_max_m2'], 'mode': 'both',
     'min': 0, 'max': 1000, 'step': 5, 'unit': 'м²', 'title': 'Цветник не больше', 'help': 'Предельная площадь цветника'},

    {'key': 'existing_clearance_tree', 'group': 'existing', 'kind': 'float', 'path': ['existing_tree_clearance_m', 'tree'],
     'mode': 'both', 'min': 2, 'max': 20, 'step': 0.5, 'unit': 'м',
     'title': 'Новое дерево от ствола существующего', 'help': 'Не ближе этого'},
    {'key': 'existing_clearance_shrub', 'group': 'existing', 'kind': 'float', 'path': ['existing_tree_clearance_m', 'shrub'],
     'mode': 'both', 'min': 0.5, 'max': 10, 'step': 0.5, 'unit': 'м',
     'title': 'Куст от ствола существующего дерева', 'help': 'Не ближе этого'},

    {'key': 'style_mode', 'group': 'style', 'kind': 'choice', 'path': ['style_mode'], 'mode': 'both',
     'choices': [['auto', 'по типу территории'], ['landscape', 'везде пейзажный'], ['formal', 'везде регулярный']],
     'title': 'Стиль', 'help': 'Пейзажный — куртины и свободные группы, регулярный — ровнее и ближе к краям'},
    *[{'key': f'landscape_share_{t}', 'group': 'style', 'kind': 'float', 'path': ['landscape_share', t], 'mode': 'both',
       'min': 0, 'max': 1, 'step': 0.05, 'title': f'Доля пейзажного: {title}',
       'help': 'Для стиля «по типу территории»: вероятность пейзажного стиля у структуры этого типа'}
      for t, title in (('street', 'улицы'), ('plaza', 'площади'), ('yard', 'дворы'), ('playground', 'у площадок'),
                       ('square', 'скверы'), ('lawn', 'газоны'))],

    {'key': 'model_temperature', 'group': 'model', 'kind': 'float', 'path': ['model_temperature'], 'mode': 'model',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Разнообразие',
     'help': '0 — всегда лучшая по оценке точка; больше — случайнее'},
    {'key': 'model_min_score_tree', 'group': 'model', 'kind': 'float', 'path': ['model_min_score', 'tree'], 'mode': 'model',
     'min': 0, 'max': 0.9, 'step': 0.05, 'title': 'Разборчивость: деревья',
     'help': 'Место с оценкой ниже не занимается: больше — деревьев меньше, только «хорошие» места'},
    {'key': 'model_min_score_shrub', 'group': 'model', 'kind': 'float', 'path': ['model_min_score', 'shrub'], 'mode': 'model',
     'min': 0, 'max': 0.9, 'step': 0.05, 'title': 'Разборчивость: кусты', 'help': 'Место с оценкой ниже не занимается'},
    {'key': 'model_group_variants', 'group': 'model', 'kind': 'int', 'path': ['model_group_variants'], 'mode': 'model',
     'min': 1, 'max': 60, 'title': 'Вариантов группы для сравнения', 'help': 'Сколько мест и форм группы оценивает модель'},

    {'key': 'edge_pull_landscape', 'group': 'algorithm', 'kind': 'float', 'path': ['edge_pull', 'landscape'],
     'mode': 'algorithm', 'min': -1, 'max': 1, 'step': 0.05, 'title': 'Тяга: пейзажный стиль',
     'help': 'Больше нуля — к середине газона, меньше — к краю'},
    {'key': 'edge_pull_formal', 'group': 'algorithm', 'kind': 'float', 'path': ['edge_pull', 'formal'],
     'mode': 'algorithm', 'min': -1, 'max': 1, 'step': 0.05, 'title': 'Тяга: регулярный стиль',
     'help': 'Больше нуля — к середине газона, меньше — к краю'},
]
BY_KEY = {p['key']: p for p in SPEC}


class ParamError(ValueError):
    """Неверные параметры: errors — {ключ: сообщение}."""

    def __init__(self, errors):
        super().__init__('; '.join(f'{k}: {v}' for k, v in errors.items()))
        self.errors = errors


def _get(config, path):
    for key in path:
        config = config[key]
    return config


def _set(config, path, value):
    for key in path[:-1]:
        config = config.setdefault(key, {})
    config[path[-1]] = value


def schema(config=None, default_placement='algorithm'):
    """Описание параметров для формы: группы и параметры с текущими (по умолчанию) значениями."""
    config = config or place.load_config()
    items = []
    for spec in SPEC:
        item = {k: v for k, v in spec.items() if k != 'path'}
        if spec['path'] is not None:
            item['default'] = _get(config, spec['path'])
        elif spec['key'] == 'placement':
            item['default'] = default_placement
        items.append(item)
    return {'groups': [{'key': k, 'title': t} for k, t in GROUPS], 'params': items}


def _value(spec, raw):
    """Проверенное значение параметра или ParamError-сообщение (строка)."""
    kind = spec['kind']
    if raw is None or raw == '':
        if spec.get('optional'):
            return None
        raise ValueError('нужно значение')
    if kind == 'choice':
        if raw not in [c[0] for c in spec['choices']]:
            raise ValueError('допустимо: ' + ', '.join(c[0] for c in spec['choices']))
        return raw
    if kind == 'sizes':
        items = raw if isinstance(raw, list) else str(raw).replace(';', ',').split(',')
        try:
            sizes = [int(str(x).strip()) for x in items if str(x).strip()]
        except ValueError:
            raise ValueError('целые числа через запятую') from None
        if not sizes or any(not spec['min'] <= s <= spec['max'] for s in sizes):
            raise ValueError(f"от {spec['min']} до {spec['max']}, хотя бы одно")
        return sizes
    try:
        value = int(raw) if kind == 'int' and float(raw) == int(float(raw)) else float(raw)
    except (TypeError, ValueError):
        raise ValueError('нужно число') from None
    if kind == 'int' and not isinstance(value, int):
        raise ValueError('нужно целое число')
    if not spec['min'] <= value <= spec['max']:
        raise ValueError(f"от {spec['min']:g} до {spec['max']:g}")
    return value


def clean(overrides):
    """Проверенные параметры {ключ: значение} или ParamError. Неизвестные ключи — ошибка."""
    errors, result = {}, {}
    for key, raw in (overrides or {}).items():
        spec = BY_KEY.get(key)
        if spec is None:
            errors[key] = 'неизвестный параметр'
            continue
        try:
            result[key] = _value(spec, raw)
        except ValueError as exc:
            errors[key] = f"«{spec['title']}»: {exc}"
    if errors:
        raise ParamError(errors)
    return result


def apply(overrides, config=None):
    """(config рассадки с параметрами, режим или None, seed или None). overrides — уже проверенные (clean)."""
    config = copy.deepcopy(config or place.load_config())
    for key, value in overrides.items():
        spec = BY_KEY[key]
        if spec['path'] is not None:
            _set(config, spec['path'], value)
    return config, overrides.get('placement'), overrides.get('seed')


def changed(overrides, config=None):
    """Только параметры, отличающиеся от значений по умолчанию, — для сводки (режим — всегда)."""
    config = config or place.load_config()
    out = {}
    for key, value in overrides.items():
        spec = BY_KEY[key]
        default = _get(config, spec['path']) if spec['path'] is not None else None
        # режим — всегда (показывается отдельно), seed — если не 0
        if (key == 'placement') or (key == 'seed' and value) or (spec['path'] is not None and value != default):
            out[key] = value
    return out
