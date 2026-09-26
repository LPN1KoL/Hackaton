"""Справочник растений для LLM: одна строка на растение, коды вместо слов — чтобы занимать мало контекста.

Собирается из CSV ассортимента 743-ПП (ветка Vlad), запретов и характеристик каталога ветки artem
и списка травянистых из городских публикаций (config/herbaceous.csv):

    python -m greening.catalog особенности_размещения_растений.csv plant_selection.json
"""

import csv
import json
import re
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / 'config'
PLANTS = CONFIG / 'plants.txt'
HERBACEOUS = CONFIG / 'herbaceous.csv'

# колонки разрешений CSV → буква территории
TERRITORIES = {
    'дворовые_территории': 'Д', 'дошкольные_учреждения': 'С', 'школы_колледжи_спортивные_учреждения': 'Ш',
    'здравоохранение_реабилитация': 'М', 'магистрали_и_местные_проезды': 'Т',
    'площади_общественно_деловые_пространства': 'П', 'парки_бульвары_скверы_набережные_сады': 'К',
    'производственные_охранные_санитарные_зоны': 'З',
}
ALL_TERRITORIES = ''.join(TERRITORIES.values())
FLAGS = {
    'ограничения_около_детских_площадок': 'дп', 'требует_дренированной_почвы': 'др',
    'чувствительность_к_реагентам_и_газам': 'г', 'ограничения_соседства_с_плодовыми': 'пл',
    'неприхотливость': 'н', 'ограничения_распространения_и_охраняемых_территорий': 'рс',
}
GROUPS = {'Лиственные деревья': 'ДЛ', 'Хвойные деревья': 'ДХ', 'Лиственные кустарники': 'КЛ',
          'Хвойные кустарники': 'КХ', 'Лианы': 'Л'}
PREFIX = {'ДЛ': 'Д', 'ДХ': 'Д', 'КЛ': 'К', 'КХ': 'К', 'Л': 'Л', 'М': 'Т', 'О': 'Т', 'Б': 'Т', 'Г': 'Т'}
ASSORTMENT = {'основной': 'о', 'дополнительный': 'д', 'перспективный': 'п'}
# 743-ПП, примечание к табл. 3.6.1: широкая крона — не ближе 10 м от жилых зданий
WIDE_CROWN = re.compile(r'^(липа|кл[её]н|дуб|конский каштан|каштан|тополь)', re.I)

HEADER = """#Справочник растений. Строка: id|название|группа|ассортимент|где разрешено|свойства
#группа: ДЛ дерево листв., ДХ дерево хвойн., КЛ кустарник листв., КХ кустарник хвойн., Л лиана, М многолетник, О однолетник, Б луковичные, Г газон
#ассортимент 743-ПП: о основной, д дополнит., п перспективный; г городской ассортимент цветников (mos.ru) и газоны (743-ПП)
#где: *=везде; Д двор, С детсад, Ш школа/спорт, М медучр., Т магистраль/проезд, П площадь, К парк/сквер, З пром/санзона
#свойства: дп нельзя у детских и спорт. площадок; шк широкая крона, ≥10м от жилых домов; г чувствителен к реагентам и газам; др нужна дренированная почва; пл не рядом с плодовыми; рс риск самосева; н неприхотлив; я ядовит; с солнце; п полутень; т тень; з засухоустойч.; у устойчив у дорог; hN высота до N м; кN крона до N м
#свойства я,с,п,т,з,у у травянистых — справочные, не из НПА
"""


def _name(raw):
    # в CSV дефисы местами превратились в нули: «перисто0ветвистый»
    name = re.sub(r'(?<=[а-яё])0(?=[а-яё])', '-', raw.strip())
    return re.sub(r'\s*\((формы и сорта|сорта)\)', '', name).strip()


def _key(name):
    return re.sub(r'[^а-яё]', '', name.casefold().replace('ё', 'е'))


def _banned(artem):
    """Названия запрещённых видов (до «/») как ключи для сравнения."""
    names = [r['text'].split('/')[0] for r in artem['restrictions'] if r['status'] == 'confirmed_project_ban']
    return [_key(n) for n in names]


def _traits(artem):
    """Высота и крона (м) и свет из обогащённых карточек каталога artem."""
    result = {}
    for plant in artem['plants']:
        traits = plant.get('traits') or {}
        codes = []
        height = (traits.get('mature_height') or {}).get('value')
        if height and height.get('max'):
            codes.append(f"h{round(height['max'])}")
        crown = (traits.get('mature_crown_width') or {}).get('value')
        if crown and crown.get('max'):
            codes.append(f"к{round(crown['max'])}")
        light = ' '.join((traits.get('light') or {}).get('value') or [])
        codes += [code for word, code in (('Full sun', 'с'), ('Partial Shade', 'п'), ('Deep shade', 'т')) if word in light]
        if codes:
            result[_key(_name(plant['name_ru']))] = codes
    return result


def build(vlad_csv, artem_json, out=PLANTS):
    artem = json.loads(Path(artem_json).read_text(encoding='utf-8'))
    banned, traits = _banned(artem), _traits(artem)
    lines, counters, skipped = [], {}, []
    with open(vlad_csv, encoding='utf-8-sig') as stream:
        for row in csv.DictReader(stream):
            name = _name(row['растение'])
            key = _key(name)
            if any(key.startswith(b) or b.startswith(key) for b in banned if b):
                skipped.append(name)
                continue
            group = GROUPS[row['группа']]
            where = ''.join(letter for column, letter in TERRITORIES.items() if row[column] == '1')
            flags = [code for column, code in FLAGS.items() if row[column] == '1']
            if WIDE_CROWN.match(name) and group.startswith('Д'):
                flags.append('шк')
            flags += traits.get(key, [])
            lines.append((PREFIX[group], name, group, ASSORTMENT[row['ассортимент']], where, flags))
    with open(HERBACEOUS, encoding='utf-8') as stream:
        for row in csv.DictReader(stream, delimiter=';'):
            flags = row['свойства'].split(',') if row['свойства'] else []
            # ядовитые — не в детских садах и не у детских площадок
            where = ALL_TERRITORIES.replace('С', '') if 'я' in flags else ALL_TERRITORIES
            if 'я' in flags:
                flags.insert(0, 'дп')
            lines.append((PREFIX[row['группа']], row['название'], row['группа'], 'г', where, flags))

    out_lines = [HEADER.rstrip('\n')]
    for prefix, name, group, assortment, where, flags in lines:
        counters[prefix] = counters.get(prefix, 0) + 1
        code = f'{prefix}{counters[prefix]:03d}'
        where = '*' if where == ALL_TERRITORIES else where
        out_lines.append('|'.join((code, name, group, assortment, where, ','.join(flags))))
    Path(out).write_text('\n'.join(out_lines) + '\n', encoding='utf-8')
    return len(lines), skipped


def load(path=PLANTS):
    """[{'id', 'name', 'group', 'assortment', 'where', 'flags'}] без строк-комментариев."""
    plants = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        code, name, group, assortment, where, flags = line.split('|')
        plants.append({'id': code, 'name': name, 'group': group, 'assortment': assortment,
                       'where': where, 'flags': set(filter(None, flags.split(',')))})
    return plants


def header(path=PLANTS):
    return '\n'.join(l for l in Path(path).read_text(encoding='utf-8').splitlines() if l.startswith('#'))


def select(plants, groups, territory, near_playground=False):
    """Строки справочника, подходящие структуре: нужные группы, разрешённые на территории."""
    rows = []
    for plant in plants:
        if plant['group'] not in groups:
            continue
        if plant['where'] != '*' and territory not in plant['where']:
            continue
        if near_playground and 'дп' in plant['flags']:
            continue
        rows.append('|'.join((plant['id'], plant['name'], plant['group'], plant['assortment'],
                              plant['where'], ','.join(sorted(plant['flags'])))))
    return rows


if __name__ == '__main__':
    count, skipped = build(sys.argv[1], sys.argv[2])
    print(f'{count} растений записано в {PLANTS}; исключены как запрещённые: {", ".join(skipped) or "нет"}')
