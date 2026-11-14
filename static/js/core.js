'use strict';

/*
 * Motion Control Center - web UI.
 *
 * Plain scripts, no build step, loaded in this order by templates/index.html:
 *   core.js        config, shared state, helpers, api(), API token, toasts, confirm dialog
 *   devices.js     device list, controls, command log, sending commands, add / rename / remove
 *   health.js      Health section of a device, linking to agents, agent setup dialog
 *   charts.js      health history charts
 *   monitoring.js  Monitoring page, containers, alert rules
 *   park.js        parking position, "Home & park", "Home all devices"
 *   main.js        refresh loop, the one delegated event handler, start-up
 * They share one global scope; only main.js runs code at load time.
 *
 * Non-blocking rules all of them follow:
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
  view: 'devices',    // 'devices' | 'monitoring'
  alerts: [],         // active alerts, from /api/pis
  alertRules: null,   // from /api/alerts (bar colours follow the thresholds)
  alertEvents: [],    // recent fired / resolved, from /api/alerts
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
