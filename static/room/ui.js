/**
 * Small helpers: dialogs, formatting, toasts, JSON requests.
 *
 * No window.confirm(): the move dialog has more to say than one line (which axis, from
 * where to where, how far, roughly how long, which firmware command), and confirm()
 * freezes the page, 3D render loop included.
 */

// ── Formatting ──────────────────────────────────
export const fmt = {
  /** position: signed, two decimals so columns line up */
  pos: (v) => `${v >= 0 ? '+' : ''}${v.toFixed(2)} m`,
  len: (v) => `${v.toFixed(2)} m`,
  deg: (v) => `${v.toFixed(1)}°`,
  by: (unit) => (unit === 'deg' ? fmt.deg : fmt.len),
  secs: (s) => (s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`),
};

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** The control center answers {status: "success", ...} or {status: "error", error, problems?}. */
export const isOk = (d) => d && d.status === 'success';

/** One readable line from an error answer, including the per-axis problems. */
export const errorText = (d, fallback = 'Request failed') => {
  const problems = (d && d.problems) || [];
  return [(d && d.error) || fallback, ...problems].join(' · ');
};

// Same storage key as the main page (static/js/core.js): the token typed there works here.
const TOKEN_KEY = 'mcc-api-token';
const storedToken = () => { try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; } };

/**
 * fetch + JSON that recognises when the answer is not JSON (e.g. an HTML error page from
 * a server still running older code) and says so, instead of "Unexpected token '<'".
 * Error answers (4xx/5xx with a JSON body) are returned, not thrown: their body says why.
 */
export async function jsonFetch(url, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (storedToken()) headers.Authorization = `Bearer ${storedToken()}`;
  let r;
  try {
    r = await fetch(url, { ...opts, headers });
  } catch {
    throw new Error(`Cannot reach ${url} (is the control center running?)`);
  }
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(r.status === 404
      ? `The server has no ${url}: it is running older code, restart / rebuild it`
      : `The server did not answer with JSON (HTTP ${r.status})`);
  }
}

// ── Dialogs ─────────────────────────────────────
function shell(inner) {
  const ov = document.createElement('div');
  ov.className = 'ov on';
  ov.innerHTML = `<div class="bx">${inner}</div>`;
  document.body.appendChild(ov);
  return ov;
}

function teardown(ov, onKey) {
  document.removeEventListener('keydown', onKey);
  ov.remove();
}

/** Confirmation dialog. Resolves true / false. `html` must already be escaped. */
export function confirmBox({ title, html = '', ok = 'OK', cancel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    const ov = shell(`
      <h3>${esc(title)}</h3>
      <div class="bxbody">${html}</div>
      <div class="acts">
        <button class="ghost" data-a="no">${esc(cancel)}</button>
        <button class="${danger ? 'danger' : 'primary'}" data-a="yes">${esc(ok)}</button>
      </div>`);

    const done = (v) => { teardown(ov, onKey); resolve(v); };
    const onKey = (e) => {
      if (e.key === 'Escape') done(false);
      if (e.key === 'Enter') done(true);
    };
    document.addEventListener('keydown', onKey);
    ov.querySelector('[data-a="no"]').onclick = () => done(false);
    ov.querySelector('[data-a="yes"]').onclick = () => done(true);
    ov.onclick = (e) => { if (e.target === ov) done(false); };
    ov.querySelector('[data-a="yes"]').focus();
  });
}

/**
 * Form dialog. fields = [{key, label, placeholder, value, textarea, required}].
 * Resolves {key: value} or null when cancelled.
 */
export function formBox({ title, note = '', fields = [], ok = 'Save' } = {}) {
  return new Promise((resolve) => {
    const rows = fields.map((f) => `
      <label>${esc(f.label)}${f.required ? ' <span class="req">*</span>' : ''}</label>
      ${f.textarea
        ? `<textarea rows="3" data-k="${esc(f.key)}"
             placeholder="${esc(f.placeholder || '')}">${esc(f.value || '')}</textarea>`
        : `<input type="text" data-k="${esc(f.key)}"
             placeholder="${esc(f.placeholder || '')}" value="${esc(f.value || '')}">`}`).join('');

    const ov = shell(`
      <h3>${esc(title)}</h3>
      ${note ? `<div class="bxnote">${note}</div>` : ''}
      ${rows}
      <div class="err" style="display:none"></div>
      <div class="acts">
        <button class="ghost" data-a="no">Cancel</button>
        <button class="primary" data-a="yes">${esc(ok)}</button>
      </div>`);

    const done = (v) => { teardown(ov, onKey); resolve(v); };
    const collect = () => {
      const out = {};
      ov.querySelectorAll('[data-k]').forEach((el) => { out[el.dataset.k] = el.value.trim(); });
      const missing = fields.filter((f) => f.required && !out[f.key]);
      if (missing.length) {
        const err = ov.querySelector('.err');
        err.textContent = `${missing[0].label} is required`;
        err.style.display = '';
        ov.querySelector(`[data-k="${missing[0].key}"]`).focus();
        return null;
      }
      return out;
    };
    const submit = () => { const v = collect(); if (v) done(v); };

    const onKey = (e) => {
      if (e.key === 'Escape') done(null);
      if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') submit();
    };
    document.addEventListener('keydown', onKey);
    ov.querySelector('[data-a="no"]').onclick = () => done(null);
    ov.querySelector('[data-a="yes"]').onclick = submit;
    ov.onclick = (e) => { if (e.target === ov) done(null); };
    ov.querySelector('[data-k]')?.focus();
  });
}

/** Short notice in the corner. Never alert(): it would freeze the 3D view. */
export function toast(msg, kind = 'ok', ms = 3600) {
  let host = document.querySelector('#toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toasts';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 260); }, ms);
}
