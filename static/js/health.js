'use strict';

// Health section of the device panel (data from the Beszel sync) and agent setup.

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
