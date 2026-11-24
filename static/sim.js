/**
 * Simulation widget, on every page (dashboard and 3D room).
 *
 * With no Pis connected, turn simulation on: a setup job (from ACQUA or this page) then
 * moves nothing and waits. This widget pops up a window for it; "Mark as done" answers
 * the job exactly as if the rig had arrived, so ACQUA's poll sees state "done".
 *
 *   chip      bottom (dashboard) or top (3D room) centre: off / on / "setup waiting"
 *   window    the waiting job: what would move, then Done / Failed / Later
 *
 * Plain script (no modules) so both pages can include it. The <script> tag's
 * data-theme="dark|light" and data-place="bottom|top" fit it to the page.
 */
(function () {
  const me = document.currentScript;
  const theme = me.dataset.theme === 'dark' ? 'dark' : 'light';
  const place = me.dataset.place === 'top' ? 'top' : 'bottom';
  const TOKEN_KEY = 'mcc-api-token';        // same key as the rest of the UI
  const later = new Set();                   // jobs the viewer chose to answer later
  let state = { on: false, job: null };
  let openJob = null;                        // job id shown in the window
  let failing = false;                       // the "why did it fail" field is open
  let timer = null;

  const css = `
  .simw{--b:#fff;--fg:#18201f;--mu:#5f6b6a;--ln:#e2e8e7;--sk:#f0f3f3;--br:#009B85;--am:#b7791f;--bad:#d1493f;
        font:13px/1.5 'Segoe UI',system-ui,sans-serif;color:var(--fg)}
  .simw.dark{--b:#161b22;--fg:#e6e9ef;--mu:#8b93a1;--ln:#30363d;--sk:#0d1116;--am:#e3a008;--bad:#f85149}
  .simw .chip{position:fixed;left:70px;z-index:350;display:flex;align-items:center;gap:7px;cursor:pointer;
        padding:6px 12px;border-radius:999px;border:1px solid var(--ln);background:var(--b);color:var(--mu);
        box-shadow:0 2px 10px #0002;font:inherit;font-size:12px}
  .simw.bottom .chip{bottom:14px;left:calc(50% + 28px);transform:translateX(-50%)}
  .simw.top .chip{top:12px;left:50%;transform:translateX(-50%)}
  .simw .chip i{width:8px;height:8px;border-radius:50%;background:var(--mu);opacity:.5}
  .simw .chip.on{color:var(--fg);border-color:var(--am)}
  .simw .chip.on i{background:var(--am);opacity:1}
  .simw .chip.wait{background:var(--am);color:#1b1300;border-color:var(--am);font-weight:600}
  .simw .chip.wait i{background:#1b1300;animation:simp 1s infinite}
  @keyframes simp{50%{opacity:.25}}
  .simw .back{position:fixed;inset:0;z-index:500;background:#0008;display:flex;align-items:center;justify-content:center;padding:16px}
  .simw .box{background:var(--b);border:1px solid var(--ln);border-radius:12px;width:min(620px,100%);
        max-height:calc(100vh - 32px);display:flex;flex-direction:column;box-shadow:0 12px 40px #0006}
  .simw .hd{padding:16px 20px 10px}
  .simw .tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--am);
        border:1px solid var(--am);border-radius:5px;padding:1px 7px;margin-bottom:6px}
  .simw h3{margin:0;font-size:17px}
  .simw .sub{color:var(--mu);margin-top:2px}
  .simw .bd{padding:0 20px;overflow:auto}
  .simw table{width:100%;border-collapse:collapse;font-size:12.5px}
  .simw th{text-align:left;color:var(--mu);font-weight:500;padding:6px 6px;border-bottom:1px solid var(--ln)}
  .simw td{padding:6px 6px;border-bottom:1px solid var(--ln);white-space:nowrap}
  .simw td.l{white-space:normal}
  .simw code{font:12px Consolas,monospace;background:var(--sk);padding:1px 5px;border-radius:4px}
  .simw .note{color:var(--mu);font-size:12px;margin:10px 0 0}
  .simw .ft{padding:14px 20px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .simw .ft .sp{flex:1}
  .simw button.b{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--ln);background:var(--b);
        color:var(--fg);cursor:pointer}
  .simw button.b:hover{border-color:var(--mu)}
  .simw button.ok{background:var(--br);border-color:var(--br);color:#fff;font-weight:600}
  .simw button.bad{border-color:var(--bad);color:var(--bad)}
  .simw button.badf{background:var(--bad);border-color:var(--bad);color:#fff}
  .simw input.why{flex:1;min-width:180px;font:inherit;padding:7px 10px;border-radius:7px;border:1px solid var(--ln);
        background:var(--sk);color:var(--fg)}
  .simw .err{color:var(--bad);font-size:12px;padding:0 20px}
  .simw p{margin:0 0 10px}`;

  const root = document.createElement('div');
  root.className = `simw ${theme} ${place}`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);
  document.body.appendChild(root);

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (v, unit) => unit === 'deg' ? `${Math.round(v)}°` : `${Number(v).toFixed(3)} m`;
  const dur = (s) => s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`;

  async function call(method, url, body) {
    const headers = { 'Content-Type': 'application/json' };
    try { const t = localStorage.getItem(TOKEN_KEY); if (t) headers.Authorization = `Bearer ${t}`; } catch { /* no storage */ }
    const res = await fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ----- drawing -----

  function chip() {
    const waiting = state.on && state.job;
    const cls = waiting ? 'chip on wait' : state.on ? 'chip on' : 'chip';
    const text = waiting ? `Simulation · setup waiting: ${esc(state.job.name)}`
      : state.on ? 'Simulation on · Pis are not moved' : 'Simulation off';
    const tip = state.on ? 'Click for options' : 'No Pis yet? Turn on simulation to answer setup jobs by hand';
    return `<button class="${cls}" data-a="chip" title="${tip}"><i></i>${text}</button>`;
  }

  function jobWindow(job, error) {
    const rows = job.steps.map((s) => `<tr>
        <td class="l">${esc(s.label)}</td><td>${esc(s.device)}</td><td><code>${esc(s.command)}</code></td>
        <td>${s.known_from ? fmt(s.from, s.unit) : '?'} → <b>${fmt(s.to, s.unit)}</b></td></tr>`).join('');
    const unchanged = job.unchanged.length ? `<p class="note">${job.unchanged.length} more axis(es) already in place: ${esc(job.unchanged.join(', '))}</p>` : '';
    const actions = failing
      ? `<input class="why" data-k="why" placeholder="Why it failed (sent to ACQUA)" value="Simulated failure">
         <button class="b" data-a="back">Back</button><button class="b badf" data-a="fail-send">Send failure</button>`
      : `<button class="b" data-a="later" title="Close this window; the chip at the bottom opens it again">Later</button>
         <span class="sp"></span>
         <button class="b bad" data-a="fail">Mark as failed…</button>
         <button class="b ok" data-a="done">✓ Mark as done</button>`;
    return `<div class="back"><div class="box" role="dialog" aria-modal="true">
      <div class="hd"><span class="tag">SIMULATION</span>
        <h3>Setup requested: ${esc(job.name)}</h3>
        <div class="sub">from <b>${esc(job.source)}</b> · ${job.steps.length} axis move(s), one at a time,
          ≈ ${dur(job.eta)} on the real rig · nothing is sent to the Pis</div></div>
      <div class="bd">
        <table><thead><tr><th>Axis</th><th>Pi</th><th>Command</th><th>Move</th></tr></thead><tbody>${rows}</tbody></table>
        ${unchanged}
        <p class="note">“Mark as done” answers the job as if every axis arrived: whoever asked (ACQUA polls
          <code>/api/motion/jobs/${esc(job.id)}</code>) sees <code>done</code>, and the 3D room moves the objects there.</p>
      </div>
      ${error ? `<div class="err">${esc(error)}</div>` : ''}
      <div class="ft">${actions}</div></div></div>`;
  }

  function switchWindow(error) {
    const on = state.on;
    return `<div class="back"><div class="box" role="dialog" aria-modal="true" style="width:min(460px,100%)">
      <div class="hd"><span class="tag">SIMULATION</span><h3>${on ? 'Simulation is on' : 'Turn on simulation?'}</h3></div>
      <div class="bd">
        <p>With simulation on, setup jobs — from ACQUA (<code>POST /api/motion/jobs</code>) or from the 3D room —
           <b>do not move any Pi</b>. A window like this one shows each job and you mark it done or failed;
           that answer goes back to whoever asked.</p>
        <p class="note">Positions are pretend positions, kept apart from the real ones. Turn it off before connecting real Pis.</p>
      </div>
      ${error ? `<div class="err">${esc(error)}</div>` : ''}
      <div class="ft"><span class="sp"></span><button class="b" data-a="close">Close</button>
        <button class="b ${on ? 'bad' : 'ok'}" data-a="${on ? 'sim-off' : 'sim-on'}">${on ? 'Turn off' : 'Turn on'}</button></div>
    </div></div>`;
  }

  let windowKind = null;      // null | 'job' | 'switch'
  let windowError = '';

  function render() {
    const keepWhy = root.querySelector('[data-k="why"]')?.value;
    let html = chip();
    if (windowKind === 'job' && state.job) html += jobWindow(state.job, windowError);
    else if (windowKind === 'switch') html += switchWindow(windowError);
    root.innerHTML = html;
    const why = root.querySelector('[data-k="why"]');
    if (why) { if (keepWhy !== undefined) why.value = keepWhy; why.focus(); }
  }

  // ----- polling -----

  async function poll() {
    clearTimeout(timer);
    if (!document.hidden) {
      try {
        const data = await call('GET', '/api/room/simulation');
        const before = JSON.stringify([state.on, state.job?.id, state.job?.state]);
        state = { on: data.on, job: data.job };
        if (state.job && !later.has(state.job.id) && windowKind === null) {
          windowKind = 'job'; openJob = state.job.id; failing = false; windowError = '';
        }
        if (windowKind === 'job' && (!state.job || state.job.id !== openJob)) windowKind = null;
        if (before !== JSON.stringify([state.on, state.job?.id, state.job?.state])) render();
      } catch { /* server away: try again */ }
    }
    timer = setTimeout(poll, state.on ? 1000 : 4000);
  }

  // ----- actions -----

  async function act(a) {
    windowError = '';
    try {
      if (a === 'chip') {
        if (state.on && state.job) { later.delete(state.job.id); windowKind = 'job'; openJob = state.job.id; failing = false; }
        else windowKind = 'switch';
      } else if (a === 'close') windowKind = null;
      else if (a === 'later') { later.add(openJob); windowKind = null; }
      else if (a === 'fail') failing = true;
      else if (a === 'back') failing = false;
      else if (a === 'sim-on' || a === 'sim-off') {
        const data = await call('POST', '/api/room/simulation', { on: a === 'sim-on' });
        state = { on: data.on, job: data.job };
        windowKind = null;
      } else if (a === 'done' || a === 'fail-send') {
        const why = root.querySelector('[data-k="why"]')?.value || '';
        await call('POST', `/api/room/jobs/${encodeURIComponent(openJob)}/finish`,
          a === 'done' ? { result: 'done' } : { result: 'error', error: why });
        windowKind = null; failing = false;
        state.job = null;
        poll();
      }
    } catch (e) {
      windowError = e.message === 'HTTP 401' || /token/i.test(e.message)
        ? `${e.message} — enter the API token on the dashboard first` : e.message;
    }
    render();
  }

  root.addEventListener('click', (e) => {
    const b = e.target.closest('[data-a]');
    if (b) act(b.dataset.a);
  });
  // keep the 3D view (orbit / zoom) from reacting under the window
  for (const type of ['pointerdown', 'wheel', 'keydown']) root.addEventListener(type, (e) => e.stopPropagation());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  render();
  poll();
}());
