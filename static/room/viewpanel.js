/**
 * The "Show" panel in the top-left corner of the 3D view: tick what to see.
 *
 *   Distances    a reference object, and the objects whose distance to it is always shown;
 *                "while adjusting" adds a short-lived line from a moving object
 *   Circles      a quick white dashed circle around any object (own radii), and the circles that
 *                came with the setup, each on / off
 *   Setup marks  the setup's pinned distances and points, each on / off
 *   Labels       numbers only
 *
 * Only changes what this viewer sees (kept in the browser), never the setup itself.
 */
import { OBJECTS, itemKey } from './overlays.js';
import { esc } from './ui.js';

const check = (attrs, checked, text, swatch = '') => `<label class="vp-ck">
  <input type="checkbox" ${attrs}${checked ? ' checked' : ''}>${swatch ? `<i class="sw ${swatch}"></i>` : ''}<span>${text}</span></label>`;

const parseRadii = (text) => text.split(/[,\s/]+/).map(parseFloat)
  .filter((r) => Number.isFinite(r) && r > 0 && r <= 10).slice(0, 6);

export function createViewPanel(host, overlays) {
  function render() {
    const v = overlays.getView();
    const cfg = overlays.getConfig();
    const refs = overlays.refs();
    const focusKey = document.activeElement?.dataset?.qr;   // keep typing in a radius field

    const fromOptions = refs.map((r) => `<option value="${esc(r.ref)}"${r.ref === v.from ? ' selected' : ''}>${esc(r.label)}</option>`).join('');
    const targets = refs.filter((r) => r.ref !== v.from && (r.object || r.style === 'dut'))
      .map((r) => check(`data-to="${esc(r.ref)}"`, v.to.includes(r.ref), esc(r.label))).join('');

    const quickRows = Object.entries(OBJECTS).map(([key, o]) => {
      const q = v.quick[key] || { on: false, radii: [1] };
      return `<div class="vp-q">${check(`data-q="${key}"`, q.on, esc(o.label))}
        <input class="vp-r" data-qr="${key}" value="${esc(q.radii.join(', '))}" title="Radii in metres, comma separated"><em>m</em></div>`;
    }).join('');
    const setupCircles = cfg.circles.map((c) => check(`data-hide="${esc(itemKey.circle(c))}"`, !v.hidden[itemKey.circle(c)],
      `${esc(overlays.refLabel(c.at))} · ${c.radii.join(' / ')} m${c.label ? ` · ${esc(c.label)}` : ''}`)).join('');

    const marks = [
      ...cfg.distances.map((d) => check(`data-hide="${esc(itemKey.distance(d))}"`, !v.hidden[itemKey.distance(d)],
        `${esc(overlays.refLabel(d.from))} → ${esc(overlays.refLabel(d.to))}`, 'line')),
      ...cfg.points.map((p) => check(`data-hide="${esc(itemKey.point(p))}"`, !v.hidden[itemKey.point(p)],
        esc(p.label || p.id), p.style === 'dut' ? 'dut' : p.style === 'ghost' ? 'ghost' : 'pt')),
    ].join('');

    host.className = v.open ? 'open' : '';
    host.innerHTML = `
      <button class="vp-head" data-act="toggle" title="Choose what the 3D view shows">
        <b>Show</b><span>${v.open ? '▾' : '▸'}</span></button>
      ${v.open ? `<div class="vp-body">
        <section>
          <h4><i class="sw measure"></i>Distances</h4>
          <div class="vp-from">From <select data-v="from">${fromOptions}</select></div>
          <div class="vp-grid">${targets}</div>
          ${check('data-v="whileAdjusting"', v.whileAdjusting, `While adjusting an object${v.to.length ? '' : ` (to ${esc(overlays.refLabel(v.from))})`}`)}
        </section>
        <section>
          <h4><i class="sw circ"></i>Circles</h4>
          ${quickRows}
          ${setupCircles ? `<div class="vp-sub">From this setup</div>${setupCircles}` : ''}
        </section>
        ${marks ? `<section><h4>Setup marks</h4>${marks}</section>` : ''}
        <section>${check('data-v="short"', v.short, 'Short labels (numbers only)')}</section>
      </div>` : ''}`;

    if (focusKey) {
      const el = host.querySelector(`[data-qr="${focusKey}"]`);
      if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
    }
  }

  host.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="toggle"]')) overlays.setView({ open: !overlays.getView().open });
  });

  host.addEventListener('change', (e) => {
    const el = e.target;
    const v = overlays.getView();
    if (el.dataset.v === 'from') {
      overlays.setView({ from: el.value, to: v.to.filter((r) => r !== el.value) });
    } else if (el.dataset.v) {
      overlays.setView({ [el.dataset.v]: el.checked });
    } else if (el.dataset.to) {
      const to = el.checked ? [...v.to, el.dataset.to] : v.to.filter((r) => r !== el.dataset.to);
      overlays.setView({ to });
    } else if (el.dataset.hide) {
      overlays.setView({ hidden: { ...v.hidden, [el.dataset.hide]: !el.checked } });
    } else if (el.dataset.q || el.dataset.qr) {
      const key = el.dataset.q || el.dataset.qr;
      const row = host.querySelector(`[data-qr="${key}"]`);
      const radii = parseRadii(row.value);
      const prev = v.quick[key] || { on: false, radii: [1] };
      const on = el.dataset.q ? el.checked : (prev.on || radii.length > 0);
      overlays.setView({ quick: { ...v.quick, [key]: { on: on && radii.length > 0, radii: radii.length ? radii : prev.radii } } });
    }
  });

  // keep the 3D orbit / zoom from reacting while using the panel
  for (const type of ['pointerdown', 'wheel']) host.addEventListener(type, (e) => e.stopPropagation());

  overlays.onChange(render);
  render();
  return { render };
}
