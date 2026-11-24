/**
 * Live mode: the 3D view follows the rig, and a slider proposes a target instead of moving the view.
 *
 *   Setup mode  slider = edit the 3D view. It is a drawing; nothing moves.
 *   Live mode   slider = propose a target. The 3D view only shows what the control center
 *               reports and never moves because someone touched a slider.
 *
 * Letting go of a slider opens a confirmation (from where to where, how far, how long,
 * which firmware command). Nothing in the view moves before OK is pressed.
 *
 * Moves and setups run as jobs on the control center (POST /api/room/move | apply), one axis
 * at a time; an axis that has not been homed since its Arduino restarted is homed first.
 *
 * Smoothing: the server estimates the position of a moving axis, but the page polls only every
 * 350 ms, so each frame eases the drawn position towards the last report (see tick). Numbers in
 * the panel always show the reported value, never the eased one.
 */
import { confirmBox, fmt, esc, toast, jsonFetch, isOk, errorText } from './ui.js';

const GET = (u) => jsonFetch(u);
const POST = (u, body) => jsonFetch(u, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});

/** Device status -> [badge class, text], same words as the Devices page. */
const DEV_TEXT = {
  online: ['ok', 'Online'],
  busy: ['warn', 'Busy'],
  'no-arduino': ['warn', 'No Arduino'],
  checking: ['warn', 'Checking'],
  offline: ['bad', 'Offline'],
  missing: ['bad', 'Not in the device list'],
};

const ACTIVE = ['running', 'aborting'];
const STEP_TEXT = { homing: 'Homing first', moving: 'Moving' };

/** Easing rate (1/s): about 2/3 of the gap closes every 110 ms. */
const EASE = 9;
/** Snap to the target inside this distance (finer than the firmware's resolution). */
const SNAP = { m: 0.001, deg: 0.1 };

export function createLive({ controls, applyToScene, statusHost }) {
  let on = false;
  let timer = null;
  let snap = null;                 // last /api/room/live
  let manifest = null;             // /api/room/axes, to compare travel with spec.js
  // Poll generation: a manual poll() bumps it so the older chain stops after its request returns
  // (clearTimeout cannot cancel a request in flight; two chains would double the request rate).
  let gen = 0;
  const wanted = {};               // id -> position reported by the control center
  const shown = {};                // id -> where the 3D view draws it now (eased)
  let pendingId = null;            // axis whose confirmation dialog is open

  const busyJob = () => !!(snap?.job && ACTIVE.includes(snap.job.state));

  // ── Polling (setTimeout chain, never setInterval: slow answers must not pile up) ──
  async function poll() {
    if (!on) return;
    const mine = ++gen;
    try {
      const s = await GET('/api/room/live');
      if (!on || mine !== gen) return;
      if (!isOk(s)) throw new Error(errorText(s, 'Could not read positions'));
      snap = s;
      for (const [id, a] of Object.entries(s.axes || {})) {
        if (!controls[id]) continue;
        wanted[id] = a.value;
        if (shown[id] === undefined) shown[id] = a.value;   // first report: jump there
      }
      paint();
    } catch (e) {
      if (!on || mine !== gen) return;
      snap = { error: String(e.message || e) };
      paint();
    }
    if (!on || mine !== gen) return;
    clearTimeout(timer);
    timer = setTimeout(poll, busyJob() ? 350 : 1800);
  }

  /** Ease every drawn position towards its reported value. dt in seconds. */
  function tick(dt) {
    if (!on) return;
    const k = Math.min(1, dt * EASE);
    for (const id of Object.keys(wanted)) {
      const w = wanted[id];
      const s = shown[id] ?? w;
      const tol = SNAP[controls[id]?.unit] ?? SNAP.m;
      const next = Math.abs(w - s) < tol ? w : s + (w - s) * k;
      if (next !== s) {
        shown[id] = next;
        applyToScene(id, next);
      }
    }
  }

  // ── Panel ───────────────────────────────────────
  function paint() {
    if (!on) return;
    paintStatus();
    const busy = busyJob();
    const job = snap?.job;
    const movingAxis = busy ? job.steps[job.index]?.axis : null;

    for (const [id, c] of Object.entries(controls)) {
      const a = snap?.axes?.[id];
      if (!a) continue;
      // "~" marks a value that is estimated (moving) or not known (not homed yet).
      c.out.textContent = (a.known ? (a.estimated ? '~' : '') : '? ') + c.format(a.value);
      c.out.title = a.known ? (a.estimated ? 'Estimated while moving' : 'Last position sent')
        : 'Not known: not homed since the Arduino restarted (a setup homes it first)';
      c.out.classList.toggle('est', !!a.estimated);
      c.wrap.classList.toggle('moving', id === movingAxis);
      // not wired / not verified in room.json: never moves for real (simulation is fine)
      const locked = !a.ready && !snap.simulation;
      c.el.disabled = busy || pendingId !== null || locked;
      c.wrap.classList.toggle('locked', locked);
      c.wrap.title = locked ? 'Not set up for the real rig yet: fill in device / ch and set "verified": true in room.json' : '';
      // Keep the slider on the rig position, except while a hand is on it: the poll would
      // otherwise snatch it back mid-drag.
      const touching = document.activeElement === c.el || !c.tgt.hidden;
      if (id !== pendingId && !touching && (busy || Math.abs(+c.el.value - a.value) > 1e-4)) {
        c.el.value = a.value;
      }
    }
  }

  function paintStatus() {
    if (!statusHost) return;
    if (snap?.error) {
      statusHost.innerHTML = `<div class="lv bad"><b>Cannot read positions</b>
        <div class="sub">${esc(snap.error)}</div></div>`;
      return;
    }
    const parts = [`<div class="lv ok"><b>Live rig</b>
      <div class="sub">Positions from the control center: the last position sent to each axis</div></div>`];

    if (snap?.config?.error) {
      parts.push(`<div class="lv bad"><b>Axis table problem</b><div class="sub">${esc(snap.config.error)}</div></div>`);
    }

    const devs = Object.entries(snap?.devices || {});
    if (devs.length) {
      parts.push(`<div class="devs">${devs.map(([id, d]) => {
        const [cls, text] = DEV_TEXT[d.status] || ['warn', d.status];
        return `<span class="dev ${cls}" title="${esc(id)} — ${esc(text)}${
          d.note ? ' · ' + esc(d.note) : ''}">${esc(id.replace(/^pi-/, ''))}</span>`;
      }).join('')}</div>`);
      const bad = devs.filter(([, d]) => d.status !== 'online');
      if (snap.simulation) {
        parts.push(`<div class="lvnote">Simulation is on: nothing is sent to the Pis. Each setup waits
          for you to mark it done in the window that pops up.</div>`);
      } else if (bad.length) {
        parts.push(`<div class="lvnote">${bad.length} of ${devs.length} devices are not online:
          their axes cannot move. See the Devices page for why.</div>`);
      }
    }

    const allAxes = Object.values(snap?.axes || {});
    const notReady = allAxes.filter((a) => !a.ready).length;
    if (notReady && !snap.simulation) {
      parts.push(`<div class="lvnote">${notReady} of ${allAxes.length} axes are not verified on the real rig yet
        (greyed out): they never move for real. Fill in their wiring in <code>room.json</code>.</div>`);
    }

    const unknown = allAxes.filter((a) => !a.known && a.ready).length;
    if (unknown && !snap.simulation) {
      parts.push(`<div class="lvnote">${unknown} axes are not homed since their Arduino last started
        (shown with "?"). Moving them homes them first.</div>`);
    }

    const job = snap?.job;
    if (job && ACTIVE.includes(job.state)) {
      const step = job.steps[job.index] || job.steps[job.steps.length - 1];
      const pct = Math.round((job.index / Math.max(1, job.steps.length)) * 100);
      parts.push(`<div class="lv job">
        <b>${job.state === 'aborting' ? 'Finishing the current axis' : esc(job.name)}</b>
        <div class="sub">${job.simulated ? 'Simulated: waiting for “Mark as done”' : step ? `${STEP_TEXT[step.state] || 'Waiting'}: ${esc(step.label)}
          ${fmt.by(step.unit)(step.from)} → ${fmt.by(step.unit)(step.to)}` : ''}</div>
        <div class="pb"><i style="width:${pct}%"></i></div>
        <div class="sub">${job.steps.length > 1 ? `Axis ${job.index + 1} of ${job.steps.length} · ` : ''}
          about ${fmt.secs(job.eta)} in total</div>
        <button class="wide danger" id="lv-stop">Stop after this axis</button>
        <div class="sub">Stopping skips the axes not started yet. <b>The axis moving now finishes its
          move</b>: the firmware has no stop command.</div>
      </div>`);
    } else if (job && job.state === 'error') {
      parts.push(`<div class="lv bad"><b>Setup failed: ${esc(job.name)}</b>
        <div class="sub">${esc(job.error || '')}</div></div>`);
    } else if (job && job.state === 'aborted') {
      parts.push(`<div class="lv warn"><b>Stopped: ${esc(job.name)}</b>
        <div class="sub">${job.steps.filter((s) => s.state === 'skipped').length} axes were not moved</div></div>`);
    }

    if (manifest?.mismatch?.length) {
      parts.push(`<div class="lv bad"><b>Travel limits differ</b><div class="sub">
        ${manifest.mismatch.map(esc).join('<br>')}<br>
        The 3D view (<code>spec.js</code>) and the axis table (<code>room.json</code>) disagree.
        The axis table wins: it is what the rig is protected by.</div></div>`);
    }

    statusHost.innerHTML = parts.join('');
    const stop = statusHost.querySelector('#lv-stop');
    if (stop) stop.onclick = doStop;
  }

  // ── A slider was let go ─────────────────────────
  /** Ask before moving one axis. Nothing in the view moves; Cancel puts the slider back. */
  async function requestMove(id, target) {
    const c = controls[id];
    const cur = wanted[id];
    if (cur === undefined) { toast('No position read for this axis yet', 'bad'); return; }
    if (Math.abs(target - cur) < 1e-4) return;
    if (busyJob()) {
      toast('An axis is moving: wait for it to finish', 'warn');
      c.el.value = cur; paint(); return;
    }

    const ax = manifest?.byId?.[id];
    const known = snap?.axes?.[id]?.known;
    const dist = Math.abs(target - cur);
    const secs = ax ? dist / ax.speed : null;
    const f = c.format;

    pendingId = id;
    paint();
    const yes = await confirmBox({
      title: 'Move this axis?',
      html: `
        <div class="mv">
          <div class="mvname">${esc(c.label)}</div>
          <div class="mvrow">
            <span class="from">${known ? '' : '? '}${f(cur)}</span>
            <span class="arrow">→</span>
            <span class="to">${f(target)}</span>
          </div>
          <div class="mvmeta">
            Distance <b>${c.unit === 'deg' ? dist.toFixed(1) + '°' : (dist * 1000).toFixed(0) + ' mm'}</b>
            ${secs !== null ? ` · about <b>${fmt.secs(secs)}</b>` : ''}
            ${ax ? ` · device <b>${esc(ax.device)}</b>` : ''}
          </div>
          ${ax ? `<div class="mvcmd">Firmware command: <code>${esc(ax.ch)}M,${
            ax.unit === 'deg' ? Math.round(target) : Math.round((target - ax.min) * 1000)}</code></div>` : ''}
        </div>
        ${known ? '' : `<div class="bxnote">This axis is not homed since its Arduino restarted:
          it is homed first (<code>${esc(ax?.ch || '')}H</code>), then moved.</div>`}
        <div class="bxnote warn">The rig will really move, and <b>it cannot be stopped halfway</b>:
          the firmware has no stop command. Make sure nobody and no cable is in the way.</div>`,
      ok: 'Move',
      cancel: 'Do not move',
    });
    pendingId = null;

    if (!yes) { c.el.value = cur; paint(); return; }

    const r = await POST('/api/room/move', { axis: id, target, source: '3d' });
    if (!isOk(r)) {
      toast(errorText(r, 'The move was not accepted'), 'bad', 6000);
      c.el.value = cur;
    }
    poll();     // refresh now so the progress bar shows up immediately
  }

  async function doStop() {
    const yes = await confirmBox({
      title: 'Stop after this axis?',
      html: `<div class="bxnote warn">Only the axes <b>not started yet</b> are skipped.
        <b>The axis moving now finishes its move</b>: the firmware has no stop command.
        To stop it at once, cut the power.</div>`,
      ok: 'Stop after this axis', danger: true, cancel: 'Keep moving',
    });
    if (!yes) return;
    const r = await POST('/api/room/stop');
    toast(isOk(r) ? 'The remaining axes are skipped (the moving one finishes)' : errorText(r, 'Could not stop'),
      isOk(r) ? 'warn' : 'bad');
    poll();
  }

  /** Send a whole setup ({id: value}) to the rig, after a confirmation listing every axis. */
  async function applySetup(axes, label) {
    const rows = Object.entries(axes)
      .filter(([id]) => controls[id])
      .map(([id, v]) => ({ id, v, c: controls[id], cur: wanted[id], known: snap?.axes?.[id]?.known }))
      .filter((r) => !r.known || r.cur === undefined || Math.abs(r.v - r.cur) > 1e-3);

    if (!rows.length) { toast('Every axis is already in place'); return; }

    const total = rows.reduce((acc, r) => {
      const ax = manifest?.byId?.[r.id];
      return acc + (ax ? Math.abs(r.v - (r.cur ?? r.v)) / ax.speed : 0);
    }, 0);
    const unhomed = rows.filter((r) => !r.known).length;

    const yes = await confirmBox({
      title: `Apply "${label}" to the rig?`,
      html: `
        <div class="bxnote">Moves the ${rows.length} axes below <b>one at a time</b>, in the order of the
          axis table. About ${fmt.secs(total)}${unhomed ? `, plus homing ${unhomed} axes first` : ''}.</div>
        <div class="mvlist">${rows.map((r) => `<div>
          <span>${esc(r.c.label)}</span>
          <span>${r.known ? r.c.format(r.cur) : '?'} → <b>${r.c.format(r.v)}</b></span>
        </div>`).join('')}</div>
        <div class="bxnote warn">The rig will really move. Make sure nobody is in the room.</div>`,
      ok: `Move ${rows.length} axes`, cancel: 'Do not move',
    });
    if (!yes) return;

    const r = await POST('/api/room/apply',
      { name: label, axes: Object.fromEntries(rows.map((x) => [x.id, x.v])), source: '3d' });
    if (!isOk(r)) { toast(errorText(r, 'The setup was not accepted'), 'bad', 7000); return; }
    if (!r.job.steps.length) { toast('Every axis is already in place'); return; }
    poll();
  }

  /** Read the rig once without starting the poll (for the mode-switch dialog). */
  async function probe() {
    try {
      const s = await GET('/api/room/live');
      if (!isOk(s)) throw new Error(errorText(s));
      const axes = Object.entries(s.axes || {}).filter(([id]) => controls[id]);
      const devs = Object.values(s.devices || {});
      return {
        state: 'live',
        total: axes.length,
        unknown: axes.filter(([, a]) => !a.known).length,
        offline: s.simulation ? 0 : devs.filter((d) => d.status !== 'online').length,
        devices: devs.length,
        busy: !!(s.job && ACTIVE.includes(s.job.state)),
      };
    } catch (e) {
      return { state: 'error', error: String(e.message || e) };
    }
  }

  // ── On / off ────────────────────────────────────
  async function enable() {
    if (on) return;
    on = true;
    if (!manifest) await loadManifest();
    await poll();
  }

  function disable() {
    on = false;
    gen++;
    clearTimeout(timer);
    timer = null;
    pendingId = null;
    if (statusHost) statusHost.innerHTML = '';
    for (const c of Object.values(controls)) {
      c.out.classList.remove('est');
      c.out.title = '';
      c.wrap.classList.remove('moving');
      c.tgt.hidden = true;
      c.el.disabled = false;
    }
  }

  /** The axis table, compared with the travel the 3D view (spec.js) uses. */
  async function loadManifest() {
    try {
      const m = await GET('/api/room/axes');
      const byId = Object.fromEntries((m.axes || []).map((a) => [a.id, a]));
      const mismatch = [];
      for (const [id, c] of Object.entries(controls)) {
        const a = byId[id];
        if (!a) { mismatch.push(`${id}: not in the axis table`); continue; }
        const near = (x, y) => Math.abs(x - y) < 1e-6;
        if (!near(a.min, c.range.min) || !near(a.max, c.range.max)) {
          mismatch.push(`${id}: 3D ${c.range.min} … ${c.range.max} / axis table ${a.min} … ${a.max}`);
        }
      }
      for (const a of m.axes || []) {
        if (!controls[a.id]) mismatch.push(`${a.id}: the 3D view has no control for it`);
      }
      manifest = { byId, mismatch };
    } catch (e) {
      manifest = { byId: {}, mismatch: [`Cannot read the axis table: ${e.message || e}`] };
    }
  }

  return { enable, disable, tick, requestMove, applySetup, poll, probe,
           get snapshot() { return snap; },
           get positions() { return { ...wanted }; },
           get busy() { return busyJob(); } };
}
