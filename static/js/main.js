'use strict';

// Refresh loop, event wiring and start-up. Loaded last: everything it calls is defined.

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
  const alertCount = state.alerts.length;
  state.alerts = res.data.alerts || [];
  renderAlertBadge();
  if (state.view === 'monitoring') {
    // New or resolved alerts: fetch the event log too.
    if (state.alerts.length !== alertCount) loadAlertState();
    renderMonitoring();
  }

  // Selection gone (device removed), or chosen before the list arrived (#device= link):
  // the right panel has to be drawn from scratch.
  const panelShown = $('#detail').dataset.id === state.selected && $('#d-badge');
  if (!state.devices[state.selected] || !panelShown) {
    if (!state.devices[state.selected]) state.selected = Object.keys(state.devices)[0] || null;
    renderList();
    renderDetail();
    return;
  }
  renderList();
  updateHeader();
  renderHealth();
  renderPark();
  renderBusy();
  if (!state.catalogs[state.selected] && statusOf(state.selected) !== 'offline') loadCatalog(state.selected);
}

/** Run one command on one device. Resolves true only if it really did what was asked. */

// ===== Events (one delegated handler; no inline onclick strings) =====

document.addEventListener('click', (e) => {
  const overlay = e.target.classList && e.target.classList.contains('ov') ? e.target : null;
  if (overlay) {                                   // click on the dimmed backdrop
    if (overlay.id === 'add-ov') closeAdd();
    if (overlay.id === 'mon-ov') closeMonitoring();
    if (overlay.id === 'chart-ov') closeCharts();
    if (overlay.id === 'cont-ov') closeContainers();
    if (overlay.id === 'rules-ov') closeRules();
    if (overlay.id === 'park-ov') closePark();
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
      openCharts(el.dataset.id || state.selected);
      break;
    case 'view': setView(el.dataset.view); break;
    case 'goto-device':
      state.selected = el.dataset.id;
      setView('devices');
      renderList();
      renderDetail();
      break;
    case 'open-containers': openContainers(el.dataset.id); break;
    case 'close-containers': closeContainers(); break;
    case 'open-rules': openRules(); break;
    case 'close-rules': closeRules(); break;
    case 'test-webhook': testWebhook(); break;
    case 'open-park': openPark(); break;
    case 'close-park': closePark(); break;
    case 'clear-park': savePark(null, true); break;
    case 'park-device': parkDevice(state.selected); break;
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
    else if ($('#cont-ov').classList.contains('on')) closeContainers();
    else if ($('#rules-ov').classList.contains('on')) closeRules();
    else if ($('#park-ov').classList.contains('on')) closePark();
  }
});

$('#charts').addEventListener('mousemove', (e) => {
  const plot = e.target.closest('.plot:not(.none)');
  if (plot) showChartCursor(plot, e.clientX);
  else hideChartCursor();
});
$('#charts').addEventListener('mouseleave', hideChartCursor);

$('#add-form').addEventListener('submit', submitAdd);
$('#rules-form').addEventListener('submit', saveRules);
$('#park-form').addEventListener('submit', savePark);

// Refresh right away when the tab becomes visible again; skip polling while hidden.
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });

// ===== Start =====

let savedView = 'devices';
try { savedView = localStorage.getItem(VIEW_KEY) || 'devices'; } catch { /* fine */ }
// Deep links: #health = Monitoring page, #device=<id> = that device's panel.
const linkedDevice = location.hash.startsWith('#device=') ? decodeURIComponent(location.hash.slice(8)) : null;
if (linkedDevice) state.selected = linkedDevice;
setView(location.hash === '#health' ? 'monitoring' : linkedDevice ? 'devices' : savedView);
refresh();
if (location.hash === '#monitoring') openMonitoring();   // shareable link to the setup steps
setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
setInterval(tickElapsed, 1000);
