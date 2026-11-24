/**
 * The job window, on every page (dashboard and 3D room).
 *
 *   window    the current setup job: progress, every axis, Stop / Mark as done.
 *             It pops up by itself when ACQUA (or a test script) sends a setup.
 *             Minimizing never stops anything: the job runs on the server.
 *   dock      bottom centre (dashboard) or top centre (3D room):
 *               card  the current job while the window is minimized
 *               chip  simulation off / on; opens the simulation settings
 *
 * Once the window has been minimized, later jobs stay minimized too (until it is opened
 * again), so a batch of setups does not pop up over and over. A manual simulated job
 * always pops up: it waits for someone to answer it.
 *
 * Plain script (no modules) so both pages can include it. The <script> tag's
 * data-theme="dark|light" and data-place="bottom|top" fit it to the page.
 */
(function () {
  'use strict';

  // ===== Config =====

  const tag = document.currentScript;
  const THEME = tag.dataset.theme === 'dark' ? 'dark' : 'light';
  const PLACE = tag.dataset.place === 'top' ? 'top' : 'bottom';
  const TOKEN_KEY = 'mcc-api-token';               // same key as the rest of the UI
  const COLLAPSED_KEY = 'mcc-job-window-collapsed';
  const POLL_ACTIVE_MS = 700;
  const POLL_IDLE_MS = 1000;                       // short jobs must not slip between two polls
  const DONE_CARD_MS = 10000;                      // a "done" card hides itself after this
  const QUIET_SOURCES = ['3d', 'ui'];              // the 3D room shows its own progress
  const SPEEDS = [1, 5, 10, 20, 50, 100];
  const ACTIVE = ['running', 'aborting'];
  const STEP_ICON = { done: '✓', homing: '⌂', moving: '▶', pending: '·', failed: '✗', skipped: '–' };
  const SOURCE_TEXT = { acqua: 'ACQUA', script: 'Test script', '3d': '3D room', ui: 'Web page', api: 'API' };

  // ===== State =====

  const ui = {
    job: null,
    sim: { on: false, mode: 'manual', speed: 1 },
    open: null,                   // null | 'job' | 'settings'
    draft: null,                  // the settings form while it is open ({ on, mode, speed })
    collapsed: readCollapsed(),   // the viewer minimized the window: keep later jobs minimized
    seen: new Set(),              // jobs already popped up once
    dismissed: new Set(),         // finished jobs whose card was closed
    endedAt: {},                  // job id -> when this page saw it end (Date.now())
    failing: false,               // the "why did it fail" field is open
    error: '',
    firstPoll: true,
  };
  let timer = null;

  // ===== Helpers =====

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const isActive = (job) => !!job && ACTIVE.includes(job.state);
  const isWaiting = (job) => !!job && job.mode === 'manual' && job.state === 'running';
  const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
  const fmt = (v, unit) => (unit === 'deg' ? `${Math.round(v)}°` : `${Number(v).toFixed(3)} m`);
  const sourceText = (s) => SOURCE_TEXT[s] || s;

  function readCollapsed() {
    try { return sessionStorage.getItem(COLLAPSED_KEY) === '1'; } catch { return false; }
  }

  function setCollapsed(value) {
    ui.collapsed = value;
    try { sessionStorage.setItem(COLLAPSED_KEY, value ? '1' : '0'); } catch { /* fine */ }
  }

  /** fetch that never throws: { ok, data, error } */
  async function api(method, url, body) {
    const headers = body ? { 'Content-Type': 'application/json' } : {};
    try { const t = localStorage.getItem(TOKEN_KEY); if (t) headers.Authorization = `Bearer ${t}`; } catch { /* no storage */ }
    try {
      const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
      const data = await res.json().catch(() => ({}));
      const ok = res.ok && data.status === 'success';
      const error = ok ? '' : (res.status === 401
        ? 'API token needed: enter it on the dashboard first' : data.error || `HTTP ${res.status}`);
      return { ok, data, error };
    } catch {
      return { ok: false, data: {}, error: 'Cannot reach the control server' };
    }
  }

  /** Seconds since the job started, and roughly how many are left. */
  function timing(job) {
    const end = isActive(job) ? Date.now() / 1000 : job.finished || Date.now() / 1000;
    const elapsed = Math.max(0, end - job.started);
    return { elapsed, left: Math.max(0, job.eta - elapsed) };
  }

  function progress(job) {
    const total = job.steps.length;
    const done = job.steps.filter((s) => s.state === 'done').length;
    return { done, total, pct: total ? Math.round((done / total) * 100) : 100 };
  }

  // ===== Pieces of the window =====

  function statusLine(job) {
    const { done, total } = progress(job);
    const { elapsed, left } = timing(job);
    if (isWaiting(job)) return 'Waiting for you to mark it done or failed';
    if (job.state === 'aborting') return 'Stopping: the axis moving now finishes its move';
    if (isActive(job)) return `Axis ${Math.min(done + 1, total)} of ${total} · ${clock(elapsed)} · about ${clock(left)} left`;
    if (job.state === 'done') return total ? `Done in ${clock(elapsed)}` : 'Done: every axis was already in place';
    return job.state === 'aborted' ? `Stopped after ${clock(elapsed)}` : `Failed after ${clock(elapsed)}`;
  }

  function badges(job) {
    const sim = job.simulated
      ? `<span class="tag sim">SIMULATION · ${job.mode === 'auto' ? `auto ${job.speed}×` : 'manual'}</span>` : '';
    return `<span class="tag">${esc(sourceText(job.source))}</span>${sim}`;
  }

  function stepRows(job) {
    const rows = job.steps.map((s) => `<tr class="st-${s.state}">
        <td class="ic">${STEP_ICON[s.state] || '·'}</td>
        <td class="l">${esc(s.label)}${s.homed_first ? ' <em>homed first</em>' : ''}</td>
        <td>${esc(s.device || '—')}</td><td><code>${esc(s.command)}</code></td>
        <td>${s.known_from ? fmt(s.from, s.unit) : '?'} → <b>${fmt(s.to, s.unit)}</b></td></tr>`).join('');
    const unchanged = job.unchanged.length
      ? `<p class="note">${job.unchanged.length} axis(es) already in place: ${esc(job.unchanged.join(', '))}</p>` : '';
    return job.steps.length
      ? `<table><thead><tr><th></th><th>Axis</th><th>Pi</th><th>Command</th><th>Move</th></tr></thead>
         <tbody>${rows}</tbody></table>${unchanged}`
      : unchanged;
  }

  function result(job) {
    if (isActive(job)) return '';
    const text = { done: '✓ Done', aborted: `■ Stopped${job.error ? `: ${esc(job.error)}` : ''}` }[job.state]
      || `✗ ${esc(job.error || 'Failed')}`;
    return `<div class="result r-${job.state}">${text}</div>`;
  }

  function jobActions(job) {
    if (isWaiting(job) && ui.failing) {
      return `<input class="why" data-k="why" placeholder="Why it failed (sent to whoever asked)" value="Simulated failure">
        <button class="b" data-a="fail-back">Back</button><button class="b badf" data-a="fail-send">Send failure</button>`;
    }
    if (isWaiting(job)) {
      return `<button class="b" data-a="minimize">Minimize</button><span class="sp"></span>
        <button class="b" data-a="stop">Stop</button>
        <button class="b bad" data-a="fail">Mark as failed…</button>
        <button class="b ok" data-a="done">✓ Mark as done</button>`;
    }
    if (isActive(job)) {
      return `<button class="b" data-a="minimize">Minimize — it keeps running</button><span class="sp"></span>
        <button class="b bad" data-a="stop" ${job.state === 'aborting' ? 'disabled' : ''}
          title="The axis moving now finishes (the firmware has no stop); the rest are skipped">Stop after this axis</button>`;
    }
    return '<span class="sp"></span><button class="b ok" data-a="close">Close</button>';
  }

  function jobWindow(job) {
    const { pct } = progress(job);
    return `<div class="back" data-a="backdrop"><div class="box" role="dialog" aria-modal="true" aria-label="Setup job">
      <div class="hd">
        <div class="tags">${badges(job)}</div>
        <button class="x" data-a="${isActive(job) ? 'minimize' : 'close'}" title="${isActive(job) ? 'Minimize' : 'Close'}">${isActive(job) ? '–' : '×'}</button>
        <h3>${esc(job.name)}</h3>
        <div class="sub">${statusLine(job)}</div>
        <div class="bar"><i class="b-${job.state}" style="width:${pct}%"></i></div>
      </div>
      <div class="bd">${stepRows(job)}${result(job)}</div>
      ${ui.error ? `<div class="err">${esc(ui.error)}</div>` : ''}
      <div class="ft">${jobActions(job)}</div>
    </div></div>`;
  }

  function settingsWindow() {
    const s = ui.draft;
    const speeds = SPEEDS.map((v) => `<option value="${v}"${v === s.speed ? ' selected' : ''}>${v}×</option>`).join('');
    return `<div class="back" data-a="backdrop"><div class="box narrow" role="dialog" aria-modal="true" aria-label="Simulation">
      <div class="hd"><span class="tag sim">SIMULATION</span><h3>No Pis yet? Simulate them</h3></div>
      <div class="bd">
        <label class="row"><input type="checkbox" data-k="on" ${s.on ? 'checked' : ''}>
          <span><b>Simulation on</b> — setup jobs (from ACQUA, the 3D room or a test script) send nothing to the Pis</span></label>
        <label class="row"><input type="radio" name="simmode" value="auto" data-k="mode" ${s.mode === 'auto' ? 'checked' : ''}>
          <span><b>Automatic</b> — each job plays out by itself, axis by axis, then reports done.
          Speed <select data-k="speed">${speeds}</select></span></label>
        <label class="row"><input type="radio" name="simmode" value="manual" data-k="mode" ${s.mode === 'manual' ? 'checked' : ''}>
          <span><b>Manual</b> — each job waits until you click “Mark as done” or “Mark as failed”</span></label>
        <p class="note">Pretend positions are kept apart from the real ones. Turn simulation off before connecting real Pis.</p>
      </div>
      ${ui.error ? `<div class="err">${esc(ui.error)}</div>` : ''}
      <div class="ft"><span class="sp"></span><button class="b" data-a="close">Cancel</button>
        <button class="b ok" data-a="save-settings">Save</button></div>
    </div></div>`;
  }

  // ===== The dock =====

  function cardVisible(job) {
    if (!job || ui.open === 'job') return false;
    if (isActive(job)) return true;
    if (ui.dismissed.has(job.id) || !ui.endedAt[job.id]) return false;
    return job.state !== 'done' || Date.now() - ui.endedAt[job.id] < DONE_CARD_MS;
  }

  function card(job) {
    const { done, total, pct } = progress(job);
    const icon = isWaiting(job) ? '⏸' : isActive(job) ? '▶' : { done: '✓', aborted: '■' }[job.state] || '✗';
    const text = isWaiting(job) ? 'waiting for you' : isActive(job) ? `${done}/${total} axes` : job.state;
    const close = isActive(job) ? '' : '<span class="cx" data-a="dismiss" title="Hide">×</span>';
    return `<button class="card c-${job.state}${isWaiting(job) ? ' wait' : ''}" data-a="expand" title="Open the job window">
      <span class="ci">${icon}</span><span class="cn">${esc(job.name)}</span><span class="cs">${text}</span>${close}
      <i style="width:${pct}%"></i></button>`;
  }

  function chip() {
    const s = ui.sim;
    const text = !s.on ? 'Simulation off' : s.mode === 'auto' ? `Simulation · auto ${s.speed}×` : 'Simulation · manual';
    return `<button class="chip${s.on ? ' on' : ''}" data-a="settings" title="Simulation settings"><i></i>${text}</button>`;
  }

  // ===== Render =====

  const root = document.createElement('div');
  root.className = `mjw ${THEME} ${PLACE}`;
  const dockEl = document.createElement('div');
  dockEl.className = 'dock';
  const windowEl = document.createElement('div');
  root.append(dockEl, windowEl);
  document.body.appendChild(root);

  function renderDock() {
    dockEl.innerHTML = `${cardVisible(ui.job) ? card(ui.job) : ''}${chip()}`;
  }

  function renderWindow() {
    const why = windowEl.querySelector('[data-k="why"]')?.value;
    const scroll = windowEl.querySelector('.bd')?.scrollTop;
    if (ui.open === 'job' && ui.job) windowEl.innerHTML = jobWindow(ui.job);
    else if (ui.open === 'settings') windowEl.innerHTML = settingsWindow();
    else windowEl.innerHTML = '';
    const whyField = windowEl.querySelector('[data-k="why"]');
    if (whyField && why !== undefined) whyField.value = why;
    const body = windowEl.querySelector('.bd');
    if (body && scroll) body.scrollTop = scroll;
  }

  /** After a poll: the dock always; the job window unless someone is typing in it.
   *  The settings form is never redrawn under the viewer's hands. */
  function refresh() {
    renderDock();
    const typing = ui.failing && windowEl.contains(document.activeElement);
    if (ui.open !== 'settings' && !typing) renderWindow();
  }

  function render() {
    renderDock();
    renderWindow();
  }

  // ===== Polling =====

  function onNewData(job, sim) {
    ui.sim = sim;
    if (job && !isActive(job) && ui.firstPoll) ui.dismissed.add(job.id);    // ended before this page opened
    ui.firstPoll = false;
    // a job that ended between two polls still shows its result in the card
    if (job && !isActive(job) && !ui.endedAt[job.id]) ui.endedAt[job.id] = Date.now();
    if (job && isActive(job) && !ui.seen.has(job.id)) {
      ui.seen.add(job.id);
      const popUp = isWaiting(job) || (!ui.collapsed && !QUIET_SOURCES.includes(job.source));
      if (popUp && ui.open !== 'settings') { ui.open = 'job'; ui.failing = false; ui.error = ''; }
    }
    ui.job = job;
    if (ui.open === 'job' && !job) ui.open = null;
  }

  async function poll() {
    clearTimeout(timer);
    if (!document.hidden) {
      const res = await api('GET', '/api/room/current');
      if (res.ok) {
        onNewData(res.data.job, res.data.simulation);
        refresh();
      }
    }
    timer = setTimeout(poll, isActive(ui.job) || ui.open ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  // ===== Actions =====

  async function post(url, body) {
    const res = await api('POST', url, body);
    ui.error = res.ok ? '' : res.error;
    return res.ok;
  }

  /** Answer a manual job. The window closes: whoever answered knows the result; the
   *  card shows it for a few seconds. */
  async function answer(body) {
    if (await post(`/api/room/jobs/${encodeURIComponent(ui.job.id)}/finish`, body)) {
      ui.failing = false;
      ui.open = null;
    }
  }

  const ACTIONS = {
    minimize() { ui.open = null; setCollapsed(true); },
    expand() { ui.open = 'job'; ui.failing = false; setCollapsed(false); },
    close() {
      if (ui.open === 'job' && ui.job) ui.dismissed.add(ui.job.id);
      ui.open = null; ui.error = '';
    },
    backdrop() { ACTIONS[ui.open === 'job' && isActive(ui.job) ? 'minimize' : 'close'](); },
    dismiss() { if (ui.job) ui.dismissed.add(ui.job.id); },
    settings() { ui.open = 'settings'; ui.draft = { ...ui.sim }; ui.error = ''; },
    fail() { ui.failing = true; },
    'fail-back'() { ui.failing = false; },
    async stop() { await post('/api/room/stop'); },
    async done() { await answer({ result: 'done' }); },
    async 'fail-send'() {
      await answer({ result: 'error', error: root.querySelector('[data-k="why"]')?.value || '' });
    },
    async 'save-settings'() {
      if (await post('/api/room/simulation', ui.draft)) ui.open = null;
    },
  };

  root.addEventListener('click', async (e) => {
    const el = e.target.closest('[data-a]');
    if (!el || el.disabled) return;
    if (el.dataset.a === 'backdrop' && e.target !== el) return;     // a click inside the box
    e.stopPropagation();
    await ACTIONS[el.dataset.a]?.();
    render();
    poll();
  });

  // the settings form writes straight into ui.draft, so any redraw keeps what was chosen
  windowEl.addEventListener('change', (e) => {
    const field = e.target.dataset.k;
    if (ui.open !== 'settings' || !field) return;
    if (field === 'on') ui.draft.on = e.target.checked;
    if (field === 'mode') ui.draft.mode = e.target.value;
    if (field === 'speed') ui.draft.speed = Number(e.target.value);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && ui.open) { ACTIONS.backdrop(); render(); }
  });
  // keep the 3D view (orbit / zoom / shortcuts) from reacting under the window
  for (const type of ['pointerdown', 'wheel', 'keydown']) root.addEventListener(type, (e) => e.stopPropagation());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  // ===== Styles (self-contained: the two pages have different style sheets) =====

  const style = document.createElement('style');
  style.textContent = `
  .mjw{--b:#fff;--fg:#18201f;--mu:#5f6b6a;--ln:#e2e8e7;--sk:#f0f3f3;--br:#009B85;--am:#b7791f;--bad:#d1493f;
       font:13px/1.5 'Segoe UI',system-ui,sans-serif;color:var(--fg)}
  .mjw.dark{--b:#161b22;--fg:#e6e9ef;--mu:#8b93a1;--ln:#30363d;--sk:#0d1116;--am:#e3a008;--bad:#f85149}
  .mjw .dock{position:fixed;z-index:350;display:flex;gap:8px;align-items:center;transform:translateX(-50%)}
  .mjw.bottom .dock{bottom:14px;left:calc(50% + 28px)}
  .mjw.top .dock{top:12px;left:50%}
  .mjw .chip,.mjw .card{font:inherit;font-size:12px;border:1px solid var(--ln);background:var(--b);color:var(--mu);
       border-radius:999px;box-shadow:0 2px 10px #0002;cursor:pointer;display:flex;align-items:center;gap:7px;padding:6px 12px}
  .mjw .chip i{width:8px;height:8px;border-radius:50%;background:var(--mu);opacity:.5}
  .mjw .chip.on{color:var(--fg);border-color:var(--am)}
  .mjw .chip.on i{background:var(--am);opacity:1}
  .mjw .card{position:relative;overflow:hidden;color:var(--fg);border-color:var(--br);padding-bottom:8px;max-width:360px}
  .mjw .card i{position:absolute;left:0;bottom:0;height:3px;background:var(--br);transition:width .4s}
  .mjw .card .ci{color:var(--br);font-weight:700}
  .mjw .card .cn{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px}
  .mjw .card .cs{color:var(--mu)}
  .mjw .card .cx{margin-left:4px;color:var(--mu);font-size:15px;line-height:1;padding:0 2px}
  .mjw .card.wait{border-color:var(--am)} .mjw .card.wait .ci{color:var(--am)}
  .mjw .card.c-error{border-color:var(--bad)} .mjw .card.c-error .ci{color:var(--bad)}
  .mjw .card.c-aborted .ci{color:var(--mu)}
  .mjw .back{position:fixed;inset:0;z-index:500;background:#0007;display:flex;align-items:center;justify-content:center;padding:16px}
  .mjw .box{background:var(--b);border:1px solid var(--ln);border-radius:12px;width:min(660px,100%);
       max-height:calc(100vh - 32px);display:flex;flex-direction:column;box-shadow:0 12px 40px #0006}
  .mjw .box.narrow{width:min(470px,100%)}
  .mjw .hd{padding:16px 20px 12px;position:relative}
  .mjw .tags{display:flex;gap:6px;margin-bottom:6px}
  .mjw .tag{font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--br);border:1px solid var(--br);border-radius:5px;padding:1px 7px}
  .mjw .tag.sim{color:var(--am);border-color:var(--am)}
  .mjw .x{position:absolute;right:14px;top:12px;font:20px/1 inherit;width:30px;height:30px;border-radius:7px;
       border:1px solid transparent;background:none;color:var(--mu);cursor:pointer}
  .mjw .x:hover{border-color:var(--ln);color:var(--fg)}
  .mjw h3{margin:0;font-size:17px;padding-right:36px}
  .mjw .sub{color:var(--mu);margin-top:2px}
  .mjw .bar{height:6px;border-radius:3px;background:var(--sk);margin-top:10px;overflow:hidden}
  .mjw .bar i{display:block;height:100%;background:var(--br);transition:width .4s}
  .mjw .bar i.b-error{background:var(--bad)} .mjw .bar i.b-aborted,.mjw .bar i.b-aborting{background:var(--mu)}
  .mjw .bd{padding:0 20px;overflow:auto}
  .mjw table{width:100%;border-collapse:collapse;font-size:12.5px}
  .mjw th{text-align:left;color:var(--mu);font-weight:500;padding:6px;border-bottom:1px solid var(--ln)}
  .mjw td{padding:6px;border-bottom:1px solid var(--ln);white-space:nowrap}
  .mjw td.l{white-space:normal} .mjw td.ic{width:14px;text-align:center;color:var(--mu)}
  .mjw td em{color:var(--mu);font-size:11px}
  .mjw tr.st-done td.ic{color:var(--br)} .mjw tr.st-failed td{color:var(--bad)}
  .mjw tr.st-moving,.mjw tr.st-homing{background:var(--sk)} .mjw tr.st-moving td.ic,.mjw tr.st-homing td.ic{color:var(--am)}
  .mjw tr.st-skipped td{color:var(--mu);text-decoration:line-through}
  .mjw code{font:12px Consolas,monospace;background:var(--sk);padding:1px 5px;border-radius:4px}
  .mjw .note{color:var(--mu);font-size:12px;margin:10px 0 0}
  .mjw .result{margin:12px 0 2px;padding:9px 12px;border-radius:8px;border:1px solid var(--ln);font-weight:600}
  .mjw .r-done{color:var(--br);border-color:var(--br)} .mjw .r-error{color:var(--bad);border-color:var(--bad)}
  .mjw .ft{padding:14px 20px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .mjw .ft .sp{flex:1}
  .mjw button.b{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--ln);background:var(--b);color:var(--fg);cursor:pointer}
  .mjw button.b:hover{border-color:var(--mu)} .mjw button.b:disabled{opacity:.5;cursor:default}
  .mjw button.ok{background:var(--br);border-color:var(--br);color:#fff;font-weight:600}
  .mjw button.bad{border-color:var(--bad);color:var(--bad)}
  .mjw button.badf{background:var(--bad);border-color:var(--bad);color:#fff}
  .mjw input.why{flex:1;min-width:180px;font:inherit;padding:7px 10px;border-radius:7px;border:1px solid var(--ln);background:var(--sk);color:var(--fg)}
  .mjw .row{display:flex;gap:10px;align-items:flex-start;margin:0 0 12px;cursor:pointer}
  .mjw .row input{margin-top:3px}
  .mjw select{font:inherit;padding:2px 6px;border-radius:6px;border:1px solid var(--ln);background:var(--sk);color:var(--fg)}
  .mjw .err{color:var(--bad);font-size:12px;padding:8px 20px 0}`;
  document.head.appendChild(style);

  render();
  poll();
}());
