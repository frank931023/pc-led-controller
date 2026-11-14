'use strict';

/*
 * Motion Control Center - web UI.
 *
 * Non-blocking rules this file follows:
 *  - Every request goes through api(): it never throws, always times out, and turns
 *    network errors / non-JSON answers into a readable message.
 *  - A command only locks *its own* device. Other devices, renaming, adding, testing
 *    and switching devices all stay usable while a long calibrate is running.
 *  - The 5 s refresh only patches the parts that changed, so typed input
 *    (move values, custom command, rename) is never wiped.
 *  - No window.confirm / alert: dialogs are in-page.
 */

// ===== Config =====

// The buttons, their timeouts and the "Home all" sequence come from each Pi
// (GET /api/pi/<id>/commands -> the Pi's commands.py), so they always match its firmware.
const POLL_MS = 5000;
const HISTORY_MAX = 30;
const ID_RE = /^[A-Za-z0-9_.-]{1,32}$/;                 // same rule as the server
const STATUS_TEXT = { online: 'Online', busy: 'Busy', 'no-arduino': 'No Arduino', offline: 'Offline', checking: 'Checking' };
const TOKEN_KEY = 'mcc-api-token';

// ===== State =====

const state = {
  devices: {},        // id -> { host, port, name, alternative_name, status, running }
  selected: null,
  history: {},        // id -> [{ cmd, text, cls, lines, at, pending }], newest first
  running: {},        // id -> { cmd, startedAt } for commands started from this tab
  catalogs: {},       // id -> command catalog from that Pi (GET /api/pi/<id>/commands)
  catalogErrors: {},  // id -> why the last catalog fetch failed
  loading: {},        // id -> true while the catalog is being fetched
  renaming: false,
  serverUp: true,
  beszel: null,       // { enabled, connected, error, unlinked } from /api/pis
  charts: null,       // { id, data, timer } while the health history dialog is open
  chartRange: '1h',   // last range picked in that dialog
  refreshing: false,
};

// ===== Utilities =====

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** The big name: alternative_name if set, otherwise name. */
const label = (id) => {
  const d = state.devices[id];
  return (d && (d.alternative_name || d.name)) || id;
};
const statusOf = (id) => {
  const s = state.devices[id] && state.devices[id].status;
  return STATUS_TEXT[s] ? s : 'offline';
};
/** Command in flight on this device: from this tab, or reported by the server (another tab). */
const busyCmd = (id) => (state.running[id] && state.running[id].cmd)
  || (state.devices[id] && state.devices[id].running) || null;

const clock = (d) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

// ----- API token (only when the server sets API_TOKEN) -----

function storedToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

function storeToken(token) {
  try { localStorage.setItem(TOKEN_KEY, token); } catch { /* private mode: ask again next time */ }
}

let tokenPrompt = null;   // one dialog even when several requests get 401 at once

/** Ask for the API token. Resolves to the token, or '' when cancelled. */
function askToken() {
  if (tokenPrompt) return tokenPrompt;
  tokenPrompt = new Promise((resolve) => {
    const ov = $('#token-ov');
    const input = $('#token-input');
    input.value = '';
    const done = (token) => {
      ov.classList.remove('on');
      $('#token-form').onsubmit = $('#token-cancel').onclick = null;
      ov.cancel = null;
      tokenPrompt = null;
      if (token) storeToken(token);
      resolve(token);
    };
    $('#token-form').onsubmit = (e) => { e.preventDefault(); done(input.value.trim()); };
    $('#token-cancel').onclick = () => done('');
    ov.cancel = () => done('');                 // used by Escape / backdrop click
    ov.classList.add('on');
    input.focus();
  });
  return tokenPrompt;
}

/**
 * fetch wrapper. Never throws. Resolves to { ok, status, data, error }.
 * ok is true only for HTTP 2xx with {"status": "success"}.
 * A 401 asks for the API token once and repeats the request with it.
 */
async function api(method, path, body, timeoutMs = 10000, retried = false) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = body ? { 'Content-Type': 'application/json' } : {};
    if (storedToken()) headers.Authorization = `Bearer ${storedToken()}`;
    const resp = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    if (resp.status === 401 && !retried) {
      clearTimeout(timer);
      if (await askToken()) return api(method, path, body, timeoutMs, true);
    }
    const text = await resp.text();
    let data;
    try { data = JSON.parse(text); } catch {
      // e.g. an HTML error page from a proxy, or an old server without this route
      return { ok: false, status: resp.status, data: {}, error: `Server returned HTTP ${resp.status} (not JSON)` };
    }
    const ok = resp.ok && data.status === 'success';
    return { ok, status: resp.status, data, error: ok ? null : (data.error || `HTTP ${resp.status}`) };
  } catch (e) {
    const error = e.name === 'AbortError'
      ? `No answer from server after ${Math.round(timeoutMs / 1000)} s`
      : 'Cannot reach the control server';
    return { ok: false, status: 0, data: {}, error };
  } finally {
    clearTimeout(timer);
  }
}

function toast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = 'toast' + (type === 'error' ? ' error' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, type === 'error' ? 5000 : 3000);
}

/**
 * In-page replacement for window.confirm. Resolves true/false, or { ok, checked }
 * when a checkbox label is given.
 */
function confirmDialog(title, text, okLabel = 'OK', checkboxLabel = null) {
  return new Promise((resolve) => {
    const ov = $('#confirm-ov');
    $('#confirm-title').textContent = title;
    $('#confirm-text').textContent = text;
    $('#confirm-yes').textContent = okLabel;
    $('#confirm-check-row').hidden = !checkboxLabel;
    $('#confirm-check').checked = false;
    $('#confirm-check-label').textContent = checkboxLabel || '';
    const done = (answer) => {
      ov.classList.remove('on');
      $('#confirm-yes').onclick = $('#confirm-no').onclick = null;
      ov.cancel = null;
      resolve(checkboxLabel ? { ok: answer, checked: answer && $('#confirm-check').checked } : answer);
    };
    $('#confirm-yes').onclick = () => done(true);
    $('#confirm-no').onclick = () => done(false);
    ov.cancel = () => done(false);              // used by Escape / backdrop click
    ov.classList.add('on');
    $('#confirm-no').focus();
  });
}

// ===== Rendering =====

function renderList() {
  const ids = Object.keys(state.devices);
  $('#count').textContent = ids.length || '';
  if (!ids.length) {
    $('#list').innerHTML = '<div class="empty">No devices yet.<br>Click <b>+ Add</b> to add one.</div>';
    return;
  }
  $('#list').innerHTML = ids.map((id) => {
    const d = state.devices[id];
    const s = statusOf(id);
    const indicator = busyCmd(id) ? '<span class="spin" title="Running a command"></span>' : `<span class="dot ${s}"></span>`;
    return `
      <div class="dv ${id === state.selected ? 'on' : ''}" data-action="select" data-id="${esc(id)}">
        ${indicator}
        <div class="t">
          <b>${esc(label(id))}</b>
          <small>${esc(d.host)}:${esc(d.port)}</small>
          ${healthLine(d.health)}
        </div>
        <span class="badge ${s}">${STATUS_TEXT[s]}</span>
      </div>`;
  }).join('') + unlinkedHtml();
}

/** Monitoring agents (Beszel systems) that belong to no device, listed under the devices. */
function unlinkedHtml() {
  const list = (state.beszel && state.beszel.unlinked) || [];
  if (!list.length) return '';
  return `
    <div class="unl">
      <h2>Unlinked agents <span class="n">${list.length}</span></h2>
      ${list.map((u) => `
        <div class="dv static">
          <span class="dot ${u.status === 'up' ? 'online' : 'offline'}"></span>
          <div class="t"><b>${esc(u.name)}</b><small>Monitoring agent · ${esc(u.status)}</small></div>
          <button class="ghost sm" data-action="add-from-agent" data-sid="${esc(u.system_id)}"
            data-name="${esc(u.name)}" title="Create a device linked to this agent">Add</button>
        </div>`).join('')}
      <p class="hint">Pis reporting health data that no device is linked to. Add one as a device,
        or link it from a device's Health section.</p>
    </div>`;
}

function groupHtml(g) {
  const buttons = g.actions.map((a) => `
    <button class="cmd" data-action="cmd" data-cmd="${esc(a.cmd)}" data-timeout="${a.timeout}" title="${esc(a.cmd)}">${esc(a.label)}</button>`).join('');
  const move = g.move ? `
    <div class="mv">
      <input type="number" step="any" id="mv-${esc(g.move.prefix)}" placeholder="${esc(g.move.unit)}" data-enter="move-${esc(g.move.prefix)}">
      <button class="cmd primary" id="move-${esc(g.move.prefix)}" data-action="move"
        data-prefix="${esc(g.move.prefix)}" data-timeout="${g.move.timeout}" title="${esc(g.move.prefix)},&lt;value&gt;">${esc(g.move.label)}</button>
    </div>` : '';
  return `
    <div class="grp">
      <div class="gh"><b>${esc(g.title)}</b><span>${esc(g.range)}</span></div>
      <div class="btns">${buttons}</div>
      ${move}
    </div>`;
}

/** Fill the Controls area of the selected device from its catalog. */
function renderControls() {
  const el = $('#groups');
  if (!el) return;
  const catalog = state.catalogs[state.selected];
  if (catalog) {
    el.innerHTML = catalog.groups.map(groupHtml).join('');
  } else {
    const error = state.catalogErrors[state.selected];
    const text = state.loading[state.selected] ? 'Loading controls…'
      : error ? `Cannot load the controls from this Pi: ${esc(error)}`
      : 'Controls appear once this Pi is reachable.';
    el.innerHTML = `<div class="empty">${text}</div>`;
  }
  renderBusy();
}

/** Fetch a device's command catalog once it is reachable. Resolves to the catalog or null. */
async function loadCatalog(id) {
  if (state.catalogs[id]) return state.catalogs[id];
  if (state.loading[id]) return null;
  state.loading[id] = true;
  const res = await api('GET', `/api/pi/${encodeURIComponent(id)}/commands`);
  delete state.loading[id];
  if (res.ok) {
    state.catalogs[id] = res.data;
    delete state.catalogErrors[id];
  } else {
    state.catalogErrors[id] = res.error;
  }
  if (id === state.selected) renderControls();
  return state.catalogs[id] || null;
}

/** Timeout (s) the Pi's catalog gives a command, falling back to its default. */
function timeoutFor(catalog, cmd) {
  const actions = catalog.groups.flatMap((g) => g.actions);
  const action = actions.find((a) => a.cmd === cmd);
  return action ? action.timeout : catalog.default_timeout;
}

/** Full re-render of the right panel. Only on device switch / add / remove. */
function renderDetail() {
  const id = state.selected;
  if (!id || !state.devices[id]) {
    $('#detail').innerHTML = '<div class="empty" style="margin:auto">Select a device on the left, or add one.</div>';
    return;
  }
  state.renaming = false;
  $('#detail').innerHTML = `
    <div class="head">
      <div class="t">
        <div id="d-title-box"><h1 data-action="rename" title="Click to rename"><span id="d-title"></span><span class="pen">✎</span></h1></div>
        <small id="d-sub"></small>
      </div>
      <span class="badge" id="d-badge"></span>
      <button id="d-test" data-action="test">Test</button>
      <button class="danger" id="d-remove" data-action="remove">Remove</button>
    </div>
    <div class="health" id="health"></div>
    <h2>Controls</h2>
    <div class="grow"><div class="groups" id="groups"></div></div>
    <div class="custom">
      <input type="text" id="custom" placeholder="Custom command" maxlength="64" data-enter="custom-send">
      <button class="cmd" id="custom-send" data-action="custom">Send</button>
    </div>
    <div class="hist" id="hist"></div>`;
  updateHeader();
  renderHealth();
  renderHistory();
  loadCatalog(id);      // marks the device as loading before the first paint
  renderControls();
}

/** Patch name / address / badge in place (keeps inputs and the rename box alive). */
function updateHeader() {
  const id = state.selected;
  const d = state.devices[id];
  if (!d || !$('#d-badge')) return;
  if (!state.renaming) $('#d-title').textContent = label(id);
  $('#d-sub').textContent = [d.name, `${d.host}:${d.port}`, d.app_version].filter(Boolean).join(' · ');
  const s = statusOf(id);
  $('#d-badge').className = `badge ${s}`;
  $('#d-badge').textContent = STATUS_TEXT[s];
}

function renderHistory() {
  const el = $('#hist');
  if (!el) return;
  const items = state.history[state.selected] || [];
  if (!items.length) {
    el.innerHTML = '<div class="empty">No commands sent yet</div>';
    return;
  }
  el.innerHTML = items.map((h, i) => {
    const result = h.pending
      ? `<span class="spin"></span><span class="res idle" data-elapsed="${h.at.getTime()}">Running…</span>`
      : `<span class="res ${h.cls}">${esc(h.text)}</span>`;
    return `
      <div class="it">
        <div class="lh"><code>${esc(h.cmd)}</code><span class="sp"></span>${result}<time>${clock(h.at)}</time></div>
        ${h.lines && h.lines.length && i < 5 ? `<pre>${esc(h.lines.join('\n'))}</pre>` : ''}
      </div>`;
  }).join('');
  tickElapsed();
}

/** Disable this device's command buttons while it is busy; highlight the running one. */
function renderBusy() {
  const id = state.selected;
  const cmd = busyCmd(id);
  const prefix = cmd ? cmd.split(',')[0] : null;
  $$('#detail .cmd').forEach((b) => {
    b.disabled = !!cmd;
    b.classList.toggle('running', !!cmd && (b.dataset.cmd === cmd || b.dataset.prefix === prefix));
  });
  const remove = $('#d-remove');
  const test = $('#d-test');
  if (remove) remove.disabled = !!cmd;
  if (test && test.textContent === 'Test') test.disabled = !!cmd;
}

function tickElapsed() {
  $$('[data-elapsed]').forEach((el) => {
    const s = Math.floor((Date.now() - Number(el.dataset.elapsed)) / 1000);
    el.textContent = `Running… ${s}s`;
  });
}

function setServerUp(up) {
  $('#nav-dot').className = 'st ' + (up ? 'ok' : 'dead');
  if (up === state.serverUp) return;
  state.serverUp = up;
  toast(up ? 'Connection to the control server restored' : 'Lost connection to the control server', up ? 'info' : 'error');
}

// ===== Pi health (Beszel) =====

const pct = (v) => (v == null ? '–' : `${Math.round(v)}%`);
const level = (v, warn, bad) => (v == null ? '' : v >= bad ? 'fail' : v >= warn ? 'warn' : '');

function fmtUptime(sec) {
  if (sec == null) return '–';
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

/** One short line under the device in the list, e.g. "CPU 12% · 48°C". */
function healthLine(h) {
  if (!h) return '';
  if (h.status !== 'up') return `<small class="hl">Agent ${esc(h.status)}</small>`;
  const parts = [`CPU ${pct(h.cpu)}`];
  if (h.temp != null) parts.push(`${Math.round(h.temp)}°C`);
  return `<small class="hl">${parts.join(' · ')}</small>`;
}

function renderHealth() {
  const el = $('#health');
  if (!el) return;
  const b = state.beszel;
  if (!b || !b.enabled) { el.innerHTML = ''; return; }   // monitoring turned off: no section at all

  const d = state.devices[state.selected];
  const h = d && d.health;
  const links = h
    ? `<span class="hlinks"><a href="#" data-action="open-charts" title="CPU, memory, disk and temperature over time">History</a>`
      + `<a href="#" data-action="unlink" title="Stop showing this agent's data for this device">Unlink</a></span>` : '';

  let body;
  if (!b.connected) {
    body = `<div class="hnote">Health monitoring not reachable${b.error ? ` — ${esc(b.error)}` : ''}</div>`;
  } else if (!h) {
    const free = b.unlinked || [];
    const off = d && d.beszel_system_id === 'none';
    const options = free.map((u) => `<option value="${esc(u.system_id)}">${esc(u.name)} (${esc(u.status)})</option>`).join('');
    const picker = free.length
      ? `<select id="link-sid">${options}</select><button class="primary" data-action="link">Link</button>` : '';
    const auto = off
      ? '<button data-action="auto-link" title="Link automatically when an agent with this Device ID connects">Auto-link</button>' : '';
    body = `<div class="hnote">${off ? 'Not linked to a monitoring agent.' : 'No monitoring agent found for this Pi.'}
              <span class="sp"></span>${picker}${auto}
              <button data-action="open-monitoring">Set up</button></div>`;
  } else {
    const agentCls = h.status === 'up' ? 'ok' : h.status === 'down' ? 'fail' : 'warn';
    // ⚠ When the agent is down these are the last values received, not live ones.
    const tiles = [
      ['Agent', h.status === 'up' ? 'Up' : h.status, agentCls],
      ['CPU', pct(h.cpu), level(h.cpu, 80, 95)],
      ['Memory', pct(h.mem), level(h.mem, 85, 95)],
      ['Disk', pct(h.disk), level(h.disk, 85, 95)],
      ['Temp', h.temp == null ? '–' : `${Math.round(h.temp)}°C`, level(h.temp, 70, 80)],
      ['Load', h.load == null ? '–' : h.load.toFixed(2), ''],
      ['Uptime', fmtUptime(h.uptime), ''],
    ];
    body = `<div class="hgrid">${tiles.map(([k, v, c]) =>
      `<div class="tile ${c}"><b>${esc(v)}</b>${k}</div>`).join('')}</div>`;
  }
  const html = `<h2>Health ${links}</h2>${body}`;
  // Only touch the DOM when something changed, so an open <select> isn't reset every 5 s.
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

async function linkSystem(id, systemId, okMsg) {
  const res = await api('POST', `/api/pi/${encodeURIComponent(id)}/beszel`, { system_id: systemId });
  if (!res.ok) { toast(`Link failed: ${res.error}`, 'error'); return; }
  toast(okMsg);
  refresh();
  // The server syncs names right away; refresh again once that has landed.
  setTimeout(refresh, 1500);
}

async function openMonitoring() {
  $('#mon-ov').classList.add('on');
  $('#mon-body').hidden = true;
  $('#mon-state').textContent = 'Loading…';
  const res = await api('GET', '/api/beszel/agent-env', null, 15000);
  if (!res.ok) { $('#mon-state').textContent = `Cannot get the agent settings: ${res.error}`; return; }
  $('#mon-state').textContent = 'Each Pi runs a small monitoring agent that reports CPU, memory, disk and '
    + `temperature to this PC (${res.data.hub_url}).`;
  $('#mon-env').textContent = res.data.env;
  $('#mon-warn').hidden = !res.data.warning;
  $('#mon-warn').textContent = res.data.warning || '';
  $('#mon-body').hidden = false;
}

function closeMonitoring() { $('#mon-ov').classList.remove('on'); }

async function copyEnv() {
  const text = $('#mon-env').textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied');
  } catch {
    // Clipboard API needs https or localhost; fall back to selecting the text.
    const range = document.createRange();
    range.selectNodeContents($('#mon-env'));
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    toast('Press Ctrl+C to copy');
  }
}

// ===== Health history charts =====
// Drawn from GET /api/pi/<id>/health/history, so nobody has to open (or log in to) Beszel.

const CHARTS = [
  { key: 'cpu', title: 'CPU', unit: '%', max: 100 },
  { key: 'mem', title: 'Memory', unit: '%', max: 100 },
  { key: 'disk', title: 'Disk', unit: '%', max: 100 },
  { key: 'temp', title: 'Temperature', unit: '°C' },
  { key: 'load', title: 'Load (1 min)', unit: '', digits: 2 },
];
const CHART_REFRESH_MS = 60000;          // the agent sends one sample a minute
const PLOT_W = 1000, PLOT_H = 100;       // SVG units; the plot is stretched to its box

const fmtMetric = (c, v) => (v == null ? '–' : `${Number(v).toFixed(c.digits || 0)}${c.unit}`);

function fmtChartTime(t, data) {
  const long = data.to - data.from > 86400;
  return new Date(t * 1000).toLocaleString([], long
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
    : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

/** Round axis top just above v: 1, 2, 2.5, 5 × 10^n. */
function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((s) => s * mag).find((top) => top >= v * 1.1) || 10 * mag;
}

function openCharts() {
  if (state.charts) clearTimeout(state.charts.timer);
  state.charts = { id: state.selected, data: null, timer: null };
  $('#chart-title').textContent = `${label(state.selected)} · Health history`;
  $('#charts').innerHTML = '';
  $('#chart-ranges').innerHTML = '';
  $('#chart-state').textContent = 'Loading…';
  $('#chart-ov').classList.add('on');
  loadCharts();
}

function closeCharts() {
  if (state.charts) clearTimeout(state.charts.timer);
  state.charts = null;
  $('#chart-ov').classList.remove('on');
}

function setChartRange(range) {
  if (!state.charts || range === state.chartRange) return;
  state.chartRange = range;
  $$('#chart-ranges button').forEach((b) => b.classList.toggle('on', b.dataset.range === range));
  $('#chart-state').textContent = 'Loading…';
  loadCharts();
}

async function loadCharts() {
  const view = state.charts;
  if (!view) return;
  clearTimeout(view.timer);
  const range = state.chartRange;
  const res = await api('GET',
    `/api/pi/${encodeURIComponent(view.id)}/health/history?range=${encodeURIComponent(range)}`, null, 15000);
  if (state.charts !== view || state.chartRange !== range) return;    // closed or switched meanwhile
  clearTimeout(view.timer);
  view.timer = setTimeout(loadCharts, CHART_REFRESH_MS);
  if (!res.ok) { $('#chart-state').textContent = `Cannot load the history: ${res.error}`; return; }
  view.data = res.data;
  renderCharts();
}

function renderCharts() {
  const data = state.charts.data;
  $('#chart-ranges').innerHTML = data.ranges.map((r) =>
    `<button class="${r === state.chartRange ? 'on' : ''}" data-action="chart-range" data-range="${esc(r)}">${esc(r)}</button>`).join('');
  $('#chart-state').textContent = data.points.length ? ''
    : `No samples in this range yet: one is stored every ${data.interval / 60} min.`;
  $('#charts').innerHTML = data.points.length ? CHARTS.map((c) => chartHtml(c, data)).join('') : '';
}

/** One metric as a line chart. The line breaks where samples are missing (Pi or agent down). */
function chartHtml(c, data) {
  const pts = data.points.filter((p) => p[c.key] != null);
  if (!pts.length) {
    return `<div class="chart"><div class="gh"><b>${esc(c.title)}</b><span>–</span></div>
            <div class="plot none">Not reported by this Pi</div></div>`;
  }

  const top = c.max || niceMax(Math.max(...pts.map((p) => p[c.key])));
  const x = (t) => ((t - data.from) / (data.to - data.from)) * PLOT_W;
  const y = (v) => PLOT_H - (Math.min(v, top) / top) * PLOT_H;

  const runs = [[]];
  pts.forEach((p, i) => {
    if (i && p.t - pts[i - 1].t > data.interval * 2.5) runs.push([]);
    runs[runs.length - 1].push([x(p.t), y(p[c.key])]);
  });
  const xy = ([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`;
  let line = '';
  let area = '';
  for (const run of runs) {
    if (run.length === 1) run.push([run[0][0] + 2, run[0][1]]);    // a lone sample still shows
    line += `M${run.map(xy).join('L')}`;
    area += `M${run[0][0].toFixed(1)},${PLOT_H}L${run.map(xy).join('L')}L${run[run.length - 1][0].toFixed(1)},${PLOT_H}Z`;
  }
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${PLOT_W}" y1="${PLOT_H * f}" y2="${PLOT_H * f}"/>`).join('');
  const latest = fmtMetric(c, pts[pts.length - 1][c.key]);

  return `
    <div class="chart" data-key="${c.key}" data-top="${top}">
      <div class="gh"><b>${esc(c.title)}</b><span class="cv" data-latest="${esc(latest)}">${esc(latest)}</span></div>
      <div class="plot">
        <svg viewBox="0 0 ${PLOT_W} ${PLOT_H}" preserveAspectRatio="none" aria-hidden="true">
          <g class="grid">${grid}</g>
          <line class="base" x1="0" x2="${PLOT_W}" y1="${PLOT_H}" y2="${PLOT_H}"/>
          <path class="area" d="${area}"/><path class="line" d="${line}"/>
        </svg>
        <span class="ymax">${esc(fmtMetric(c, top))}</span>
        <div class="cursor" hidden><i></i></div>
      </div>
      <div class="ax"><span>${fmtChartTime(data.from, data)}</span>
        <span>${fmtChartTime((data.from + data.to) / 2, data)}</span><span>Now</span></div>
    </div>`;
}

/** Hovering one chart shows the same moment on all of them. */
function showChartCursor(plot, clientX) {
  const data = state.charts && state.charts.data;
  if (!data || !data.points.length) return;
  const box = plot.getBoundingClientRect();
  const t = data.from + ((clientX - box.left) / box.width) * (data.to - data.from);
  const p = data.points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
  const left = ((p.t - data.from) / (data.to - data.from)) * 100;
  $$('#charts .chart[data-key]').forEach((card) => {
    const c = CHARTS.find((m) => m.key === card.dataset.key);
    const v = p[c.key];
    const top = Number(card.dataset.top);
    const cursor = $('.cursor', card);
    const dot = $('i', cursor);
    cursor.hidden = false;
    cursor.style.left = `${left}%`;
    dot.hidden = v == null;
    if (v != null) dot.style.top = `${100 - (Math.min(v, top) / top) * 100}%`;
    $('.cv', card).textContent = `${fmtMetric(c, v)} · ${fmtChartTime(p.t, data)}`;
  });
}

function hideChartCursor() {
  $$('#charts .chart[data-key]').forEach((card) => {
    $('.cursor', card).hidden = true;
    $('.cv', card).textContent = $('.cv', card).dataset.latest;
  });
}

// ===== History =====

function pushHistory(id, entry) {
  const list = state.history[id] || (state.history[id] = []);
  list.unshift(entry);
  if (list.length > HISTORY_MAX) list.length = HISTORY_MAX;
  if (id === state.selected) renderHistory();
  return entry;
}

function finishHistory(id, entry, fields) {
  Object.assign(entry, fields, { pending: false });
  if (id === state.selected) renderHistory();
}

/** Turn a successful /command answer into a history result. */
function interpretResult(piResponse) {
  const r = piResponse || {};
  // Older Pi builds return a single string instead of an array
  const raw = r.arduino_response;
  const lines = Array.isArray(raw) ? raw : (raw != null && raw !== '' ? [String(raw)] : []);
  // ⚠ Firmware still ends with DONE when a move exceeds the travel limit; only this
  // line tells us nothing actually moved (e.g. axis not calibrated yet).
  const exceeded = lines.some((l) => /exceeded max travel/i.test(l));
  if (r.arduino_result === 'DONE') {
    return exceeded ? { text: 'Out of range', cls: 'warn', lines, ok: false }
                    : { text: 'Done', cls: 'ok', lines, ok: true };
  }
  if (r.arduino_result === 'ERR') return { text: 'Unknown command', cls: 'fail', lines, ok: false };
  return { text: r.arduino_result || 'No response', cls: 'idle', lines, ok: false };
}

// ===== Actions =====

async function refresh() {
  if (state.refreshing) return;          // never stack polls
  state.refreshing = true;
  const res = await api('GET', '/api/pis', null, 8000);
  state.refreshing = false;

  if (!res.ok) { setServerUp(false); return; }
  setServerUp(true);

  state.devices = res.data.pis || {};
  state.beszel = res.data.beszel || null;

  if (!state.devices[state.selected]) {
    state.selected = Object.keys(state.devices)[0] || null;
    renderList();
    renderDetail();
    return;
  }
  renderList();
  updateHeader();
  renderHealth();
  renderBusy();
  if (!state.catalogs[state.selected] && statusOf(state.selected) !== 'offline') loadCatalog(state.selected);
}

/** Run one command on one device. Resolves true only if it really did what was asked. */
async function runCommand(id, cmd, timeout) {
  if (busyCmd(id)) {
    toast(`${label(id)} is busy running ${busyCmd(id)}`, 'error');
    return false;
  }
  state.running[id] = { cmd, startedAt: Date.now() };
  const entry = pushHistory(id, { cmd, at: new Date(), pending: true });
  renderList();
  if (id === state.selected) renderBusy();

  // Server waits up to timeout + 5 s for the Pi; give it a little more before giving up here.
  const res = await api('POST', `/api/pi/${encodeURIComponent(id)}/command`, { command: cmd, timeout }, (timeout + 15) * 1000);

  delete state.running[id];
  // The last poll may still say "running" for this device; clear it so the buttons
  // unlock now instead of on the next poll.
  if (state.devices[id] && state.devices[id].running === cmd) state.devices[id].running = null;
  let ok = false;
  if (res.ok) {
    const r = interpretResult(res.data.pi_response);
    finishHistory(id, entry, r);
    if (state.devices[id]) state.devices[id].status = 'online';
    ok = r.ok;
  } else {
    const text = res.status === 409 ? `Busy: ${res.error}` : res.error;
    finishHistory(id, entry, { text, cls: 'fail', lines: [] });
    if (res.data.pi_status && state.devices[id]) state.devices[id].status = res.data.pi_status;
  }
  renderList();
  if (id === state.selected) { updateHeader(); renderBusy(); }
  return ok;
}

function runOnSelected(cmd, timeout) {
  if (state.selected) runCommand(state.selected, cmd, timeout);
}

async function homeAll() {
  const ids = Object.keys(state.devices);
  if (!ids.length) return;
  const btn = $('#btn-home-all');
  btn.disabled = true;
  btn.textContent = 'Homing…';

  // Devices run in parallel; inside one device the axes go one after another (the
  // sequence comes from that Pi's catalog) and stop at the first failure.
  const results = await Promise.all(ids.map(async (id) => {
    if (busyCmd(id)) return 'skipped';
    const catalog = await loadCatalog(id);
    if (!catalog) return 'failed';
    for (const cmd of catalog.home_sequence) {
      if (!(await runCommand(id, cmd, timeoutFor(catalog, cmd)))) return 'failed';
    }
    return 'ok';
  }));

  btn.disabled = false;
  btn.textContent = 'Home all devices';
  const n = (k) => results.filter((r) => r === k).length;
  const parts = [`${n('ok')} homed`];
  if (n('failed')) parts.push(`${n('failed')} failed`);
  if (n('skipped')) parts.push(`${n('skipped')} busy, skipped`);
  toast(`Home all: ${parts.join(', ')}`, n('failed') ? 'error' : 'info');
}

async function testDevice(id, quiet = false) {
  const btn = id === state.selected ? $('#d-test') : null;
  if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }

  const res = await api('POST', `/api/pi/${encodeURIComponent(id)}/test`, null, 10000);
  if (state.devices[id] && res.data.pi_status) state.devices[id].status = res.data.pi_status;
  if (!quiet) {
    if (res.ok) toast(`${label(id)} connected`);
    else toast(`${label(id)}: ${res.error}`, 'error');
  }

  if (btn && btn.isConnected) { btn.textContent = 'Test'; btn.disabled = !!busyCmd(id); }
  renderList();
  if (id === state.selected) updateHeader();
  return res.ok;
}

async function testAll() {
  const ids = Object.keys(state.devices);
  if (!ids.length) return;
  const btn = $('#btn-test-all');
  btn.disabled = true;
  btn.textContent = 'Testing…';
  const results = await Promise.all(ids.map((id) => testDevice(id, true)));
  btn.disabled = false;
  btn.textContent = 'Test all connections';
  const up = results.filter(Boolean).length;
  toast(`${up} of ${ids.length} online`, up === ids.length ? 'info' : 'error');
}

async function removeDevice(id) {
  const d = state.devices[id];
  const linked = !!d.health;
  const text = `${id} (${d.host}) will be removed from this list. The Pi itself is not changed.`
    + (linked ? ' If its monitoring agent keeps running, it registers again and shows up under "Unlinked agents".' : '');
  const answer = await confirmDialog(`Remove ${label(id)}?`, text, 'Remove',
    linked ? 'Also delete its health history' : null);
  if (!(linked ? answer.ok : answer)) return;
  const alsoBeszel = linked && answer.checked;
  const res = await api('DELETE', `/api/pi/${encodeURIComponent(id)}${alsoBeszel ? '?beszel=1' : ''}`);
  if (!res.ok) { toast(`Remove failed: ${res.error}`, 'error'); return; }
  delete state.devices[id];
  delete state.history[id];
  delete state.catalogs[id];
  if (state.selected === id) state.selected = Object.keys(state.devices)[0] || null;
  renderList();
  renderDetail();
  toast(res.data.beszel_deleted ? 'Device and its health history removed' : 'Device removed');
}

function startRename() {
  const id = state.selected;
  if (!id || state.renaming) return;
  state.renaming = true;
  const d = state.devices[id];
  const box = $('#d-title-box');
  box.innerHTML = `<input type="text" class="rn" maxlength="64" placeholder="${esc(d.name)}" value="${esc(d.alternative_name || '')}">`;
  const input = $('input', box);

  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const previous = d.alternative_name || '';
    const alt = input.value.trim();

    // Optimistic: show the new name right away, roll back if the server refuses.
    if (save && alt !== previous) d.alternative_name = alt;
    state.renaming = false;
    restoreTitle();
    renderList();

    if (save && alt !== previous) {
      const res = await api('PATCH', `/api/pi/${encodeURIComponent(id)}`, { alternative_name: alt });
      if (!res.ok) {
        if (state.devices[id]) state.devices[id].alternative_name = previous;
        toast(`Rename failed: ${res.error}`, 'error');
        renderList();
        if (id === state.selected) updateHeader();
      }
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') finish(true);
    if (e.key === 'Escape') { e.stopPropagation(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  input.focus();
  input.select();

  function restoreTitle() {
    if (!box.isConnected) return;          // user already switched device
    box.innerHTML = '<h1 data-action="rename" title="Click to rename"><span id="d-title"></span><span class="pen">✎</span></h1>';
    updateHeader();
  }
}

// ----- add dialog -----

/** Open the add dialog; `agent` = { sid, name } when adding from an unlinked Beszel agent. */
function openAdd(agent = null) {
  $('#add-err').textContent = '';
  $('#add-form').dataset.sid = agent ? agent.sid : '';
  $('#add-agent').hidden = !agent;
  if (agent) {
    $('#add-agent-name').textContent = agent.name;
    // The agent name is the Pi's PI_ID, which should also be the Device ID.
    const id = agent.name.replace(/[^A-Za-z0-9_.-]/g, '-').slice(0, 32);
    $('#f-id').value = id;
    $('#f-name').value = id;
  }
  $('#add-ov').classList.add('on');
  (agent ? $('#f-host') : $('#f-id')).focus();
}

function closeAdd() {
  $('#add-ov').classList.remove('on');
  $('#add-form').reset();
  $('#add-form').dataset.sid = '';
  $('#add-agent').hidden = true;
}

async function submitAdd(e) {
  e.preventDefault();
  const pi_id = $('#f-id').value.trim();
  const host = $('#f-host').value.trim();
  const port = parseInt($('#f-port').value, 10) || 5000;
  const name = $('#f-name').value.trim() || pi_id;
  const err = $('#add-err');

  // Same rules as the server, checked here so the user gets instant feedback.
  if (!ID_RE.test(pi_id)) { err.textContent = 'Device ID: letters, digits, "-", "_" or "." (max 32)'; return; }
  if (state.devices[pi_id]) { err.textContent = `${pi_id} already exists`; return; }
  if (!host || /[:/\s]/.test(host)) { err.textContent = 'Host: hostname or IP only, no http:// and no port'; return; }

  const submit = $('#add-submit');
  submit.disabled = true;
  submit.textContent = 'Adding…';
  const beszel_system_id = $('#add-form').dataset.sid || '';
  const res = await api('POST', '/api/pi', { pi_id, host, port, name, beszel_system_id });
  submit.disabled = false;
  submit.textContent = 'Add';

  if (!res.ok) { err.textContent = res.error; return; }
  state.devices[pi_id] = res.data.pi;
  state.selected = pi_id;
  closeAdd();
  renderList();
  renderDetail();
  toast(`${label(pi_id)} added, checking connection…`);
}

// ===== Events (one delegated handler; no inline onclick strings) =====

document.addEventListener('click', (e) => {
  const overlay = e.target.classList && e.target.classList.contains('ov') ? e.target : null;
  if (overlay) {                                   // click on the dimmed backdrop
    if (overlay.id === 'add-ov') closeAdd();
    if (overlay.id === 'mon-ov') closeMonitoring();
    if (overlay.id === 'chart-ov') closeCharts();
    if ((overlay.id === 'confirm-ov' || overlay.id === 'token-ov') && overlay.cancel) overlay.cancel();
    return;
  }

  const el = e.target.closest('[data-action]');
  if (!el || el.disabled) return;
  const action = el.dataset.action;

  switch (action) {
    case 'select':
      if (el.dataset.id !== state.selected) {
        state.selected = el.dataset.id;
        renderList();
        renderDetail();
      }
      break;
    case 'cmd':
      runOnSelected(el.dataset.cmd, Number(el.dataset.timeout));
      break;
    case 'move': {
      const input = $(`#mv-${el.dataset.prefix}`);
      const v = input.value.trim();
      if (v === '' || !Number.isFinite(Number(v))) { toast('Enter a number first', 'error'); input.focus(); return; }
      runOnSelected(`${el.dataset.prefix},${v}`, Number(el.dataset.timeout));
      break;
    }
    case 'custom': {
      const input = $('#custom');
      const cmd = input.value.trim();
      if (!cmd) { toast('Enter a command first', 'error'); input.focus(); return; }
      const catalog = state.catalogs[state.selected];
      if (!catalog) { toast('Controls not loaded yet', 'error'); return; }
      runOnSelected(cmd, catalog.default_timeout);
      break;
    }
    case 'rename': startRename(); break;
    case 'test': testDevice(state.selected); break;
    case 'remove': removeDevice(state.selected); break;
    case 'open-add': openAdd(); break;
    case 'close-add': closeAdd(); break;
    case 'home-all': homeAll(); break;
    case 'test-all': testAll(); break;
    case 'open-monitoring': openMonitoring(); break;
    case 'close-monitoring': closeMonitoring(); break;
    case 'copy-env': copyEnv(); break;
    case 'add-from-agent': openAdd({ sid: el.dataset.sid, name: el.dataset.name }); break;
    case 'link': {
      const sel = $('#link-sid');
      if (sel && sel.value) linkSystem(state.selected, sel.value, 'Linked to the monitoring agent');
      break;
    }
    case 'unlink':
      e.preventDefault();
      linkSystem(state.selected, null, 'Unlinked. Use Link to connect it again.');
      break;
    case 'auto-link': linkSystem(state.selected, '', 'Auto-link on'); break;
    case 'open-charts':
      e.preventDefault();
      openCharts();
      break;
    case 'close-charts': closeCharts(); break;
    case 'chart-range': setChartRange(el.dataset.range); break;
  }
});

// Enter in an input with data-enter="<button id>" clicks that button
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.dataset && e.target.dataset.enter) {
    e.preventDefault();
    const btn = document.getElementById(e.target.dataset.enter);
    if (btn && !btn.disabled) btn.click();
  }
  if (e.key === 'Escape') {
    const confirmOv = $('#confirm-ov');
    const tokenOv = $('#token-ov');
    if (tokenOv.classList.contains('on') && tokenOv.cancel) tokenOv.cancel();
    else if (confirmOv.classList.contains('on') && confirmOv.cancel) confirmOv.cancel();
    else if ($('#add-ov').classList.contains('on')) closeAdd();
    else if ($('#mon-ov').classList.contains('on')) closeMonitoring();
    else if ($('#chart-ov').classList.contains('on')) closeCharts();
  }
});

$('#charts').addEventListener('mousemove', (e) => {
  const plot = e.target.closest('.plot:not(.none)');
  if (plot) showChartCursor(plot, e.clientX);
  else hideChartCursor();
});
$('#charts').addEventListener('mouseleave', hideChartCursor);

$('#add-form').addEventListener('submit', submitAdd);

// Refresh right away when the tab becomes visible again; skip polling while hidden.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

// ===== Start =====

refresh();
if (location.hash === '#monitoring') openMonitoring();   // shareable link to the setup steps
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
setInterval(tickElapsed, 1000);
