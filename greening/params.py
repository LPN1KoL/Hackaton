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
    ('mode', 'Вариант рассадки',
     'Кто выбирает точки посадки и какой из случайных вариантов показать. Виды растений от этого не меняются.'),
    ('amount', 'Сколько сажать',
     'Сколько места в каждой зоне отдать кустарникам и цветнику и нужен ли предел числу деревьев. Остальное — газон.'),
    ('groups', 'Группы',
     'Растения сажают не по одному, а группами: так посадки смотрятся естественно и проще в уходе. '
     'Здесь — сколько растений в группе и какой она формы.'),
    ('variety', 'Разнообразие',
     'Как сочетать выбранные растения: группы из двух видов кустов, кусты под новыми деревьями, низкие растения '
     'ближе к дорожке, цветник из нескольких видов, луговой газон с полосой обычного вдоль дорожек.'),
    ('density', 'Плотность и просветы',
     'Расстояния между растениями сверх норм: как тесно стоят кусты в группе, какой шаг у изгороди и ряда деревьев, '
     'сколько свободного места между группами.'),
    ('access', 'Удобство ухода',
     'Кусты ставятся туда, куда садовник подойдёт с дорожки и где вокруг них можно выкосить газон: не посреди '
     'большого газона, не у проезжей части без тротуара рядом, без узких полосок газона между кустами и краем.'),
    ('existing', 'Существующие деревья',
     'Старые деревья сохраняются. Новые посадки держатся от их стволов на этом расстоянии, чтобы не мешать кронам и корням.'),
    ('style', 'Стиль',
     'Пейзажный стиль — свободные группы и вытянутые куртины, как в парке. Регулярный — ровнее, ближе к краям дорожек, '
     'как вдоль улицы. Стиль выбирается для каждой структуры (двора, сквера, полосы).'),
    ('model', 'Модель',
     'Модель обучена на проектах озеленения и оценивает, насколько место похоже на выбор проектировщика.'),
    ('algorithm', 'Алгоритм',
     'Алгоритм выбирает место по правилам: открытые места, края газона, удалённость от других посадок.'),
)

# key — имя в API; path — путь в placement.json (None — не конфиг); kind — float | int | sizes | choice;
# advanced — в форме под «Тонкой настройкой» карточки
SPEC = [
    {'key': 'placement', 'group': 'mode', 'kind': 'choice', 'path': None, 'mode': 'both', 'default': None,
     'choices': [['algorithm', 'алгоритм'], ['model', 'модель']],
     'title': 'Кто выбирает места',
     'help': 'Алгоритм ставит растения по правилам композиции, модель — туда, где их чаще сажают проектировщики. '
             'В обоих случаях только в зоны, где вид разрешён нормами.'},
    {'key': 'seed', 'group': 'mode', 'kind': 'int', 'path': None, 'mode': 'both', 'default': 0, 'min': 0, 'max': 999999,
     'title': 'Номер варианта',
     'help': 'Рассадка делает случайный выбор (где начать группу, какой формы она будет). Другой номер — другая '
             'раскладка тех же растений; тот же номер — точно тот же результат. Кубик — случайный номер.'},

    {'key': 'shrub_cover_landscape', 'group': 'amount', 'kind': 'float', 'path': ['shrub_cover_share', 'landscape'],
     'mode': 'both', 'min': 0, 'max': 0.8, 'step': 0.05, 'unit': 'доля зоны',
     'title': 'Кустарники в пейзажном стиле',
     'help': 'Какая часть зоны будет под кронами кустов. 0,25 — четверть зоны; больше — гуще и дороже, меньше — больше газона.'},
    {'key': 'shrub_cover_formal', 'group': 'amount', 'kind': 'float', 'path': ['shrub_cover_share', 'formal'],
     'mode': 'both', 'min': 0, 'max': 0.8, 'step': 0.05, 'unit': 'доля зоны',
     'title': 'Кустарники в регулярном стиле',
     'help': 'То же для регулярного стиля — обычно меньше, посадки строже.'},
    {'key': 'bed_share', 'group': 'amount', 'kind': 'float', 'path': ['bed_share'], 'mode': 'both',
     'min': 0, 'max': 0.5, 'step': 0.01, 'unit': 'доля зоны', 'title': 'Цветник',
     'help': 'Какую часть зоны занять цветником, если он выбран для этого места. 0 — без цветников.'},
    {'key': 'bed_max_m2', 'group': 'amount', 'kind': 'float', 'path': ['bed_max_m2'], 'mode': 'both',
     'min': 0, 'max': 1000, 'step': 5, 'unit': 'м²', 'title': 'Цветник не больше', 'advanced': True,
     'help': 'Чтобы на большом газоне цветник не разрастался: уход за ним дорогой.'},
    {'key': 'tree_max_per_100m2', 'group': 'amount', 'kind': 'float', 'path': ['tree_max_per_100m2'], 'mode': 'both',
     'min': 0, 'max': 5, 'step': 0.1, 'optional': True, 'unit': 'на 100 м²', 'title': 'Деревьев не больше',
     'help': 'Без предела деревья ставятся везде, где хватает места с нужными просветами. Предел — сколько новых '
             'деревьев допустимо на каждые 100 м² зоны.'},
    {'key': 'solitary_max_tree', 'group': 'amount', 'kind': 'int', 'path': ['solitary_max', 'tree'], 'mode': 'both',
     'min': 0, 'max': 200, 'optional': True, 'title': 'Одиночных деревьев в зоне, не больше', 'advanced': True,
     'help': 'Солитер — дерево, стоящее отдельно, как акцент. 0 — только группы и ряды; без предела — сколько поместится.'},
    {'key': 'solitary_max_shrub', 'group': 'amount', 'kind': 'int', 'path': ['solitary_max', 'shrub'], 'mode': 'both',
     'min': 0, 'max': 500, 'optional': True, 'title': 'Одиночных кустов в зоне, не больше', 'advanced': True,
     'help': 'То же для кустов, выбранных как одиночная посадка.'},

    {'key': 'group_sizes_small', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'small'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Мелкие кусты (крона до 1,5 м): сколько в группе',
     'help': 'Отметьте допустимые размеры — рассадка выбирает из них. Нечётное число смотрится естественнее.'},
    {'key': 'group_sizes_medium', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'medium'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Средние кусты (крона 1,5–3 м): сколько в группе',
     'help': 'Чем крупнее куст, тем меньше их обычно в группе.'},
    {'key': 'group_sizes_large', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'large'], 'mode': 'both',
     'min': 1, 'max': 25, 'title': 'Крупные кусты (крона больше 3 м): сколько в группе',
     'help': '1 — крупный куст может стоять один, как акцент.'},
    {'key': 'group_sizes_tree', 'group': 'groups', 'kind': 'sizes', 'path': ['group_sizes', 'tree'], 'mode': 'both',
     'min': 1, 'max': 15, 'title': 'Деревья: сколько в группе',
     'help': 'Для деревьев, выбранных групповой посадкой. Встаёт столько, сколько помещается с нужными просветами.'},
    {'key': 'drift_landscape', 'group': 'groups', 'kind': 'float', 'path': ['drift_share', 'landscape'], 'mode': 'both',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Вытянутые куртины в пейзажном стиле', 'advanced': True,
     'help': 'Куртина — группа, вытянутая лентой вдоль края газона. Доля групп от 5 растений, которые станут '
             'куртинами; остальные — компактные пятна.'},
    {'key': 'drift_formal', 'group': 'groups', 'kind': 'float', 'path': ['drift_share', 'formal'], 'mode': 'both',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Вытянутые куртины в регулярном стиле', 'advanced': True,
     'help': 'То же для регулярного стиля.'},
    {'key': 'jitter_landscape', 'group': 'groups', 'kind': 'float', 'path': ['group_jitter', 'landscape'], 'mode': 'both',
     'min': 0, 'max': 0.15, 'step': 0.01, 'unit': '× шага', 'title': 'Неровность групп в пейзажном стиле', 'advanced': True,
     'help': '0 — растения в группе стоят ровной сеткой; больше — чуть вразброс, как в природе.'},
    {'key': 'jitter_formal', 'group': 'groups', 'kind': 'float', 'path': ['group_jitter', 'formal'], 'mode': 'both',
     'min': 0, 'max': 0.15, 'step': 0.01, 'unit': '× шага', 'title': 'Неровность групп в регулярном стиле', 'advanced': True,
     'help': 'То же для регулярного стиля — обычно почти ровно.'},

    {'key': 'mixed_share', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'mixed_share'], 'mode': 'both',
     'min': 0, 'max': 1, 'step': 0.05, 'unit': 'доля групп',
     'title': 'Смешанные группы кустов',
     'help': 'Какая часть групп кустов будет из двух видов разной высоты: низкий — спереди, у дорожки, высокий — за ним. '
             'Второй вид берётся из уже выбранных для того же двора или сквера. 0 — каждая группа из одного вида.'},
    {'key': 'underplanting_share', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'underplanting_share'],
     'mode': 'both', 'min': 0, 'max': 1, 'step': 0.05, 'unit': 'доля групп',
     'title': 'Кусты у новых деревьев',
     'help': 'Какая часть групп кустов встанет дугой у нового дерева, по краю его кроны, лицом к дорожке, — '
             'двухъярусная группа. Нужны дерево и кусты в одной зоне.'},
    {'key': 'height_grading', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'height_grading'], 'mode': 'both',
     'min': 0, 'max': 2, 'step': 0.1,
     'title': 'Низкие — к дорожке, высокие — дальше',
     'help': 'Насколько строго держать порядок по высоте, когда в зоне несколько видов кустов: низкие ближе к дорожке, '
             'высокие в глубине, чтобы друг друга не закрывали. 0 — без порядка.'},
    {'key': 'grading_depth_m', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'grading_depth_m'], 'mode': 'both',
     'min': 1, 'max': 20, 'step': 0.5, 'unit': 'м', 'advanced': True,
     'title': 'Глубина ступеней',
     'help': 'На каком расстоянии от дорожки встают самые высокие кусты зоны; остальные — ближе, по росту.'},
    {'key': 'mixborder_species', 'group': 'variety', 'kind': 'int', 'path': ['variety', 'mixborder_species'],
     'mode': 'both', 'min': 1, 'max': 5,
     'title': 'Видов в цветнике',
     'help': 'Цветник из нескольких многолетников куртинами — миксбордер: к выбранному виду добавляются виды, которые '
             'цветут в другой сезон, и цветник красив с весны до осени. 1 — цветник из одного вида.'},
    {'key': 'mixborder_min_m2', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'mixborder_min_m2'],
     'mode': 'both', 'min': 0, 'max': 100, 'step': 1, 'unit': 'м²', 'advanced': True,
     'title': 'Миксбордер — от площади',
     'help': 'Цветник меньше этого остаётся из одного вида: на маленьком пятне куртины будут слишком мелкими.'},
    {'key': 'meadow_border_m', 'group': 'variety', 'kind': 'float', 'path': ['variety', 'meadow_border_m'], 'mode': 'both',
     'min': 0.5, 'max': 10, 'step': 0.5, 'optional': True, 'unit': 'м', 'advanced': True,
     'title': 'Полоса газона у дорожек при луговом',
     'help': 'Если в зоне выбран луговой или мавританский газон, вдоль дорожек остаётся полоса обычного стриженого '
             'газона такой ширины — так луг выглядит ухоженным. Без значения — газоны делят зону поровну.'},

    {'key': 'shrub_share', 'group': 'density', 'kind': 'float', 'path': ['shrub_share'], 'mode': 'both',
     'min': 0.4, 'max': 1.5, 'step': 0.05, 'unit': '× кроны',
     'title': 'Как тесно стоят кусты в группе',
     'help': 'Расстояние между кустами одной группы в долях ширины кроны. 0,7 — кроны слегка смыкаются; '
             '1 — касаются; меньше — плотная масса, больше — каждый куст отдельно.'},
    {'key': 'group_gap_m', 'group': 'density', 'kind': 'float', 'path': ['group_gap_m'], 'mode': 'both',
     'min': 0.2, 'max': 5, 'step': 0.1, 'unit': 'м',
     'title': 'Зазор между группами кустов',
     'help': 'Свободная полоса между краями крон соседних групп: проход для ухода и чтобы группы не сливались в одну.'},
    {'key': 'tree_unit_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_unit_spacing_m'], 'mode': 'both',
     'min': 4, 'max': 25, 'step': 0.5, 'unit': 'м',
     'title': 'Просвет между деревьями',
     'help': 'Между отдельными деревьями, группами деревьев и рядами. Больше — деревьев меньше и каждому больше '
             'простора; меньше — тенистее.'},
    {'key': 'hedge_share', 'group': 'density', 'kind': 'float', 'path': ['hedge_share'], 'mode': 'both',
     'min': 0.2, 'max': 1.0, 'step': 0.05, 'unit': '× кроны',
     'title': 'Плотность живой изгороди',
     'help': 'Шаг посадки в изгороди в долях кроны. Меньше — изгородь быстрее сомкнётся в сплошную стенку.'},
    {'key': 'hedge_max_m', 'group': 'density', 'kind': 'float', 'path': ['hedge_max_m'], 'mode': 'both',
     'min': 0.3, 'max': 2.0, 'step': 0.1, 'unit': 'м', 'title': 'Шаг изгороди не больше', 'advanced': True,
     'help': 'Чтобы изгородь из крупных кустов не стала редкой.'},
    {'key': 'tree_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_spacing_m'], 'mode': 'both',
     'min': 3, 'max': 12, 'step': 0.5, 'unit': 'м', 'title': 'Шаг деревьев внутри группы', 'advanced': True,
     'help': 'Не меньше 0,8 ширины кроны вида — иначе кроны будут мешать друг другу.'},
    {'key': 'tree_row_spacing_m', 'group': 'density', 'kind': 'float', 'path': ['tree_row_spacing_m'], 'mode': 'both',
     'min': 3, 'max': 15, 'step': 0.5, 'unit': 'м', 'title': 'Шаг деревьев в ряду', 'advanced': True,
     'help': 'Для рядовой посадки (аллеи). По 743-ПП, табл. 3.6.2, в один ряд — 5–6 м.'},

    {'key': 'existing_clearance_tree', 'group': 'existing', 'kind': 'float', 'path': ['existing_tree_clearance_m', 'tree'],
     'mode': 'both', 'min': 2, 'max': 20, 'step': 0.5, 'unit': 'м',
     'title': 'Новое дерево — от старого дерева',
     'help': 'Ближе новое дерево окажется под кроной старого и будет расти в тени.'},
    {'key': 'existing_clearance_shrub', 'group': 'existing', 'kind': 'float', 'path': ['existing_tree_clearance_m', 'shrub'],
     'mode': 'both', 'min': 0.5, 'max': 10, 'step': 0.5, 'unit': 'м',
     'title': 'Куст — от старого дерева',
     'help': 'Кусты можно ближе, но не вплотную к стволу: при посадке повредят корни.'},

    {'key': 'access_path_reach', 'group': 'access', 'kind': 'float', 'path': ['access', 'path_reach_m'],
     'mode': 'both', 'min': 2, 'max': 50, 'step': 0.5, 'optional': True, 'unit': 'м',
     'title': 'Куст — от дорожки, не дальше',
     'help': 'Чтобы не сажать кусты в глубине больших массивов (лес в парке, широкий луг), куда садовнику не дойти. '
             'Газоны между дорожками это не задевает. Считается от тротуара, мощения или площадки.'},
    {'key': 'access_lone_edge', 'group': 'access', 'kind': 'float', 'path': ['access', 'lone_edge_m'],
     'mode': 'both', 'min': 0.5, 'max': 30, 'step': 0.5, 'optional': True, 'unit': 'м',
     'title': 'Одиночный куст — от края газона, не дальше',
     'help': 'Одиночный куст посреди газона приходится обкашивать со всех сторон. Группы это правило не касается.'},
    {'key': 'access_road_only', 'group': 'access', 'kind': 'float', 'path': ['access', 'road_only_m'],
     'mode': 'both', 'min': 0.5, 'max': 20, 'step': 0.5, 'optional': True, 'unit': 'м', 'advanced': True,
     'title': 'У проезжей части — дорожка не дальше',
     'help': 'Куст, крона которого ближе 1 м к проезжей части, ставится, только если рядом есть тротуар: иначе '
             'ухаживать за ним можно только с проезда.'},
    {'key': 'access_mow_min', 'group': 'access', 'kind': 'float', 'path': ['access', 'mow_min_m'],
     'mode': 'both', 'min': 0.3, 'max': 3, 'step': 0.1, 'optional': True, 'unit': 'м', 'advanced': True,
     'title': 'Полоска газона у края — не уже',
     'help': 'Между кустами и краем газона — либо ничего (кусты у самого края), либо полоса, по которой пройдёт '
             'газонокосилка. Изгородей и мест, где ближе к краю не пускают нормы, это не касается.'},

    {'key': 'style_mode', 'group': 'style', 'kind': 'choice', 'path': ['style_mode'], 'mode': 'both',
     'choices': [['auto', 'по типу территории'], ['landscape', 'везде пейзажный'], ['formal', 'везде регулярный']],
     'title': 'Стиль посадок',
     'help': '«По типу территории» — дворы и скверы чаще пейзажные, улицы — регулярные (доли — в тонкой настройке).'},
    *[{'key': f'landscape_share_{t}', 'group': 'style', 'kind': 'float', 'path': ['landscape_share', t], 'mode': 'both',
       'min': 0, 'max': 1, 'step': 0.05, 'title': f'Пейзажный стиль: {title}', 'advanced': True,
       'help': f'При стиле «по типу территории»: какая доля {genitive} получит пейзажный стиль, остальные — регулярный.'}
      for t, title, genitive in (('street', 'улицы', 'улиц'), ('plaza', 'площади', 'площадей'), ('yard', 'дворы', 'дворов'),
                                 ('playground', 'у площадок', 'территорий у площадок'), ('square', 'скверы', 'скверов'),
                                 ('lawn', 'газоны', 'газонов'))],

    {'key': 'model_temperature', 'group': 'model', 'kind': 'float', 'path': ['model_temperature'], 'mode': 'model',
     'min': 0, 'max': 1, 'step': 0.05, 'title': 'Разброс выбора мест',
     'help': '0 — модель всегда берёт лучшее по своей оценке место; больше — допускает и места похуже, '
             'раскладка разнообразнее.'},
    {'key': 'model_min_score_tree', 'group': 'model', 'kind': 'float', 'path': ['model_min_score', 'tree'], 'mode': 'model',
     'min': 0, 'max': 0.9, 'step': 0.05, 'title': 'Разборчивость для деревьев', 'advanced': True,
     'help': 'Места с оценкой ниже этой модель не занимает. Больше — деревьев меньше, но только в «типичных» местах.'},
    {'key': 'model_min_score_shrub', 'group': 'model', 'kind': 'float', 'path': ['model_min_score', 'shrub'], 'mode': 'model',
     'min': 0, 'max': 0.9, 'step': 0.05, 'title': 'Разборчивость для кустов', 'advanced': True,
     'help': 'То же для кустов.'},
    {'key': 'model_group_variants', 'group': 'model', 'kind': 'int', 'path': ['model_group_variants'], 'mode': 'model',
     'min': 1, 'max': 60, 'title': 'Сколько вариантов группы сравнивать', 'advanced': True,
     'help': 'Для каждой группы модель примеряет столько мест и форм и берёт лучшую. Больше — точнее, но дольше.'},

    {'key': 'edge_pull_landscape', 'group': 'algorithm', 'kind': 'float', 'path': ['edge_pull', 'landscape'],
     'mode': 'algorithm', 'min': -1, 'max': 1, 'step': 0.05, 'title': 'Где ставить группы в пейзажном стиле',
     'help': 'Меньше нуля — ближе к краям газона, вдоль дорожек; больше — к середине газона.'},
    {'key': 'edge_pull_formal', 'group': 'algorithm', 'kind': 'float', 'path': ['edge_pull', 'formal'],
     'mode': 'algorithm', 'min': -1, 'max': 1, 'step': 0.05, 'title': 'Где ставить группы в регулярном стиле',
     'help': 'То же для регулярного стиля — обычно ближе к краям.'},
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
    return {'groups': [{'key': k, 'title': t, 'about': about} for k, t, about in GROUPS], 'params': items}


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
