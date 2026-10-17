// 3D-вид результата: отдельная страница (scene.html), данные — GET /jobs/<id>/scene/ (greening/scene.py).
// Поверхности — плиты с текстурами и бортами, здания — по этажности, растения — инстансы по форме вида,
// сезоны, время суток, «до/после», рост посадок. Рендер только по изменению — вкладка в покое не грузит GPU.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { Sky } from 'three/addons/objects/Sky.js';
import { mergeGeometries, mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { GTAOPass } from 'three/addons/postprocessing/GTAOPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

const root = document.getElementById('scene');
const canvas = root.querySelector('.sc-canvas');
const loading = root.querySelector('.sc-loading');
const loadingText = root.querySelector('.sc-loading-text');
const card = root.querySelector('.sc-card');
const legend = root.querySelector('.sc-legend');
const gallery = root.querySelector('.sc-gallery');

// --- случайность с зерном: одна и та же сцена при каждом открытии ------------------------------------
function rng(seed) {
    let s = (seed >>> 0) || 1;
    return function () {
        s ^= s << 13; s ^= s >>> 17; s ^= s << 5;
        return (s >>> 0) / 4294967296;
    };
}
function hash(i, k) {
    const x = Math.sin(i * 127.1 + (k || 0) * 311.7) * 43758.5453;
    return x - Math.floor(x);
}
function hash3(x, y, z) {
    const v = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453;
    return v - Math.floor(v);
}
function noise3(x, y, z) {
    const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
    const xf = x - xi, yf = y - yi, zf = z - zi;
    const u = xf * xf * (3 - 2 * xf), v = yf * yf * (3 - 2 * yf), w = zf * zf * (3 - 2 * zf);
    let out = 0;
    for (let dx = 0; dx < 2; dx++) for (let dy = 0; dy < 2; dy++) for (let dz = 0; dz < 2; dz++) {
        out += hash3(xi + dx, yi + dy, zi + dz) * (dx ? u : 1 - u) * (dy ? v : 1 - v) * (dz ? w : 1 - w);
    }
    return out;
}
const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

// --- процедурные текстуры: серые тонируются цветом материала, цветные — как есть ------------------------
const maxAniso = () => renderer.capabilities.getMaxAnisotropy();
function canvasTexture(size, draw, meters, color) {
    const c = document.createElement('canvas');
    c.width = c.height = size;
    const ctx = c.getContext('2d');
    draw(ctx, size, rng(size * 7 + meters * 13));
    const t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(1 / meters, 1 / meters);
    t.anisotropy = maxAniso();
    if (color !== false) t.colorSpace = THREE.SRGBColorSpace;
    return t;
}
function speckle(ctx, size, rand, count, colors, rmax) {
    for (let i = 0; i < count; i++) {
        ctx.fillStyle = colors[Math.floor(rand() * colors.length)];
        const r = rand() * rmax + 0.5;
        ctx.fillRect(rand() * size, rand() * size, r, r);
    }
}
function blotches(ctx, size, rand, count, color, alpha, rmax) {
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    for (let i = 0; i < count; i++) {
        ctx.beginPath();
        ctx.arc(rand() * size, rand() * size, rand() * rmax + 4, 0, Math.PI * 2);
        ctx.fill();
    }
    ctx.globalAlpha = 1;
}
const TEX = {};
function textures() {
    TEX.asphalt = canvasTexture(512, function (ctx, s, r) {
        ctx.fillStyle = '#545557'; ctx.fillRect(0, 0, s, s);
        blotches(ctx, s, r, 40, '#3e4044', 0.25, 40);
        blotches(ctx, s, r, 30, '#62656a', 0.18, 30);
        speckle(ctx, s, r, 9000, ['#6c6f73', '#3a3c3f', '#5d6064', '#7a7d80'], 1.2);
    }, 6);
    TEX.pavers = canvasTexture(512, function (ctx, s, r) {
        const n = 8, step = s / n;
        ctx.fillStyle = '#8e8b85'; ctx.fillRect(0, 0, s, s);
        for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
            const l = 168 + Math.floor(r() * 22);
            ctx.fillStyle = `rgb(${l},${l - 3},${l - 9})`;
            ctx.fillRect(i * step + 2, j * step + 2, step - 4, step - 4);
        }
        speckle(ctx, s, r, 5000, ['#9d9a94', '#c4c1ba', '#b0ada6'], 1.2);
    }, 2);
    TEX.tiles = canvasTexture(512, function (ctx, s, r) {
        const n = 4, step = s / n;
        ctx.fillStyle = '#9d9a92'; ctx.fillRect(0, 0, s, s);
        for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
            const l = 186 + Math.floor(r() * 16);
            ctx.fillStyle = `rgb(${l},${l - 2},${l - 8})`;
            ctx.fillRect(i * step + 2, j * step + 2, step - 4, step - 4);
        }
        speckle(ctx, s, r, 6000, ['#aaa79f', '#cfccc4', '#b8b5ad'], 1.4);
    }, 4);
    TEX.grass = canvasTexture(512, function (ctx, s, r) {
        ctx.fillStyle = '#b4b4b4'; ctx.fillRect(0, 0, s, s);
        blotches(ctx, s, r, 50, '#8c8c8c', 0.25, 50);
        blotches(ctx, s, r, 40, '#d6d6d6', 0.18, 40);
        for (let i = 0; i < 14000; i++) {
            const l = 130 + Math.floor(r() * 110);
            ctx.strokeStyle = `rgb(${l},${l},${l})`;
            const x = r() * s, y = r() * s, a = r() * Math.PI;
            ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(a) * 3, y + Math.sin(a) * 3); ctx.stroke();
        }
    }, 3);
    TEX.soil = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#a8a8a8'; ctx.fillRect(0, 0, s, s);
        blotches(ctx, s, r, 30, '#808080', 0.3, 20);
        speckle(ctx, s, r, 5000, ['#c4c4c4', '#7a7a7a', '#999'], 1.5);
    }, 2);
    TEX.gravel = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#b8b8b8'; ctx.fillRect(0, 0, s, s);
        speckle(ctx, s, r, 7000, ['#e0e0e0', '#8a8a8a', '#c8c8c8', '#a0a0a0'], 2.5);
    }, 2);
    TEX.rubber = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#c8c8c8'; ctx.fillRect(0, 0, s, s);
        speckle(ctx, s, r, 6000, ['#d8d8d8', '#b0b0b0'], 1.2);
    }, 2);
    TEX.roof = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#b0b0b0'; ctx.fillRect(0, 0, s, s);
        blotches(ctx, s, r, 20, '#a2a2a2', 0.25, 30);
        speckle(ctx, s, r, 3000, ['#c0c0c0', '#999'], 1.5);
    }, 6);
    TEX.ground = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#c8c8c8'; ctx.fillRect(0, 0, s, s);
        blotches(ctx, s, r, 30, '#c2c2c2', 0.2, 50);
        speckle(ctx, s, r, 3000, ['#cfcfcf', '#c0c0c0'], 1.2);
    }, 24);
    // листва: пятна для рельефа и для цветков/плодов поверх кроны
    TEX.leaf = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#808080'; ctx.fillRect(0, 0, s, s);
        for (let i = 0; i < 1600; i++) {
            const l = Math.floor(r() * 255);
            ctx.fillStyle = `rgb(${l},${l},${l})`;
            ctx.beginPath(); ctx.arc(r() * s, r() * s, r() * 5 + 1.5, 0, Math.PI * 2); ctx.fill();
        }
    }, 1, false);
    TEX.leaf.repeat.set(5, 5);
    // гроздь листьев для карточек по краю кроны: белое — лист, чёрное — просвет
    TEX.cards = canvasTexture(128, function (ctx, s, r) {
        ctx.fillStyle = '#000'; ctx.fillRect(0, 0, s, s);
        ctx.fillStyle = '#fff';
        for (let i = 0; i < 16; i++) {
            const a = r() * Math.PI * 2, d = r() * s * 0.32;
            ctx.save();
            ctx.translate(s / 2 + Math.cos(a) * d, s / 2 + Math.sin(a) * d);
            ctx.rotate(r() * Math.PI);
            ctx.beginPath(); ctx.ellipse(0, 0, s * 0.11, s * 0.05, 0, 0, Math.PI * 2); ctx.fill();
            ctx.restore();
        }
    }, 1, false);
    TEX.cards.repeat.set(1, 1);
    TEX.dots = canvasTexture(256, function (ctx, s, r) {
        ctx.fillStyle = '#000'; ctx.fillRect(0, 0, s, s);
        ctx.fillStyle = '#fff';
        for (let i = 0; i < 200; i++) {
            ctx.beginPath(); ctx.arc(r() * s, r() * s, r() * 2.5 + 1.8, 0, Math.PI * 2); ctx.fill();
        }
    }, 1, false);
    TEX.dots.repeat.set(4, 4);
}
// фасад: одна клетка — 3 × 3 м (этаж и шаг окон)
function facade(wall, glass) {
    return canvasTexture(128, function (ctx, s, r) {
        ctx.fillStyle = wall; ctx.fillRect(0, 0, s, s);
        speckle(ctx, s, r, 600, ['rgba(0,0,0,0.04)', 'rgba(255,255,255,0.05)'], 2);
        const x0 = s * 0.24, x1 = s * 0.76, y0 = s * 0.2, y1 = s * 0.7;
        ctx.fillStyle = 'rgba(0,0,0,0.12)'; ctx.fillRect(x0 - 3, y0 - 3, x1 - x0 + 6, y1 - y0 + 7);
        const g = ctx.createLinearGradient(0, y0, 0, y1);
        g.addColorStop(0, glass[0]); g.addColorStop(1, glass[1]);
        ctx.fillStyle = g; ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        ctx.fillStyle = '#eceae6';
        ctx.fillRect((x0 + x1) / 2 - 1.5, y0, 3, y1 - y0);
        ctx.fillRect(x0, y0 + (y1 - y0) * 0.35, x1 - x0, 3);
        ctx.fillStyle = 'rgba(0,0,0,0.08)'; ctx.fillRect(0, s - 4, s, 4);
    }, 3);
}

// --- геометрия из полигонов сцены ------------------------------------------------------------------------
function ringPoints(flat) {
    const pts = [];
    for (let i = 0; i < flat.length; i += 2) pts.push(new THREE.Vector2(flat[i], flat[i + 1]));
    return pts;
}
function toShapes(polys) {
    const shapes = [];
    for (const rings of polys) {
        if (!rings.length || rings[0].length < 6) continue;
        const shape = new THREE.Shape(ringPoints(rings[0]));
        for (let i = 1; i < rings.length; i++) shape.holes.push(new THREE.Path(ringPoints(rings[i])));
        shapes.push(shape);
    }
    return shapes;
}
// плита толщиной depth от y0; ось y чертежа уходит на север (−z сцены)
function slab(polys, depth, y0) {
    const shapes = toShapes(polys);
    if (!shapes.length) return null;
    const geo = depth > 0
        ? new THREE.ExtrudeGeometry(shapes, { depth: depth, bevelEnabled: false, curveSegments: 1 })
        : new THREE.ShapeGeometry(shapes, 1);
    geo.rotateX(-Math.PI / 2);
    geo.translate(0, y0 || 0, 0);
    return geo;
}
// ExtrudeGeometry: группа 0 — крышки, 1 — стены; делятся, чтобы сливать здания по материалам
function splitGroups(geo) {
    const out = [];
    for (const group of geo.groups) {
        const part = new THREE.BufferGeometry();
        for (const name of ['position', 'normal', 'uv']) {
            const a = geo.attributes[name];
            part.setAttribute(name, new THREE.BufferAttribute(
                a.array.slice(group.start * a.itemSize, (group.start + group.count) * a.itemSize), a.itemSize));
        }
        out[group.materialIndex] = part;
    }
    return out;
}
function polyArea(flat) {
    let a = 0;
    for (let i = 0, n = flat.length; i < n; i += 2) {
        const j = (i + 2) % n;
        a += flat[i] * flat[j + 1] - flat[j] * flat[i + 1];
    }
    return Math.abs(a) / 2;
}
function inRing(x, y, flat) {
    let inside = false;
    for (let i = 0, j = flat.length - 2; i < flat.length; j = i, i += 2) {
        const xi = flat[i], yi = flat[i + 1], xj = flat[j], yj = flat[j + 1];
        if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
}
function inPoly(x, y, rings) {
    if (!inRing(x, y, rings[0])) return false;
    for (let i = 1; i < rings.length; i++) if (inRing(x, y, rings[i])) return false;
    return true;
}
// равномерные точки внутри полигонов: count на всю площадь
function sample(polys, density, rand, cap) {
    const pts = [];
    for (const rings of polys) {
        const flat = rings[0];
        let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
        for (let i = 0; i < flat.length; i += 2) {
            minx = Math.min(minx, flat[i]); maxx = Math.max(maxx, flat[i]);
            miny = Math.min(miny, flat[i + 1]); maxy = Math.max(maxy, flat[i + 1]);
        }
        let want = Math.round(polyArea(flat) * density), tries = want * 6 + 20;
        while (want > 0 && tries-- > 0) {
            const x = lerp(minx, maxx, rand()), y = lerp(miny, maxy, rand());
            if (inPoly(x, y, rings)) { pts.push([x, y]); want--; }
        }
        if (cap && pts.length >= cap) break;
    }
    return pts;
}

// --- кроны: облако «капель» с шумом, нормировано в цилиндр радиус 1, высота 0…1 -----------------------
function cluster(rand, n, c, spread, size, sy) {
    const out = [];
    for (let i = 0; i < n; i++) {
        const a = rand() * Math.PI * 2, d = Math.sqrt(rand());
        const yy = (rand() * 2 - 1);
        out.push([c[0] + Math.cos(a) * d * spread[0], c[1] + yy * spread[1], c[2] + Math.sin(a) * d * spread[2],
                  lerp(size[0], size[1], rand()), sy || 1]);
    }
    return out;
}
const LAYOUTS = {
    round: r => cluster(r, 7, [0, 0.55, 0], [0.42, 0.3, 0.42], [0.38, 0.5]),
    oval: r => cluster(r, 8, [0, 0.6, 0], [0.3, 0.45, 0.3], [0.32, 0.42]),
    spreading: r => cluster(r, 9, [0, 0.5, 0], [0.7, 0.2, 0.7], [0.35, 0.48], 0.75),
    birch: r => cluster(r, 11, [0, 0.62, 0], [0.28, 0.5, 0.28], [0.22, 0.32]),
    weeping: r => cluster(r, 6, [0, 0.7, 0], [0.4, 0.15, 0.4], [0.35, 0.45]).concat(
        Array.from({ length: 8 }, (_, i) => {
            const a = i / 8 * Math.PI * 2 + r() * 0.4;
            return [Math.cos(a) * 0.62, 0.35 + r() * 0.15, Math.sin(a) * 0.62, 0.28, 1.5];
        })),
    small_tree: r => cluster(r, 6, [0, 0.5, 0], [0.4, 0.28, 0.4], [0.38, 0.48]),
    columnar: r => Array.from({ length: 7 }, (_, i) => [(r() - 0.5) * 0.12, 0.12 + i * 0.13, (r() - 0.5) * 0.12,
                                                           0.26 - Math.abs(i - 2.5) * 0.03, 1.4]),
    pine: r => cluster(r, 6, [0, 0.55, 0], [0.6, 0.3, 0.6], [0.32, 0.42], 0.5),
    bush: r => cluster(r, 5, [0, 0.38, 0], [0.45, 0.14, 0.45], [0.4, 0.5]),
    upright: r => cluster(r, 6, [0, 0.55, 0], [0.32, 0.32, 0.32], [0.32, 0.42], 1.25),
    arching: r => cluster(r, 7, [0, 0.38, 0], [0.6, 0.14, 0.6], [0.35, 0.45], 0.7),
    low: r => cluster(r, 5, [0, 0.28, 0], [0.5, 0.1, 0.5], [0.35, 0.45], 0.6),
    creeping: r => cluster(r, 8, [0, 0.15, 0], [0.75, 0.05, 0.75], [0.28, 0.38], 0.4),
    mugo: r => cluster(r, 6, [0, 0.3, 0], [0.5, 0.12, 0.5], [0.35, 0.45], 0.6),
    vine: r => cluster(r, 5, [0, 0.55, 0], [0.2, 0.4, 0.2], [0.3, 0.38], 1.3),
};
const CONES = { spruce: { tiers: 7, jag: 0.22 }, larch: { tiers: 5, jag: 0.35 }, cone_bush: { tiers: 3, jag: 0.12 } };
// доля высоты до кроны (штамб)
const TRUNK = { round: 0.36, oval: 0.3, spreading: 0.36, birch: 0.3, weeping: 0.25, small_tree: 0.3,
                spruce: 0.06, larch: 0.1, pine: 0.55, columnar: 0.08 };
const VARIANTS = 3;

function finishCrown(geo, seed, rough, flatBase, cards) {
    geo.deleteAttribute('normal');
    geo.deleteAttribute('uv');
    geo = mergeVertices(geo, 1e-4);
    const pos = geo.attributes.position;
    const box = new THREE.Box3().setFromBufferAttribute(pos);
    const c = box.getCenter(new THREE.Vector3());
    const v = new THREE.Vector3();
    for (let i = 0; i < pos.count; i++) {
        v.fromBufferAttribute(pos, i);
        const d = v.clone().sub(c);
        const n = noise3(v.x * 4 + seed, v.y * 4, v.z * 4) - 0.5 + (noise3(v.x * 11, v.y * 11 + seed, v.z * 11) - 0.5) * 0.5;
        v.addScaledVector(d.normalize(), n * rough);
        if (flatBase) v.y = Math.max(v.y, box.min.y + (box.max.y - box.min.y) * 0.04);
        pos.setXYZ(i, v.x, v.y, v.z);
    }
    box.setFromBufferAttribute(pos);
    const radius = Math.max(-box.min.x, box.max.x, -box.min.z, box.max.z);
    const h = box.max.y - box.min.y;
    const colors = new Float32Array(pos.count * 3), uvs = new Float32Array(pos.count * 2);
    for (let i = 0; i < pos.count; i++) {
        const x = pos.getX(i) / radius, y = (pos.getY(i) - box.min.y) / h, z = pos.getZ(i) / radius;
        pos.setXYZ(i, x, y, z);
        // светлее сверху и снаружи, темнее внутри и снизу — объём без дорогого освещения
        const rr = Math.min(1, Math.sqrt(x * x + z * z));
        const b = (0.5 + 0.42 * Math.pow(y, 0.8)) * (0.78 + 0.22 * rr) * (0.92 + hash(i, seed) * 0.16);
        colors[i * 3] = colors[i * 3 + 1] = colors[i * 3 + 2] = b;
        uvs[i * 2] = Math.atan2(z, x) / (Math.PI * 2) + 0.5;
        uvs[i * 2 + 1] = y;
    }
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(uvs, 2));
    geo.computeVertexNormals();
    return withCards(geo.toNonIndexed(), seed, cards);
}
// карточки листьев по поверхности кроны: рваный край вместо гладкой капли; нормаль — от центра кроны,
// чтобы освещение карточки совпадало с кроной. Группа 1 геометрии — для материала с прозрачностью.
function withCards(body, seed, count) {
    const rand = rng(seed * 31 + 7);
    const pos = body.attributes.position, col = body.attributes.color;
    const P = [], N = [], C = [], U = [];
    const v = new THREE.Vector3(), n = new THREE.Vector3(), a = new THREE.Vector3(), b = new THREE.Vector3();
    const corners = [[-1, -1], [1, -1], [1, 1], [-1, -1], [1, 1], [-1, 1]];
    for (let k = 0; k < count; k++) {
        const i = Math.floor(rand() * pos.count);
        v.fromBufferAttribute(pos, i);
        n.set(v.x, v.y - 0.5, v.z).normalize();
        v.addScaledVector(n, 0.02 + rand() * 0.07);
        a.set(rand() - 0.5, rand() - 0.5, rand() - 0.5).normalize();
        b.crossVectors(n, a).normalize();
        a.crossVectors(b, n).normalize();
        const size = 0.11 + rand() * 0.08;
        const shade = col.getX(i) * (0.85 + rand() * 0.25);
        for (const [x, y] of corners) {
            P.push(v.x + (a.x * x + b.x * y) * size, v.y + (a.y * x + b.y * y) * size, v.z + (a.z * x + b.z * y) * size);
            N.push(n.x, n.y, n.z);
            C.push(shade, shade, shade);
            U.push((x + 1) / 2, (y + 1) / 2);
        }
    }
    const cardsGeo = new THREE.BufferGeometry();
    cardsGeo.setAttribute('position', new THREE.Float32BufferAttribute(P, 3));
    cardsGeo.setAttribute('normal', new THREE.Float32BufferAttribute(N, 3));
    cardsGeo.setAttribute('color', new THREE.Float32BufferAttribute(C, 3));
    cardsGeo.setAttribute('uv', new THREE.Float32BufferAttribute(U, 2));
    const geo = mergeGeometries([body, cardsGeo], true);
    geo.computeBoundingSphere();
    return geo;
}
function crownGeometry(shape, variant, shrub) {
    const seed = variant * 17 + shape.length * 5;
    const rand = rng(seed * 9973 + 11);
    if (CONES[shape]) {
        const { tiers, jag } = CONES[shape];
        const parts = [];
        for (let t = 0; t < tiers; t++) {
            const f = t / tiers;
            const g = new THREE.ConeGeometry(1 - f * 0.82, 1.9 / tiers, 14, 2);
            g.rotateY(rand() * Math.PI);
            g.translate((rand() - 0.5) * 0.05, f * 0.8 + 0.95 / tiers, (rand() - 0.5) * 0.05);
            parts.push(g);
        }
        return finishCrown(mergeGeometries(parts), seed, jag, false, 260);
    }
    const parts = (LAYOUTS[shape] || LAYOUTS.round)(rand).map(function ([x, y, z, s, sy]) {
        const g = new THREE.IcosahedronGeometry(s, 1);
        g.scale(1, sy, 1);
        g.translate(x, y, z);
        return g;
    });
    return finishCrown(mergeGeometries(parts), seed, shrub ? 0.14 : 0.18, shrub, shrub ? 170 : 420);
}

// --- состояние ---------------------------------------------------------------------------------------------
const state = { season: 'summer', time: 'day', stage: 'after', age: 1, mode: 'orbit',
                context: true, existing: true, highlight: false, quality: true };
let data = null, renderer, scene, camera, controls, composer, gtao, sun, hemi, sky, envTarget, pmrem;
let center = new THREE.Vector3(), radius = 200;
const crowns = [];      // {mesh, overlay, recs}
let trunks = null, trunkRecs = [];
const pickable = [];
const layers = { newLawns: [], beds: [], hedges: [], context: [], lawnBase: null };
let bedFoliage = null, bedFlowers = null, bedRecs = { foliage: [], flowers: [] };
let marker = null;
let dirty = true;
const invalidate = () => { dirty = true; };

const SURF = {
    roadway: { depth: 0.02, tex: 'asphalt' },
    sealed: { depth: 0.1, tex: 'tiles', color: '#b3aea4' },
    sidewalk: { depth: 0.15, tex: 'pavers', color: '#c4bfb6' },
    paving: { depth: 0.15, tex: 'tiles', color: '#cbc5ba' },
    gravel: { depth: 0.12, tex: 'gravel', color: '#c2b59a' },
    playground: { depth: 0.16, tex: 'rubber', color: '#b9654c' },
    lawn: { depth: 0.12, tex: 'grass', color: '#88a35c' },
    flowerbed: { depth: 0.14, tex: 'soil', color: '#7a6450' },
    ground: { depth: 0.1, tex: 'soil', color: '#a08a6a' },
    unknown: { depth: 0.1, tex: 'grass', color: '#a8b08a' },
};
const OBJECT_COLORS = { playground_children: '#c0624a', playground_sport: '#3f8a63', special_surface: '#4f7fae', dog_area: '#b9a37e' };
const LAWN_Y = 0.12;
const GROUND_DROP = 0.4;
// наложения на газон (пересев, площадки) — со сдвигом глубины, чтобы не мерцали
function overlay(material) {
    material.polygonOffset = true;
    material.polygonOffsetFactor = -2;
    material.polygonOffsetUnits = -4;
    return material;
}

// --- цвета по сезону ----------------------------------------------------------------------------------------
const tmp = new THREE.Color(), tmp2 = new THREE.Color();
function leafColor(p, rec) {
    if (state.season === 'autumn') {
        if (p.existing && !p.autumn) return tmp.set(p.leaf);
        if (p.name === 'Существующее дерево') {
            const pal = ['#c49a3a', '#d8b040', '#b8742e', '#8e8a3a', p.leaf];
            return tmp.set(pal[Math.floor(rec.h * pal.length)]);
        }
        return tmp.set(p.autumn || p.leaf);
    }
    tmp.set(p.leaf || p.foliage || '#5f8f3e');
    if (state.season === 'spring' && p.autumn) tmp.lerp(tmp2.set('#a8d46a'), 0.35);
    return tmp;
}
function jitter(color, h) {
    const hsl = {};
    color.getHSL(hsl);
    return color.setHSL(hsl.h + (h - 0.5) * 0.035, clamp(hsl.s * (0.74 + h * 0.18), 0, 1), clamp(hsl.l * (0.86 + h * 0.26), 0, 1));
}
function overlayColor(p) {
    if (p.flower && p.bloom === state.season) return p.flower;
    if (state.season === 'autumn' && p.fruit) return p.fruit;
    return null;
}

// --- растения: записи, размеры по возрасту, инстансы ----------------------------------------------------
function plantRecords() {
    const recs = [];
    const ratio = data.ratio;
    const hedgeMode = data.hedges.length > 0;
    data.plants.forEach(function (row, i) {
        const [x, y, kind, crown, id, sid, gid, role, ref] = row;
        const p = data.profiles[kind];
        if (!p || !p.shape) return;
        if (hedgeMode && role === 'живая изгородь') return;
        const tree = p.group === 'ДЛ' || p.group === 'ДХ';
        const r = p.h && p.k ? clamp(p.h / p.k, 0.5, 4) : (ratio[p.shape] || 1.2);
        let H1 = crown * r;
        if (p.h) H1 = Math.min(H1, p.h);
        H1 = tree ? Math.max(H1, 4.5) : clamp(H1, 0.35, 4);
        const c2 = p.k ? Math.max(crown, Math.min(p.k, crown * 2)) : crown * 1.5;
        const H2 = p.h ? Math.max(H1, Math.min(p.h, H1 * 2.2)) : H1 * 1.5;
        const v = 0.88 + hash(i, 5) * 0.24;
        H1 *= v;
        recs.push({ x, y, p, tree, isNew: true, id, sid, ref, role, gid, h: hash(i, 3),
                    sizes: [[tree ? Math.min(1.6, crown * 0.4) : crown * 0.35, tree ? Math.min(4, H1 * 0.55) : H1 * 0.4],
                            [crown, H1], [c2, H2]] });
    });
    // существующие: размер и крона посчитаны при сборке сцены (разные размеры, не шире промежутка до соседних)
    data.existing.forEach(function ([x, y, kind, row, c, H], i) {
        const p = data.profiles[kind];
        recs.push({ x, y, p, tree: !!TRUNK[p.shape], isNew: false, row, h: hash(i, 7), sizes: [[c, H], [c, H], [c, H]] });
    });
    return recs;
}
function sizeAt(rec) {
    const a = state.age, s = rec.sizes;
    if (!rec.isNew) return s[1];
    const t = a <= 1 ? a * a * (3 - 2 * a) : a - 1;
    const [from, to] = a <= 1 ? [s[0], s[1]] : [s[1], s[2]];
    return [lerp(from[0], to[0], t), lerp(from[1], to[1], t)];
}
const leafMaterial = () => new THREE.MeshStandardMaterial({
    vertexColors: true, roughness: 0.92, metalness: 0, bumpMap: TEX.leaf, bumpScale: 2.2 });

function buildPlants() {
    const recs = plantRecords();
    const groups = new Map();
    for (const rec of recs) {
        rec.variant = Math.floor(rec.h * VARIANTS);
        const key = rec.p.shape + ':' + rec.variant;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(rec);
    }
    const overlayMat = new THREE.MeshStandardMaterial({ alphaMap: TEX.dots, alphaTest: 0.5, roughness: 0.7, side: THREE.DoubleSide });
    const material = [leafMaterial(), new THREE.MeshStandardMaterial({
        vertexColors: true, roughness: 0.9, alphaMap: TEX.cards, alphaTest: 0.45, side: THREE.DoubleSide })];
    for (const [key, list] of groups) {
        const [shape, variant] = key.split(':');
        const shrub = !TRUNK[shape];
        const geo = crownGeometry(shape, Number(variant), shrub);
        const mesh = new THREE.InstancedMesh(geo, material, list.length);
        mesh.castShadow = true;
        mesh.receiveShadow = true;
        const overlay = new THREE.InstancedMesh(geo, overlayMat, list.length);
        overlay.renderOrder = 1;
        list.forEach(function (rec, i) { rec.mesh = mesh; rec.index = i; });
        mesh.userData.recs = list;
        crowns.push({ mesh, overlay, recs: list });
        scene.add(mesh, overlay);
        pickable.push(mesh);
    }
    trunkRecs = recs.filter(r => TRUNK[r.p.shape]);
    const trunkGeo = new THREE.CylinderGeometry(0.65, 1, 1, 7, 1, true);
    trunkGeo.translate(0, 0.5, 0);
    trunks = new THREE.InstancedMesh(trunkGeo, new THREE.MeshStandardMaterial({ roughness: 0.95 }), Math.max(1, trunkRecs.length));
    trunks.castShadow = true;
    scene.add(trunks);
    updatePlants();
}

const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0);
const vp = new THREE.Vector3(), vs = new THREE.Vector3();
const ZERO = new THREE.Matrix4().makeScale(0, 0, 0);
function visible(rec) {
    if (rec.isNew) return state.stage === 'after';
    return state.existing;
}
function updatePlants() {
    for (const { mesh, overlay, recs } of crowns) {
        recs.forEach(function (rec, i) {
            if (!visible(rec)) {
                mesh.setMatrixAt(i, ZERO); overlay.setMatrixAt(i, ZERO);
                return;
            }
            const [c, H] = sizeAt(rec);
            const trunk = TRUNK[rec.p.shape] || 0;
            const base = LAWN_Y + H * trunk;
            const ch = H * (1 - trunk);
            q.setFromAxisAngle(up, rec.h * Math.PI * 2);
            const j = 0.92 + hash(rec.h * 1000, 2) * 0.16;
            vs.set(c / 2 * j, ch, c / 2 * (2 - j));
            mesh.setMatrixAt(i, m4.compose(vp.set(rec.x, base, -rec.y), q, vs));
            const col = overlayColor(rec.p);
            overlay.setMatrixAt(i, col ? m4.compose(vp, q, vs.multiplyScalar(1.025)) : ZERO);
            const leaf = jitter(leafColor(rec.p, rec).clone(), rec.h);
            if (state.highlight && !rec.isNew) leaf.lerp(tmp2.set('#c3c7c2'), 0.8);
            mesh.setColorAt(i, leaf);
            overlay.setColorAt(i, tmp.set(col || '#ffffff'));
            rec.size = [c, H];
        });
        for (const m of [mesh, overlay]) {
            m.instanceMatrix.needsUpdate = true;
            if (m.instanceColor) m.instanceColor.needsUpdate = true;
            m.computeBoundingSphere();
        }
    }
    trunkRecs.forEach(function (rec, i) {
        if (!visible(rec)) { trunks.setMatrixAt(i, ZERO); return; }
        const [c, H] = rec.size || sizeAt(rec);
        const trunk = TRUNK[rec.p.shape];
        const r = clamp(H * 0.016, 0.05, 0.38);
        trunks.setMatrixAt(i, m4.compose(vp.set(rec.x, LAWN_Y, -rec.y), q.identity(), vs.set(r, H * trunk + H * (1 - trunk) * 0.35, r)));
        trunks.setColorAt(i, tmp.set(rec.p.bark && rec.p.shape === 'birch' ? rec.p.bark : rec.p.shape === 'pine' ? '#8a5a3c' : '#5b4a3e'));
    });
    trunks.instanceMatrix.needsUpdate = true;
    if (trunks.instanceColor) trunks.instanceColor.needsUpdate = true;
    trunks.computeBoundingSphere();
    updateBeds();
    updateHedges();
    invalidate();
}

// --- цветники и луга: листва и цветки мелкими инстансами ------------------------------------------------
function buildBeds() {
    const rand = rng(4242);
    const foliage = [], flowers = [];
    let total = 0;
    for (const bed of data.beds) total += bed.a || 0;
    const scale = total * 14 > 160000 ? 160000 / (total * 14) : 1;
    for (const bed of data.beds) {
        const p = data.profiles[bed.s];
        if (!p) continue;
        for (const [x, y] of sample(bed.p, 4 * scale, rand)) foliage.push({ x, y, p, h: rand() });
        for (const [x, y] of sample(bed.p, 10 * scale, rand)) flowers.push({ x, y, p, h: rand() });
    }
    for (const lawn of data.lawns) {
        const p = data.profiles[lawn.s];
        if (!p || !p.flowers) continue;
        for (const [x, y] of sample(lawn.p, (p.density || 0.5) * scale, rand, 40000)) flowers.push({ x, y, p, h: rand(), meadow: true });
    }
    bedRecs = { foliage, flowers };
    if (foliage.length) {
        const g = new THREE.IcosahedronGeometry(1, 0);
        g.translate(0, 0.35, 0);
        bedFoliage = new THREE.InstancedMesh(g, new THREE.MeshStandardMaterial({ roughness: 0.9, bumpMap: TEX.leaf, bumpScale: 1.5 }), foliage.length);
        bedFoliage.receiveShadow = true;
        scene.add(bedFoliage);
    }
    if (flowers.length) {
        const g = new THREE.IcosahedronGeometry(1, 0);
        bedFlowers = new THREE.InstancedMesh(g, new THREE.MeshStandardMaterial({ roughness: 0.6 }), flowers.length);
        scene.add(bedFlowers);
    }
}
function updateBeds() {
    const show = state.stage === 'after';
    const grow = state.age < 1 ? 0.55 + 0.45 * state.age : 1;
    if (bedFoliage) {
        bedRecs.foliage.forEach(function (r, i) {
            if (!show) { bedFoliage.setMatrixAt(i, ZERO); return; }
            const s = (0.16 + r.h * 0.1) * grow;
            bedFoliage.setMatrixAt(i, m4.compose(vp.set(r.x, LAWN_Y, -r.y), q.identity(), vs.set(s, s * 0.8, s)));
            tmp.set(r.p.foliage || '#5a8a48');
            if (state.season === 'autumn') tmp.lerp(tmp2.set('#8f7a48'), 0.45);
            bedFoliage.setColorAt(i, jitter(tmp, r.h));
        });
        bedFoliage.instanceMatrix.needsUpdate = true;
        if (bedFoliage.instanceColor) bedFoliage.instanceColor.needsUpdate = true;
        bedFoliage.computeBoundingSphere();
    }
    if (bedFlowers) {
        bedRecs.flowers.forEach(function (r, i) {
            const blooming = r.p.bloom === state.season || (r.meadow && state.season !== 'autumn');
            if (!show || !blooming) { bedFlowers.setMatrixAt(i, ZERO); return; }
            const s = (r.meadow ? 0.045 : 0.06 + r.h * 0.03) * grow;
            const yy = LAWN_Y + (r.meadow ? 0.12 + r.h * 0.12 : 0.25 + r.h * 0.25) * grow;
            bedFlowers.setMatrixAt(i, m4.compose(vp.set(r.x, yy, -r.y), q.identity(), vs.set(s, s * 0.7, s)));
            const pal = r.p.flowers || ['#f4f0e4'];
            bedFlowers.setColorAt(i, tmp.set(pal[Math.floor(r.h * pal.length) % pal.length]));
        });
        bedFlowers.instanceMatrix.needsUpdate = true;
        if (bedFlowers.instanceColor) bedFlowers.instanceColor.needsUpdate = true;
        bedFlowers.computeBoundingSphere();
    }
}
function updateHedges() {
    for (const hedge of layers.hedges) {
        hedge.visible = state.stage === 'after';
        const H = hedge.userData.height;
        hedge.scale.y = state.age <= 1 ? lerp(0.4, 1, state.age) * H : H * lerp(1, 1.3, state.age - 1);
        hedge.material.color.copy(jitter(leafColor(hedge.userData.p, { h: 0.5 }).clone(), 0.5));
    }
}

// --- поверхности, здания, окружение ------------------------------------------------------------------------
function surfaceMaterial(kind, colorOverride) {
    const spec = SURF[kind];
    const map = TEX[spec.tex].clone();
    map.needsUpdate = true;
    return new THREE.MeshStandardMaterial({ map, color: colorOverride || spec.color || '#ffffff', roughness: 0.95 });
}
function buildGround() {
    const size = radius * 24;
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(size, size),
        new THREE.MeshStandardMaterial({ map: TEX.ground, color: '#c3c1b5', roughness: 1 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.set(center.x, -GROUND_DROP - 0.05, center.z);
    ground.receiveShadow = true;
    ground.material.map.repeat.set(size / 24, size / 24);
    scene.add(ground);

    for (const kind of Object.keys(SURF)) {
        const polys = data.surfaces[kind];
        if (!polys || !polys.length) continue;
        // плиты уходят ниже земли: издалека нет мерцания на стыке, вблизи виден борт
        const geo = slab(polys, SURF[kind].depth + GROUND_DROP, -GROUND_DROP);
        if (!geo) continue;
        const mesh = new THREE.Mesh(geo, surfaceMaterial(kind));
        mesh.receiveShadow = true;
        scene.add(mesh);
        if (kind === 'lawn') layers.lawnBase = mesh;
    }
    for (const obj of data.objects) {
        const geo = slab(obj.p, 0, 0.172);
        if (!geo) continue;
        const mesh = new THREE.Mesh(geo, overlay(surfaceMaterial('playground', OBJECT_COLORS[obj.type] || '#b9654c')));
        mesh.receiveShadow = true;
        scene.add(mesh);
    }
    // новые газоны (пересев) и цветники — поверх газона; «до» их нет
    const lawnMats = new Map();
    for (const lawn of data.lawns) {
        const p = data.profiles[lawn.s] || {};
        const color = p.lawn || '#6ea44a';
        if (!lawnMats.has(color)) lawnMats.set(color, overlay(surfaceMaterial('lawn', color)));
        const geo = slab(lawn.p, 0, LAWN_Y + 0.004);
        if (!geo) continue;
        const mesh = new THREE.Mesh(geo, lawnMats.get(color));
        mesh.receiveShadow = true;
        layers.newLawns.push(mesh);
        scene.add(mesh);
    }
    const soil = surfaceMaterial('flowerbed', '#6a5440');
    for (const bed of data.beds) {
        const geo = slab(bed.p, 0.06, LAWN_Y);
        if (!geo) continue;
        const mesh = new THREE.Mesh(geo, soil);
        mesh.receiveShadow = true;
        layers.beds.push(mesh);
        scene.add(mesh);
    }
    for (const hedge of data.hedges) {
        const p = data.profiles[hedge.s];
        if (!p) continue;
        const geo = slab(hedge.p, 1, 0);
        if (!geo) continue;
        const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ roughness: 0.92, bumpMap: TEX.leaf, bumpScale: 2 }));
        mesh.material.bumpMap = TEX.leaf.clone();
        mesh.material.bumpMap.repeat.set(1.2, 1.2);
        mesh.material.bumpMap.needsUpdate = true;
        mesh.position.y = LAWN_Y;
        mesh.castShadow = mesh.receiveShadow = true;
        mesh.userData = { p, height: clamp((p.h || 1.4) * 0.8, 0.8, 1.6) };
        layers.hedges.push(mesh);
        scene.add(mesh);
    }
}
const PALETTES = [
    { wall: '#e8e2d6', glass: ['#3e4b57', '#71828f'] },
    { wall: '#d7d1c6', glass: ['#3a4651', '#6a7b88'] },
    { wall: '#cdb9a3', glass: ['#3e4954', '#73838e'] },
    { wall: '#e3ddd2', glass: ['#46525d', '#7d8c97'] },
    { wall: '#bfc3c4', glass: ['#38434d', '#677884'] },
];
function buildBuildings() {
    const roofMat = new THREE.MeshStandardMaterial({ map: TEX.roof, color: '#a7a49d', roughness: 0.9 });
    const plainMat = new THREE.MeshStandardMaterial({ color: '#cfcac0', roughness: 0.9 });
    const waterMat = new THREE.MeshStandardMaterial({ color: '#5b8aa3', roughness: 0.08, metalness: 0.1 });
    const wallMats = PALETTES.map(function (pal) {
        const map = facade(pal.wall, pal.glass);
        // v стены = (1 − высота) / 3: сдвиг на треть клетки — этажи от земли
        map.offset.y = -1 / 3;
        return new THREE.MeshStandardMaterial({ map, roughness: 0.75 });
    });
    const buckets = new Map();   // ключ → {material, parts, context}
    function put(key, material, geo, context) {
        if (!buckets.has(key)) buckets.set(key, { material, parts: [], context });
        buckets.get(key).parts.push(geo);
    }
    data.buildings.forEach(function (b, i) {
        const geo = slab(b.p, b.h, 0);
        if (!geo) return;
        const [caps, sides] = splitGroups(geo);
        const ctx = !b.in;
        if (b.water) { put('water' + ctx, waterMat, geo, ctx); return; }
        if (caps) put('roof' + ctx, roofMat, caps, ctx);
        if (!sides) return;
        if (b.h < 3) put('plain' + ctx, plainMat, sides, ctx);
        else {
            const n = Math.floor(hash(i, 5) * wallMats.length);
            put('wall' + n + ctx, wallMats[n], sides, ctx);
        }
    });
    for (const { material, parts, context } of buckets.values()) {
        const mesh = new THREE.Mesh(mergeGeometries(parts.map(g => {
            if (g.groups.length) { g = g.clone(); g.clearGroups(); }
            return g;
        })), material);
        mesh.castShadow = mesh.receiveShadow = true;
        if (context) layers.context.push(mesh);
        scene.add(mesh);
    }
}
// фигуры людей на тротуарах — для масштаба
function buildPeople() {
    const rand = rng(777);
    const polys = [].concat(data.surfaces.sidewalk || [], data.surfaces.paving || []);
    let area = 0;
    for (const rings of polys) area += polyArea(rings[0]);
    const count = Math.min(160, Math.round(area / 220));
    if (!count) return;
    const pts = sample(polys, count / Math.max(area, 1), rand).slice(0, count);
    const body = new THREE.CapsuleGeometry(0.19, 0.95, 3, 8);
    body.translate(0, 0.72, 0);
    const head = new THREE.SphereGeometry(0.115, 10, 8);
    head.translate(0, 1.6, 0);
    const geo = mergeGeometries([body, head]);
    const mesh = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ roughness: 0.8 }), pts.length);
    const colors = ['#3b4a5e', '#7a3b3b', '#d8d2c4', '#2f2f33', '#5b6e4a', '#a8743a', '#46607e', '#8c8c8c'];
    pts.forEach(function ([x, y], i) {
        const s = 0.9 + rand() * 0.2;
        mesh.setMatrixAt(i, m4.compose(vp.set(x, 0.15, -y), q.setFromAxisAngle(up, rand() * 6.28), vs.set(s, s, s)));
        mesh.setColorAt(i, tmp.set(colors[Math.floor(rand() * colors.length)]));
    });
    mesh.castShadow = true;
    scene.add(mesh);
}

// --- небо, солнце, тени ----------------------------------------------------------------------------------------
const TIMES = {
    morning: { el: 14, az: 105, color: '#ffd9b0', sun: 2.4, turbidity: 8, rayleigh: 2.2, fog: '#d9d5cf', exposure: 0.62 },
    day: { el: 46, az: 135, color: '#fff6e8', sun: 3.0, turbidity: 5, rayleigh: 1.2, fog: '#d3dde4', exposure: 0.55 },
    evening: { el: 9, az: 262, color: '#ffbf85', sun: 2.2, turbidity: 9, rayleigh: 2.6, fog: '#dccdbf', exposure: 0.66 },
};
const sunDir = new THREE.Vector3();
function applyTime() {
    const t = TIMES[state.time];
    const el = THREE.MathUtils.degToRad(state.season === 'autumn' ? t.el * 0.75 : t.el);
    const az = THREE.MathUtils.degToRad(t.az);
    sunDir.set(Math.sin(az) * Math.cos(el), Math.sin(el), -Math.cos(az) * Math.cos(el));
    const u = sky.material.uniforms;
    u.sunPosition.value.copy(sunDir);
    u.turbidity.value = t.turbidity;
    u.rayleigh.value = t.rayleigh;
    u.mieCoefficient.value = 0.005;
    u.mieDirectionalG.value = 0.8;
    sun.color.set(t.color);
    sun.intensity = t.sun;
    hemi.intensity = 0.55;
    scene.fog.color.set(t.fog);
    renderer.toneMappingExposure = t.exposure * 1.45;
    // окружение для отражений и рассеянного света — из того же неба
    const envScene = new THREE.Scene();
    const envSky = new Sky();
    envSky.scale.setScalar(1000);
    for (const k of Object.keys(u)) envSky.material.uniforms[k].value = u[k].value;
    envScene.add(envSky);
    if (envTarget) envTarget.dispose();
    envTarget = pmrem.fromScene(envScene, 0.02, 0.1, 2000);
    scene.environment = envTarget.texture;
    scene.environmentIntensity = 0.35;
    invalidate();
}
function updateShadow() {
    const dist = camera.position.distanceTo(controls.target);
    // ближняя плоскость — от расстояния до цели: точности глубины хватает и на обзоре, и у земли
    const near = state.mode === 'walk' ? 0.1 : clamp(dist * 0.004, 0.1, 4);
    if (Math.abs(camera.near - near) > near * 0.2) { camera.near = near; camera.updateProjectionMatrix(); }
    // дымка — за предметом съёмки, а не на нём: от расстояния до цели
    const haze = state.mode === 'walk' ? 120 : dist;
    scene.fog.near = haze * 1.6 + 60;
    scene.fog.far = haze * 6 + 600;
    const s = clamp(dist * 0.85, 35, radius * 1.4);
    const cam = sun.shadow.camera;
    cam.left = -s; cam.right = s; cam.top = s; cam.bottom = -s;
    cam.near = 1; cam.far = s * 6 + 400;
    cam.updateProjectionMatrix();
    const focus = state.mode === 'walk'
        ? camera.position.clone().add(camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(s * 0.6)).setY(0)
        : controls.target.clone().setY(0);
    sun.position.copy(focus).addScaledVector(sunDir, s * 3 + 200);
    sun.target.position.copy(focus);
    sun.target.updateMatrixWorld();
    // смещение — в долях глубины карты теней: ~5 см при любом её размере
    sun.shadow.bias = -0.05 / (cam.far - cam.near);
}

// --- камера ---------------------------------------------------------------------------------------------------------
let flight = null;
function camPose(target, dist, elDeg, azDeg) {
    const el = THREE.MathUtils.degToRad(elDeg), az = THREE.MathUtils.degToRad(azDeg);
    return { target: target.clone(), pos: target.clone().add(new THREE.Vector3(
        Math.sin(az) * Math.cos(el), Math.sin(el), -Math.cos(az) * Math.cos(el)).multiplyScalar(dist)) };
}
function flyTo(pose, instant) {
    if (instant) {
        camera.position.copy(pose.pos);
        controls.target.copy(pose.target);
        controls.update();
        invalidate();
        return;
    }
    flight = { from: { pos: camera.position.clone(), target: controls.target.clone() }, to: pose, t0: performance.now(), ms: 900 };
}
function setMode(mode) {
    state.mode = mode;
    const walk = mode === 'walk';
    controls.enableZoom = !walk;
    controls.enablePan = !walk;
    controls.minDistance = walk ? 0.05 : 2;
    controls.maxDistance = walk ? 0.06 : radius * 5;
    controls.maxPolarAngle = walk ? Math.PI * 0.95 : Math.PI * 0.48;
    controls.rotateSpeed = walk ? -0.35 : 1;
    root.querySelector('[data-hint="walk"]').hidden = !walk;
    root.querySelector('[data-hint="orbit"]').hidden = walk;
    root.querySelectorAll('[data-view]').forEach(b => b.classList.toggle('is-on', b.dataset.view === (walk ? 'walk' : '')));
}
// обзор: камера поперёк главной оси участка (длинный участок ложится вдоль кадра), с южной стороны —
// чтобы солнце светило сбоку и тени были видны; расстояние — чтобы участок вошёл по ширине кадра
let axis = null;
function territoryAxis() {
    if (axis) return axis;
    let n = 0, mx = 0, my = 0, sxx = 0, syy = 0, sxy = 0;
    const pts = [];
    for (const rings of data.territory) for (let i = 0; i < rings[0].length; i += 2) pts.push([rings[0][i], rings[0][i + 1]]);
    for (const [x, y] of pts) { mx += x; my += y; n++; }
    mx /= n; my /= n;
    for (const [x, y] of pts) { sxx += (x - mx) ** 2; syy += (y - my) ** 2; sxy += (x - mx) * (y - my); }
    const angle = 0.5 * Math.atan2(2 * sxy, sxx - syy);
    const ux = Math.cos(angle), uy = Math.sin(angle);
    let lo = Infinity, hi = -Infinity, wlo = Infinity, whi = -Infinity;
    for (const [x, y] of pts) {
        const t = (x - mx) * ux + (y - my) * uy, w = -(x - mx) * uy + (y - my) * ux;
        lo = Math.min(lo, t); hi = Math.max(hi, t); wlo = Math.min(wlo, w); whi = Math.max(whi, w);
    }
    // азимут оси (от севера по часовой) и два перпендикуляра — берётся тот, что ближе к югу-юго-западу
    const az = 90 - THREE.MathUtils.radToDeg(angle);
    const options = [az + 90, az - 90].map(a => ((a % 360) + 360) % 360);
    const diff = a => Math.abs(((a - 205 + 540) % 360) - 180);
    axis = { az: options.sort((a, b) => diff(a) - diff(b))[0], length: hi - lo, width: whi - wlo };
    return axis;
}
function overviewPose() {
    const { az, length, width } = territoryAxis();
    const vfov = THREE.MathUtils.degToRad(camera.fov);
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * camera.aspect);
    const dist = Math.max(length / 2 / Math.tan(hfov / 2), width / Math.tan(vfov / 2)) * 0.82;
    return camPose(center, clamp(dist, 60, radius * 2.5), 38, az);
}
function topPose() { return camPose(center, radius * 2.3, 89, 180); }
function structurePose(s, el) {
    const t = new THREE.Vector3(s.x, 0, -s.y);
    return camPose(t, clamp(Math.max(s.w, s.h) * 0.95, 30, radius * 1.5), el || 38, 205);
}
// пешеход: точка тротуара в окне структуры, взгляд — на её середину
let walkPoints = null;
function walkPose(s) {
    if (!walkPoints) {
        const polys = [].concat(data.surfaces.sidewalk || [], data.surfaces.paving || []);
        let area = 0;
        for (const rings of polys) area += polyArea(rings[0]);
        const trees = new Map();
        for (const { recs } of crowns) for (const r of recs) if (r.tree) {
            const key = Math.floor(r.x / 4) + ':' + Math.floor(r.y / 4);
            if (!trees.has(key)) trees.set(key, []);
            trees.get(key).push(r);
        }
        const near = function (x, y, d) {
            for (let i = -1; i <= 1; i++) for (let j = -1; j <= 1; j++) {
                for (const r of trees.get((Math.floor(x / 4) + i) + ':' + (Math.floor(y / 4) + j)) || []) {
                    if (Math.hypot(r.x - x, r.y - y) < d) return true;
                }
            }
            return false;
        };
        // не в стволе: точки ближе 2 м к деревьям отбрасываются
        walkPoints = sample(polys, Math.min(0.2, 6000 / Math.max(area, 1)), rng(99)).filter(([x, y]) => !near(x, y, 2));
        walkPoints.near = near;
    }
    const target = s ? new THREE.Vector3(s.x, 0, -s.y) : center.clone();
    const want = s ? clamp(Math.min(s.w, s.h) * 0.35, 8, 40) : radius * 0.3;
    const scored = [];
    for (const [x, y] of walkPoints) {
        if (s && (Math.abs(x - s.x) > s.w / 2 || Math.abs(y - s.y) > s.h / 2)) continue;
        scored.push([Math.abs(Math.hypot(x - target.x, -y - target.z) - want), x, y]);
    }
    scored.sort((a, b) => a[0] - b[0]);
    // первые 10 м взгляда — без стволов
    const clear = function (x, y) {
        const dx = target.x - x, dy = -target.z - y, len = Math.hypot(dx, dy) || 1;
        for (let t = 1.5; t <= 10; t += 1.5) {
            if (walkPoints.near(x + dx / len * t, y + dy / len * t, 1.3)) return false;
        }
        return true;
    };
    const best = scored.slice(0, 200).find(([, x, y]) => clear(x, y)) || scored[0];
    const pos = best ? new THREE.Vector3(best[1], 1.7, -best[2]) : target.clone().add(new THREE.Vector3(0, 1.7, want));
    const dir = target.clone().setY(1.6).sub(pos).normalize();
    return { pos, target: pos.clone().addScaledVector(dir, 0.055) };
}
function goWalk(s) {
    setMode('walk');
    flyTo(walkPose(s || largestStructure()));
}
function largestStructure() {
    return data.structures.slice().sort((a, b) => b.w * b.h - a.w * a.h)[0];
}
const keys = new Set();
function walkStep(dt) {
    if (state.mode !== 'walk' || !keys.size) return false;
    const fwd = camera.getWorldDirection(new THREE.Vector3()).setY(0).normalize();
    const right = new THREE.Vector3().crossVectors(fwd, up);
    const move = new THREE.Vector3();
    if (keys.has('w') || keys.has('arrowup') || keys.has('ц')) move.add(fwd);
    if (keys.has('s') || keys.has('arrowdown') || keys.has('ы')) move.sub(fwd);
    if (keys.has('d') || keys.has('arrowright') || keys.has('в')) move.add(right);
    if (keys.has('a') || keys.has('arrowleft') || keys.has('ф')) move.sub(right);
    if (!move.lengthSq()) return false;
    move.normalize().multiplyScalar(dt * (keys.has('shift') ? 9 : 3.5));
    camera.position.add(move);
    controls.target.add(move);
    return true;
}

// --- выбор растения щелчком -----------------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
function pick(event) {
    const rect = canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(ndc, camera);
    const hit = raycaster.intersectObjects(pickable, false)[0];
    if (!hit || hit.instanceId == null) { closeCard(); return; }
    const rec = hit.object.userData.recs[hit.instanceId];
    if (!rec || !visible(rec)) { closeCard(); return; }
    showCard(rec);
}
const ROLE_NAMES = { 'солитер': 'солитер', 'группа': 'группа' };
function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function showCard(rec) {
    const [c, H] = rec.size || sizeAt(rec);
    const structure = data.structures.find(s => s.id === rec.sid);
    const note = rec.isNew && data.plantings[rec.sid] ? data.plantings[rec.sid][rec.ref] : null;
    const age = state.age < 0.5 ? 'при посадке' : state.age < 1.5 ? 'через ~10 лет' : 'взрослое';
    card.innerHTML = '<button type="button" class="sc-close" aria-label="Закрыть">×</button>'
        + '<h3>' + esc(rec.p.name) + '</h3>'
        + '<div class="sc-card-sub">'
        + (rec.isNew ? '<span class="sc-tag">новая посадка</span> ' : '<span class="sc-tag is-old">существующее дерево</span> ')
        + esc(rec.role ? (ROLE_NAMES[rec.role] || rec.role) : '') + '</div>'
        + (structure ? '<p>' + esc(structure.name) + ' · ' + esc(structure.id) + (rec.id ? ' · ' + esc(rec.id) : '') + '</p>' : '')
        + '<p>Высота ~' + H.toFixed(1) + ' м, крона ~' + c.toFixed(1) + ' м' + (rec.isNew ? ' (' + age + ')' : '') + '</p>'
        + (note && note.reason ? '<p>' + esc(note.reason) + (note.reason.length >= 400 ? '…' : '') + '</p>' : '')
        + (!rec.isNew && /^Существующ/.test(rec.p.name) ? '<p>Порода в подоснове не подписана — показано условно.</p>' : '')
        + (!rec.isNew && rec.row ? '<p>Полоса деревьев: стволы условные, через 5 м.</p>' : '');
    card.hidden = false;
    card.querySelector('.sc-close').addEventListener('click', closeCard);
    if (!marker) {
        marker = new THREE.Mesh(new THREE.RingGeometry(0.85, 1, 48), new THREE.MeshBasicMaterial({ color: '#ffffff', transparent: true, opacity: 0.9, depthTest: false }));
        marker.rotation.x = -Math.PI / 2;
        marker.renderOrder = 5;
        scene.add(marker);
    }
    marker.visible = true;
    marker.position.set(rec.x, LAWN_Y + 0.05, -rec.y);
    marker.scale.setScalar(Math.max(0.6, c / 2 + 0.3));
    invalidate();
}
function closeCard() {
    card.hidden = true;
    if (marker) marker.visible = false;
    invalidate();
}

// --- легенда: новые виды на сцене ---------------------------------------------------------------------------
function updateLegend() {
    const counts = new Map();
    for (const { recs } of crowns) for (const r of recs) if (r.isNew) counts.set(r.p, (counts.get(r.p) || 0) + 1);
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
    legend.innerHTML = state.stage === 'after' ? top.map(function ([p, n]) {
        const col = overlayColor(p) || '#' + leafColor(p, { h: 0.5 }).getHexString();
        return '<span><i style="background:' + col + '"></i>' + esc(p.name.split(/[«(/]/)[0].trim()) + ' · ' + n + '</span>';
    }).join('') : '';
}

// --- рендер ---------------------------------------------------------------------------------------------------------
function render() {
    updateShadow();
    if (state.quality && composer) composer.render();
    else renderer.render(scene, camera);
}
function resize() {
    const w = root.clientWidth, h = root.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    if (composer) composer.setSize(w, h);
    invalidate();
}
let last = performance.now();
function loop(now) {
    requestAnimationFrame(loop);
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    if (flight) {
        const t = clamp((now - flight.t0) / flight.ms, 0, 1);
        const e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        camera.position.lerpVectors(flight.from.pos, flight.to.pos, e);
        controls.target.lerpVectors(flight.from.target, flight.to.target, e);
        if (t >= 1) flight = null;
        dirty = true;
    }
    if (walkStep(dt)) dirty = true;
    if (controls.update()) dirty = true;
    if (dirty) {
        dirty = false;
        render();
    }
}

// --- снимки: PNG текущего вида и серия для отчёта -----------------------------------------------------------
function capture(width, height) {
    return new Promise(function (resolve) {
        const w = root.clientWidth, h = root.clientHeight, ratio = renderer.getPixelRatio();
        renderer.setPixelRatio(1);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
        if (composer) { composer.setPixelRatio(1); composer.setSize(width, height); }
        render();
        canvas.toBlob(function (blob) { resolve(blob); }, 'image/png');
        renderer.setPixelRatio(ratio);
        if (composer) composer.setPixelRatio(ratio);
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        if (composer) composer.setSize(w, h);
        invalidate();
    });
}
const stem = (root.dataset.name || 'генплан').replace(/\.dxf$/i, '');
function download(blob, name) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
async function shot() {
    const blob = await capture(Math.round(root.clientWidth * 2), Math.round(root.clientHeight * 2));
    download(blob, stem + '_3d.png');
}
const SEASON_NAMES = { spring: 'весна', summer: 'лето', autumn: 'осень' };
function seriesPlan() {
    const plan = [
        { title: 'Обзор — после', pose: overviewPose(), stage: 'after', mode: 'orbit' },
        { title: 'Обзор — до', pose: overviewPose(), stage: 'before', mode: 'orbit' },
        { title: 'Сверху', pose: topPose(), stage: 'after', mode: 'orbit' },
    ];
    const big = data.structures.slice().sort((a, b) => b.w * b.h - a.w * a.h).slice(0, 6);
    for (const s of big) plan.push({ title: s.name + ' · ' + s.id, pose: structurePose(s, 32), stage: 'after', mode: 'orbit' });
    for (const s of big.slice(0, 3)) plan.push({ title: 'Взгляд пешехода · ' + s.id, pose: walkPose(s), stage: 'after', mode: 'walk' });
    return plan;
}
async function series() {
    const grid = gallery.querySelector('.sc-gallery-grid');
    const status = gallery.querySelector('.sc-gallery-state');
    const all = gallery.querySelector('[data-action="download-all"]');
    grid.innerHTML = '';
    all.disabled = true;
    gallery.hidden = false;
    const saved = { pos: camera.position.clone(), target: controls.target.clone(), stage: state.stage, mode: state.mode };
    const plan = seriesPlan();
    const files = [];
    for (let i = 0; i < plan.length; i++) {
        const item = plan[i];
        status.textContent = 'кадр ' + (i + 1) + ' из ' + plan.length;
        if (state.stage !== item.stage) { state.stage = item.stage; applyStage(); }
        setMode(item.mode);
        flyTo(item.pose, true);
        await new Promise(r => requestAnimationFrame(r));
        const blob = await capture(1920, 1080);
        const name = stem + '_3d_' + String(i + 1).padStart(2, '0') + '_' + SEASON_NAMES[state.season] + '.png';
        files.push({ blob, name });
        const url = URL.createObjectURL(blob);
        grid.insertAdjacentHTML('beforeend', '<figure><img src="' + url + '" alt=""><figcaption><span>'
            + esc(item.title) + '</span><a href="' + url + '" download="' + esc(name) + '">PNG</a></figcaption></figure>');
    }
    state.stage = saved.stage;
    applyStage();
    setMode(saved.mode);
    flyTo({ pos: saved.pos, target: saved.target }, true);
    status.textContent = plan.length + ' кадров, 1920 × 1080, ' + SEASON_NAMES[state.season];
    all.disabled = false;
    all.onclick = async function () {
        for (const f of files) {
            download(f.blob, f.name);
            await new Promise(r => setTimeout(r, 350));
        }
    };
}

// --- переключатели -----------------------------------------------------------------------------------------------
function applyStage() {
    const after = state.stage === 'after';
    for (const m of layers.newLawns) m.visible = after;
    for (const m of layers.beds) m.visible = after;
    if (layers.lawnBase) layers.lawnBase.material.color.set(after ? SURF.lawn.color : '#a2a86e');
    root.querySelectorAll('[data-group="stage"] button').forEach(b => b.classList.toggle('is-on', b.dataset.value === state.stage));
    updatePlants();
    updateLegend();
}
function applySeason() {
    const lawn = { spring: '#90b25f', summer: '#88a35c', autumn: '#a3a15e' }[state.season];
    SURF.lawn.color = lawn;
    if (layers.lawnBase) layers.lawnBase.material.color.set(state.stage === 'after' ? lawn : '#a2a86e');
    applyTime();
    updatePlants();
    updateLegend();
}
function bindUi() {
    root.querySelectorAll('.sc-seg').forEach(function (seg) {
        seg.addEventListener('click', function (e) {
            const b = e.target.closest('button');
            if (!b) return;
            seg.querySelectorAll('button').forEach(x => x.classList.toggle('is-on', x === b));
            const group = seg.dataset.group;
            state[group] = b.dataset.value;
            if (group === 'season') applySeason();
            else if (group === 'time') applyTime();
            else applyStage();
        });
    });
    const age = root.querySelector('.sc-age');
    let ageFrame = 0;
    age.addEventListener('input', function () {
        state.age = Number(age.value);
        cancelAnimationFrame(ageFrame);
        ageFrame = requestAnimationFrame(updatePlants);
    });
    root.querySelectorAll('[data-view]').forEach(function (b) {
        b.addEventListener('click', function () {
            if (b.dataset.view === 'walk') { goWalk(); return; }
            setMode('orbit');
            flyTo(b.dataset.view === 'top' ? topPose() : overviewPose());
        });
    });
    const select = root.querySelector('.sc-structure');
    for (const s of data.structures) {
        const o = document.createElement('option');
        o.value = s.id;
        o.textContent = s.id + ' · ' + s.name;
        select.appendChild(o);
    }
    select.addEventListener('change', function () {
        const s = data.structures.find(x => x.id === select.value);
        if (!s) return;
        if (state.mode === 'walk') goWalk(s);
        else flyTo(structurePose(s));
    });
    root.querySelectorAll('[data-toggle]').forEach(function (input) {
        input.addEventListener('change', function () {
            const key = input.dataset.toggle;
            state[key] = input.checked;
            if (key === 'context') for (const m of layers.context) m.visible = input.checked;
            if (key === 'existing' || key === 'highlight') updatePlants();
            invalidate();
        });
    });
    root.querySelector('[data-action="shot"]').addEventListener('click', shot);
    root.querySelector('[data-action="series"]').addEventListener('click', series);
    gallery.querySelector('[data-action="close-gallery"]').addEventListener('click', function () { gallery.hidden = true; });

    let down = null;
    canvas.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; });
    canvas.addEventListener('pointerup', function (e) {
        if (down && Math.hypot(e.clientX - down[0], e.clientY - down[1]) < 5) pick(e);
        down = null;
    });
    window.addEventListener('keydown', function (e) {
        if (e.target.closest && e.target.closest('select, input')) return;
        keys.add(e.key.toLowerCase());
        if (state.mode === 'walk' && e.key.startsWith('Arrow')) e.preventDefault();
    });
    window.addEventListener('keyup', e => keys.delete(e.key.toLowerCase()));
    window.addEventListener('blur', () => keys.clear());
    window.addEventListener('resize', resize);
}

// --- запуск ---------------------------------------------------------------------------------------------------------
function setup() {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: 'high-performance' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.5));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    textures();

    const [x0, y0, x1, y1] = data.extent;
    center.set((x0 + x1) / 2, 0, -(y0 + y1) / 2);
    radius = Math.max(60, Math.hypot(x1 - x0, y1 - y0) / 2);

    scene = new THREE.Scene();
    scene.fog = new THREE.Fog('#d3dde4', radius * 1.3, radius * 5.5);
    camera = new THREE.PerspectiveCamera(42, 1, 0.25, radius * 30);
    sky = new Sky();
    sky.scale.setScalar(radius * 25);
    scene.add(sky);
    pmrem = new THREE.PMREMGenerator(renderer);
    hemi = new THREE.HemisphereLight('#dfe9f3', '#7d7a66', 0.55);
    scene.add(hemi);
    sun = new THREE.DirectionalLight('#ffffff', 3);
    sun.castShadow = true;
    const size = Math.min(4096, renderer.capabilities.maxTextureSize);
    sun.shadow.mapSize.set(size, size);
    sun.shadow.normalBias = 0.03;
    scene.add(sun, sun.target);

    controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.screenSpacePanning = false;
    controls.addEventListener('change', invalidate);

    buildGround();
    buildBuildings();
    buildBeds();
    buildPlants();
    buildPeople();

    composer = new EffectComposer(renderer, new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: 4 }));
    composer.addPass(new RenderPass(scene, camera));
    gtao = new GTAOPass(scene, camera, 1, 1);
    gtao.updateGtaoMaterial({ radius: 1.6, distanceExponent: 1.4, thickness: 2, scale: 1.1, samples: 12 });
    gtao.blendIntensity = 0.9;
    composer.addPass(gtao);
    composer.addPass(new OutputPass());

    setMode('orbit');
    applySeason();
    applyStage();
    bindUi();
    resize();
    flyTo(overviewPose(), true);
    requestAnimationFrame(loop);
}

async function start() {
    const slow = setTimeout(function () {
        loadingText.textContent = 'Собираем сцену по чертежу — в первый раз это может занять до минуты…';
    }, 3000);
    try {
        const response = await fetch(root.dataset.scene);
        if (!response.ok) throw new Error(response.status === 404 ? 'Результат ещё не готов или задача не найдена.' : 'Не удалось загрузить сцену (' + response.status + ').');
        data = await response.json();
        clearTimeout(slow);
        loadingText.textContent = 'Строим 3D-вид…';
        await new Promise(r => setTimeout(r, 30));
        setup();
        loading.classList.add('is-done');
    } catch (err) {
        clearTimeout(slow);
        console.error(err);
        loading.classList.add('is-error');
        loadingText.textContent = err.message || String(err);
    }
}
start();
window.greening3d = { get scene() { return scene; }, get data() { return data; }, get camera() { return camera; }, get controls() { return controls; }, state, invalidate, render: () => render(), get renderer() { return renderer; } };
