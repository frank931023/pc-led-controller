'use strict';

// Test scripts page: the scripts, Run / Stop, and the live log of the latest run
// (GET /api/scripts, POST /api/scripts/<id>/run, GET /api/scripts/runs/<id>, POST /api/scripts/stop).
// The job a script sends shows up in the job window (motion.js) like any other.

const SCRIPT_POLL_MS = 600;
const SCRIPT_SPEEDS = [1, 5, 10, 20, 50, 100];
const SCRIPT_SPEED_KEY = 'mcc-script-speed';
const RUN_TEXT = { running: 'Running', passed: 'Passed', failed: 'Failed', stopped: 'Stopped', error: 'Crashed' };
const LOG_ICON = { pass: '✓', fail: '✗', warn: '!', step: '›', info: '' };

const scripts = { list: [], run: null, timer: null, loaded: false };

// ===== Data =====

async function loadScripts() {
  const res = await api('GET', '/api/scripts');
  if (!res.ok) {
    $('#scr-body').innerHTML = `<div class="empty">Cannot load the scripts: ${esc(res.error)}</div>`;
    return;
  }
  scripts.list = res.data.scripts;
  scripts.run = res.data.run;
  scripts.loaded = true;
  renderScripts();
  if (scriptRunning()) pollRun();
}

const scriptRunning = () => !!scripts.run && scripts.run.state === 'running';

async function pollRun() {
  clearTimeout(scripts.timer);
  if (!scripts.run || state.view !== 'scripts') return;
  const res = await api('GET', `/api/scripts/runs/${encodeURIComponent(scripts.run.id)}`);
  if (res.ok) {
    scripts.run = res.data.run;
    renderRun();
    renderScriptList();
  }
  if (scriptRunning()) scripts.timer = setTimeout(pollRun, SCRIPT_POLL_MS);
}

function chosenSpeed() {
  try { return Number(localStorage.getItem(SCRIPT_SPEED_KEY)) || 10; } catch { return 10; }
}

function rememberSpeed(value) {
  try { localStorage.setItem(SCRIPT_SPEED_KEY, String(value)); } catch { /* fine */ }
}

// ===== Actions =====

async function runScript(id) {
  const res = await api('POST', `/api/scripts/${encodeURIComponent(id)}/run`, { speed: Number($('#scr-speed').value) });
  if (!res.ok) { toast(res.error, 'error'); return; }
  scripts.run = res.data.run;
  renderScripts();
  pollRun();
}

async function stopScript() {
  const res = await api('POST', '/api/scripts/stop');
  if (!res.ok) toast(res.error, 'error');
  pollRun();
}

// ===== Rendering =====

function renderScripts() {
  const select = $('#scr-speed');
  if (!select.options.length) {
    select.innerHTML = SCRIPT_SPEEDS.map((v) => `<option value="${v}">${v}×</option>`).join('');
    select.value = String(SCRIPT_SPEEDS.includes(chosenSpeed()) ? chosenSpeed() : 10);
    select.onchange = () => rememberSpeed(select.value);
  }
  $('#scr-body').innerHTML = `
    <div class="scr-list" id="scr-list"></div>
    <div class="scr-run" id="scr-run"></div>`;
  renderScriptList();
  renderRun();
}

function renderScriptList() {
  const busy = scriptRunning();
  $('#scr-list').innerHTML = scripts.list.map((s) => {
    const mine = busy && scripts.run.script === s.id;
    return `<div class="scr-card${mine ? ' on' : ''}">
      <div class="scr-name">${esc(s.name)}${s.interactive ? '<span class="scr-tag">needs you</span>' : ''}</div>
      <div class="scr-desc">${esc(s.description)}</div>
      <button class="${mine ? '' : 'primary'}" data-action="run-script" data-id="${esc(s.id)}" ${busy ? 'disabled' : ''}>
        ${mine ? 'Running…' : 'Run'}</button>
    </div>`;
  }).join('');
}

function renderRun() {
  const host = $('#scr-run');
  const run = scripts.run;
  if (!run) {
    host.innerHTML = `<div class="empty">Pick a script on the left and press <b>Run</b>.<br>
      Simulation is switched on while it runs and put back afterwards.</div>`;
    return;
  }
  const log = $('#scr-log');
  const stuckToBottom = !log || log.scrollHeight - log.scrollTop - log.clientHeight < 30;
  const scrolledTo = log ? log.scrollTop : 0;
  const elapsed = ((run.finished || Date.now() / 1000) - run.started).toFixed(1);
  host.innerHTML = `
    <div class="scr-head">
      <span class="scr-state s-${run.state}">${RUN_TEXT[run.state] || esc(run.state)}</span>
      <b>${esc(run.name)}</b>
      <span class="n">${run.mode} · ${run.speed}× · ${elapsed} s ·
        ${run.checks.passed} passed${run.checks.failed ? `, <span class="bad">${run.checks.failed} failed</span>` : ''}</span>
      <span class="sp"></span>
      ${run.state === 'running' ? '<button class="danger" data-action="stop-script">Stop</button>' : ''}
    </div>
    <div class="scr-log" id="scr-log">${run.log.map(logLine).join('')}</div>`;
  const fresh = $('#scr-log');
  fresh.scrollTop = stuckToBottom ? fresh.scrollHeight : scrolledTo;
}

function logLine(line) {
  return `<div class="ll l-${line.level}"><span class="t">${line.at.toFixed(1)}</span>
    <span class="i">${LOG_ICON[line.level] ?? ''}</span><span class="x">${esc(line.text)}</span></div>`;
}
