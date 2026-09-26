"""Просмотр разбиения на структуры, газоны и зоны — один HTML-файл без зависимостей.

    python -m greening.zones_html РАЗМЕТКА.greening.geojson [--plan ПЛАН.plan.json] [--out файл.html]

Карта всего участка: поверхности, существующие деревья, зоны всех структур цветом по разрешённым
посадкам. Клик по структуре (на карте или в списке) — приближение к ней, контуры её газонов
и таблица зон: место, что закрыло деревья и кустарники, объяснение из плана. Режим «Почему нет
деревьев» красит секции без деревьев по числу сработавших запретов; при наведении — список норм.
"""

import argparse
import html
import json
import re
from pathlib import Path

from shapely.geometry import shape
from shapely.ops import unary_union

from . import place, recommend
from .split import MARGIN

SIMPLIFY = 0.05
SURFACES = ('roadway', 'sidewalk', 'paving', 'sealed', 'gravel', 'playground', 'building',
            'lawn', 'flowerbed', 'ground', 'unknown')
ZONE_KINDS = {
    'tree_shrub': 'деревья и кустарники', 'tree': 'только деревья', 'shrub': 'кустарники',
    'herbaceous': 'только травянистые', 'none': 'ничего (неизвестная поверхность)',
}
# сколько норм запретили деревья в секции → ступень цвета в режиме «Почему нет деревьев»
BAN_STEPS = ((1, '1 норма'), (2, '2 нормы'), (3, '3–4 нормы'), (5, '5 и больше'))
PLANTING_WORDS = {'tree': 'деревья', 'shrub': 'кустарники'}
# радиус точки посадочного места куста в группе на карте, м
GROUP_SPOT = 0.25


def _path(geometry):
    """SVG path; ось y переворачивается, чтобы текст не зеркалился."""
    parts = []
    geometry = geometry.simplify(SIMPLIFY)
    for polygon in getattr(geometry, 'geoms', [geometry]):
        if polygon.geom_type != 'Polygon' or polygon.is_empty:
            continue
        for ring in (polygon.exterior, *polygon.interiors):
            coords = list(ring.coords)[:-1]
            if len(coords) >= 3:
                parts.append('M' + 'L'.join(f'{x:.2f} {-y:.2f}' for x, y in coords) + 'Z')
    return ''.join(parts)


def _kind(allowed):
    if 'tree' in allowed and 'shrub' in allowed:
        return 'tree_shrub'
    for kind in ('tree', 'shrub', 'herbaceous'):
        if kind in allowed:
            return kind
    return 'none'


def _short(title):
    return re.split(r'[,(]', title)[0].strip().lower()


def _blocked_text(blocked):
    """«деревья: кабель связи 2 м — 80%, газопровод 1.5 м — 40%» по каждому закрытому типу посадки."""
    return [f"{PLANTING_WORDS[planting]}: " + ', '.join(
                f"{_short(r['object'])} {r['distance_m']:g} м — {round(r['share'] * 100)}%" for r in rules)
            for planting, rules in blocked.items() if rules]


def _labels(geometry, minimum):
    return [[round(q.x, 2), round(-q.y, 2)] for part in getattr(geometry, 'geoms', [geometry])
            if part.area >= minimum for q in [part.representative_point()]]


def build(geojson, plan=None, placement=None):
    features = [(shape(f['geometry']), f['properties']) for f in geojson['features']]
    sections = {p['id']: g for g, p in features if p['layer'] == 'section'}
    planned = {s['id']: s for s in plan['structures']} if plan else {}
    territory = unary_union([g for g, p in features if p['layer'] == 'territory'])
    minx, miny, maxx, maxy = territory.buffer(5).bounds
    data = {
        'view': [minx, -maxy, maxx - minx, maxy - miny],
        'territory': _path(territory),
        'surfaces': [{'kind': p['surface'], 'd': _path(g)} for g, p in features
                     if p['layer'] == 'surface' and p['surface'] in SURFACES],
        'trees': [[round(g.x, 2), round(-g.y, 2)] for g, p in features if p['layer'] == 'tree'],
        # у существующих деревьев в подоснове только значок ствола — крона рисуется условной
        'existing_crown': place.load_config().get('existing_crown_m', 6.0),
        'rows': ''.join(_path(g) for g, p in features if p['layer'] == 'tree_row'),
        'objects': [],
        'bans': [],
        'structures': [],
        'plants': [],
        'beds': [],
        'lawns': [],
        'groups': [],
    }
    refs = {}
    for g, p in features:
        if p['layer'] == 'structure':
            at = g if g.geom_type == 'Point' else g.representative_point()
            data['objects'].append({'type': recommend.OBJECTS.get(p['type'], p['type']),
                                    'x': round(at.x, 2), 'y': round(-at.y, 2),
                                    'd': '' if g.geom_type == 'Point' else _path(g)})
        elif p['layer'] == 'section' and p['surface'] != 'unknown':
            rules = sorted({(_short(r['object']), r['distance_m'], r['doc']) for r in p['rules'] if r['planting'] == 'tree'},
                           key=lambda r: -r[1])
            if 'tree' in p['allowed']:
                data['bans'].append({'d': _path(g), 'n': 0, 't': f"{p['id']}: деревья можно"})
            else:
                data['bans'].append({'d': _path(g), 'n': len(rules), 't': f"{p['id']}, деревья нельзя:\n" + '\n'.join(
                    f"{name} — {distance:g} м ({doc})" for name, distance, doc in rules)})

    for structure in recommend.structures(geojson):
        answer = planned.get(structure['id'], {})
        if answer and abs(answer.get('area_m2', 0) - structure['area_m2']) > 0.2:
            answer = {}
        explained = {z['id']: z.get('explanation', {}) for z in answer.get('zones', [])}
        plantings, details = {}, []
        for number, item in enumerate(answer.get('plantings', [])):
            plantings.setdefault(item['zone'], []).append(
                f"{item.get('name', item['plant_id'])}"
                + (f" — {item['quantity']} {item.get('unit', '')}".rstrip() if item.get('quantity') is not None else ''))
            # полное объяснение посадки: текст LLM (reason) и то, что пишет код (norms, facts, warnings)
            details.append({'i': number, 'zone': item['zone'], 'name': item.get('name', item.get('plant_id')),
                            'group': item.get('group'), 'role': item.get('role'), 'quantity': item.get('quantity'),
                            'unit': item.get('unit'), 'reason': item.get('reason', ''), 'norms': item.get('norms', []),
                            'facts': item.get('facts', []), 'warnings': item.get('warnings', []),
                            'by': item.get('by', 'llm'), 'placed': 0})
            refs[(structure['id'], item['zone'], item.get('plant_id'), item.get('role'))] = (structure['id'], number)
        zones = []
        for zone in structure['zones']:
            geometry = unary_union([sections[s] for s in zone['sections']])
            zones.append({'id': zone['id'], 'patch': zone['patch'], 'kind': _kind(zone['allowed']),
                          'area': zone['area_m2'], 'place': zone.get('place', ''), 'reason': zone.get('empty_reason', ''),
                          'tags': [structure['tag_titles'].get(t, t) for t in zone['tags']],
                          'blocked': _blocked_text(zone.get('blocked_by', {})),
                          'decision': explained.get(zone['id'], {}).get('decision', ''),
                          'plantings': plantings.get(zone['id'], []),
                          'explanation': explained.get(zone['id'], {}).get('place', ''),
                          'sections': len(zone['sections']), 'd': _path(geometry), 'labels': _labels(geometry, 2)})
        patches = [{'id': p['id'], 'd': _path(p['geometry']), 'labels': _labels(p['geometry'], 20),
                    'area': p['area_m2'], 'place': p['place'], 'small': p['small']} for p in structure['patches']]
        w = structure['geometry'].buffer(MARGIN).bounds
        data['structures'].append({
            'id': structure['id'], 'name': structure['name'], 'place': structure['place'],
            'area': structure['area_m2'], 'total': structure['total_m2'],
            'composition': recommend._composition_text(structure['composition']),
            'trees': structure['existing_trees'],
            'status': answer.get('status', ''), 'justification': answer.get('justification', ''),
            'window': [w[0], -w[3], w[2] - w[0], w[3] - w[1]],
            'outline': _path(structure['geometry']), 'patches': patches, 'zones': zones,
            'plantings': details, 'type': structure['type'],
        })

    # рассадка: каждая точка и пятно ссылаются на свою посадку (структура, номер) — по клику её объяснение
    by_id = {s['id']: s for s in data['structures']}
    for f in (placement or {}).get('features', []):
        p = f['properties']
        g = shape(f['geometry'])
        sid, number = refs.get((p['structure'], p['zone'], p.get('plant_id'), p.get('role')), (p['structure'], -1))
        if number >= 0 and p['layer'] != 'group':
            item = by_id[sid]['plantings'][number]
            item['placed'] = round(item['placed'] + (1 if p['layer'] == 'plant' else p.get('area_m2', 0)), 1)
        if p['layer'] == 'plant':
            # куст в группе или изгороди — точка посадочного места: крону показывает контур группы
            in_group = p['kind'] == 'shrub' and (p.get('group_size', 1) > 1 or p.get('role') in ('живая изгородь', 'рядовая посадка'))
            radius = GROUP_SPOT if in_group else round(p['crown_m'] / 2, 2)
            data['plants'].append([round(g.x, 2), round(-g.y, 2), radius, p['kind'],
                                   f"{p['name']} ({p['role']}), {p['structure']}/{p['zone']}", sid, number])
        elif p['layer'] == 'group':
            data['groups'].append({'d': _path(g), 't': f"{p['label']} — {p['role']}", 's': sid, 'i': number,
                                   'label': p['label'], 'at': _labels(g, 0)[:1]})
        else:
            data['beds' if p['layer'] == 'bed' else 'lawns'].append(
                {'d': _path(g), 't': f"{p['name']}: {p['area_m2']} м²", 's': sid, 'i': number})
    return data


def render(data, title):
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    legend = ''.join(f'<span class="sw z-{k}"></span>{html.escape(v)}' for k, v in ZONE_KINDS.items())
    legend += '<span class="sw tree-sw"></span>существующее дерево'
    bans = '<span class="sw b-0"></span>деревья можно' + ''.join(
        f'<span class="sw b-{n}"></span>{html.escape(t)}' for n, t in BAN_STEPS)
    return (TEMPLATE.replace('__TITLE__', html.escape(title)).replace('__LEGEND__', legend)
            .replace('__BANS__', bans).replace('__DATA__', payload))


TEMPLATE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Зоны: __TITLE__</title>
<style>
:root{--bg:#f6f5f1;--panel:#fff;--ink:#1d1f1c;--muted:#6b6f68;--line:#dcdad3;--accent:#2f6fdf;
--roadway:#b9b9b6;--sidewalk:#d9d7d1;--paving:#d8c9a8;--sealed:#cfcdc6;--gravel:#ddd0b0;--playground:#e7c3e0;--building:#a0785a;
--lawn:#dbe8cf;--flowerbed:#f0cfe0;--ground:#e6dcc4;--unknown:#f3d2cc;
--z-tree_shrub:#2e7d32;--z-tree:#4b8b3b;--z-shrub:#8bbf4a;--z-herbaceous:#f1e58a;--z-none:#e0715f;--tree:#2c5e2e;
--b-0:#3f8f45;--b-1:#f6d38b;--b-2:#f0a35e;--b-3:#dd6b3d;--b-5:#a8322a}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171816;--panel:#20221f;--ink:#e9ebe6;--muted:#9da198;--line:#363933;--accent:#7aa7ff;
--roadway:#4a4b49;--sidewalk:#5d5e5a;--paving:#6b604b;--sealed:#555651;--gravel:#6b6450;--playground:#6d4d68;--building:#7a5a42;
--lawn:#2f3c2a;--flowerbed:#5a3a4b;--ground:#4d4535;--unknown:#6a3d37;--z-herbaceous:#c9bb52;--tree:#9fd49f;
--b-0:#4fa556;--b-1:#b89a55;--b-2:#c27a3f;--b-3:#c2552c;--b-5:#e0564b}}
:root[data-theme=dark]{--bg:#171816;--panel:#20221f;--ink:#e9ebe6;--muted:#9da198;--line:#363933;--accent:#7aa7ff;
--roadway:#4a4b49;--sidewalk:#5d5e5a;--paving:#6b604b;--sealed:#555651;--gravel:#6b6450;--playground:#6d4d68;--building:#7a5a42;
--lawn:#2f3c2a;--flowerbed:#5a3a4b;--ground:#4d4535;--unknown:#6a3d37;--z-herbaceous:#c9bb52;--tree:#9fd49f;
--b-0:#4fa556;--b-1:#b89a55;--b-2:#c27a3f;--b-3:#c2552c;--b-5:#e0564b}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;display:grid;grid-template-columns:320px 1fr;height:100vh}
aside{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
aside header{padding:14px 16px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0 0 8px}
input{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);font:inherit}
#list{overflow:auto;flex:1}
.item{padding:9px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.item:hover{background:var(--bg)}.item.on{background:var(--bg);box-shadow:inset 3px 0 var(--accent)}
.item b{font-variant-numeric:tabular-nums}.item small{display:block;color:var(--muted)}
main{display:grid;grid-template-rows:minmax(300px,1fr) auto;min-height:0;min-width:0}
#mapwrap{position:relative;min-height:0}
svg{width:100%;height:100%;display:block;cursor:grab;background:var(--bg)}
svg.drag{cursor:grabbing}
.legend{position:absolute;left:10px;bottom:10px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:12px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;max-width:calc(100% - 20px)}
.tools{position:absolute;right:10px;top:10px;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
button{font:inherit;padding:5px 10px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:6px;cursor:pointer}
button[aria-pressed=true]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:4px;vertical-align:-2px}
.z-tree_shrub{background:var(--z-tree_shrub)}.z-tree{background:var(--z-tree)}.z-shrub{background:var(--z-shrub)}.z-herbaceous{background:var(--z-herbaceous)}.z-none{background:var(--z-none)}
.tree-sw{border:1.5px solid var(--tree);border-radius:50%;background:none}
.b-0{background:var(--b-0)}.b-1{background:var(--b-1)}.b-2{background:var(--b-2)}.b-3{background:var(--b-3)}.b-5{background:var(--b-5)}
#legend-bans{display:none}.norms #legend-zones{display:none}.norms #legend-bans{display:flex}
.norms #gz{display:none}#gb{display:none}.norms #gb{display:inline}
#detail{background:var(--panel);border-top:1px solid var(--line);max-height:45vh;overflow:auto;padding:12px 16px}
#detail h2{font-size:15px;margin:0 0 2px}#detail .meta{color:var(--muted);margin-bottom:8px}
table{border-collapse:collapse;width:100%}td,th{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--muted);font-size:12px}td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
tr.hl{background:var(--bg)}tr.patch td{background:var(--bg);font-weight:600;border-top:2px solid var(--line)}
ul{margin:0;padding-left:16px}.muted{color:var(--muted)}.blocked{color:var(--muted);font-size:12px}
.zone{stroke:var(--ink);stroke-opacity:.35;fill-opacity:.85;cursor:pointer}
.zone.dim{fill-opacity:.35;stroke-opacity:.1}.zone.hl{stroke:var(--accent);stroke-opacity:1;fill-opacity:1}
.ban{stroke:var(--panel);stroke-opacity:.6}
.outline{fill:none;stroke:var(--accent);stroke-opacity:0}.outline.on{stroke-opacity:1}
.patch{fill:none;stroke:var(--ink);stroke-opacity:.8;stroke-dasharray:5 3}
.label{fill:var(--ink);paint-order:stroke;stroke:var(--panel);stroke-linejoin:round;text-anchor:middle;dominant-baseline:middle;pointer-events:none;font-weight:600}
.label.u{fill:var(--accent)}
.pl-tree{fill:#1f6b2a;fill-opacity:.55;stroke:#0d3d14;stroke-width:1}.pl-shrub{fill:#6aa84f;fill-opacity:.8;stroke:#2d5a1e;stroke-width:.6}
.pl-herbaceous{fill:#e0a3c8}.bed{fill:#e58fc0;fill-opacity:.75;stroke:#a0437a}
#gpl{display:none}.planted #gpl{display:inline}.planted #gz .zone{fill-opacity:.35}
@media (max-width:760px){body{grid-template-columns:1fr;grid-template-rows:40vh 1fr}aside{border-right:0;border-bottom:1px solid var(--line)}}
</style></head><body>
<aside><header><h1>__TITLE__</h1><input id="q" placeholder="Поиск: P0012, сквер, проезд…"></header><div id="list"></div></aside>
<main><div id="mapwrap"><svg id="map" xmlns="http://www.w3.org/2000/svg"></svg>
<div class="tools"><button id="plant" aria-pressed="false" hidden>Рассадка</button><button id="bans" aria-pressed="false">Почему нет деревьев</button><button id="all">Весь участок</button><button id="theme">Тема</button></div>
<div class="legend" id="legend-zones">__LEGEND__</div><div class="legend" id="legend-bans">__BANS__</div></div>
<section id="detail"><span class="muted">Выберите структуру в списке или на карте. Колесо — масштаб, перетаскивание — сдвиг. «Почему нет деревьев» — наведите на секцию, чтобы увидеть нормы.</span></section></main>
<script>
const D=__DATA__;const NS='http://www.w3.org/2000/svg';const svg=document.getElementById('map');
const el=(t,a,p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);(p||svg).appendChild(e);return e};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let view=D.view.slice();const setView=v=>{view=v;svg.setAttribute('viewBox',v.join(' '));scaleLabels()};
const gs=el('g',{});for(const s of D.surfaces)el('path',{d:s.d,fill:`var(--${s.kind})`,'fill-rule':'evenodd'},gs);
el('path',{d:D.territory,fill:'none',stroke:'var(--muted)','stroke-dasharray':'4 3','vector-effect':'non-scaling-stroke','fill-rule':'evenodd'});
const gz=el('g',{id:'gz'}),gb=el('g',{id:'gb'}),gt=el('g',{}),go=el('g',{}),gp=el('g',{}),gl=el('g',{}),gh=el('g',{});
const step=n=>n>=5?5:n>=3?3:n;
for(const b of D.bans){const p=el('path',{d:b.d,class:'ban','fill-rule':'evenodd',fill:`var(--b-${step(b.n)})`,'vector-effect':'non-scaling-stroke'},gb);el('title',{},p).textContent=b.t}
const gpl=el('g',{id:'gpl'});
for(const b of D.beds){const p=el('path',{d:b.d,class:'bed','fill-rule':'evenodd','vector-effect':'non-scaling-stroke'},gpl);el('title',{},p).textContent=b.t}
for(const [x,y,r,k,t] of D.plants){const c=el('circle',{cx:x,cy:y,r:Math.max(r,.25),class:'pl-'+k,'vector-effect':'non-scaling-stroke'},gpl);el('title',{},c).textContent=t}
const zoneEls={};
for(const s of D.structures){for(const z of s.zones){const p=el('path',{d:z.d,class:'zone','fill-rule':'evenodd',fill:`var(--z-${z.kind})`,'vector-effect':'non-scaling-stroke'},gz);
p.addEventListener('click',e=>{e.stopPropagation();select(s.id,z.id)});(zoneEls[s.id]??={})[z.id]=p}
s.el=el('path',{d:s.outline,class:'outline','fill-rule':'evenodd','vector-effect':'non-scaling-stroke','stroke-width':2.5},gh)}
if(D.rows)el('path',{d:D.rows,fill:'var(--tree)','fill-opacity':.12,stroke:'var(--tree)','stroke-opacity':.4,'stroke-dasharray':'3 3','vector-effect':'non-scaling-stroke'},gt);
for(const [x,y] of D.trees)el('circle',{cx:x,cy:y,r:.6,fill:'none',stroke:'var(--tree)','vector-effect':'non-scaling-stroke'},gt);
for(const o of D.objects){if(o.d)el('path',{d:o.d,fill:'var(--playground)',stroke:'var(--muted)','vector-effect':'non-scaling-stroke'},go);else el('circle',{cx:o.x,cy:o.y,r:.5,fill:'var(--muted)'},go);
el('title',{},go.lastChild).textContent=o.type}
let labels=[];
function scaleLabels(){const px=svg.clientWidth?Math.max(view[2]/svg.clientWidth,view[3]/svg.clientHeight):1;for(const t of labels){const k=t.classList.contains('u')?16:12;t.setAttribute('font-size',k*px);t.setAttribute('stroke-width',3*px)}}
let current=null;
const names={tree_shrub:'деревья, кустарники',tree:'деревья',shrub:'кустарники',herbaceous:'травянистые',none:'ничего'};
function zoneRow(z,zid){return `<tr data-z="${z.id}" class="${z.id===zid?'hl':''}"><td><span class="sw z-${z.kind}"></span><b>${z.id}</b></td><td>${names[z.kind]}</td>
<td class="num">${z.area} м²<br><span class="muted">секций ${z.sections}</span></td>
<td>${esc(z.place)}${z.tags.length?`<br><span class="muted">${z.tags.map(esc).join('; ')}</span>`:''}${z.reason?`<br><span class="muted">${esc(z.reason)}</span>`:''}</td>
<td class="blocked">${z.blocked.length?z.blocked.map(esc).join('<br>'):'—'}</td>
<td>${z.decision?esc(z.decision):'<span class="muted">нет объяснения</span>'}${z.plantings.length?`<ul>${z.plantings.map(p=>`<li>${esc(p)}</li>`).join('')}</ul>`:''}</td></tr>`}
function select(id,zid){const s=D.structures.find(x=>x.id===id);if(!s)return;
if(current!==id){current=id;setView(s.window.slice());history.replaceState(null,'','#'+id)}
for(const x of D.structures)x.el.classList.toggle('on',x.id===id);
for(const sid in zoneEls)for(const zid2 in zoneEls[sid]){const p=zoneEls[sid][zid2];p.classList.toggle('dim',sid!==id);p.classList.toggle('hl',sid===id&&zid2===zid)}
gp.replaceChildren();gl.replaceChildren();labels=[];
for(const p of s.patches){el('path',{d:p.d,class:'patch','fill-rule':'evenodd','vector-effect':'non-scaling-stroke'},gp);
for(const [x,y] of p.labels){const t=el('text',{x,y,class:'label u'},gl);t.textContent=p.id;labels.push(t)}}
for(const z of s.zones)for(const [x,y] of z.labels){const t=el('text',{x,y,class:'label'},gl);t.textContent=z.id;labels.push(t)}
scaleLabels();
document.querySelectorAll('.item').forEach(e=>e.classList.toggle('on',e.dataset.id===id));
const it=document.querySelector(`.item[data-id="${id}"]`);if(it&&zid===undefined)it.scrollIntoView({block:'nearest'});
const rows=s.patches.map(p=>`<tr class="patch"><td colspan="6">Газон ${p.id} · ${p.area} м² · ${esc(p.place)}${p.small?' · мелкий, газон без подбора':''}</td></tr>`
+s.zones.filter(z=>z.patch===p.id).map(z=>zoneRow(z,zid)).join('')).join('');
document.getElementById('detail').innerHTML=`<h2>${s.id} · ${esc(s.name)}</h2>
<div class="meta">${esc(s.place)} · всего ${s.total} м² (${esc(s.composition)}) · газоны ${s.area} м² в ${s.patches.length} шт. · существующих деревьев ${s.trees}${s.status?' · '+s.status:''}</div>
${s.justification?`<p>${esc(s.justification)}</p>`:''}
<table><tr><th>Зона</th><th>Можно</th><th>Площадь</th><th>Место и условия</th><th>Что закрыло деревья и кустарники</th><th>Решение</th></tr>${rows}</table>`;
document.querySelectorAll('#detail tr[data-z]').forEach(r=>{r.onmouseenter=()=>zoneEls[id][r.dataset.z].classList.add('hl');r.onmouseleave=()=>{if(r.dataset.z!==zid)zoneEls[id][r.dataset.z].classList.remove('hl')}});
if(zid){const r=document.querySelector(`#detail tr[data-z="${zid}"]`);if(r)r.scrollIntoView({block:'nearest'})}}
const list=document.getElementById('list');
function renderList(q){q=q.trim().toLowerCase();list.innerHTML=D.structures.filter(s=>!q||(s.id+' '+s.name+' '+s.place).toLowerCase().includes(q))
.map(s=>`<div class="item${s.id===current?' on':''}" data-id="${s.id}"><b>${s.id}</b> · ${esc(s.name)}<small>${esc(s.place)} · всего ${s.total} м², газоны ${s.area} м² · газонов ${s.patches.length}, зон ${s.zones.length}</small></div>`).join('');
list.querySelectorAll('.item').forEach(e=>e.onclick=()=>select(e.dataset.id))}
document.getElementById('q').oninput=e=>renderList(e.target.value);renderList('');
document.getElementById('all').onclick=()=>{current=null;setView(D.view.slice());for(const x of D.structures)x.el.classList.remove('on');
for(const sid in zoneEls)for(const z in zoneEls[sid])zoneEls[sid][z].classList.remove('dim','hl');gp.replaceChildren();gl.replaceChildren();labels=[];
document.querySelectorAll('.item').forEach(e=>e.classList.remove('on'))};
const plantBtn=document.getElementById('plant');
if(D.plants.length||D.beds.length){plantBtn.hidden=false;plantBtn.onclick=()=>{const on=document.body.classList.toggle('planted');plantBtn.setAttribute('aria-pressed',on)};plantBtn.click()}
svg.appendChild(gt);svg.appendChild(gpl);svg.appendChild(gp);svg.appendChild(gl);svg.appendChild(gh);
const bansBtn=document.getElementById('bans');
bansBtn.onclick=()=>{const on=document.body.classList.toggle('norms');bansBtn.setAttribute('aria-pressed',on)};
document.getElementById('theme').onclick=()=>{const r=document.documentElement;const dark=r.dataset.theme?r.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;r.dataset.theme=dark?'light':'dark'};
svg.addEventListener('wheel',e=>{e.preventDefault();const r=svg.getBoundingClientRect();const k=e.deltaY>0?1.2:1/1.2;
const sx=view[2]/r.width,sy=view[3]/r.height,s=Math.max(sx,sy);const ox=view[0]+(view[2]-r.width*s)/2,oy=view[1]+(view[3]-r.height*s)/2;
const mx=ox+(e.clientX-r.left)*s,my=oy+(e.clientY-r.top)*s;setView([mx-(mx-view[0])*k,my-(my-view[1])*k,view[2]*k,view[3]*k])},{passive:false});
let drag=null;svg.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,v:view.slice(),moved:false}});
svg.addEventListener('pointermove',e=>{if(!drag)return;const r=svg.getBoundingClientRect();const s=Math.max(drag.v[2]/r.width,drag.v[3]/r.height);
const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(!drag.moved&&Math.abs(dx)+Math.abs(dy)>3){drag.moved=true;svg.classList.add('drag');svg.setPointerCapture(e.pointerId)}
if(drag.moved)setView([drag.v[0]-dx*s,drag.v[1]-dy*s,drag.v[2],drag.v[3]])});
svg.addEventListener('pointerup',e=>{drag=null;svg.classList.remove('drag')});
addEventListener('resize',scaleLabels);setView(view);
const hash=decodeURIComponent(location.hash.slice(1));if(hash){const [id,mode]=hash.split(':');if(mode==='norms')bansBtn.click();select(id)}
</script></body></html>"""


def main():
    parser = argparse.ArgumentParser(description='HTML-просмотр разбиения на структуры, газоны и зоны')
    parser.add_argument('geojson', help='результат python -m greening (*.greening.geojson)')
    parser.add_argument('--plan', help='*.plan.json — объяснения зон и посадки, необязательно')
    parser.add_argument('--placement', help='*.placement.geojson — рассадка (python -m greening.place), необязательно')
    parser.add_argument('--out', help='файл HTML (по умолчанию <имя>.zones.html рядом с разметкой)')
    args = parser.parse_args()

    source = Path(args.geojson)
    stem = source.name.replace('.greening.geojson', '')
    out = Path(args.out) if args.out else source.with_name(f'{stem}.zones.html')
    geojson = json.loads(source.read_text(encoding='utf-8'))
    plan = json.loads(Path(args.plan).read_text(encoding='utf-8')) if args.plan else None
    placement = json.loads(Path(args.placement).read_text(encoding='utf-8')) if args.placement else None
    out.write_text(render(build(geojson, plan, placement), stem), encoding='utf-8')
    print(json.dumps({'out': str(out), 'size_mb': round(out.stat().st_size / 2 ** 20, 1)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
