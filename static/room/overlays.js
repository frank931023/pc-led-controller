/**
 * Overlays: drawing aids on top of the 3D room. They never move anything on the rig.
 *
 * Two layers:
 *
 *   setup overlays   part of a setup (setup.overlays, validated by mcc/setups.py):
 *                    points    markers attached to an object with an offset (e.g. the DUT 20 cm
 *                              behind the table centre, or a manually placed prop)
 *                    circles   white dashed reference circles around an object or point
 *                    distances pinned pairs ("HATS mouth -> DUT")
 *
 *   view             what this viewer chooses to see (the "Show" panel, viewpanel.js; kept in the
 *                    browser, never saved with a setup):
 *                    - distances from a reference object to the ticked objects (yellow)
 *                    - while an object is adjusted, its distance to the ticked objects (or, when none
 *                      is ticked, to the reference object only)
 *                    - a quick circle around any object, with its own radii
 *                    - each setup overlay item switched on / off
 *                    - short labels (numbers only)
 *
 * Every object has a reference point computed from the axes (OBJECTS), so everything follows
 * moving objects. Ported and generalised from the Teams V5 setup preview (annotations.js).
 */
import * as THREE from 'three';
import { CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { CONFIG as C } from './config.js';

// ── Reference points of the objects (room coordinates, metres; origin = floor centre) ──
export const MOUTH_FWD = 0.15;                  // MRP in front of the HATS turntable centre
const TABLE_TOP = C.tables.thickness;           // table top = lift + thickness
const SCREEN_FACE = 0.06;                       // display face in front of screen.z
const SPEAKER_FRONT = C.speakerRail.z + C.speakerStand.base.depth / 2;

/** key -> {label, at(ax)}. key = the axis id prefix ("hats.mrp" -> "hats"). */
export const OBJECTS = {
  hats: {
    label: 'HATS mouth',
    at: (ax) => {
      const rot = THREE.MathUtils.degToRad(ax['hats.rot'] ?? 0);
      return new THREE.Vector3(ax['hats.x'] + Math.sin(rot) * MOUTH_FWD, ax['hats.mrp'],
        ax['hats.z'] + Math.cos(rot) * MOUTH_FWD);
    },
  },
  tableFront: { label: 'Front table top', at: (ax) => new THREE.Vector3(0, ax['tableFront.lift'] + TABLE_TOP, ax['tableFront.z']) },
  tableBack: { label: 'Back table top', at: (ax) => new THREE.Vector3(0, ax['tableBack.lift'] + TABLE_TOP, ax['tableBack.z']) },
  screen: { label: 'Display centre', at: (ax) => new THREE.Vector3(0, ax['screen.lift'], ax['screen.z'] - SCREEN_FACE) },
  speakers: { label: 'Speaker array', at: (ax) => new THREE.Vector3(ax['speakers.x'], ax['speakers.lift'], SPEAKER_FRONT) },
  micRig1: { label: 'Mic 1', at: (ax) => new THREE.Vector3(ax['micRig1.x'], ax['micRig1.h'], ax['micRig1.z']) },
  micRig2: { label: 'Mic 2', at: (ax) => new THREE.Vector3(ax['micRig2.x'], ax['micRig2.h'], ax['micRig2.z']) },
};

export const POINT_STYLES = { dut: 'DUT', ghost: 'Manual prop', point: 'Marker' };
export const emptyOverlays = () => ({ points: [], circles: [], distances: [] });

/** Keys of setup overlay items in view.hidden */
export const itemKey = {
  point: (p) => `p:${p.id}`,
  circle: (c) => `c:${c.id}`,
  distance: (d) => `d:${d.from}>${d.to}`,
};

const COLORS = { circle: 0xffffff, pinned: 0x3fd0b0, measure: 0xe3b341 };
const MEASURE_HOLD = 2.5;          // seconds the while-adjusting lines stay after the last change
const VIEW_KEY = 'mccRoomView';
const CFG_KEY = 'mccRoomOverlays';

const defaultView = () => ({
  open: true,
  from: 'hats',                    // reference object of the distance lines
  to: [],                          // ticked objects / points: always show their distance to `from`
  whileAdjusting: true,
  quick: {},                       // object key -> {on, radii}: quick circles
  hidden: {},                      // itemKey -> true: setup overlay items switched off
  short: false,                    // numbers-only labels
});

// ── Small builders ──────────────────────────────
function label(cls, prio) {
  const outer = document.createElement('div');
  outer.className = 'ann-anchor';
  const el = document.createElement('div');
  el.className = `ann ${cls}`;
  outer.appendChild(el);
  const obj = new CSS2DObject(outer);
  obj.userData.ann = { el, auto: 0, prio };
  return obj;
}
const setText = (lab, text) => { if (lab.userData.ann.el.textContent !== text) lab.userData.ann.el.textContent = text; };

const dashed = (color, opacity, dash, gap) => new THREE.LineDashedMaterial({
  color, dashSize: dash, gapSize: gap, transparent: true, opacity, depthTest: false });

/** Ring in the horizontal plane; angle 0 = +Z. span 'half' = -90°..+90° (rotate the group to aim it). */
function ring(radius, span) {
  const [from, to] = span === 'half' ? [-Math.PI / 2, Math.PI / 2] : [0, Math.PI * 2];
  const pts = [];
  for (let i = 0; i <= 120; i++) {
    const a = from + (to - from) * (i / 120);
    pts.push(new THREE.Vector3(Math.sin(a) * radius, 0, Math.cos(a) * radius));
  }
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), dashed(COLORS.circle, 0.8, 0.06, 0.04));
  line.computeLineDistances();
  line.renderOrder = 5;
  return line;
}

function segment(color) {
  const geo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, 1)]);
  const line = new THREE.Line(geo, dashed(color, 0.95, 0.05, 0.03));
  line.renderOrder = 6;
  return line;
}

function setSegment(line, a, b) {
  const pos = line.geometry.attributes.position;
  pos.setXYZ(0, a.x, a.y, a.z);
  pos.setXYZ(1, b.x, b.y, b.z);
  pos.needsUpdate = true;
  line.geometry.computeBoundingSphere();
  line.computeLineDistances();
}

const signed = (v) => `${v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}`;
/** Horizontal distance (what test specs give) and height difference b - a */
export function distanceText(a, b, short = false) {
  const horiz = Math.hypot(b.x - a.x, b.z - a.z).toFixed(2);
  return short ? `${horiz} ↕${signed(b.y - a.y)}` : `${horiz} m · ↕ ${signed(b.y - a.y)} m`;
}

const MATS = {
  dut: new THREE.MeshStandardMaterial({ color: 0xff9f1a, emissive: 0x552800, roughness: 0.5 }),
  ghost: new THREE.MeshStandardMaterial({ color: 0xb48cff, transparent: true, opacity: 0.45, roughness: 0.6, depthWrite: false }),
  point: new THREE.MeshBasicMaterial({ color: 0xffffff }),
};
const GEOS = {
  dut: new THREE.BoxGeometry(0.16, 0.05, 0.10),
  ghost: new THREE.SphereGeometry(0.05, 18, 12),
  point: new THREE.SphereGeometry(0.02, 12, 8),
};

const storage = {
  get: (k, d) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ══════════════════════════════════════════════════
export function createOverlays({ scene, getAxes }) {
  const root = new THREE.Group();
  scene.add(root);

  let cfg = emptyOverlays();
  let built = { points: [], circles: [], distances: [] };
  const quick = new Map();           // object key -> {group, lab, radii}
  const lines = new Map();           // "from>to" -> {line, lab}: reference + while-adjusting lines
  let view = { ...defaultView(), ...storage.get(VIEW_KEY, {}) };
  let measuring = null;              // {key, until}
  let clock = 0;
  const listeners = new Set();
  const notify = () => listeners.forEach((fn) => fn());

  // ── References ──
  function position(ref, ax) {
    if (OBJECTS[ref]) return OBJECTS[ref].at(ax);
    const p = cfg.points.find((x) => x.id === ref);
    if (!p || !OBJECTS[p.on]) return null;
    return OBJECTS[p.on].at(ax).add(new THREE.Vector3(...p.offset));
  }
  const refLabel = (ref) => OBJECTS[ref]?.label || cfg.points.find((x) => x.id === ref)?.label || ref;
  /** Objects, then the setup's points: everything a circle or distance can use */
  const refs = () => [
    ...Object.entries(OBJECTS).map(([key, o]) => ({ ref: key, label: o.label, object: true })),
    ...cfg.points.map((p) => ({ ref: p.id, label: p.label || p.id, style: p.style, on: p.on })),
  ];
  const shown = (key) => !view.hidden[key];

  // ── Setup overlays ──
  function dropLabel(lab) { lab.element.remove(); }

  function rebuild() {
    for (const b of [...built.points, ...built.circles, ...built.distances]) {
      root.remove(b.mesh || b.group || b.line, b.lab);
      dropLabel(b.lab);
    }
    built = { points: [], circles: [], distances: [] };
    for (const p of cfg.points) {
      const style = MATS[p.style] ? p.style : 'point';
      const mesh = new THREE.Mesh(GEOS[style], MATS[style]);
      const lab = label(style === 'dut' ? 'dut' : style === 'ghost' ? 'ghost' : 'pt', 2);
      setText(lab, p.label || p.id);
      root.add(mesh, lab);
      built.points.push({ def: p, mesh, lab });
    }
    for (const c of cfg.circles) {
      const group = new THREE.Group();
      c.radii.forEach((r) => group.add(ring(r, c.span)));
      const lab = label('circ', 3);
      root.add(group, lab);
      built.circles.push({ def: c, group, lab });
    }
    for (const d of cfg.distances) {
      const line = segment(COLORS.pinned);
      const lab = label('dist', 1);
      root.add(line, lab);
      built.distances.push({ def: d, line, lab });
    }
  }

  function setConfig(next) {
    cfg = {
      points: [...(next?.points || [])],
      circles: [...(next?.circles || [])],
      distances: [...(next?.distances || [])],
    };
    storage.set(CFG_KEY, cfg);
    // view.to may name points of the previous setup: keep only what still exists
    const known = new Set(refs().map((r) => r.ref));
    view.to = view.to.filter((ref) => known.has(ref));
    if (!known.has(view.from)) view.from = 'hats';
    rebuild();
    notify();
  }
  const getConfig = () => JSON.parse(JSON.stringify(cfg));

  const uniqueId = (base, list) => {
    const slug = (base || 'item').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 24) || 'item';
    let id = slug, n = 2;
    while (list.some((x) => x.id === id) || OBJECTS[id]) id = `${slug}-${n++}`;
    return id;
  };
  function add(kind, item) {
    const next = getConfig();
    if (kind === 'points') item = { ...item, id: uniqueId(item.label, next.points) };
    if (kind === 'circles') item = { ...item, id: uniqueId(item.label || `circle-${item.at}`, next.circles) };
    next[kind].push(item);
    setConfig(next);
  }
  function remove(kind, index) {
    const next = getConfig();
    const [gone] = next[kind].splice(index, 1);
    if (kind === 'points' && gone) {
      next.circles = next.circles.filter((c) => c.at !== gone.id);
      next.distances = next.distances.filter((d) => d.from !== gone.id && d.to !== gone.id);
    }
    setConfig(next);
  }

  // ── View ──
  function setView(patch) {
    view = { ...view, ...patch };
    storage.set(VIEW_KEY, view);
    if (!view.whileAdjusting) measuring = null;
    notify();
  }
  const getView = () => JSON.parse(JSON.stringify(view));

  /** An object is being adjusted (drag, slider, or moving on the rig). */
  function adjusting(key) {
    if (!view.whileAdjusting || !OBJECTS[key]) return;
    measuring = { key, until: clock + MEASURE_HOLD };
  }

  function syncQuick() {
    for (const [key, q] of quick) {
      const want = view.quick[key];
      if (!want?.on || want.radii.join() !== q.radii.join()) {
        root.remove(q.group, q.lab);
        dropLabel(q.lab);
        quick.delete(key);
      }
    }
    for (const [key, want] of Object.entries(view.quick)) {
      if (!want?.on || !want.radii?.length || quick.has(key) || !OBJECTS[key]) continue;
      const group = new THREE.Group();
      want.radii.forEach((r) => group.add(ring(r, 'full')));
      const lab = label('circ', 3);
      root.add(group, lab);
      quick.set(key, { group, lab, radii: [...want.radii] });
    }
  }

  // ── Every frame ──
  function placeCircleLabel(lab, center, radii, facing, text) {
    const r = Math.max(...radii);
    const a = facing + Math.PI / 2;
    lab.position.set(center.x + Math.sin(a) * r, center.y + 0.05, center.z + Math.cos(a) * r);
    setText(lab, text);
  }

  function update(dt) {
    clock += dt;
    const ax = getAxes();
    const mouth = OBJECTS.hats.at(ax);
    const short = view.short;

    for (const b of built.points) {
      const p = position(b.def.id, ax);
      const visible = shown(itemKey.point(b.def)) && !!p;
      b.mesh.visible = b.lab.visible = visible;
      if (!visible) continue;
      b.mesh.position.copy(p);
      b.lab.position.set(p.x, p.y + (b.def.style === 'dut' ? 0.09 : 0.1), p.z);
    }

    for (const b of built.circles) {
      const center = position(b.def.at, ax);
      const visible = shown(itemKey.circle(b.def)) && !!center;
      b.group.visible = b.lab.visible = visible;
      if (!visible) continue;
      center.add(new THREE.Vector3(...(b.def.offset || [0, 0, 0])));
      if (b.def.level === 'mouth') center.y = mouth.y;
      b.group.position.copy(center);
      // a half circle opens towards the HATS mouth (falls back to -Z, the HATS side of the room)
      const dx = mouth.x - center.x, dz = mouth.z - center.z;
      const facing = Math.hypot(dx, dz) > 0.01 ? Math.atan2(dx, dz) : Math.PI;
      b.group.rotation.y = facing;
      const radii = b.def.radii.map((r) => `${r} m`).join(' / ');
      placeCircleLabel(b.lab, center, b.def.radii, facing,
        short || !b.def.label ? radii : `${radii} · ${b.def.label}`);
    }

    for (const b of built.distances) {
      const a = position(b.def.from, ax), c = position(b.def.to, ax);
      const visible = shown(itemKey.distance(b.def)) && !!a && !!c;
      b.line.visible = b.lab.visible = visible;
      if (!visible) continue;
      setSegment(b.line, a, c);
      setText(b.lab, short ? distanceText(a, c, true)
        : `${refLabel(b.def.from)} → ${refLabel(b.def.to)}: ${distanceText(a, c)}`);
      b.lab.position.copy(a).lerp(c, 0.5).add(new THREE.Vector3(0, 0.07, 0));
    }

    syncQuick();
    for (const [key, q] of quick) {
      const center = OBJECTS[key].at(ax);
      q.group.position.copy(center);
      placeCircleLabel(q.lab, center, q.radii, Math.PI,
        `${short ? '' : `${OBJECTS[key].label} · `}${q.radii.map((r) => `${r} m`).join(' / ')}`);
    }

    updateLines(ax, short);
  }

  /** Reference lines (always) and while-adjusting lines (for a moment) */
  function updateLines(ax, short) {
    const wanted = new Map();                    // "from>to" -> fade 0..1
    for (const to of view.to) if (to !== view.from) wanted.set(`${view.from}>${to}`, 1);
    if (measuring && clock < measuring.until) {
      const fade = Math.min(1, (measuring.until - clock) / 0.5);
      const targets = view.to.length ? view.to : [view.from];
      for (const to of targets) {
        const p = cfg.points.find((x) => x.id === to);
        if (to === measuring.key || p?.on === measuring.key) continue;   // riding on the moved object
        const key = `${measuring.key}>${to}`, reverse = `${to}>${measuring.key}`;
        if (!wanted.has(key) && !wanted.has(reverse)) wanted.set(key, fade);
      }
    } else {
      measuring = null;
    }

    for (const [key, m] of lines) {
      if (!wanted.has(key)) { root.remove(m.line, m.lab); dropLabel(m.lab); lines.delete(key); }
    }
    for (const [key, fade] of wanted) {
      const [fromRef, toRef] = key.split('>');
      const a = position(fromRef, ax), b = position(toRef, ax);
      if (!a || !b) continue;
      let m = lines.get(key);
      if (!m) { m = { line: segment(COLORS.measure), lab: label('measure', 2) }; root.add(m.line, m.lab); lines.set(key, m); }
      setSegment(m.line, a, b);
      m.line.material.opacity = 0.9 * fade;
      m.lab.userData.ann.el.style.opacity = fade;
      setText(m.lab, short ? distanceText(a, b, true) : `${refLabel(toRef)} ${distanceText(a, b)}`);
      m.lab.position.copy(a).lerp(b, 0.62).add(new THREE.Vector3(0, 0.05, 0));
    }
  }

  /**
   * After the label renderer has drawn: push overlapping labels apart (pinned distances keep their
   * place first, then points and measuring labels, circle labels give way). Slides, never jumps.
   */
  const overlap = (p, q) => p.left < q.right && p.right > q.left && p.top < q.bottom && p.bottom > q.top;
  const shift = (r, dy) => ({ left: r.left, right: r.right, top: r.top + dy, bottom: r.bottom + dy });
  function layout(dt = 1 / 60) {
    const k = 1 - Math.exp(-14 * dt);
    const items = [];
    root.traverse((o) => {
      if (o.isCSS2DObject && o.visible && o.element.style.display !== 'none') items.push(o);
    });
    items.sort((p, q) => p.userData.ann.prio - q.userData.ann.prio);
    const placed = [];
    for (const o of items) {
      const a = o.userData.ann;
      const base = o.element.getBoundingClientRect();
      if (!base.width) continue;
      let target = 0;
      for (let i = 0; i < 12; i++) {
        const hit = placed.find((p) => overlap(shift(base, target), p));
        if (!hit) break;
        target = hit.bottom + 3 - base.top;
      }
      a.auto += (target - a.auto) * k;
      if (Math.abs(target - a.auto) < 0.5) a.auto = target;
      a.el.style.transform = `translateY(${a.auto}px)`;
      placed.push(shift(base, target));
    }
  }

  setConfig(storage.get(CFG_KEY, emptyOverlays()));   // a reload keeps what was being edited

  return {
    setConfig, getConfig, add, remove, refs, refLabel, update, layout, adjusting,
    setView, getView, onChange: (fn) => listeners.add(fn),
  };
}
