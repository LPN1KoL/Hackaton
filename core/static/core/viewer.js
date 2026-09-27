// Просмотр результата: слои с чекбоксами поверх друг друга и объяснения ко всем посадкам.
// Данные — view.json задачи (greening/view.build): поверхности, зоны, рассадка, структуры.
(function () {
    'use strict';

    const NS = 'http://www.w3.org/2000/svg';
    // метров на пиксель, дальше которых подписи групп кустов скрываются
    const GROUP_LABEL_PX = 0.08;
    const root = document.getElementById('viewer');
    if (!root) return;

    const svg = root.querySelector('.viewer-map svg');
    const layersBox = root.querySelector('.viewer-layers-list');
    const list = root.querySelector('.viewer-structures');
    const search = root.querySelector('.viewer-search');
    const info = root.querySelector('.viewer-info');
    const titleEl = root.querySelector('.viewer-title');
    const metaEl = root.querySelector('.viewer-meta');

    const KIND_NAMES = { tree_shrub: 'деревья и кустарники', tree: 'деревья', shrub: 'кустарники',
        herbaceous: 'только травянистые', none: 'ничего' };
    const GROUP_NAMES = { 'ДЛ': 'дерево лиственное', 'ДХ': 'дерево хвойное', 'КЛ': 'кустарник лиственный',
        'КХ': 'кустарник хвойный', 'Л': 'лиана', 'М': 'многолетник', 'О': 'однолетник', 'Б': 'луковичные', 'Г': 'газон' };

    // слои: порядок — снизу вверх; on — включён при открытии
    const LAYERS = [
        { id: 'surfaces', title: 'Покрытия (подоснова)', swatch: 'var(--v-lawn)', on: true },
        { id: 'zones', title: 'Зоны: что можно сажать', swatch: 'var(--z-tree_shrub)', on: true },
        { id: 'lawns', title: 'Газон (посев)', swatch: 'var(--p-lawn)', on: false },
        { id: 'beds', title: 'Цветники', swatch: 'var(--p-bed)', on: true },
        { id: 'territory', title: 'Граница работ', swatch: 'var(--muted)', on: true, line: true },
        { id: 'structures', title: 'Границы структур', swatch: 'var(--v-accent)', on: true, line: true },
        { id: 'patches', title: 'Газоны структур (u1…)', swatch: 'var(--text)', on: false, line: true },
        { id: 'objects', title: 'Площадки и объекты', swatch: 'var(--v-playground)', on: true },
        { id: 'existing', title: 'Существующие деревья', swatch: 'var(--v-tree)', on: true, ring: true },
        { id: 'shrubs', title: 'Новые кустарники', swatch: 'var(--p-shrub)', on: true, dot: true },
        { id: 'trees', title: 'Новые деревья', swatch: 'var(--p-tree)', on: true, dot: true },
        { id: 'labels_groups', title: 'Подписи групп кустов', swatch: 'var(--p-shrub)', on: true, text: true },
        { id: 'labels', title: 'Подписи зон и газонов', swatch: 'var(--text)', on: true, text: true },
    ];

    let D = null;
    let view = [0, 0, 1, 1];
    let groups = {};
    let current = null;
    let labels = [];
    // подписи групп кустов живут всё время просмотра, подписи зон — только у выбранной структуры
    let groupLabels = [];
    const byId = {};

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

    function title(node, text) { el('title', {}, node).textContent = text; }

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

    // ---------- слои ----------

    function draw() {
        svg.replaceChildren();
        groups = {};
        groupLabels = [];
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
                s.zoneEls[z.id] = p;
            });
            s.outlineEl = el('path', { d: s.outline, 'class': 'v-structure', 'fill-rule': 'evenodd' }, groups.structures);
            s.outlineEl.dataset.s = s.id;
            s.patches.forEach(function (p) {
                el('path', { d: p.d, 'class': 'v-patch', 'fill-rule': 'evenodd' }, groups.patches);
            });
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
            title(c, 'Существующее дерево (крона условно ' + (crown * 2) + ' м)');
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
        });

        LAYERS.forEach(function (layer) { groups[layer.id].style.display = layer.on ? '' : 'none'; });
    }

    function renderLayers() {
        layersBox.innerHTML = LAYERS.slice().reverse().map(function (layer) {
            const kind = layer.line ? ' is-line' : layer.ring ? ' is-ring' : layer.dot ? ' is-dot' : layer.text ? ' is-text' : '';
            return '<label class="viewer-layer"><input type="checkbox" data-layer="' + layer.id + '"' +
                (layer.on ? ' checked' : '') + '><span class="viewer-swatch' + kind + '" style="--sw:' + layer.swatch +
                '"></span>' + esc(layer.title) + '</label>';
        }).join('');
        layersBox.querySelectorAll('input').forEach(function (input) {
            input.addEventListener('change', function () {
                const layer = LAYERS.find(function (l) { return l.id === input.dataset.layer; });
                layer.on = input.checked;
                groups[layer.id].style.display = layer.on ? '' : 'none';
            });
        });
    }

    // ---------- список структур ----------

    function renderList(query) {
        const q = (query || '').trim().toLowerCase();
        list.innerHTML = D.structures.filter(function (s) {
            return !q || (s.id + ' ' + s.name + ' ' + s.place).toLowerCase().indexOf(q) !== -1;
        }).map(function (s) {
            const planted = s.plantings.filter(function (p) { return p.by !== 'code'; }).length;
            return '<button type="button" class="viewer-item' + (s.id === current ? ' is-on' : '') + '" data-s="' + s.id + '">' +
                '<b>' + s.id + '</b> ' + esc(s.name) + '<small>' + esc(s.place) + ' · газоны ' + s.area + ' м² · посадок ' +
                planted + '</small></button>';
        }).join('');
        list.querySelectorAll('.viewer-item').forEach(function (b) {
            b.addEventListener('click', function () { select(b.dataset.s, { zoom: true }); });
        });
    }

    // ---------- объяснения ----------

    function unitText(p) {
        // количество считает рассадка, а не LLM: показываем посаженное
        return esc(p.placed) + ' ' + esc(p.unit === 'шт' ? 'шт' : 'м²');
    }

    function plantingCard(s, p) {
        const warnings = p.warnings && p.warnings.length
            ? '<ul class="viewer-warn">' + p.warnings.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '';
        const norms = p.norms && p.norms.length
            ? '<div class="viewer-sub">Нормы</div><ul>' + p.norms.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>' : '';
        const facts = p.facts && p.facts.length
            ? '<div class="viewer-sub">Свойства по справочнику</div><p class="viewer-facts">' + p.facts.map(esc).join(' · ') + '</p>' : '';
        return '<article class="viewer-card" data-i="' + p.i + '">' +
            '<header><b>' + esc(p.name) + '</b><span>' + esc(p.role) + ' · ' + unitText(p) + '</span></header>' +
            '<div class="viewer-tags"><span>зона ' + esc(p.zone) + '</span><span>' + esc(GROUP_NAMES[p.group] || p.group || '') + '</span>' +
            (p.by === 'code' ? '<span>решено без LLM</span>' : '') + '</div>' +
            (p.reason ? '<p>' + esc(p.reason) + '</p>' : '') + norms + facts + warnings + '</article>';
    }

    function zoneCard(z) {
        const blocked = z.blocked && z.blocked.length
            ? '<div class="viewer-sub">Что закрыло деревья и кустарники</div><ul>' +
              z.blocked.map(function (b) { return '<li>' + esc(b) + '</li>'; }).join('') + '</ul>' : '';
        return '<article class="viewer-card viewer-zone" data-z="' + z.id + '">' +
            '<header><b><span class="viewer-dot z-' + z.kind + '"></span>' + z.id + ' · газон ' + esc(z.patch) + '</b>' +
            '<span>' + z.area + ' м² · можно: ' + esc(KIND_NAMES[z.kind]) + '</span></header>' +
            '<p class="viewer-place">' + esc(z.explanation || z.place) + '</p>' +
            (z.decision ? '<p>' + esc(z.decision) + '</p>' : '<p class="viewer-muted">Нет объяснения</p>') +
            (z.tags.length ? '<p class="viewer-facts">' + z.tags.map(esc).join(' · ') + '</p>' : '') +
            (z.reason ? '<p class="viewer-facts">' + esc(z.reason) + '</p>' : '') + blocked + '</article>';
    }

    function showStructure(s) {
        const plantings = s.plantings.slice().sort(function (a, b) {
            return (a.by === 'code') - (b.by === 'code') || a.zone.localeCompare(b.zone, undefined, { numeric: true });
        });
        info.innerHTML =
            '<h2>' + s.id + ' · ' + esc(s.name) + '</h2>' +
            '<p class="viewer-muted">' + esc(s.place) + ' · всего ' + s.total + ' м² (' + esc(s.composition) + ') · газоны ' +
            s.area + ' м² · существующих деревьев ' + s.trees + '</p>' +
            (s.justification ? '<div class="viewer-sub">Замысел</div><p>' + esc(s.justification) + '</p>' : '') +
            '<div class="viewer-tabs"><button type="button" class="is-on" data-tab="plantings">Посадки (' + plantings.length +
            ')</button><button type="button" data-tab="zones">Зоны (' + s.zones.length + ')</button></div>' +
            '<div class="viewer-tab" data-tab="plantings">' + plantings.map(function (p) { return plantingCard(s, p); }).join('') + '</div>' +
            '<div class="viewer-tab" data-tab="zones" hidden>' + s.zones.map(zoneCard).join('') + '</div>';
        info.querySelectorAll('.viewer-tabs button').forEach(function (b) {
            b.addEventListener('click', function () { tab(b.dataset.tab); });
        });
        info.querySelectorAll('.viewer-card[data-i]').forEach(function (card) {
            card.addEventListener('mouseenter', function () { highlight(s.id, +card.dataset.i); });
            card.addEventListener('mouseleave', function () { highlight(null); });
        });
        info.querySelectorAll('.viewer-zone').forEach(function (card) {
            card.addEventListener('mouseenter', function () { s.zoneEls[card.dataset.z].classList.add('is-hl'); });
            card.addEventListener('mouseleave', function () { s.zoneEls[card.dataset.z].classList.remove('is-hl'); });
        });
        info.scrollTop = 0;
    }

    function tab(name) {
        info.querySelectorAll('.viewer-tabs button').forEach(function (b) { b.classList.toggle('is-on', b.dataset.tab === name); });
        info.querySelectorAll('.viewer-tab').forEach(function (t) { t.hidden = t.dataset.tab !== name; });
    }

    function showOverview() {
        const s = D.summary || {};
        const plants = s.plants || {};
        const count = function (k) { return plants[k] ? plants[k].placed : 0; };
        info.innerHTML = '<h2>Участок целиком</h2>' +
            '<p class="viewer-muted">Выберите структуру в списке или щёлкните по карте: по растению — его обоснование, по зоне — объяснение места.</p>' +
            '<dl class="viewer-stats">' +
            '<dt>Структур</dt><dd>' + D.structures.length + '</dd>' +
            '<dt>Зон объяснено</dt><dd>' + (s.zones_explained != null ? s.zones_explained + ' из ' + s.zones : '—') + '</dd>' +
            '<dt>Новых деревьев</dt><dd>' + count('tree') + '</dd>' +
            '<dt>Новых кустарников</dt><dd>' + count('shrub') + '</dd>' +
            '<dt>Цветники и газон, м²</dt><dd>' + Math.round(count('herbaceous_m2')) + '</dd>' +
            '<dt>Нарушений правил рассадки</dt><dd>' + (s.violations != null ? s.violations : '—') + '</dd>' +
            '</dl>';
    }

    function highlight(sid, index) {
        svg.querySelectorAll('.is-hl').forEach(function (n) { n.classList.remove('is-hl'); });
        if (sid == null) return;
        svg.querySelectorAll('[data-s="' + sid + '"][data-i="' + index + '"]').forEach(function (n) { n.classList.add('is-hl'); });
    }

    function select(sid, options) {
        const s = byId[sid];
        if (!s) return;
        options = options || {};
        if (current !== sid) {
            current = sid;
            if (options.zoom) setView(s.window.slice());
            D.structures.forEach(function (x) { x.outlineEl.classList.toggle('is-on', x.id === sid); });
            svg.querySelectorAll('.v-zone').forEach(function (z) { z.classList.toggle('is-dim', z.dataset.s !== sid); });
            drawLabels(s);
            showStructure(s);
            renderList(search.value);
        } else if (options.zoom) {
            setView(s.window.slice());
        }
        if (options.planting != null) {
            tab('plantings');
            const card = info.querySelector('.viewer-card[data-i="' + options.planting + '"]');
            if (card) {
                info.querySelectorAll('.viewer-card.is-on').forEach(function (c) { c.classList.remove('is-on'); });
                card.classList.add('is-on');
                card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
            highlight(sid, options.planting);
        }
        if (options.zone) {
            tab('zones');
            const card = info.querySelector('.viewer-zone[data-z="' + options.zone + '"]');
            if (card) {
                info.querySelectorAll('.viewer-card.is-on').forEach(function (c) { c.classList.remove('is-on'); });
                card.classList.add('is-on');
                card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
        }
    }

    function drawLabels(s) {
        groups.labels.replaceChildren();
        labels = [];
        s.patches.forEach(function (p) {
            p.labels.forEach(function (at) {
                const t = el('text', { x: at[0], y: at[1], 'class': 'v-label is-patch' }, groups.labels);
                t.textContent = p.id;
                labels.push(t);
            });
        });
        s.zones.forEach(function (z) {
            z.labels.forEach(function (at) {
                const t = el('text', { x: at[0], y: at[1], 'class': 'v-label' }, groups.labels);
                t.textContent = z.id;
                labels.push(t);
            });
        });
        scaleLabels();
    }

    function clearSelection() {
        current = null;
        D.structures.forEach(function (x) { x.outlineEl.classList.remove('is-on'); });
        svg.querySelectorAll('.v-zone').forEach(function (z) { z.classList.remove('is-dim'); });
        groups.labels.replaceChildren();
        labels = [];
        highlight(null);
        showOverview();
        renderList(search.value);
    }

    // ---------- масштаб и сдвиг ----------

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
        if (target.dataset.i != null && target.dataset.s) {
            select(target.dataset.s, { planting: +target.dataset.i });
        } else if (target.dataset.z) {
            select(target.dataset.s, { zone: target.dataset.z });
        } else if (target.dataset.s) {
            select(target.dataset.s);
        }
    });

    root.querySelector('[data-action="fit"]').addEventListener('click', function () { setView(D.view.slice()); });
    root.querySelector('[data-action="overview"]').addEventListener('click', function () {
        clearSelection();
        setView(D.view.slice());
    });
    root.querySelector('[data-action="zoom-in"]').addEventListener('click', function () { zoom(1 / 1.5); });
    root.querySelector('[data-action="zoom-out"]').addEventListener('click', function () { zoom(1.5); });

    function zoom(k) {
        const cx = view[0] + view[2] / 2;
        const cy = view[1] + view[3] / 2;
        setView([cx - view[2] * k / 2, cy - view[3] * k / 2, view[2] * k, view[3] * k]);
    }

    search.addEventListener('input', function () { renderList(search.value); });
    window.addEventListener('resize', function () { if (!root.hidden) scaleLabels(); });

    function close() {
        root.hidden = true;
        document.body.classList.remove('has-viewer');
    }

    root.querySelector('[data-action="close"]').addEventListener('click', close);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !root.hidden) close(); });

    window.GreeningViewer = {
        // data — view.json; meta — {name, summary, links: {dxf, plan}}
        open: function (data, meta) {
            if (D !== data) {
                D = data;
                D.summary = meta.summary;
                current = null;
                draw();
                renderLayers();
                renderList('');
                search.value = '';
                showOverview();
            }
            titleEl.textContent = meta.name;
            metaEl.textContent = D.structures.length + ' структур · ' + D.plants.length + ' растений';
            root.querySelector('[data-link="dxf"]').href = meta.links.dxf;
            root.querySelector('[data-link="plan"]').href = meta.links.plan;
            root.hidden = false;
            document.body.classList.add('has-viewer');
            requestAnimationFrame(function () { setView(current ? byId[current].window.slice() : D.view.slice()); });
        },
        close: close,
    };
}());
