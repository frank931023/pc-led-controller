'use strict';

// Devices page: list, selected device panel, controls, command log, device actions.

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
        ${alertsFor(id).length ? `<span class="badge offline" title="${esc(alertsFor(id).map((a) => a.text).join('\n'))}">⚠ ${alertsFor(id).length}</span>` : ''}
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
  if (id === state.selected) { renderControls(); renderPark(); }   // parking row now knows axis names
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
  $('#detail').dataset.id = id;
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
    <div class="park" id="park"></div>
    <h2>Controls</h2>
    <div class="grow"><div class="groups" id="groups"></div></div>
    <div class="custom">
      <input type="text" id="custom" placeholder="Custom command" maxlength="64" data-enter="custom-send">
      <button class="cmd" id="custom-send" data-action="custom">Send</button>
    </div>
    <div class="hist" id="hist"></div>`;
  updateHeader();
  renderHealth();
  renderPark();
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
