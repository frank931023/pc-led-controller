'use strict';

// Monitoring page: every Pi's health, containers, alert rules.

// ===== Views (Devices / Monitoring) =====

const VIEW_KEY = 'mcc-view';

function setView(view) {
  state.view = view === 'monitoring' ? 'monitoring' : 'devices';
  try { localStorage.setItem(VIEW_KEY, state.view); } catch { /* fine */ }
  $('#dev-view').hidden = state.view !== 'devices';
  $('#mon-view').hidden = state.view !== 'monitoring';
  $('#nav-devices').classList.toggle('on', state.view === 'devices');
  $('#nav-monitoring').classList.toggle('on', state.view === 'monitoring');
  if (state.view === 'monitoring') {
    renderMonitoring();
    loadAlertState();
  }
}

/** Red counter on the Monitoring icon + per-device alerts, from GET /api/pis. */
function renderAlertBadge() {
  const n = state.alerts.length;
  const badge = $('#nav-alerts');
  badge.hidden = !n;
  badge.textContent = n > 9 ? '9+' : String(n);
}

const alertsFor = (id) => state.alerts.filter((a) => a.device_id === id);

function metricBar(name, value, unit, warn, bad, max = 100) {
  const shown = value == null ? '–' : `${Math.round(value)}${unit}`;
  const width = value == null ? 0 : Math.max(0, Math.min(100, (value / max) * 100));
  return `
    <div class="mrow">
      <span class="mk">${name}</span>
      <span class="mbar"><i class="${level(value, warn, bad)}" style="width:${width}%"></i></span>
      <span class="mv ${level(value, warn, bad)}">${shown}</span>
    </div>`;
}

function monitorCardHtml(id) {
  const d = state.devices[id];
  const h = d.health;
  const s = statusOf(id);
  const active = alertsFor(id);
  let body;
  if (!h) {
    body = `<div class="hnote">${d.beszel_system_id === 'none' ? 'Not linked to a monitoring agent.' : 'No monitoring agent yet.'}
              <span class="sp"></span><button class="sm" data-action="goto-device" data-id="${esc(id)}">Link…</button></div>`;
  } else {
    const r = state.alertRules;
    const th = (k, dflt) => (r && r[k] && r[k].enabled ? r[k].threshold : dflt);
    body = `
      ${metricBar('CPU', h.cpu, '%', th('cpu', 90) * 0.85, th('cpu', 90))}
      ${metricBar('Memory', h.mem, '%', th('mem', 90) * 0.9, th('mem', 90))}
      ${metricBar('Disk', h.disk, '%', th('disk', 90) * 0.9, th('disk', 90))}
      ${metricBar('Temp', h.temp, '°C', th('temp', 80) - 10, th('temp', 80), 100)}
      <div class="mfoot">
        <span>Load ${h.load == null ? '–' : h.load.toFixed(2)}</span>
        <span>Up ${fmtUptime(h.uptime)}</span>
        <span>${h.agent_version ? `agent ${esc(h.agent_version)}` : ''}</span>
      </div>
      <div class="macts">
        <button class="sm" data-action="open-charts" data-id="${esc(id)}">History</button>
        <button class="sm" data-action="open-containers" data-id="${esc(id)}">Containers</button>
      </div>`;
  }
  const agent = !h ? '' : `<span class="badge ${h.status === 'up' ? 'online' : h.status === 'down' ? 'offline' : 'checking'}"
      title="Monitoring agent">Agent ${h.status === 'up' ? 'up' : esc(h.status)}</span>`;
  return `
    <div class="mcard ${active.length ? 'alerting' : ''}">
      <div class="mhead">
        <div class="t"><b>${esc(label(id))}</b><small>${esc(id)} · ${esc(d.host)}</small></div>
        <span class="badge ${s}" title="Control connection">${STATUS_TEXT[s]}</span>
        ${agent}
      </div>
      ${active.map((a) => `<div class="malert">${esc(a.text)}</div>`).join('')}
      ${body}
    </div>`;
}

function renderMonitoring() {
  const el = $('#mon-body-view');
  if (!el || state.view !== 'monitoring') return;
  const b = state.beszel;
  const ids = Object.keys(state.devices);
  const linked = ids.filter((id) => state.devices[id].health).length;
  $('#mon-sub').textContent = b && b.enabled ? `${linked} of ${ids.length} devices reporting` : '';

  let html;
  if (!b || !b.enabled) {
    html = '<div class="empty">Health monitoring is off (BESZEL_URL is empty on the server).</div>';
  } else {
    const banner = b.connected ? ''
      : `<div class="hnote">Monitoring data not reachable${b.error ? ` — ${esc(b.error)}` : ''}</div>`;
    const alertBox = state.alerts.length
      ? `<div class="abox"><h2>Active alerts <span class="n">${state.alerts.length}</span></h2>
           ${state.alerts.map((a) => `<div class="arow"><span class="dot offline"></span><span>${esc(a.text)}</span>
             <time>since ${clock(new Date(a.since * 1000))}</time></div>`).join('')}</div>` : '';
    const cards = ids.length ? ids.map(monitorCardHtml).join('')
      : '<div class="empty">No devices yet. Add them on the Devices page.</div>';
    const free = b.unlinked || [];
    const unlinked = free.length ? `
      <h2 class="sect">Unlinked agents <span class="n">${free.length}</span></h2>
      <div class="mgrid">${free.map((u) => `
        <div class="mcard dim"><div class="mhead">
          <div class="t"><b>${esc(u.name)}</b><small>Monitoring agent · ${esc(u.status)}</small></div>
          <button class="sm" data-action="add-from-agent" data-sid="${esc(u.system_id)}" data-name="${esc(u.name)}">Add as device</button>
        </div></div>`).join('')}</div>` : '';
    const events = (state.alertEvents || []).slice(0, 20);
    const log = events.length ? `
      <h2 class="sect">Recent alert events</h2>
      <div class="elog">${events.map((ev) => `
        <div class="erow ${ev.kind}"><time>${new Date(ev.at * 1000).toLocaleString()}</time>
          <span>${ev.kind === 'fired' ? 'Fired' : 'Resolved'}</span><span>${esc(ev.text)}</span></div>`).join('')}</div>` : '';
    html = `${banner}${alertBox}<div class="mgrid">${cards}</div>${unlinked}${log}`;
  }
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

/** Rules (for bar colours) + recent events; the active list itself comes with /api/pis. */
async function loadAlertState() {
  const res = await api('GET', '/api/alerts');
  if (!res.ok) return null;
  state.alertRules = res.data.settings.rules;
  state.alertEvents = res.data.events;
  state.alertSettings = res.data.settings;
  state.webhookError = res.data.webhook_error;
  renderMonitoring();
  return res.data;
}

// ----- containers -----

async function openContainers(id) {
  $('#cont-title').textContent = `${label(id)} · Containers`;
  $('#cont-state').textContent = 'Loading…';
  $('#cont-table').innerHTML = '';
  $('#cont-ov').classList.add('on');
  const res = await api('GET', `/api/pi/${encodeURIComponent(id)}/containers`, null, 15000);
  if (!$('#cont-ov').classList.contains('on')) return;
  if (!res.ok) { $('#cont-state').textContent = `Cannot load the containers: ${res.error}`; return; }
  const list = res.data.containers;
  $('#cont-state').textContent = list.length ? '' : 'No containers reported by this Pi.';
  if (!list.length) return;
  $('#cont-table').innerHTML = `
    <thead><tr><th>Name</th><th>Status</th><th class="num">CPU</th><th class="num">Memory</th><th>Image</th></tr></thead>
    <tbody>${list.map((c) => `
      <tr><td><b>${esc(c.name)}</b></td>
        <td>${esc(c.status || '–')}${c.health ? ` <span class="badge ${c.health === 'healthy' ? 'online' : c.health === 'unhealthy' ? 'offline' : 'checking'}">${esc(c.health)}</span>` : ''}</td>
        <td class="num">${c.cpu == null ? '–' : `${c.cpu.toFixed(1)}%`}</td>
        <td class="num">${c.memory == null ? '–' : `${Math.round(c.memory)} MB`}</td>
        <td class="mono">${esc(c.image || '')}</td></tr>`).join('')}</tbody>`;
}

function closeContainers() { $('#cont-ov').classList.remove('on'); }

// ----- alert rules -----

const RULE_ROWS = [
  ['agent_down', 'Monitoring agent down', null],
  ['cpu', 'CPU at or above', '%'],
  ['mem', 'Memory at or above', '%'],
  ['disk', 'Disk at or above', '%'],
  ['temp', 'Temperature at or above', '°C'],
];

async function openRules() {
  $('#rules-err').textContent = '';
  $('#rules-rows').innerHTML = '<div class="empty">Loading…</div>';
  $('#rules-ov').classList.add('on');
  const data = await loadAlertState();
  if (!data) { $('#rules-rows').innerHTML = '<div class="empty">Cannot load the alert rules.</div>'; return; }
  const rules = data.settings.rules;
  $('#rules-rows').innerHTML = RULE_ROWS.map(([key, text, unit]) => `
    <div class="rrow">
      <label class="check"><input type="checkbox" data-rule="${key}" ${rules[key].enabled ? 'checked' : ''}> ${text}</label>
      ${unit ? `<span class="rth"><input type="number" min="1" max="${key === 'temp' ? 150 : 100}" step="1"
          data-threshold="${key}" value="${rules[key].threshold}"> ${unit}</span>` : '<span class="rth"></span>'}
    </div>`).join('');
  $('#rules-webhook').value = data.settings.webhook_url || '';
  if (data.webhook_error) $('#rules-err').textContent = `Last webhook delivery failed: ${data.webhook_error}`;
}

function closeRules() { $('#rules-ov').classList.remove('on'); }

function collectRules() {
  const rules = {};
  RULE_ROWS.forEach(([key, , unit]) => {
    rules[key] = { enabled: $(`[data-rule="${key}"]`).checked };
    if (unit) rules[key].threshold = Number($(`[data-threshold="${key}"]`).value);
  });
  return { rules, webhook_url: $('#rules-webhook').value.trim() };
}

async function saveRules(e) {
  e.preventDefault();
  const btn = $('#rules-save');
  btn.disabled = true;
  const res = await api('PUT', '/api/alerts', collectRules());
  btn.disabled = false;
  if (!res.ok) { $('#rules-err').textContent = res.error; return; }
  state.alertRules = res.data.settings.rules;
  closeRules();
  renderMonitoring();
  toast('Alert rules saved');
}

async function testWebhook() {
  // Save first so the test goes to the URL currently typed in.
  $('#rules-err').textContent = '';
  const btn = $('#rules-test');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  let res = await api('PUT', '/api/alerts', collectRules());
  if (res.ok) res = await api('POST', '/api/alerts/test', null, 15000);
  btn.disabled = false;
  btn.textContent = 'Send test';
  if (res.ok) toast('Test message sent — check the channel');
  else $('#rules-err').textContent = res.error;
}
