// Редактирование результата — отдельное окно с двумя вкладками:
//  «Пожелания и параметры» — текст в запрос LLM (уровень 1) и параметры рассадки (уровень 2, GET /params/);
//  «Перемещение растений» — свой обзор, растения и группы двигаются мышью (уровень 3), сохраняется новая версия.
// Перемещения делаются после пересчёта: пересчёт строит рассадку заново, и сдвинутые растения встали бы иначе.
(function () {
    'use strict';

    const NS = 'http://www.w3.org/2000/svg';
    const root = document.getElementById('editor');
    if (!root) return;

    const paramsUrl = document.body.dataset.paramsUrl;
    const paramsBox = root.querySelector('.ed-params');
    const textInput = root.querySelector('.ed-text');
    const paneError = root.querySelector('[data-error="params"]');
    const moveError = root.querySelector('[data-error="move"]');
    const svg = root.querySelector('.ed-map svg');
    const statMoved = root.querySelector('[data-stat="moved"]');
    const statOutside = root.querySelector('[data-stat="outside"]');
    const noIds = root.querySelector('.ed-noids');
    const movedNote = root.querySelector('.ed-moved-note');
    const titleEl = root.querySelector('.viewer-title');

    const ALLOWS = { tree_shrub: ['tree', 'shrub'], tree: ['tree'], shrub: ['shrub'], herbaceous: [], none: [] };

    let schema = null;
    let D = null;           // view.json текущей версии
    let meta = null;        // {name, summary, params, preferences}
    let handlers = null;    // {revise(body), move(moves)} — запуск пересчёта (app.js)
    let values = {};        // параметры в форме
    const controls = {};    // ключ → {set(value), row}

    function el(tag, attrs, parent) {
        const node = document.createElementNS(NS, tag);
        for (const key in attrs) node.setAttribute(key, attrs[key]);
        if (parent) parent.appendChild(node);
        return node;
    }

    function html(tag, cls, parent, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        if (parent) parent.appendChild(node);
        return node;
    }

    function fmt(value, spec) {
        if (value === null || value === undefined) return 'без предела';
        if (Array.isArray(value)) return value.join(', ');
        if (typeof value === 'number' && spec && spec.step && spec.step < 1) {
            const digits = String(spec.step).split('.')[1].length;
            return value.toFixed(digits).replace('.', ',');
        }
        return String(value).replace('.', ',');
    }

    // ---------- вкладки ----------

    function tab(name) {
        root.querySelectorAll('.ed-tabs button').forEach(function (b) { b.classList.toggle('is-on', b.dataset.tab === name); });
        root.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.dataset.pane !== name; });
        if (name === 'move') requestAnimationFrame(function () { if (!view) fit(); else setView(view); });
    }
    root.querySelectorAll('.ed-tabs button').forEach(function (b) {
        b.addEventListener('click', function () { tab(b.dataset.tab); });
    });

    // ---------- параметры: наглядные элементы по описанию с сервера ----------

    function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

    function markChanged() {
        schema.params.forEach(function (spec) {
            const c = controls[spec.key];
            c.row.classList.toggle('is-changed', spec.key !== 'placement' && !same(values[spec.key], spec.default));
        });
        // изменённый параметр из тонкой настройки не прячем
        paramsBox.querySelectorAll('.ed-more').forEach(function (more) {
            if (more.querySelector('.ed-param.is-changed')) open(more, true);
        });
        const mode = values.placement;
        paramsBox.querySelectorAll('[data-group]').forEach(function (g) {
            g.hidden = (g.dataset.group === 'model' && mode !== 'model') || (g.dataset.group === 'algorithm' && mode !== 'algorithm');
        });
    }

    function setValue(key, value) {
        values[key] = value;
        controls[key].set(value);
        markChanged();
    }

    // ползунок со значением; optional — переключатель «без предела»
    function slider(spec, row) {
        const box = html('div', 'ed-slider', row);
        let toggle = null;
        if (spec.optional) {
            const label = html('label', 'ed-unlimited', box);
            toggle = html('input', null, label);
            toggle.type = 'checkbox';
            html('span', null, label, 'без предела');
        }
        const input = html('input', 'ed-range', box);
        input.type = 'range';
        input.min = spec.min;
        input.max = spec.max;
        input.step = spec.step || 1;
        const out = html('output', 'ed-value', box);
        if (spec.key.startsWith('edge_pull')) {
            const ends = html('div', 'ed-ends', row);
            html('span', null, ends, '← к краю');
            html('span', null, ends, 'к середине →');
        }
        function paint(value) {
            const v = value === null ? Number(spec.min) : value;
            const share = (v - spec.min) / (spec.max - spec.min || 1) * 100;
            input.style.setProperty('--fill', share + '%');
            out.textContent = fmt(value, spec) + (spec.unit && value !== null ? ' ' + spec.unit : '');
        }
        input.addEventListener('input', function () {
            values[spec.key] = spec.kind === 'int' ? parseInt(input.value, 10) : parseFloat(input.value);
            if (toggle) toggle.checked = false;
            input.disabled = false;
            paint(values[spec.key]);
            markChanged();
        });
        if (toggle) {
            toggle.addEventListener('change', function () {
                values[spec.key] = toggle.checked ? null : (spec.kind === 'int' ? parseInt(input.value, 10) : parseFloat(input.value));
                input.disabled = toggle.checked;
                paint(values[spec.key]);
                markChanged();
            });
        }
        return function set(value) {
            if (toggle) toggle.checked = value === null;
            input.disabled = value === null;
            if (value !== null) input.value = value;
            paint(value);
        };
    }

    // переключатель-сегменты для выбора
    function segments(spec, row) {
        const box = html('div', 'ed-segments', row);
        const buttons = spec.choices.map(function (choice) {
            const b = html('button', null, box, choice[1]);
            b.type = 'button';
            b.dataset.value = choice[0];
            b.addEventListener('click', function () { setValue(spec.key, choice[0]); });
            return b;
        });
        return function set(value) {
            buttons.forEach(function (b) { b.classList.toggle('is-on', b.dataset.value === value); });
        };
    }

    // размеры групп — «фишки»: какие числа растений в группе возможны
    function chips(spec, row) {
        const box = html('div', 'ed-chips', row);
        const top = Math.min(spec.max, spec.key.endsWith('_tree') ? 9 : 15);
        const items = [];
        for (let n = spec.min; n <= top; n++) {
            const b = html('button', null, box, String(n));
            b.type = 'button';
            b.dataset.n = n;
            b.addEventListener('click', function () {
                const current = (values[spec.key] || []).slice();
                const at = current.indexOf(n);
                if (at >= 0) {
                    if (current.length === 1) return;     // хотя бы один размер
                    current.splice(at, 1);
                } else {
                    current.push(n);
                }
                current.sort(function (a, b2) { return a - b2; });
                setValue(spec.key, current);
            });
            items.push(b);
        }
        return function set(value) {
            items.forEach(function (b) { b.classList.toggle('is-on', (value || []).indexOf(+b.dataset.n) >= 0); });
        };
    }

    // seed — счётчик с кнопками и кубиком
    function stepper(spec, row) {
        const box = html('div', 'ed-stepper', row);
        const minus = html('button', null, box, '−');
        const input = html('input', null, box);
        const plus = html('button', null, box, '+');
        const dice = html('button', 'ed-dice', box, '🎲');
        [minus, plus, dice].forEach(function (b) { b.type = 'button'; });
        dice.title = 'Случайный вариант';
        input.type = 'number';
        input.min = spec.min;
        input.max = spec.max;
        function take(v) { setValue(spec.key, Math.max(spec.min, Math.min(spec.max, v))); }
        minus.addEventListener('click', function () { take((values[spec.key] || 0) - 1); });
        plus.addEventListener('click', function () { take((values[spec.key] || 0) + 1); });
        dice.addEventListener('click', function () { take(Math.floor(Math.random() * 1000)); });
        input.addEventListener('change', function () { take(parseInt(input.value, 10) || 0); });
        return function set(value) { input.value = value; };
    }

    // «Тонкая настройка» карточки: плавно раскрывается (высота 0fr → 1fr)
    function open(more, state) {
        more.classList.toggle('is-open', state);
        more.querySelector('.ed-more-toggle').setAttribute('aria-expanded', state ? 'true' : 'false');
    }

    function build() {
        paramsBox.replaceChildren();
        schema.groups.forEach(function (group) {
            const items = schema.params.filter(function (p) { return p.group === group.key; });
            if (!items.length) return;
            const card = html('section', 'ed-card', paramsBox);
            card.dataset.group = group.key;
            const head = html('h3', null, card, group.title);
            if (group.key === 'model' || group.key === 'algorithm') html('span', 'ed-badge', head, 'только ' + (group.key === 'model' ? 'модель' : 'алгоритм'));
            if (group.about) html('p', 'ed-about', card, group.about);
            const advanced = items.filter(function (spec) { return spec.advanced; });
            let more = null;
            let moreBody = null;
            if (advanced.length) {
                more = html('div', 'ed-more', null);
                const toggle = html('button', 'ed-more-toggle', more);
                toggle.type = 'button';
                toggle.setAttribute('aria-expanded', 'false');
                html('span', null, toggle, 'Тонкая настройка · ' + advanced.length);
                toggle.insertAdjacentHTML('beforeend', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>');
                toggle.addEventListener('click', function () { open(more, !more.classList.contains('is-open')); });
                moreBody = html('div', null, html('div', 'ed-more-body', more));
            }
            items.filter(function (spec) { return !spec.advanced; }).concat(advanced).forEach(function (spec) {
                const row = html('div', 'ed-param', spec.advanced ? moreBody : card);
                const label = html('div', 'ed-label', row);
                html('span', null, label, spec.title);
                const reset = html('button', 'ed-reset', label, '↺');
                reset.type = 'button';
                reset.title = 'Вернуть значение по умолчанию';
                reset.addEventListener('click', function () { setValue(spec.key, spec.default); });
                label.addEventListener('dblclick', function () { setValue(spec.key, spec.default); });
                let set;
                if (spec.kind === 'choice') set = segments(spec, row);
                else if (spec.kind === 'sizes') set = chips(spec, row);
                else if (spec.key === 'seed') set = stepper(spec, row);
                else set = slider(spec, row);
                if (spec.help) html('div', 'ed-help', row, spec.help);
                controls[spec.key] = { set: set, row: row };
            });
            if (more) card.appendChild(more);
        });
    }

    function fill(current) {
        values = {};
        schema.params.forEach(function (spec) {
            const has = current && Object.prototype.hasOwnProperty.call(current, spec.key);
            values[spec.key] = has ? current[spec.key] : spec.default;
            controls[spec.key].set(values[spec.key]);
        });
        markChanged();
    }

    const ready = fetch(paramsUrl).then(function (r) { return r.json(); }).then(function (data) {
        schema = data;
        build();
    }).catch(function () {
        paramsBox.textContent = 'Не удалось загрузить параметры рассадки.';
    });

    root.querySelector('[data-action="reset"]').addEventListener('click', function () {
        const placement = values.placement;
        fill({});
        setValue('placement', placement);
    });

    root.querySelector('[data-action="recalculate"]').addEventListener('click', function () {
        if (moved().length && !window.confirm('Несохранённые перемещения растений пропадут: пересчёт строит рассадку заново. Пересчитать?')) return;
        paneError.hidden = true;
        handlers.revise({ preferences: textInput.value.trim(), params: JSON.stringify(values) }).catch(function (err) {
            paneError.textContent = err.message;
            paneError.hidden = false;
        });
    });

    // ---------- перемещение: свой обзор ----------

    let view = null;
    let plants = [];       // {id, gid, kind, x, y, x0, y0, node}
    let outlines = {};     // gid → {node, dx, dy}
    let zones = [];        // {node, allows, box}
    let history = [];      // шаги для «Отменить»: [[{plant, x, y}], …]
    let mode = 'plant';

    function setView(next) {
        view = next;
        svg.setAttribute('viewBox', view.join(' '));
    }

    function fit() { if (D) setView(D.view.slice()); }

    function pixel() {
        const w = svg.clientWidth || 1;
        const h = svg.clientHeight || 1;
        return Math.max(view[2] / w, view[3] / h);
    }

    function toMap(e) {
        const pt = svg.createSVGPoint();
        pt.x = e.clientX;
        pt.y = e.clientY;
        return pt.matrixTransform(svg.getScreenCTM().inverse());
    }

    function draw() {
        svg.replaceChildren();
        const layer = function (cls) { return el('g', { 'class': cls }, svg); };
        const base = layer('ed-base');
        D.surfaces.forEach(function (s) { el('path', { d: s.d, 'class': 'v-surf s-' + s.kind, 'fill-rule': 'evenodd' }, base); });
        el('path', { d: D.territory, 'class': 'v-territory', 'fill-rule': 'evenodd' }, base);
        const zoneLayer = layer('ed-zones');
        zones = [];
        D.structures.forEach(function (s) {
            s.zones.forEach(function (z) {
                const node = el('path', { d: z.d, 'class': 'v-zone ed-zone z-' + z.kind, 'fill-rule': 'evenodd' }, zoneLayer);
                zones.push({ node: node, allows: ALLOWS[z.kind] || [], box: null });
            });
        });
        const beds = layer('ed-beds');
        D.lawns.forEach(function (b) { el('path', { d: b.d, 'class': 'v-lawn ed-static', 'fill-rule': 'evenodd' }, beds); });
        D.beds.forEach(function (b) { el('path', { d: b.d, 'class': 'v-bed ed-static', 'fill-rule': 'evenodd' }, beds); });
        const existing = layer('ed-existing');
        if (D.rows) el('path', { d: D.rows, 'class': 'v-rows' }, existing);
        const crown = (D.existing_crown || 6) / 2;
        D.trees.forEach(function (t) {
            el('circle', { cx: t[0], cy: t[1], r: crown, 'class': 'v-existing' }, existing);
            el('circle', { cx: t[0], cy: t[1], r: 0.3, 'class': 'v-existing-trunk' }, existing);
        });
        const groupLayer = layer('ed-groups');
        outlines = {};
        (D.groups || []).forEach(function (g) {
            const node = el('path', { d: g.d, 'class': 'v-group ed-group', 'fill-rule': 'evenodd' }, groupLayer);
            node.dataset.gid = g.gid;
            el('title', {}, node).textContent = g.t + ' — перетащите, чтобы сдвинуть группу; с Shift — повернуть';
            if (g.gid != null) outlines[g.gid] = { node: node, dx: 0, dy: 0 };
        });
        const plantLayer = layer('ed-plants');
        plants = D.plants.map(function (p, n) {
            const node = el('circle', { cx: p[0], cy: p[1], r: Math.max(p[2], 0.35), 'class': 'v-plant ed-plant pl-' + p[3] }, plantLayer);
            node.dataset.n = n;
            el('title', {}, node).textContent = p[4];
            return { id: p[7], gid: p[8], kind: p[3], x: p[0], y: p[1], x0: p[0], y0: p[1], node: node };
        });
        history = [];
        noIds.hidden = plants.every(function (p) { return p.id; });
        update();
    }

    function inZone(p) {
        if (p.kind !== 'tree' && p.kind !== 'shrub') return true;
        const point = new DOMPoint(p.x, p.y);
        for (const z of zones) {
            if (z.allows.indexOf(p.kind) < 0) continue;
            if (!z.box) z.box = z.node.getBBox();
            const b = z.box;
            if (p.x < b.x || p.x > b.x + b.width || p.y < b.y || p.y > b.y + b.height) continue;
            if (z.node.isPointInFill(point)) return true;
        }
        return false;
    }

    function moved() { return plants.filter(function (p) { return Math.abs(p.x - p.x0) > 0.005 || Math.abs(p.y - p.y0) > 0.005; }); }

    // сдвинутое помечается; вне зоны, где вид разрешён нормами, — красным (двигать можно, проверка — при сохранении)
    function update(list) {
        (list || plants).forEach(function (p) {
            const shifted = Math.abs(p.x - p.x0) > 0.005 || Math.abs(p.y - p.y0) > 0.005;
            p.node.classList.toggle('is-moved', shifted);
            p.outside = shifted && !inZone(p);
            p.node.classList.toggle('is-outside', p.outside);
        });
        const all = moved();
        statMoved.textContent = all.length;
        statOutside.textContent = all.filter(function (p) { return p.outside; }).length;
        statOutside.parentNode.classList.toggle('is-bad', all.some(function (p) { return p.outside; }));
    }

    function shift(list, dx, dy) {
        list.forEach(function (p) {
            p.x += dx;
            p.y += dy;
            p.node.setAttribute('cx', p.x);
            p.node.setAttribute('cy', p.y);
        });
    }

    function syncOutlines(gids) {
        gids.forEach(function (gid) {
            const o = outlines[gid];
            if (!o) return;
            const members = plants.filter(function (p) { return p.gid === gid; });
            // контур сдвигается и поворачивается вместе с группой; растения группы, сдвинутые по одному, контур не тянут
            const m = motion(members);
            o.node.classList.toggle('is-broken', !m);
            if (m) o.node.setAttribute('transform', 'translate(' + m.nx + ' ' + m.ny + ') rotate(' + m.deg + ') translate(' + (-m.ox) + ' ' + (-m.oy) + ')');
        });
    }

    // сдвиг с поворотом от исходных мест к нынешним (по центрам, наименьшие квадраты), как place._rigid на сервере;
    // null — растения сдвинуты не как одно целое
    function motion(members) {
        const n = members.length;
        let ox = 0, oy = 0, nx = 0, ny = 0, cross = 0, dot = 0;
        members.forEach(function (p) { ox += p.x0; oy += p.y0; nx += p.x; ny += p.y; });
        ox /= n; oy /= n; nx /= n; ny /= n;
        members.forEach(function (p) {
            const a = p.x0 - ox, b = p.y0 - oy, c = p.x - nx, d = p.y - ny;
            cross += a * d - b * c;
            dot += a * c + b * d;
        });
        const angle = n > 1 ? Math.atan2(cross, dot) : 0;
        const cos = Math.cos(angle), sin = Math.sin(angle);
        const rigid = members.every(function (p) {
            const a = p.x0 - ox, b = p.y0 - oy;
            return Math.hypot(a * cos - b * sin + nx - p.x, a * sin + b * cos + ny - p.y) < 0.02;
        });
        return rigid ? { deg: angle * 180 / Math.PI, ox: ox, oy: oy, nx: nx, ny: ny } : null;
    }

    function rotate(list, cx, cy, angle) {
        const cos = Math.cos(angle), sin = Math.sin(angle);
        list.forEach(function (p) {
            const dx = p.x - cx, dy = p.y - cy;
            p.x = cx + dx * cos - dy * sin;
            p.y = cy + dx * sin + dy * cos;
            p.node.setAttribute('cx', p.x);
            p.node.setAttribute('cy', p.y);
        });
    }

    let drag = null;

    svg.addEventListener('wheel', function (e) {
        e.preventDefault();
        const m = toMap(e);
        const k = e.deltaY > 0 ? 1.2 : 1 / 1.2;
        setView([m.x - (m.x - view[0]) * k, m.y - (m.y - view[1]) * k, view[2] * k, view[3] * k]);
    }, { passive: false });

    svg.addEventListener('pointerdown', function (e) {
        const target = e.target;
        const at = toMap(e);
        const turn = mode === 'rotate' || e.shiftKey;
        let list = null;
        if (target.classList.contains('ed-plant')) {
            const p = plants[+target.dataset.n];
            list = (mode !== 'plant' || turn) && p.gid != null ? plants.filter(function (q) { return q.gid === p.gid; }) : [p];
        } else if (target.classList.contains('ed-group')) {
            const gid = +target.dataset.gid;
            list = plants.filter(function (q) { return q.gid === gid; });
        }
        if (list && list.length) {
            drag = { kind: 'move', list: list, x: at.x, y: at.y, before: list.map(function (p) { return { plant: p, x: p.x, y: p.y }; }) };
            // поворот — вокруг центра группы, на угол, на который мышь обошла его; одно растение поворачивать незачем
            if (turn && list.length > 1) {
                drag.kind = 'rotate';
                drag.cx = list.reduce(function (s, p) { return s + p.x; }, 0) / list.length;
                drag.cy = list.reduce(function (s, p) { return s + p.y; }, 0) / list.length;
                drag.a = Math.atan2(at.y - drag.cy, at.x - drag.cx);
                svg.classList.add('is-rotating');
            }
            list.forEach(function (p) { p.node.classList.add('is-drag'); });
        } else {
            drag = { kind: 'pan', cx: e.clientX, cy: e.clientY, v: view.slice() };
        }
        svg.setPointerCapture(e.pointerId);
        svg.classList.add('is-dragging');
    });

    svg.addEventListener('pointermove', function (e) {
        if (!drag) return;
        if (drag.kind === 'pan') {
            const box = svg.getBoundingClientRect();
            const s = Math.max(drag.v[2] / box.width, drag.v[3] / box.height);
            setView([drag.v[0] - (e.clientX - drag.cx) * s, drag.v[1] - (e.clientY - drag.cy) * s, drag.v[2], drag.v[3]]);
            return;
        }
        const at = toMap(e);
        if (drag.kind === 'rotate') {
            const a = Math.atan2(at.y - drag.cy, at.x - drag.cx);
            rotate(drag.list, drag.cx, drag.cy, a - drag.a);
            drag.a = a;
        } else {
            shift(drag.list, at.x - drag.x, at.y - drag.y);
            drag.x = at.x;
            drag.y = at.y;
        }
        syncOutlines(new Set(drag.list.map(function (p) { return p.gid; })));
    });

    function stop() {
        if (!drag) return;
        if (drag.list) {
            drag.list.forEach(function (p) { p.node.classList.remove('is-drag'); });
            const changed = drag.before.some(function (b) { return b.plant.x !== b.x || b.plant.y !== b.y; });
            if (changed) history.push(drag.before);
            update(drag.list);
        }
        drag = null;
        svg.classList.remove('is-dragging', 'is-rotating');
    }
    svg.addEventListener('pointerup', stop);
    svg.addEventListener('pointercancel', stop);

    root.querySelectorAll('.ed-mode button').forEach(function (b) {
        b.addEventListener('click', function () {
            mode = b.dataset.mode;
            svg.classList.toggle('is-rotate', mode === 'rotate');
            root.querySelectorAll('.ed-mode button').forEach(function (x) { x.classList.toggle('is-on', x === b); });
        });
    });

    function restore(steps) {
        const gids = new Set();
        steps.forEach(function (s) {
            s.plant.x = s.x;
            s.plant.y = s.y;
            s.plant.node.setAttribute('cx', s.x);
            s.plant.node.setAttribute('cy', s.y);
            gids.add(s.plant.gid);
        });
        syncOutlines(gids);
        update(steps.map(function (s) { return s.plant; }));
    }

    root.querySelector('[data-action="undo"]').addEventListener('click', function () {
        if (history.length) restore(history.pop());
    });
    root.querySelector('[data-action="revert"]').addEventListener('click', function () {
        restore(moved().map(function (p) { return { plant: p, x: p.x0, y: p.y0 }; }));
        history = [];
    });
    root.querySelector('[data-action="fit"]').addEventListener('click', fit);
    root.querySelector('[data-action="zoom-in"]').addEventListener('click', function () { zoom(1 / 1.5); });
    root.querySelector('[data-action="zoom-out"]').addEventListener('click', function () { zoom(1.5); });

    function zoom(k) {
        const cx = view[0] + view[2] / 2;
        const cy = view[1] + view[3] / 2;
        setView([cx - view[2] * k / 2, cy - view[3] * k / 2, view[2] * k, view[3] * k]);
    }

    root.querySelector('[data-action="save-moves"]').addEventListener('click', function () {
        const list = moved();
        moveError.hidden = true;
        if (!list.length) {
            moveError.textContent = 'Ничего не сдвинуто: перетащите растение или группу мышью.';
            moveError.hidden = false;
            return;
        }
        if (list.some(function (p) { return !p.id; })) {
            moveError.textContent = 'У этой версии нет номеров растений — пересчитайте результат (вкладка «Пожелания и параметры»), потом двигайте.';
            moveError.hidden = false;
            return;
        }
        // на карте y перевёрнут (SVG вниз), в чертеже — вверх
        handlers.move(list.map(function (p) { return { id: p.id, x: Math.round(p.x * 100) / 100, y: -Math.round(p.y * 100) / 100 }; }))
            .catch(function (err) {
                moveError.textContent = err.message;
                moveError.hidden = false;
            });
    });

    // ---------- окно ----------

    function close(force) {
        if (!force && moved().length && !window.confirm('Перемещения не сохранены и пропадут. Закрыть?')) return;
        root.hidden = true;
        document.body.classList.remove('has-viewer');
    }

    root.querySelector('[data-action="close"]').addEventListener('click', function () { close(false); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !root.hidden) close(false); });

    window.GreeningEditor = {
        // data — view.json; info — {name, summary, params, preferences}; calls — {revise(body), move(moves)} → Promise
        open: function (data, info, calls) {
            meta = info;
            handlers = calls;
            titleEl.textContent = 'Редактирование: ' + info.name;
            textInput.value = info.preferences || '';
            paneError.hidden = true;
            moveError.hidden = true;
            const count = info.summary && info.summary.moved;
            movedNote.hidden = !count;
            movedNote.textContent = count ? 'В этой версии вручную перемещено растений: ' + count
                + '. Пересчёт пожеланий или параметров построит рассадку заново — эти перемещения пропадут.' : '';
            if (D !== data) {
                D = data;
                view = null;
                draw();
            }
            ready.then(function () {
                if (!schema) return;
                const current = Object.assign({}, info.params || {});
                if (!current.placement) current.placement = info.summary && info.summary.placement;
                fill(current);
            });
            root.hidden = false;
            document.body.classList.add('has-viewer');
            tab('params');
        },
        close: function () { close(true); },
    };
}());
