/**
 * 3D test room — entry point. Ported from ACQUA Automation's soundproofroom; the scene files
 * (spec.js, look.js, config.js, builders.js, interaction.js) are the same, this file, live.js,
 * setups.js and ui.js are adapted to the control center's API.
 *
 * Two modes, one scene
 *   Setup mode  offline drawing. Sliders and dragging edit the 3D view; save it as a setup.
 *               Nothing on the rig moves.
 *   Live mode   the 3D view shows the rig as reported by the control center. A slider proposes
 *               a target and asks before moving; dragging objects is off.
 *
 * Overlays (overlays.js): reference circles, marker points and distances drawn on top of the
 * scene; they are saved with each setup. See the Overlays group in the side panel.
 *
 * ⚠ Every control id below is shared with mcc/room.json (which Pi / firmware axis it is) and
 *   with every saved setup. Renaming an id breaks saved setups and live mode.
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';

import { CONFIG as C } from './config.js';
import {
  buildRoom, buildSpeakerRail, buildSpeakerArray, setArrayLift,
  buildFloorRails, buildHats, setHatsAngle, setHatsHeight, setHeadAngle, setHatsCross,
  buildTable, setTableHeight, buildMicRig, setMicHeight, setMicCross,
  buildScreen, setScreenLift, buildLights,
} from './builders.js';
import { createInteraction } from './interaction.js';
import { createLive } from './live.js';
import { fmt, esc, toast, confirmBox } from './ui.js';
import { listSetups, saveCurrent, saveFromLive, removeSetup, applyToScene } from './setups.js';
import { createOverlays, OBJECTS, POINT_STYLES, emptyOverlays } from './overlays.js';
import { createViewPanel } from './viewpanel.js';

const $ = (s) => document.querySelector(s);
const storage = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

// ── Renderer, camera, orbit ─────────────────────
const container = $('#viewport');
const scene = new THREE.Scene();
scene.background = new THREE.Color(C.background);

const camera = new THREE.PerspectiveCamera(
  C.camera.fov, container.clientWidth / container.clientHeight, C.camera.near, C.camera.far);
camera.position.set(...C.camera.position);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(container.clientWidth, container.clientHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
container.appendChild(renderer.domElement);

// HTML labels in 3D (overlays): drawn over the canvas, never catching the mouse
const labelRenderer = new CSS2DRenderer();
labelRenderer.setSize(container.clientWidth, container.clientHeight);
Object.assign(labelRenderer.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
container.appendChild(labelRenderer.domElement);

const controls3d = new OrbitControls(camera, renderer.domElement);
controls3d.enableDamping = true;
controls3d.dampingFactor = 0.08;
controls3d.target.set(...C.camera.target);
controls3d.maxPolarAngle = Math.PI * 0.495;
controls3d.minDistance = C.camera.minDistance;
controls3d.maxDistance = C.camera.maxDistance;

// ── Scene ───────────────────────────────────────
scene.add(buildLights(), buildRoom(), buildSpeakerRail(), buildFloorRails());

const speakers = buildSpeakerArray();
scene.add(speakers);

const hats = buildHats();
scene.add(hats);

const screen = buildScreen();
scene.add(screen);

const tables = C.tables.items.map((item) => {
  const g = buildTable(item);
  scene.add(g);
  return { item, group: g };
});

const micRigs = C.micRig.items.map((item) => {
  const g = buildMicRig(item);
  scene.add(g);
  return { item, group: g };
});

const grid = new THREE.GridHelper(
  Math.max(C.room.width, C.room.depth), C.grid.divisions, C.grid.color1, C.grid.color2);
grid.position.y = 0.002;
grid.material.transparent = true;
grid.material.opacity = C.grid.opacity;
scene.add(grid);

/**
 * HATS height with the model corrected. builders.js assumes "holder -> MRP = 22.5 cm", but the
 * modelled torso + neck + head put the red MRP dot 0.617 m above the holder, so hats.mrp = 1.20
 * drew the mouth at 1.59 m. Lay it out the original way, measure where the MRP dot really is,
 * and shift the body so the dot sits exactly at the axis value (found by the Teams V5 preview).
 * Distances in the overlays are computed from the axis value, so the model has to agree with it.
 */
const _mrp = new THREE.Vector3();
function setHatsMrp(group, h) {
  setHatsHeight(group, h);
  group.updateMatrixWorld(true);
  const body = group.getObjectByName('hatsBody');
  const column = group.getObjectByName('standColumn');
  const mrp = group.getObjectByName('mrp');
  if (!body || !column || !mrp) return;
  const actual = mrp.getWorldPosition(_mrp).y;
  body.position.y = Math.max(0.05, body.position.y + (h - actual));
  column.scale.y = body.position.y;
  column.position.y = body.position.y / 2;
}

const tableGroup = (key) => tables.find((t) => t.item.key === key).group;
const micGroup = (key) => micRigs.find((m) => m.item.key === key).group;

// ── Dragging: every draggable object ────────────
const dragTargets = [
  { key: 'speakers', group: speakers, planeY: C.speakerStand.lift.default,
    axes: { x: { ...C.speakerRail.travel } } },

  // HATS: Z runs on the lower rail (whole assembly), X on the upper cross rail (child only)
  { key: 'hats', group: hats, planeY: C.floorRailX.y,
    axes: { x: { ...C.floorRailX.travel }, z: { ...C.floorRailZ.travel } },
    apply: { x: setHatsCross } },

  { key: 'screen', group: screen, planeY: 0.4,
    axes: { z: { ...C.screen.travelZ } } },

  ...tables.map(({ item, group }) => ({
    key: item.key, group, planeY: 0.4,
    axes: { z: { ...item.travelZ } },
  })),

  ...micRigs.map(({ item, group }) => ({
    key: item.key, group, planeY: C.micRig.bridge.y,
    axes: { x: { ...item.travelX }, z: { ...item.travelZ } },
    apply: { x: setMicCross },
  })),
];

const interaction = createInteraction({
  renderer, camera, controls: controls3d, targets: dragTargets,
  onChange: (key, axis, value, source) => {
    const c = controls[`${key}.${axis}`];
    if (!c) return;
    drawn[c.id] = value;
    if (source !== 'slider') overlays.adjusting(key);
    // In live mode the readout is the reported position, written by live.js (with its
    // estimated mark). Writing the eased in-between value here would overwrite it.
    if (mode === 'live') return;
    if (source !== 'slider') c.el.value = value;
    c.out.textContent = c.format(value);
  },
});

// ══════════════════════════════════════════════════
//  Controls
// ══════════════════════════════════════════════════
//   drag  → this axis can also be dragged in 3D (goes through interaction)
//   apply → slider only, calls the builder's setter
const PANEL = [
  {
    title: 'Speaker array (2 × 4, floor standing)',
    items: [
      { id: 'speakers.x', label: 'Floor rail position X', unit: 'm',
        range: C.speakerRail.travel, format: fmt.pos,
        drag: { key: 'speakers', axis: 'x' },
        hint: 'Rail 2.00 m · stands on the floor, not hung from the ceiling' },
      { id: 'speakers.lift', label: 'Array centre height', unit: 'm',
        range: C.speakerStand.lift,
        apply: (v) => setArrayLift(speakers, v),
        hint: `Column ${C.speakerStand.lift.min} – ${C.speakerStand.lift.max} m` },
    ],
  },
  {
    title: 'Display (opposite the speakers)',
    items: [
      { id: 'screen.z', label: 'Position Z', unit: 'm',
        range: C.screen.travelZ, format: fmt.pos,
        drag: { key: 'screen', axis: 'z' } },
      { id: 'screen.lift', label: 'Display centre height', unit: 'm',
        range: C.screen.lift, apply: (v) => setScreenLift(screen, v),
        hint: `Panel ${C.screen.width} × ${C.screen.height} m (estimated)` },
    ],
  },
  {
    title: 'HATS (B&K 4128)',
    items: [
      { id: 'hats.z', label: 'Position Z (lower rail)', unit: 'm',
        range: C.floorRailZ.travel, format: fmt.pos,
        drag: { key: 'hats', axis: 'z' }, hint: 'Rail 4.61 m' },
      { id: 'hats.x', label: 'Position X (upper rail)', unit: 'm',
        range: C.floorRailX.travel, format: fmt.pos,
        drag: { key: 'hats', axis: 'x' }, hint: 'Cross rail 1.60 m (estimated)' },
      { id: 'hats.mrp', label: 'MRP height', unit: 'm',
        range: { ...C.hats.stand, default: C.hats.stand.test },
        apply: (v) => setHatsMrp(hats, v),
        hint: 'HMS II.3 stand · nominal 1.20 m · 0.75 – 1.50 m' },
      { id: 'hats.rot', label: 'Turntable angle', unit: 'deg', step: 1,
        range: { min: -180, max: 180, default: C.hats.turntable.defaultAngleDeg },
        apply: (v) => setHatsAngle(hats, v) },
      { id: 'hats.head', label: 'Head tilt', unit: 'deg', step: 0.5,
        range: C.hats.headAngle,
        apply: (v) => setHeadAngle(hats, v),
        hint: 'Specified as "vertical or 17°"; continuous here' },
      { type: 'preset', target: 'hats.head', values: C.hats.headAngle.presets,
        format: (v) => `${v}°` },
    ],
  },
  {
    title: 'Tables (height + Z)',
    items: tables.flatMap(({ item }) => [
      { id: `${item.key}.z`, label: `${item.label} — Z`, unit: 'm',
        range: item.travelZ, format: fmt.pos,
        drag: { key: item.key, axis: 'z' } },
      { id: `${item.key}.lift`, label: `${item.label} — height`, unit: 'm',
        range: { ...item.lift, default: item.lift.test },
        apply: (v) => setTableHeight(tableGroup(item.key), v),
        hint: `Test ${item.lift.test} m · max ${item.lift.max} m` },
    ]),
  },
  {
    title: 'Ceiling mic booms × 2',
    items: micRigs.flatMap(({ item }) => [
      { id: `${item.key}.z`, label: `${item.label} — Z`, unit: 'm',
        range: item.travelZ, format: fmt.pos,
        drag: { key: item.key, axis: 'z' } },
      { id: `${item.key}.x`, label: `${item.label} — X`, unit: 'm',
        range: item.travelX, format: fmt.pos,
        drag: { key: item.key, axis: 'x' } },
      { id: `${item.key}.h`, label: `${item.label} — height Y`, unit: 'm',
        range: { ...C.micRig.height, default: item.heightDefault },
        apply: (v) => setMicHeight(micGroup(item.key), v),
        hint: 'GRAS 40AC / 40AF · 0.70 – 1.50 m' },
      { type: 'preset', target: `${item.key}.h`, values: C.micRig.height.presets,
        format: (v) => `${v} m` },
    ]),
  },
];

// ══════════════════════════════════════════════════
//  Modes
// ══════════════════════════════════════════════════
// Setup mode by default: talking to the rig, and jumping the view to it, the moment the page
// opens surprises people. ?mode=live opens live mode; the last choice is remembered.
const MODES = {
  live: {
    hint: '<b>The 3D view shows the rig</b> · slider = propose a target, you are asked before anything moves'
        + '<br><b>Dragging objects is off</b> · drag: orbit · wheel: zoom · right-drag: pan',
  },
  setup: {
    hint: '<b>Drag an object</b>: speaker array · display · HATS · tables · mic booms'
        + '<br><b>Drag empty space</b>: orbit · <b>wheel</b>: zoom · <b>right-drag</b>: pan'
        + '<br>This mode <b>never moves the rig</b>',
  },
};

const params = new URLSearchParams(location.search);
let mode = params.get('mode') === 'live' ? 'live'
  : params.get('mode') === 'setup' ? 'setup'
    : (storage.get('mccRoomMode') === 'live' ? 'live' : 'setup');

/** id -> control. live.js and setups.js both use this registry. */
const controls = {};

/** id -> value the 3D view draws right now (overlays measure from these) */
const drawn = {};

const overlays = createOverlays({ scene, getAxes: () => drawn });

/** Live mode controller; created after buildPanel() (it needs #livestatus). */
let live = null;

// ══════════════════════════════════════════════════
//  Side panel
// ══════════════════════════════════════════════════
function buildPanel() {
  const panel = $('#panel');
  panel.innerHTML = `
    <h1>Test room</h1>
    <div class="sub">Room ${C.room.width} × ${C.room.depth} × ${C.room.height} m</div>
    <div class="modesw" id="modesw">
      <button data-mode="live">Live</button>
      <button data-mode="setup">Setup</button>
    </div>
    <div id="livestatus"></div>
    <div id="modebar"></div>
    <div class="group ovl" id="overlays"></div>`;

  for (const grp of PANEL) {
    const box = document.createElement('div');
    box.className = 'group';
    box.innerHTML = `<h2>${grp.title}</h2>`;

    for (const it of grp.items) {
      if (it.type === 'preset') {
        const row = document.createElement('div');
        row.className = 'views presets';
        for (const v of it.values) {
          const b = document.createElement('button');
          b.textContent = it.format ? it.format(v) : v;
          // In live mode a preset is "move the slider here": it goes through the same confirmation
          b.onclick = () => {
            const c = controls[it.target];
            if (!c) return;
            if (mode === 'live') { c.el.value = v; c.el.dispatchEvent(new Event('change')); }
            else c.set(v);
          };
          row.appendChild(b);
        }
        box.appendChild(row);
        continue;
      }
      box.appendChild(makeControl(it));
    }
    panel.appendChild(box);
  }

  panel.appendChild(makeViewBox());
}

/** One control: readout + slider + (live mode) target hint */
function makeControl(it) {
  const wrap = document.createElement('div');
  wrap.className = 'ctl';
  wrap.dataset.id = it.id;
  wrap.innerHTML = `
    <div class="row"><span class="name">${it.label}</span><span class="val">—</span></div>
    <input type="range">
    <div class="tgt" hidden></div>
    ${it.hint ? `<div class="lim">${it.hint}</div>` : ''}`;

  const el = wrap.querySelector('input');
  const out = wrap.querySelector('.val');
  const tgt = wrap.querySelector('.tgt');
  const format = it.format || fmt.by(it.unit);
  const r = it.range;

  el.min = r.min; el.max = r.max; el.step = it.step ?? 0.01;
  el.value = r.default ?? r.min;

  /** Apply to the 3D view only (live mode is driven by reported positions) */
  const apply = (v) => {
    drawn[it.id] = v;
    if (it.drag) interaction.set(it.drag.key, it.drag.axis, v);
    else it.apply(v);
  };

  /** Slider + readout + 3D together: setup mode and loading a setup */
  const set = (v) => {
    el.value = v;
    out.textContent = format(parseFloat(el.value));
    apply(parseFloat(el.value));
  };

  el.addEventListener('input', () => {
    const v = parseFloat(el.value);
    if (mode === 'setup') {
      out.textContent = format(v);
      apply(v);
      overlays.adjusting(it.id.split('.')[0]);
      return;
    }
    // Live mode: the slider only picks a target; the 3D view does not move
    tgt.hidden = false;
    tgt.innerHTML = `Target <b>${format(v)}</b> — you are asked when you let go`;
  });

  // Ask on "change" (let go), not on every "input" step
  el.addEventListener('change', async () => {
    if (mode !== 'live') return;
    await live.requestMove(it.id, parseFloat(el.value));
    tgt.hidden = true;
  });

  controls[it.id] = {
    id: it.id, label: it.label, unit: it.unit, range: r, format,
    el, out, wrap, tgt, apply, set,
    get value() { return parseFloat(el.value); },
  };
  set(parseFloat(el.value));
  return wrap;
}

function makeViewBox() {
  const v = document.createElement('div');
  v.className = 'group';
  v.innerHTML = `<h2>View</h2>
    <div class="views">
      <button data-view="iso">Iso</button>
      <button data-view="top">Top</button>
      <button data-view="front">Front</button>
      <button data-view="side">Side</button>
    </div>`;

  const VIEWS = {
    iso: { pos: C.camera.position, target: C.camera.target },
    top: { pos: [0, 9.5, 0.01], target: [0, 0, 0] },
    front: { pos: [0, 1.3, 8.4], target: [0, 1.1, 0] },
    side: { pos: [8.4, 1.5, 0], target: [0, 1.1, 0] },
  };
  v.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => {
    const p = VIEWS[b.dataset.view];
    camera.position.set(...p.pos);
    controls3d.target.set(...p.target);
  }));
  return v;
}

// ══════════════════════════════════════════════════
//  Mode bar: setups
// ══════════════════════════════════════════════════
let setupCache = [];
let editing = null;         // the saved setup being edited in setup mode
let example = null;         // the read-only example loaded last (shown so it is clear why Save makes a copy)

async function refreshSetups() {
  try { setupCache = await listSetups(); } catch { setupCache = []; }
  renderModeBar();
}

/** The design defaults of spec.js, in the list as a built-in entry so they are always reachable. */
const DEFAULT_ID = '__default__';
const shortName = (n) => (n.length > 42 ? n.slice(0, 41) + '…' : n);

function setupOptions(selected, { withDefault = true } = {}) {
  const opts = ['<option value="">Choose a setup…</option>'];
  if (withDefault) {
    opts.push('<optgroup label="Built in">',
      `<option value="${DEFAULT_ID}"${selected === DEFAULT_ID ? ' selected' : ''}>Design positions (spec.js)</option>`,
      '</optgroup>');
  }
  const groups = new Map();
  for (const s of setupCache) {
    const g = s.readonly ? `Examples · ${s.group || 'library'}` : 'Saved setups';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(s);
  }
  for (const [g, list] of groups) {
    opts.push(`<optgroup label="${esc(g)}">`);
    for (const s of list) {
      opts.push(`<option value="${esc(s.id)}"${s.id === selected ? ' selected' : ''}>`
        + `${esc(shortName(s.name))}${s.source === 'live' ? ' · from rig' : ''}</option>`);
    }
    opts.push('</optgroup>');
  }
  return opts.join('');
}

function defaultSetup() {
  return {
    id: DEFAULT_ID, name: 'Design positions',
    axes: Object.fromEntries(Object.values(controls).map((c) => [c.id, c.range.default ?? c.range.min])),
  };
}

const pickSetup = (id) => (id === DEFAULT_ID ? defaultSetup() : setupCache.find((x) => x.id === id));

function renderModeBar() {
  const bar = $('#modebar');
  bar.className = 'group modebar';
  if (mode === 'setup') {
    bar.innerHTML = `<h2>Setups</h2>
      ${editing ? `<div class="editing">Editing <b>${esc(editing.name)}</b></div>` : ''}
      ${!editing && example ? `<div class="editing">Example <b>${esc(example.name)}</b> (read only):
        save a copy to keep changes</div>` : ''}
      <button class="wide primary" id="mb-save">${editing ? 'Update this setup' : 'Save current setup…'}</button>
      ${editing ? '<button class="wide" id="mb-saveas">Save as a new setup…</button>' : ''}
      <label class="lbl">Load positions into the view</label>
      <select id="mb-pick">${setupOptions(editing?.id || example?.id)}</select>
      <div class="views">
        <button id="mb-load">Load</button>
        ${editing ? '<button id="mb-delete">Delete</button>' : ''}
      </div>
      <div class="lim">This mode only changes the view. To move the rig, switch to Live and apply a setup.</div>`;

    $('#mb-save').onclick = async () => {
      const s = await saveCurrent(snapshotAxes(), { existing: editing, overlays: overlays.getConfig() });
      if (s) { editing = s; await refreshSetups(); }
    };
    const saveas = $('#mb-saveas');
    if (saveas) saveas.onclick = async () => {
      const s = await saveCurrent(snapshotAxes(), { overlays: overlays.getConfig() });
      if (s) { editing = s; await refreshSetups(); }
    };
    const del = $('#mb-delete');
    if (del) del.onclick = async () => {
      if (await removeSetup(editing)) { editing = null; await refreshSetups(); }
    };
    $('#mb-load').onclick = () => {
      const id = $('#mb-pick').value;
      if (!id) { toast('Choose a setup first', 'warn'); return; }
      const s = pickSetup(id);
      if (!s) { toast('Setup not found', 'warn'); return; }
      const r = applyToScene(s, controls);
      overlays.setConfig(s.overlays || emptyOverlays());
      // Not a setup that can be overwritten (design defaults, examples): Save creates a new one
      editing = id === DEFAULT_ID || s.readonly ? null : s;
      example = s.readonly ? s : null;
      renderModeBar();
      toast(`Loaded "${s.name}" (${r.applied.length} axes`
        + (r.absent.length ? `, ${r.absent.length} not in it` : '') + ')');
    };
    return;
  }

  // Live mode: apply a saved setup to the rig, or save where the rig is now.
  bar.innerHTML = `<h2>Apply a setup to the rig</h2>
    <select id="mb-apply-pick">${setupOptions(null)}</select>
    <button class="wide primary" id="mb-apply">Apply to the rig…</button>
    <button class="wide" id="mb-savelive">Save the rig position as a setup…</button>
    <div class="lim">Axes move one at a time, in the order of the axis table; you see the list first.</div>`;
  $('#mb-apply').onclick = () => {
    const id = $('#mb-apply-pick').value;
    if (!id) { toast('Choose a setup first', 'warn'); return; }
    const s = pickSetup(id);
    if (!s) { toast('Setup not found', 'warn'); return; }
    overlays.setConfig(s.overlays || emptyOverlays());
    live.applySetup(s.axes || {}, s.name);
  };
  $('#mb-savelive').onclick = async () => {
    const s = await saveFromLive(live.positions, overlays.getConfig());
    if (s) await refreshSetups();
  };
}

function paintModeButtons() {
  $('#modesw')?.querySelectorAll('[data-mode]').forEach((b) => {
    b.classList.toggle('on', b.dataset.mode === mode);
  });
}

/** Every axis's value in the view, for saving */
function snapshotAxes() {
  return Object.fromEntries(Object.values(controls).map((c) => [c.id, Math.round(c.value * 10000) / 10000]));
}

// ══════════════════════════════════════════════════
//  Overlays panel: circles, distances, marker points
// ══════════════════════════════════════════════════
const OBJECT_OPTIONS = () => Object.entries(OBJECTS)
  .map(([k, o]) => `<option value="${k}">${esc(o.label)}</option>`).join('');
const REF_OPTIONS = () => overlays.refs()
  .map((r) => `<option value="${esc(r.ref)}">${esc(r.label)}</option>`).join('');
const num = (sel) => parseFloat($(sel).value);

function renderOverlayPanel() {
  const host = $('#overlays');
  if (!host) return;
  const cfg = overlays.getConfig();
  const row = (kind, i, text) => `<div class="ovrow"><span>${text}</span>
    <button class="x" data-del="${kind}:${i}" title="Remove">×</button></div>`;
  const opened = new Set([...host.querySelectorAll('details[open]')].map((d) => d.dataset.k));

  host.innerHTML = `<h2>Setup overlays</h2>
    <div class="lim">Circles, points and pinned distances that belong to this setup: saved with it,
      nothing on the rig moves. Choose what to see in the <b>Show</b> panel at the top left of the view.</div>

    <div class="ovlist">
      <div class="ovh">Circles</div>
      ${cfg.circles.map((c, i) => row('circles', i, `${esc(overlays.refLabel(c.at))} · ${c.radii.join(' / ')} m${
        c.span === 'half' ? ' · half' : ''}${c.level === 'mouth' ? ' · mouth height' : ''}${c.label ? ` · ${esc(c.label)}` : ''}`)).join('')
        || '<div class="ovnone">None</div>'}
      <div class="ovh">Distances</div>
      ${cfg.distances.map((d, i) => row('distances', i,
        `${esc(overlays.refLabel(d.from))} → ${esc(overlays.refLabel(d.to))}`)).join('') || '<div class="ovnone">None</div>'}
      <div class="ovh">Points</div>
      ${cfg.points.map((p, i) => row('points', i, `${esc(p.label || p.id)} · on ${esc(OBJECTS[p.on]?.label || p.on)}
        (${p.offset.map((v) => v.toFixed(2)).join(', ')})`)).join('') || '<div class="ovnone">None</div>'}
    </div>

    <details data-k="circle"><summary>Add a circle</summary>
      <label class="lbl">Centre</label><select id="oc-at">${REF_OPTIONS()}</select>
      <label class="lbl">Radii (m, comma separated)</label><input id="oc-radii" value="1, 1.5">
      <div class="ov2">
        <select id="oc-span"><option value="full">Full circle</option><option value="half">Half, towards HATS</option></select>
        <select id="oc-level"><option value="anchor">At the centre's height</option><option value="mouth">At HATS mouth height</option></select>
      </div>
      <label class="lbl">Label (optional)</label><input id="oc-label" placeholder="e.g. Talker circles">
      <button class="wide" id="oc-add">Add circle</button>
    </details>
    <details data-k="distance"><summary>Pin a distance</summary>
      <div class="ov2"><select id="od-from">${REF_OPTIONS()}</select><select id="od-to">${REF_OPTIONS()}</select></div>
      <button class="wide" id="od-add">Pin distance</button>
    </details>
    <details data-k="point"><summary>Add a point</summary>
      <label class="lbl">Label</label><input id="op-label" placeholder="e.g. DUT">
      <div class="ov2">
        <select id="op-on">${OBJECT_OPTIONS()}</select>
        <select id="op-style">${Object.entries(POINT_STYLES).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
      </div>
      <label class="lbl">Offset from the object X / Y / Z (m)</label>
      <div class="ov3"><input id="op-x" type="number" step="0.01" value="0"><input id="op-y" type="number" step="0.01" value="0">
        <input id="op-z" type="number" step="0.01" value="0"></div>
      <button class="wide" id="op-add">Add point</button>
    </details>
    <button class="wide" id="ov-clear"${cfg.points.length + cfg.circles.length + cfg.distances.length ? '' : ' disabled'}>Clear all overlays</button>`;

  host.querySelectorAll('details').forEach((d) => { if (opened.has(d.dataset.k)) d.open = true; });
  host.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = () => { const [kind, i] = b.dataset.del.split(':'); overlays.remove(kind, +i); };
  });
  $('#od-to').selectedIndex = Math.min(1, $('#od-to').options.length - 1);

  $('#oc-add').onclick = () => {
    const radii = $('#oc-radii').value.split(/[,\s]+/).map(parseFloat).filter((r) => r > 0 && r <= 10);
    if (!radii.length) { toast('Give at least one radius between 0 and 10 m', 'warn'); return; }
    overlays.add('circles', { at: $('#oc-at').value, offset: [0, 0, 0], radii: radii.slice(0, 6),
      span: $('#oc-span').value, level: $('#oc-level').value, label: $('#oc-label').value.trim().slice(0, 80) });
  };
  $('#od-add').onclick = () => {
    const from = $('#od-from').value, to = $('#od-to').value;
    if (from === to) { toast('Pick two different objects', 'warn'); return; }
    overlays.add('distances', { from, to });
  };
  $('#op-add').onclick = () => {
    const text = $('#op-label').value.trim();
    if (!text) { toast('Give the point a label', 'warn'); return; }
    const offset = [num('#op-x'), num('#op-y'), num('#op-z')]
      .map((v) => (Number.isFinite(v) ? Math.max(-10, Math.min(10, v)) : 0));
    overlays.add('points', { label: text.slice(0, 80), on: $('#op-on').value, offset, style: $('#op-style').value });
  };
  $('#ov-clear').onclick = async () => {
    const yes = await confirmBox({
      title: 'Clear all overlays?',
      html: '<div class="bxnote">Removes every circle, distance and point from the view. '
        + 'Saved setups keep theirs until you save over them.</div>',
      ok: 'Clear', danger: true,
    });
    if (yes) overlays.setConfig(emptyOverlays());
  };
}

// ══════════════════════════════════════════════════
//  Switching modes
// ══════════════════════════════════════════════════
// Switching always asks: the same slider means "draw" in one mode and "move the rig" in the
// other. Switching to live reads the rig first so the dialog can say what it will show.
async function askSwitch(next) {
  if (next === 'setup') {
    return confirmBox({
      title: 'Switch to Setup mode?',
      html: `
        <div class="mdsw setup">
          <div class="r"><i>✏️</i><span>Sliders and dragging <b>edit the 3D view</b></span></div>
          <div class="r"><i>🔒</i><span>This mode <b>never moves the rig</b></span></div>
          <div class="r"><i>💾</i><span>Save the result as a setup</span></div>
        </div>
        <div class="bxnote">The sliders start from <b>the rig's last reported positions</b>.</div>`,
      ok: 'Switch to Setup', cancel: 'Stay in Live',
    });
  }

  const p = await live.probe();
  const status = p.state === 'live'
    ? `<div class="mdst ok"><b>Rig connected</b>
         <div>${p.total} axes${p.unknown ? ` · ${p.unknown} not homed yet (shown with "?")` : ''}${
           p.offline ? ` · ${p.offline} of ${p.devices} devices not online` : ''}</div></div>`
    : `<div class="mdst bad"><b>Cannot read positions</b><div>${esc(p.error || '')}</div></div>`;

  return confirmBox({
    title: 'Switch to Live mode?',
    html: `
      ${status}
      <div class="mdsw live">
        <div class="r"><i>📡</i><span>The 3D view shows <b>the rig as reported</b>, not your drawing</span></div>
        <div class="r"><i>🎚️</i><span>A slider <b>proposes a target</b>; you are asked again before it moves</span></div>
        <div class="r"><i>🚫</i><span><b>Dragging objects is off</b> (the camera still works)</span></div>
      </div>
      ${p.busy ? '<div class="bxnote warn">The rig <b>is moving right now</b>.</div>' : ''}
      ${p.state === 'error' ? '<div class="bxnote warn">You can still switch; the view updates once positions can be read.</div>' : ''}`,
    ok: 'Switch to Live', cancel: 'Cancel',
  });
}

async function setMode(next, { remember = true, ask = false } = {}) {
  if (!MODES[next]) return;
  if (next === mode && ask) return;
  // Not while the rig moves: the sliders would turn back into "edit the view" mid-move
  if (mode === 'live' && next !== 'live' && live.busy) {
    toast('The rig is still moving: switch when it is done', 'warn');
    return;
  }
  if (ask && !(await askSwitch(next))) {
    paintModeButtons();
    return;
  }
  mode = next;
  if (remember) storage.set('mccRoomMode', mode);

  document.body.dataset.mode = mode;
  paintModeButtons();
  const hint = $('#hint');
  if (hint) hint.innerHTML = MODES[mode].hint;

  interaction.setEnabled(mode === 'setup');

  if (mode === 'live') {
    await live.enable();
  } else {
    live.disable();
    // Back from live: start from where the rig is (usually the next step is "adjust and save")
    for (const [id, v] of Object.entries(live.positions)) controls[id]?.set(v);
    Object.values(controls).forEach((c) => { c.el.disabled = false; c.tgt.hidden = true; });
  }
  renderModeBar();
}

// ══════════════════════════════════════════════════
//  Start
// ══════════════════════════════════════════════════
buildPanel();

live = createLive({
  controls,
  applyToScene: (id, v) => {
    controls[id]?.apply(v);
    overlays.adjusting(id.split('.')[0]);       // the rig moves this object: measure from it
  },
  statusHost: $('#livestatus'),
});

renderOverlayPanel();
overlays.onChange(renderOverlayPanel);
createViewPanel($('#viewpanel'), overlays);

$('#modesw').querySelectorAll('[data-mode]').forEach((b) => {
  b.onclick = () => setMode(b.dataset.mode, { ask: true });
});

await refreshSetups();

// ?setup=<id>: open a saved setup in setup mode
const wantSetup = params.get('setup');
if (wantSetup) {
  const s = setupCache.find((x) => x.id === wantSetup);
  if (s) {
    mode = 'setup';
    applyToScene(s, controls);
    editing = s;
    toast(`Loaded "${s.name}"`);
  } else {
    toast('Setup not found', 'bad');
  }
}

await setMode(mode, { remember: false });

// ── Loop ────────────────────────────────────────
function onResize() {
  const w = container.clientWidth, h = container.clientHeight;
  if (!w || !h) return;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
}
window.addEventListener('resize', onResize);
new ResizeObserver(onResize).observe(container);

let last = performance.now();
renderer.setAnimationLoop(() => {
  const now = performance.now();
  const dt = Math.min(0.1, (now - last) / 1000);
  last = now;
  live.tick(dt);            // live mode: ease towards the reported positions
  overlays.update(dt);
  controls3d.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
  overlays.layout(dt);      // after the labels are placed: push overlapping ones apart
});
