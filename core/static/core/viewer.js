// Просмотр результата: карта и объяснения по уровням — участок → структура → газон → растение.
// Данные — view.json задачи (greening/view.build): поверхности, зоны, рассадка, структуры.
// По умолчанию видно только главное: два режима карты вместо списка слоёв (полный список — под кнопкой «Слои»),
// справа — то, что выбрано; подробности (нормы, свойства, зоны газона) раскрываются по запросу.
(function () {
    'use strict';

    const NS = 'http://www.w3.org/2000/svg';
    // метров на пиксель, дальше которых подписи групп кустов скрываются
    const GROUP_LABEL_PX = 0.08;
    // а дальше этого — и номера газонов и зон: издалека они сливаются в кашу
    const NUMBER_LABEL_PX = 0.4;
    // структур больше — над списком появляется поиск
    const SEARCH_FROM = 8;
    const root = document.getElementById('viewer');
    if (!root) return;

    const svg = root.querySelector('.viewer-map svg');
    const info = root.querySelector('.viewer-info');
    const legend = root.querySelector('.vw-legend');
    const layersBox = root.querySelector('.vw-layers-list');
    const layersPanel = root.querySelector('.vw-layers-panel');
    const titleEl = root.querySelector('.viewer-title');
    const metaEl = root.querySelector('.viewer-meta');

    const KIND_NAMES = { tree_shrub: 'деревья и кустарники', tree: 'только деревья', shrub: 'только кустарники',
        herbaceous: 'только газон и цветы', none: 'ничего нельзя' };
    const GROUP_NAMES = { 'ДЛ': 'дерево лиственное', 'ДХ': 'дерево хвойное', 'КЛ': 'кустарник лиственный',
        'КХ': 'кустарник хвойный', 'Л': 'лиана', 'М': 'многолетник', 'О': 'однолетник', 'Б': 'луковичные', 'Г': 'газон' };
    const TIER = { 'ДЛ': 0, 'ДХ': 0, 'КЛ': 1, 'КХ': 1, 'Л': 1 };

    // слои: порядок — снизу вверх
    const LAYERS = [
        { id: 'surfaces', title: 'Покрытия (подоснова)', swatch: 'var(--v-lawn)' },
        { id: 'zones', title: 'Зоны: что можно сажать', swatch: 'var(--z-tree_shrub)' },
        { id: 'lawns', title: 'Газон (посев)', swatch: 'var(--p-lawn)' },
        { id: 'beds', title: 'Цветники', swatch: 'var(--p-bed)' },
        { id: 'territory', title: 'Граница работ', swatch: 'var(--muted)', line: true },
        { id: 'structures', title: 'Границы структур', swatch: 'var(--v-accent)', line: true },
        { id: 'patches', title: 'Границы газонов', swatch: 'var(--text)', line: true },
        { id: 'objects', title: 'Площадки и объекты', swatch: 'var(--v-playground)' },
        { id: 'existing', title: 'Существующие деревья', swatch: 'var(--v-tree)', ring: true },
        { id: 'shrubs', title: 'Новые кустарники', swatch: 'var(--p-shrub)', dot: true },
        { id: 'trees', title: 'Новые деревья', swatch: 'var(--p-tree)', dot: true },
        { id: 'labels_groups', title: 'Подписи групп кустов', swatch: 'var(--p-shrub)', text: true },
        { id: 'labels', title: 'Номера газонов и зон', swatch: 'var(--text)', text: true },
        { id: 'focus', title: '', hidden: true },
    ];
    // режимы карты: какие слои включены и что в легенде
    const MODES = {
        plan: {
            layers: ['surfaces', 'zones', 'beds', 'territory', 'structures', 'patches', 'objects', 'existing', 'shrubs',
                'trees', 'labels_groups', 'labels'],
            legend: [
                { swatch: 'var(--z-tree_shrub)', title: 'зона: можно деревья и кустарники' },
                { swatch: 'var(--z-herbaceous)', title: 'только газон и цветы' },
                { swatch: 'var(--p-tree)', dot: true, title: 'новое дерево' },
                { swatch: 'var(--p-shrub)', title: 'новые кустарники, группа' },
                { swatch: 'var(--p-bed)', title: 'цветник' },
                { swatch: 'var(--v-tree)', ring: true, title: 'существующее дерево' },
                { swatch: 'var(--v-accent)', line: true, title: 'граница структуры' },
                { swatch: 'var(--text)', line: true, title: 'граница газона' },
            ],
        },
        norms: {
            layers: ['surfaces', 'zones', 'territory', 'structures', 'patches', 'objects', 'existing', 'shrubs', 'trees', 'labels'],
            legend: [
                { swatch: 'var(--z-tree_shrub)', title: 'можно деревья и кустарники' },
                { swatch: 'var(--z-tree)', title: 'только деревья' },
                { swatch: 'var(--z-shrub)', title: 'только кустарники' },
                { swatch: 'var(--z-herbaceous)', title: 'только газон и цветы' },
                { swatch: 'var(--z-none)', title: 'ничего нельзя' },
            ],
            note: 'Зоны построены по нормативным отступам от сетей, зданий, проездов и площадок. Щёлкните по зоне — почему здесь так.',
        },
    };

    let D = null;
    let mode = 'plan';
    let view = [0, 0, 1, 1];
    let groups = {};
    let labels = [];
    // подписи групп кустов живут всё время просмотра, номера газонов и зон — только у выбранной структуры
    let groupLabels = [];
    // что открыто справа: {sid, tab, planting, zone, species}
    let state = {};
    let byId = {};
    const visible = {};

    function el(tag, attrs, parent) {
        const node = document.createElementNS(NS, tag);
        for (const key in attrs) node.setAttribute(key, attrs[key]);
        (parent || svg).appendChild(node);
        return node;
    }

    function esc(text) {
        return String(text == null ? '' : text).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    function cap(text) { text = String(text || ''); return text.charAt(0).toUpperCase() + text.slice(1); }
    function num(value) { return Math.round(+value || 0).toLocaleString('ru-RU'); }
    function title(node, text) { el('title', {}, node).textContent = text; }
    function amount(value, unit) { return num(value) + ' ' + (unit === 'шт' ? 'шт' : 'м²'); }
    function list(items) { return '<ul>' + items.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>'; }

    function swatch(item) {
        const kind = item.line ? ' is-line' : item.ring ? ' is-ring' : item.dot ? ' is-dot' : item.text ? ' is-text' : '';
        return '<span class="viewer-swatch' + kind + '" style="--sw:' + item.swatch + '"></span>';
    }

    // ---------- карта ----------

    function setView(next) {
        view = next;
        svg.setAttribute('viewBox', view.join(' '));
        scaleLabels();
    }

    function pixel() {
        const w = svg.clientWidth || 1;
        const h = svg.clientHeight || 1;
        return Math.max(view[2] / w, view[3] / h);
    }

    function scaleLabels() {
        const px = pixel();
        // подписи групп — только вблизи: издалека они длиннее самих групп и налезают друг на друга
        svg.classList.toggle('is-far', px > GROUP_LABEL_PX);
        svg.classList.toggle('is-very-far', px > NUMBER_LABEL_PX);
        labels.concat(groupLabels).forEach(function (t) {
            const size = t.classList.contains('is-patch') ? 15 : t.classList.contains('v-group-label') ? 10 : 12;
            t.setAttribute('font-size', size * px);
            t.setAttribute('stroke-width', 3 * px);
        });
        // кроны меньше 2 пикселей не видны — рисуем их хотя бы точкой
        const min = 1.2 * px;
        svg.querySelectorAll('.v-plant').forEach(function (c) {
            c.setAttribute('r', Math.max(+c.dataset.r, min));
        });
    }

    function draw() {
        svg.replaceChildren();
        groups = {};
        groupLabels = [];
        byId = {};
        LAYERS.forEach(function (layer) { groups[layer.id] = el('g', { 'data-layer': layer.id }); });

        D.surfaces.forEach(function (s) {
            el('path', { d: s.d, 'class': 'v-surf s-' + s.kind, 'fill-rule': 'evenodd' }, groups.surfaces);
        });
        el('path', { d: D.territory, 'class': 'v-territory', 'fill-rule': 'evenodd' }, groups.territory);

        D.structures.forEach(function (s) {
            byId[s.id] = s;
            s.zoneEls = {};
            s.zones.forEach(function (z) {
                const p = el('path', { d: z.d, 'class': 'v-zone z-' + z.kind, 'fill-rule': 'evenodd' }, groups.zones);
                p.dataset.s = s.id;
                p.dataset.z = z.id;
                title(p, z.id + ': ' + KIND_NAMES[z.kind]);
                s.zoneEls[z.id] = p;
            });
            s.outlineEl = el('path', { d: s.outline, 'class': 'v-structure', 'fill-rule': 'evenodd' }, groups.structures);
            s.outlineEl.dataset.s = s.id;
            title(s.outlineEl, cap(s.name) + ' · ' + s.id);
            s.patches.forEach(function (p) {
                el('path', { d: p.d, 'class': 'v-patch', 'fill-rule': 'evenodd' }, groups.patches);
            });
            s.count = { tree: 0, shrub: 0 };
        });

        D.lawns.forEach(function (b) {
            const p = el('path', { d: b.d, 'class': 'v-lawn', 'fill-rule': 'evenodd' }, groups.lawns);
            p.dataset.s = b.s; p.dataset.i = b.i;
            title(p, b.t);
        });
        D.beds.forEach(function (b) {
            const p = el('path', { d: b.d, 'class': 'v-bed', 'fill-rule': 'evenodd' }, groups.beds);
            p.dataset.s = b.s; p.dataset.i = b.i;
            title(p, b.t);
        });

        D.objects.forEach(function (o) {
            const node = o.d
                ? el('path', { d: o.d, 'class': 'v-object' }, groups.objects)
                : el('circle', { cx: o.x, cy: o.y, r: 0.5, 'class': 'v-object-point' }, groups.objects);
            title(node, o.type);
        });

        if (D.rows) el('path', { d: D.rows, 'class': 'v-rows' }, groups.existing);
        // существующее дерево: условная крона пунктиром (в подоснове размера кроны нет) и точка ствола
        const crown = (D.existing_crown || 6) / 2;
        D.trees.forEach(function (t) {
            const c = el('circle', { cx: t[0], cy: t[1], r: crown, 'class': 'v-existing' }, groups.existing);
            title(c, 'Существующее дерево — сохраняется (крона условно ' + (crown * 2) + ' м)');
            el('circle', { cx: t[0], cy: t[1], r: 0.3, 'class': 'v-existing-trunk' }, groups.existing);
        });

        // группы и изгороди кустов — один контур с подписью «вид ×N»; внутри — точки посадочных мест
        (D.groups || []).forEach(function (g) {
            const p = el('path', { d: g.d, 'class': 'v-group', 'fill-rule': 'evenodd' }, groups.shrubs);
            p.dataset.s = g.s; p.dataset.i = g.i;
            title(p, g.t);
        });
        (D.groups || []).forEach(function (g) {
            if (!g.at || !g.at.length) return;
            const t = el('text', { x: g.at[0][0], y: g.at[0][1], 'class': 'v-label v-group-label' }, groups.labels_groups);
            t.textContent = g.label;
            groupLabels.push(t);
        });

        D.plants.forEach(function (p) {
            const group = p[3] === 'tree' ? groups.trees : groups.shrubs;
            const c = el('circle', { cx: p[0], cy: p[1], r: p[2], 'class': 'v-plant pl-' + p[3] }, group);
            c.dataset.r = p[2];
            c.dataset.s = p[5];
            c.dataset.i = p[6];
            title(c, p[4]);
            const s = byId[p[5]];
            if (s && s.count[p[3]] != null) s.count[p[3]] += 1;
        });
        applyLayers();
    }

    function applyLayers() {
        LAYERS.forEach(function (layer) { groups[layer.id].style.display = layer.hidden || visible[layer.id] ? '' : 'none'; });
        svg.classList.toggle('is-norms', mode === 'norms');
    }

    function setMode(next) {
        mode = next;
        LAYERS.forEach(function (layer) { visible[layer.id] = MODES[mode].layers.indexOf(layer.id) !== -1; });
        root.querySelectorAll('.vw-modes button').forEach(function (b) { b.classList.toggle('is-on', b.dataset.mode === mode); });
        if (groups.surfaces) applyLayers();
        renderLayers();
        renderLegend();
        drawLabels();
    }

    function renderLayers() {
        layersBox.innerHTML = LAYERS.filter(function (l) { return !l.hidden; }).reverse().map(function (layer) {
            return '<label class="viewer-layer"><input type="checkbox" data-layer="' + layer.id + '"' +
                (visible[layer.id] ? ' checked' : '') + '>' + swatch(layer) + esc(layer.title) + '</label>';
        }).join('');
        layersBox.querySelectorAll('input').forEach(function (input) {
            input.addEventListener('change', function () {
                visible[input.dataset.layer] = input.checked;
                applyLayers();
            });
        });
    }

    function renderLegend() {
        const m = MODES[mode];
        legend.innerHTML = m.legend.map(function (item) {
            return '<span>' + swatch(item) + esc(item.title) + '</span>';
        }).join('') + (m.note ? '<p>' + esc(m.note) + '</p>' : '');
    }

    // номера газонов (план) или зон (нормы) — только у выбранной структуры, иначе карта в цифрах
    function drawLabels() {
        if (!groups.labels) return;
        groups.labels.replaceChildren();
        labels = [];
        const s = byId[state.sid];
        if (s) {
            if (mode === 'plan') {
                s.patches.forEach(function (p) {
                    if (p.small) return;
                    p.labels.slice(0, 1).forEach(function (at) {
                        const t = el('text', { x: at[0], y: at[1], 'class': 'v-label is-patch' }, groups.labels);
                        t.textContent = p.id;
                        labels.push(t);
                    });
                });
            } else {
                s.zones.forEach(function (z) {
                    z.labels.slice(0, 1).forEach(function (at) {
                        const t = el('text', { x: at[0], y: at[1], 'class': 'v-label' }, groups.labels);
                        t.textContent = z.id;
                        labels.push(t);
                    });
                });
            }
        }
        scaleLabels();
    }

    // подсветка: растения и контуры посадок, а также контур газона или зоны поверх всего
    function highlight(sid, indices) {
        svg.querySelectorAll('.is-hl').forEach(function (n) { n.classList.remove('is-hl'); });
        if (sid == null) return;
        [].concat(indices).forEach(function (i) {
            svg.querySelectorAll('[data-s="' + sid + '"][data-i="' + i + '"]').forEach(function (n) { n.classList.add('is-hl'); });
        });
    }

    function focus(d) {
        groups.focus.replaceChildren();
        if (d) el('path', { d: d, 'class': 'v-focus', 'fill-rule': 'evenodd' }, groups.focus);
    }

    // ---------- данные для объяснений ----------

    function patchOf(s, zoneId) {
        const z = s.zones.find(function (x) { return x.id === zoneId; });
        return z ? z.patch : '';
    }

    // посадки структуры по видам: один вид — одна карточка, сколько бы зон он ни занимал
    function species(s) {
        if (s.species) return s.species;
        const map = {};
        s.plantings.forEach(function (p) {
            const key = p.name;
            const sp = map[key] || (map[key] = { name: p.name, group: p.group, unit: p.unit, placed: 0, items: [],
                roles: [], reasons: [], norms: [], facts: p.facts || [], warnings: [], byCode: true, missed: [] });
            sp.items.push(p);
            // где рассадка не нашла места, у посадки пометка об этом — для вида это сноска, а не предупреждение
            if (!(+p.placed > 0)) { sp.missed.push(p.zone); return; }
            sp.placed += +p.placed || 0;
            if (p.role && sp.roles.indexOf(p.role) === -1) sp.roles.push(p.role);
            if (p.reason && sp.reasons.indexOf(p.reason) === -1) sp.reasons.push(p.reason);
            (p.norms || []).forEach(function (n) { if (sp.norms.indexOf(n) === -1) sp.norms.push(n); });
            (p.warnings || []).forEach(function (w) { if (sp.warnings.indexOf(w) === -1) sp.warnings.push(w); });
            if (p.by !== 'code' && p.by !== 'rules') sp.byCode = false;
        });
        s.species = Object.keys(map).map(function (k) { return map[k]; }).sort(function (a, b) {
            return (TIER[a.group] != null ? TIER[a.group] : 2) - (TIER[b.group] != null ? TIER[b.group] : 2) ||
                (b.placed > 0) - (a.placed > 0) || b.placed - a.placed;
        });
        return s.species;
    }

    // что посажено на газоне: виды с количеством
    function patchContents(s, patchId) {
        const zones = s.zones.filter(function (z) { return z.patch === patchId; }).map(function (z) { return z.id; });
        const map = {};
        s.plantings.forEach(function (p) {
            if (zones.indexOf(p.zone) === -1 || !(+p.placed > 0)) return;
            const it = map[p.name] || (map[p.name] = { name: p.name, unit: p.unit, placed: 0, group: p.group, i: [] });
            it.placed += +p.placed;
            it.i.push(p.i);
        });
        return Object.keys(map).map(function (k) { return map[k]; }).sort(function (a, b) {
            return (TIER[a.group] != null ? TIER[a.group] : 2) - (TIER[b.group] != null ? TIER[b.group] : 2);
        });
    }

    function normsList(norms) {
        return '<ul class="vw-checks">' + norms.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
    }

    function chips(items) {
        return '<div class="vw-chips">' + items.map(function (x) { return '<span>' + esc(x) + '</span>'; }).join('') + '</div>';
    }

    // ---------- правая панель ----------

    function render() {
        highlight(null);
        focus(null);
        const s = byId[state.sid];
        if (!s) info.innerHTML = overviewHtml();
        else if (state.planting != null) info.innerHTML = plantingHtml(s, s.plantings.find(function (p) { return p.i === state.planting; }));
        else if (state.zone) info.innerHTML = zoneHtml(s, s.zones.find(function (z) { return z.id === state.zone; }));
        else info.innerHTML = structureHtml(s);
        bindPanel(s);
        info.scrollTop = 0;
        D.structures.forEach(function (x) { x.outlineEl.classList.toggle('is-on', x.id === state.sid); });
        svg.querySelectorAll('.v-zone').forEach(function (z) { z.classList.toggle('is-dim', !!state.sid && z.dataset.s !== state.sid); });
        if (s && state.planting != null) highlight(s.id, state.planting);
        if (s && state.zone) focus(s.zoneEls[state.zone].getAttribute('d'));
    }

    function overviewHtml() {
        const sum = D.summary || {};
        const plants = sum.plants || {};
        const count = function (k) { return plants[k] ? plants[k].placed : 0; };
        const ok = sum.clearance_violations === 0 || (sum.clearance_violations == null && sum.violations === 0);
        const q = (state.query || '').trim().toLowerCase();
        const items = D.structures.filter(function (s) {
            return !q || (s.id + ' ' + s.name + ' ' + s.place).toLowerCase().indexOf(q) !== -1;
        });
        return '<h2>Участок целиком</h2>' +
            '<p class="viewer-muted">Территория разбита на ' + D.structures.length + ' ' + plural(D.structures.length, 'структуру', 'структуры', 'структур') +
            ' — дворы, скверы, полосы вдоль улиц. У каждой — свой замысел озеленения. Выберите структуру в списке или на карте.</p>' +
            '<div class="vw-stats">' +
            stat(count('tree'), 'новых деревьев') + stat(count('shrub'), 'кустарников') +
            stat(count('herbaceous_m2'), 'м² газона и цветников') +
            '</div>' +
            (sum.violations != null || sum.clearance_violations != null
                ? '<p class="vw-status' + (ok ? ' is-ok' : ' is-bad') + '">' + (ok
                    ? 'Нормативные отступы соблюдены: каждое растение проверено по исходному чертежу'
                    : 'Есть нарушения: ' + (sum.clearance_violations || sum.violations) + ' — подробности в проверке отступов') + '</p>' : '') +
            '<h3 class="vw-h3">Структуры</h3>' +
            (D.structures.length > SEARCH_FROM ? '<input type="search" class="viewer-search" placeholder="Поиск: двор, улица, P0002" value="' + esc(state.query || '') + '">' : '') +
            '<div class="vw-list">' + items.map(function (s) {
                return '<button type="button" class="vw-item" data-s="' + s.id + '"><span class="vw-item-title">' + esc(cap(s.name)) +
                    '<i>' + s.id + '</i></span><small>' + esc(s.place) + '</small><small>' +
                    'газоны ' + num(s.area) + ' м² · деревьев ' + s.count.tree + ' · кустарников ' + s.count.shrub + '</small></button>';
            }).join('') + '</div>';
    }

    function stat(value, label) { return '<div><b>' + num(value) + '</b><span>' + esc(label) + '</span></div>'; }

    function plural(n, one, few, many) {
        const m10 = n % 10, m100 = n % 100;
        return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
    }

    function back(label, target) {
        return '<button type="button" class="vw-back" data-back="' + target + '">← ' + esc(label) + '</button>';
    }

    function structureHead(s) {
        return '<h2>' + esc(cap(s.name)) + ' <i class="vw-id">' + s.id + '</i></h2>' +
            '<p class="viewer-muted">' + esc(cap(s.place)) + '</p>';
    }

    function structureHtml(s) {
        const sp = species(s);
        const tab = state.tab || 'idea';
        const patches = s.patches.filter(function (p) { return !p.small; });
        return back('Весь участок', 'overview') + structureHead(s) +
            '<div class="vw-stats is-small">' + stat(s.count.tree, 'новых деревьев') + stat(s.count.shrub, 'кустарников') +
            stat(s.area, 'м² газонов') + stat(s.trees, 'деревьев сохраняется') + '</div>' +
            '<div class="viewer-tabs">' +
            tabButton('idea', 'Замысел', tab) + tabButton('patches', 'Газоны · ' + patches.length, tab) +
            tabButton('plants', 'Растения · ' + sp.length, tab) + '</div>' +
            '<div class="vw-tab">' + (tab === 'idea' ? ideaHtml(s, sp) : tab === 'patches' ? patchesHtml(s) : plantsHtml(s, sp)) + '</div>';
    }

    function tabButton(id, text, current) {
        return '<button type="button" data-tab="' + id + '"' + (id === current ? ' class="is-on"' : '') + '>' + esc(text) + '</button>';
    }

    function ideaHtml(s, sp) {
        const planted = sp.filter(function (x) { return x.placed > 0; });
        const note = s.status === 'small' ? 'Газоны мелкие — засеваются газоном без подбора растений.'
            : s.status === 'rules' ? 'Растения подобраны правилами (LLM не ответила по этой структуре).' : '';
        return (note ? '<p class="viewer-muted">' + esc(note) + '</p>' : '') +
            (planted.length ? '<h3 class="vw-h3">Что посажено</h3><div class="vw-chips is-link">' + planted.map(function (x) {
                return '<button type="button" data-species="' + esc(x.name) + '">' + esc(x.name) + ' <b>' + amount(x.placed, x.unit) + '</b></button>';
            }).join('') + '</div>' : '') +
            (s.justification ? '<h3 class="vw-h3">Замысел</h3><p class="vw-idea is-clamped">' + esc(s.justification) + '</p>' +
                '<button type="button" class="vw-more">Читать полностью</button>' : '') +
            replacedNote(s) +
            '<p class="vw-tip">Подробнее — во вкладках: <b>Газоны</b> — что и почему на каждом газоне, <b>Растения</b> — обоснование и нормы по каждому виду.</p>';
    }

    // замысел LLM написан до проверки норм: если код заменил деревья, об этом — под текстом
    function replacedNote(s) {
        const pairs = [];
        s.plantings.forEach(function (p) {
            if (!p.replaced) return;
            const pair = '«' + p.replaced.name + '» → «' + p.name + '»';
            if (pairs.indexOf(pair) === -1) pairs.push(pair);
        });
        if (!pairs.length) return '';
        return '<p class="vw-note">При проверке норм заменено: ' + esc(pairs.join(', ')) + ' — у исходного вида широкая крона, ' +
            'а до жилого дома меньше 10 м (743-ПП, табл. 3.6.1, примечание 3). Замысел выше написан до замены.</p>';
    }

    function patchesHtml(s) {
        const big = s.patches.filter(function (p) { return !p.small; }).sort(function (a, b) { return b.area - a.area; });
        const small = s.patches.filter(function (p) { return p.small; });
        return '<p class="viewer-muted">Газон — сплошной участок зелени между дорожками. Внутри он делится на зоны по нормам: где можно деревья, где только кустарники или газон.</p>' +
            big.map(function (p) { return patchCard(s, p); }).join('') +
            (small.length ? '<p class="viewer-muted">Ещё ' + small.length + ' ' + plural(small.length, 'мелкий газон', 'мелких газона', 'мелких газонов') +
                ' (меньше 20 м²) — засеваются газоном без подбора.</p>' : '');
    }

    function patchCard(s, p) {
        const zones = s.zones.filter(function (z) { return z.patch === p.id; });
        const contents = patchContents(s, p.id);
        return '<article class="viewer-card vw-patch" data-patch="' + p.id + '">' +
            '<header><b>Газон ' + p.id + '</b><span>' + num(p.area) + ' м² · ' + esc(p.place) + '</span></header>' +
            (contents.length ? '<ul class="vw-amounts">' + contents.map(function (c) {
                return '<li data-i="' + c.i.join(',') + '"><span>' + esc(c.name) + '</span><b>' + amount(c.placed, c.unit) + '</b></li>';
            }).join('') + '</ul>' : '<p class="viewer-muted">Без новых посадок</p>') +
            '<details><summary>Зоны по нормам и решения · ' + zones.length + '</summary>' +
            zones.map(function (z) { return zoneRow(z); }).join('') + '</details></article>';
    }

    function zoneRow(z) {
        return '<div class="vw-zone" data-z="' + z.id + '">' +
            '<div class="vw-zone-head"><span class="viewer-dot z-' + z.kind + '"></span><b>' + z.id + '</b> · ' + num(z.area) +
            ' м² · ' + esc(KIND_NAMES[z.kind]) + '</div>' +
            (z.explanation ? '<p class="viewer-place">' + esc(cap(z.explanation)) + '</p>' : '') +
            (z.decision ? '<p>' + esc(cap(z.decision)) + '</p>' : '') +
            (z.blocked && z.blocked.length ? '<p class="viewer-facts">Почему ограничено: ' + z.blocked.map(esc).join('; ') + '</p>' : '') +
            '</div>';
    }

    function plantsHtml(s, sp) {
        return sp.map(function (x) {
            const open = state.species === x.name;
            const where = x.items.filter(function (p) { return +p.placed > 0; })
                .map(function (p) { return 'газон ' + patchOf(s, p.zone) + ' (' + p.zone + ')'; });
            return '<article class="viewer-card vw-species' + (open ? ' is-on' : '') + '" data-species="' + esc(x.name) + '">' +
                '<header><b>' + esc(x.name) + '</b><span>' + esc(GROUP_NAMES[x.group] || '') + ' · ' + esc(x.roles.join(', ')) +
                ' · ' + (x.placed > 0 ? amount(x.placed, x.unit) : 'места не нашлось') + '</span></header>' +
                (x.byCode ? '<div class="viewer-tags"><span>подобрано правилами</span></div>' : '') +
                (x.reasons[0] ? '<p>' + esc(cap(x.reasons[0])) + '</p>' : '') +
                (x.warnings.length ? '<ul class="viewer-warn">' + x.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
                '<details' + (open ? ' open' : '') + '><summary>Нормы, свойства и места посадки</summary>' +
                (x.norms.length ? '<div class="viewer-sub">Соответствие нормам</div>' + normsList(x.norms) : '') +
                (x.facts.length ? '<div class="viewer-sub">Свойства по справочнику</div>' + chips(x.facts) : '') +
                (where.length ? '<div class="viewer-sub">Где посажено</div><p class="viewer-facts">' + esc(where.join(', ')) + '</p>' : '') +
                (x.missed.length ? '<p class="viewer-facts">Ещё ' + x.missed.length + ' ' + plural(x.missed.length, 'место', 'места', 'мест') +
                    ' (' + esc(x.missed.join(', ')) + ') — подходило по нормам, но было занято другими посадками и существующими деревьями.</p>' : '') +
                (x.reasons.length > 1 ? '<div class="viewer-sub">Обоснования для других мест</div>' + list(x.reasons.slice(1).map(cap)) : '') +
                '</details></article>';
        }).join('');
    }

    function plantingHtml(s, p) {
        if (!p) return structureHtml(s);
        const z = s.zones.find(function (x) { return x.id === p.zone; }) || {};
        return back(cap(s.name) + ' · ' + s.id, 'structure') +
            '<h2>' + esc(p.name) + '</h2>' +
            '<p class="viewer-muted">' + esc(cap(GROUP_NAMES[p.group] || '')) + ' · ' + esc(p.role) + ' · здесь ' + amount(p.placed, p.unit) + '</p>' +
            (p.replaced ? '<div class="viewer-tags"><span>заменено при проверке норм</span></div>'
                : p.by === 'code' || p.by === 'rules' ? '<div class="viewer-tags"><span>подобрано правилами</span></div>' : '') +
            '<div class="vw-block"><div class="viewer-sub">Где</div><p>Газон ' + esc(z.patch) + ', зона ' + esc(p.zone) +
            (z.explanation ? ' — ' + esc(z.explanation) : '') + '</p>' +
            '<p class="viewer-facts">По нормам здесь можно: ' + esc(KIND_NAMES[z.kind] || '') + '</p></div>' +
            (p.reason ? '<div class="vw-block"><div class="viewer-sub">Почему это растение</div><p>' + esc(cap(p.reason)) + '</p>' +
                replacedHtml(p) + '</div>' : '') +
            (p.norms && p.norms.length ? '<div class="vw-block"><div class="viewer-sub">Соответствие нормам</div>' + normsList(p.norms) + '</div>' : '') +
            (p.facts && p.facts.length ? '<div class="vw-block"><div class="viewer-sub">Свойства по справочнику</div>' + chips(p.facts) + '</div>' : '') +
            (p.warnings && p.warnings.length ? '<ul class="viewer-warn">' + p.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '') +
            '<div class="vw-links"><button type="button" class="viewer-button" data-goto-species="' + esc(p.name) + '">Все посадки вида в структуре</button>' +
            '<button type="button" class="viewer-button" data-goto-zone="' + esc(p.zone) + '">Решение по зоне</button></div>';
    }

    // дерево поставлено кодом вместо выбранного LLM (широкая крона у здания): исходный выбор — по запросу
    function replacedHtml(p) {
        if (!p.replaced) return '';
        return '<details class="vw-replaced"><summary>Что предлагалось сначала</summary><p>«' + esc(p.replaced.name) + '»' +
            (p.replaced.reason ? ': ' + esc(p.replaced.reason) : '') + '</p></details>';
    }

    function zoneHtml(s, z) {
        if (!z) return structureHtml(s);
        const plantings = s.plantings.filter(function (p) { return p.zone === z.id && +p.placed > 0; });
        return back(cap(s.name) + ' · ' + s.id, 'structure') +
            '<h2><span class="viewer-dot z-' + z.kind + '"></span>Зона ' + z.id + ' <i class="vw-id">газон ' + esc(z.patch) + '</i></h2>' +
            '<p class="viewer-muted">' + num(z.area) + ' м² · по нормам ' + esc(KIND_NAMES[z.kind]) + '</p>' +
            (z.explanation || z.place ? '<div class="vw-block"><div class="viewer-sub">Что это за место</div><p>' + esc(cap(z.explanation || z.place)) + '</p></div>' : '') +
            '<div class="vw-block"><div class="viewer-sub">Решение</div>' +
            (z.decision ? '<p>' + esc(cap(z.decision)) + '</p>' : '<p class="viewer-muted">Нет объяснения</p>') +
            (plantings.length ? list(plantings.map(function (p) { return p.name + ' — ' + p.role + ', ' + amount(p.placed, p.unit); })) : '') + '</div>' +
            (z.blocked && z.blocked.length ? '<div class="vw-block"><div class="viewer-sub">Что ограничило посадки</div>' + list(z.blocked) + '</div>' : '') +
            (z.reason ? '<div class="vw-block"><div class="viewer-sub">Почему пусто</div><p>' + esc(cap(z.reason)) + '</p></div>' : '') +
            (z.tags.length ? '<div class="vw-block"><div class="viewer-sub">Условия места</div>' + chips(z.tags) + '</div>' : '');
    }

    function bindPanel(s) {
        info.querySelectorAll('[data-back]').forEach(function (b) {
            b.addEventListener('click', function () {
                if (b.dataset.back === 'overview') { open({}); setView(D.view.slice()); }
                else open({ sid: s.id, tab: state.tab });
            });
        });
        const searchInput = info.querySelector('.viewer-search');
        if (searchInput) {
            searchInput.addEventListener('input', function () {
                state.query = searchInput.value;
                const at = searchInput.selectionStart;
                render();
                const again = info.querySelector('.viewer-search');
                again.focus();
                again.setSelectionRange(at, at);
            });
        }
        info.querySelectorAll('.vw-item').forEach(function (b) {
            b.addEventListener('click', function () { select(b.dataset.s, { zoom: true }); });
            b.addEventListener('mouseenter', function () { focus(byId[b.dataset.s].outline); });
            b.addEventListener('mouseleave', function () { focus(null); });
        });
        if (!s) return;
        info.querySelectorAll('.viewer-tabs button').forEach(function (b) {
            b.addEventListener('click', function () { open({ sid: s.id, tab: b.dataset.tab }); });
        });
        info.querySelectorAll('[data-species]').forEach(function (node) {
            const sp = species(s).find(function (x) { return x.name === node.dataset.species; });
            if (!sp) return;
            const indices = sp.items.map(function (p) { return p.i; });
            node.addEventListener('mouseenter', function () { highlight(s.id, indices); });
            node.addEventListener('mouseleave', function () { highlight(null); });
            if (node.tagName === 'BUTTON') {
                node.addEventListener('click', function () { open({ sid: s.id, tab: 'plants', species: sp.name }); });
            }
        });
        info.querySelectorAll('.vw-amounts li').forEach(function (li) {
            li.addEventListener('mouseenter', function () { highlight(s.id, li.dataset.i.split(',').map(Number)); });
            li.addEventListener('mouseleave', function () { highlight(null); });
        });
        info.querySelectorAll('.vw-patch').forEach(function (card) {
            const p = s.patches.find(function (x) { return x.id === card.dataset.patch; });
            card.addEventListener('mouseenter', function () { focus(p.d); });
            card.addEventListener('mouseleave', function () { focus(null); });
        });
        info.querySelectorAll('.vw-zone').forEach(function (row) {
            const patch = s.patches.find(function (x) { return x.id === row.closest('.vw-patch').dataset.patch; });
            row.addEventListener('mouseenter', function () { focus(s.zoneEls[row.dataset.z].getAttribute('d')); });
            row.addEventListener('mouseleave', function () { focus(patch.d); });
        });
        const more = info.querySelector('.vw-more');
        if (more) {
            const idea = info.querySelector('.vw-idea');
            // короткий текст и так виден целиком
            if (idea.scrollHeight <= idea.clientHeight + 2) more.hidden = true;
            more.addEventListener('click', function () {
                const clamped = idea.classList.toggle('is-clamped');
                more.textContent = clamped ? 'Читать полностью' : 'Свернуть';
            });
        }
        info.querySelectorAll('[data-goto-species]').forEach(function (b) {
            b.addEventListener('click', function () { open({ sid: s.id, tab: 'plants', species: b.dataset.gotoSpecies }); });
        });
        info.querySelectorAll('[data-goto-zone]').forEach(function (b) {
            b.addEventListener('click', function () { open({ sid: s.id, zone: b.dataset.gotoZone }); });
        });
        // открытая карточка — в поле зрения
        const on = info.querySelector('.vw-species.is-on');
        if (on) requestAnimationFrame(function () { on.scrollIntoView({ block: 'center' }); });
    }

    function open(next) {
        const changed = next.sid !== state.sid;
        state = Object.assign({ query: state.query }, next);
        render();
        if (changed) drawLabels();
    }

    function select(sid, options) {
        const s = byId[sid];
        if (!s) return;
        options = options || {};
        if (options.zoom) setView(s.window.slice());
        open({ sid: sid, tab: options.tab || (state.sid === sid ? state.tab : 'idea'), planting: options.planting,
            zone: options.zone, species: options.species });
    }

    // ---------- масштаб, сдвиг и щелчки по карте ----------

    let drag = null;

    svg.addEventListener('wheel', function (e) {
        e.preventDefault();
        const box = svg.getBoundingClientRect();
        const k = e.deltaY > 0 ? 1.2 : 1 / 1.2;
        const s = pixel();
        const ox = view[0] + (view[2] - box.width * s) / 2;
        const oy = view[1] + (view[3] - box.height * s) / 2;
        const mx = ox + (e.clientX - box.left) * s;
        const my = oy + (e.clientY - box.top) * s;
        setView([mx - (mx - view[0]) * k, my - (my - view[1]) * k, view[2] * k, view[3] * k]);
    }, { passive: false });

    svg.addEventListener('pointerdown', function (e) {
        drag = { x: e.clientX, y: e.clientY, v: view.slice(), moved: false };
    });

    svg.addEventListener('pointermove', function (e) {
        if (!drag) return;
        const dx = e.clientX - drag.x;
        const dy = e.clientY - drag.y;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 3) {
            drag.moved = true;
            svg.classList.add('is-dragging');
            svg.setPointerCapture(e.pointerId);
        }
        if (drag.moved) {
            const box = svg.getBoundingClientRect();
            const s = Math.max(drag.v[2] / box.width, drag.v[3] / box.height);
            setView([drag.v[0] - dx * s, drag.v[1] - dy * s, drag.v[2], drag.v[3]]);
        }
    });

    svg.addEventListener('pointerup', function (e) {
        const moved = drag && drag.moved;
        drag = null;
        svg.classList.remove('is-dragging');
        if (moved) return;
        // щелчок: растение и цветник → посадка, зона → её объяснение, контур структуры → структура
        const target = document.elementFromPoint(e.clientX, e.clientY);
        if (!target || !target.dataset) return;
        if (target.dataset.i != null && target.dataset.s) select(target.dataset.s, { planting: +target.dataset.i });
        else if (target.dataset.z) select(target.dataset.s, { zone: target.dataset.z });
        else if (target.dataset.s) select(target.dataset.s, { tab: state.tab });
    });

    root.querySelector('[data-action="fit"]').addEventListener('click', function () { setView(D.view.slice()); });
    root.querySelector('[data-action="zoom-in"]').addEventListener('click', function () { zoom(1 / 1.5); });
    root.querySelector('[data-action="zoom-out"]').addEventListener('click', function () { zoom(1.5); });

    function zoom(k) {
        const cx = view[0] + view[2] / 2;
        const cy = view[1] + view[3] / 2;
        setView([cx - view[2] * k / 2, cy - view[3] * k / 2, view[2] * k, view[3] * k]);
    }

    root.querySelectorAll('.vw-modes button').forEach(function (b) {
        b.addEventListener('click', function () { setMode(b.dataset.mode); });
    });
    root.querySelector('[data-action="layers"]').addEventListener('click', function (e) {
        e.stopPropagation();
        layersPanel.hidden = !layersPanel.hidden;
    });
    root.addEventListener('click', function (e) {
        if (!layersPanel.hidden && !layersPanel.contains(e.target)) layersPanel.hidden = true;
    });

    window.addEventListener('resize', function () { if (!root.hidden) scaleLabels(); });

    function close() {
        root.hidden = true;
        document.body.classList.remove('has-viewer');
    }

    root.querySelector('[data-action="close"]').addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape' || root.hidden) return;
        if (!layersPanel.hidden) layersPanel.hidden = true;
        else close();
    });

    window.GreeningViewer = {
        // data — view.json; meta — {name, summary, links: {dxf, plan}}
        open: function (data, meta) {
            if (D !== data) {
                D = data;
                D.summary = meta.summary;
                state = {};
                setMode('plan');
                draw();
                render();
            }
            titleEl.textContent = meta.name;
            metaEl.textContent = D.structures.length + ' ' + plural(D.structures.length, 'структура', 'структуры', 'структур') +
                ' · ' + num(D.plants.length) + ' растений';
            root.querySelector('[data-link="dxf"]').href = meta.links.dxf;
            root.querySelector('[data-link="plan"]').href = meta.links.plan;
            root.hidden = false;
            document.body.classList.add('has-viewer');
            requestAnimationFrame(function () { setView(state.sid ? byId[state.sid].window.slice() : D.view.slice()); });
        },
        close: close,
    };
}());
