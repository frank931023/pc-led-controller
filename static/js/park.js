'use strict';

// Parking position per device, "Home & park" and "Home all devices".

// ===== Parking position =====

/** Axes that can be parked: every group with a move command, from the Pi's catalog. */
const parkAxes = (catalog) => catalog.groups.filter((g) => g.move);

function parkSummary(id) {
  const park = (state.devices[id] && state.devices[id].park) || {};
  const catalog = state.catalogs[id];
  const keys = Object.keys(park);
  if (!keys.length) return 'Not set — Home leaves every axis at its home position';
  if (!catalog) {
    // Pi not reachable yet: names for the current firmware, raw prefix for anything else.
    const names = { SXM: 'X', SYM: 'Y', SZM: 'Z', RM: 'Rotation', TM: 'Tilt' };
    return Object.keys(names).filter((k) => k in park).concat(keys.filter((k) => !(k in names)))
      .map((k) => `${names[k] || k} ${park[k]}`).join(' · ');
  }
  return parkAxes(catalog).filter((g) => g.move.prefix in park)
    .map((g) => `${g.title.replace(' Axis', '')} ${park[g.move.prefix]}${g.move.unit === '°' ? '°' : ` ${g.move.unit}`}`)
    .join(' · ');
}

function renderPark() {
  const el = $('#park');
  if (!el) return;
  const id = state.selected;
  const html = `
    <span class="pk">Parking</span>
    <span class="pv">${esc(parkSummary(id))}</span>
    <span class="sp"></span>
    <button class="sm" data-action="open-park">Edit</button>
    <button class="sm cmd" data-action="park-device" title="Home every axis, then move to the parking position">Home &amp; park</button>`;
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; renderBusy(); }
}

async function openPark() {
  const id = state.selected;
  const catalog = await loadCatalog(id);
  if (!catalog) { toast('Controls not loaded yet: this Pi must be reachable', 'error'); return; }
  const park = state.devices[id].park || {};
  $('#park-title').textContent = `${label(id)} · Parking position`;
  $('#park-err').textContent = '';
  $('#park-rows').innerHTML = parkAxes(catalog).map((g) => `
    <label for="pk-${esc(g.move.prefix)}">${esc(g.title)} <span class="faint">${esc(g.range)}</span></label>
    <input type="number" step="any" id="pk-${esc(g.move.prefix)}" data-park="${esc(g.move.prefix)}"
      placeholder="Home (${esc(g.move.unit)})" value="${park[g.move.prefix] ?? ''}">`).join('');
  $('#park-ov').classList.add('on');
  const first = $('#park-rows input');
  if (first) first.focus();
}

function closePark() { $('#park-ov').classList.remove('on'); }

async function savePark(e, clear = false) {
  if (e) e.preventDefault();
  const id = state.selected;
  const park = {};
  if (!clear) {
    for (const input of $$('#park-rows [data-park]')) {
      const v = input.value.trim();
      if (v === '') continue;
      if (!Number.isFinite(Number(v))) { $('#park-err').textContent = 'Positions must be numbers'; return; }
      park[input.dataset.park] = Number(v);
    }
  }
  const res = await api('PATCH', `/api/pi/${encodeURIComponent(id)}`, { park });
  if (!res.ok) { $('#park-err').textContent = res.error; return; }
  if (state.devices[id]) state.devices[id].park = res.data.pi.park;
  closePark();
  renderPark();
  toast(clear ? 'Parking position cleared' : 'Parking position saved');
}

/**
 * Home every axis of one device, then move to its parking position.
 * Resolves 'ok' | 'failed' | 'skipped'. Stops at the first failed step.
 */
async function homeAndPark(id) {
  if (busyCmd(id)) return 'skipped';
  const catalog = await loadCatalog(id);
  if (!catalog) return 'failed';
  for (const cmd of catalog.home_sequence) {
    if (!(await runCommand(id, cmd, timeoutFor(catalog, cmd)))) return 'failed';
  }
  const park = (state.devices[id] && state.devices[id].park) || {};
  for (const g of parkAxes(catalog)) {
    if (!(g.move.prefix in park)) continue;
    if (!(await runCommand(id, `${g.move.prefix},${park[g.move.prefix]}`, g.move.timeout))) return 'failed';
  }
  return 'ok';
}

async function parkDevice(id) {
  const result = await homeAndPark(id);
  if (result === 'ok') toast(`${label(id)}: homed${Object.keys(state.devices[id].park || {}).length ? ' and parked' : ''}`);
  else if (result === 'skipped') toast(`${label(id)} is busy`, 'error');
  else toast(`${label(id)}: stopped — see the command log`, 'error');
}

async function homeAll() {
  const ids = Object.keys(state.devices);
  if (!ids.length) return;
  const btn = $('#btn-home-all');
  btn.disabled = true;
  btn.textContent = 'Homing…';

  // Devices run in parallel; inside one device the steps go one after another — home
  // sequence from that Pi's catalog, then its parking position — and stop at the first failure.
  const results = await Promise.all(ids.map(homeAndPark));

  btn.disabled = false;
  btn.textContent = 'Home all devices';
  const n = (k) => results.filter((r) => r === k).length;
  const parts = [`${n('ok')} done`];
  if (n('failed')) parts.push(`${n('failed')} failed`);
  if (n('skipped')) parts.push(`${n('skipped')} busy, skipped`);
  toast(`Home all: ${parts.join(', ')}`, n('failed') ? 'error' : 'info');
}
