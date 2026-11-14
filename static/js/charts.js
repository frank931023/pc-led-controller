'use strict';

// ===== Health history charts =====
// Drawn from GET /api/pi/<id>/health/history, so nobody has to open (or log in to) Beszel.

const CHARTS = [
  { key: 'cpu', title: 'CPU', unit: '%', max: 100 },
  { key: 'mem', title: 'Memory', unit: '%', max: 100 },
  { key: 'disk', title: 'Disk', unit: '%', max: 100 },
  { key: 'temp', title: 'Temperature', unit: '°C' },
  { key: 'load', title: 'Load (1 min)', unit: '', digits: 2 },
];
const CHART_REFRESH_MS = 60000;          // the agent sends one sample a minute
const PLOT_W = 1000, PLOT_H = 100;       // SVG units; the plot is stretched to its box

const fmtMetric = (c, v) => (v == null ? '–' : `${Number(v).toFixed(c.digits || 0)}${c.unit}`);

function fmtChartTime(t, data) {
  const long = data.to - data.from > 86400;
  return new Date(t * 1000).toLocaleString([], long
    ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
    : { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
}

/** Round axis top just above v: 1, 2, 2.5, 5 × 10^n. */
function niceMax(v) {
  if (!(v > 0)) return 1;
  const mag = 10 ** Math.floor(Math.log10(v));
  return [1, 2, 2.5, 5, 10].map((s) => s * mag).find((top) => top >= v * 1.1) || 10 * mag;
}

function openCharts(id = state.selected) {
  if (state.charts) clearTimeout(state.charts.timer);
  state.charts = { id, data: null, timer: null };
  $('#chart-title').textContent = `${label(id)} · Health history`;
  $('#charts').innerHTML = '';
  $('#chart-ranges').innerHTML = '';
  $('#chart-state').textContent = 'Loading…';
  $('#chart-ov').classList.add('on');
  loadCharts();
}

function closeCharts() {
  if (state.charts) clearTimeout(state.charts.timer);
  state.charts = null;
  $('#chart-ov').classList.remove('on');
}

function setChartRange(range) {
  if (!state.charts || range === state.chartRange) return;
  state.chartRange = range;
  $$('#chart-ranges button').forEach((b) => b.classList.toggle('on', b.dataset.range === range));
  $('#chart-state').textContent = 'Loading…';
  loadCharts();
}

async function loadCharts() {
  const view = state.charts;
  if (!view) return;
  clearTimeout(view.timer);
  const range = state.chartRange;
  const res = await api('GET',
    `/api/pi/${encodeURIComponent(view.id)}/health/history?range=${encodeURIComponent(range)}`, null, 15000);
  if (state.charts !== view || state.chartRange !== range) return;    // closed or switched meanwhile
  clearTimeout(view.timer);
  view.timer = setTimeout(loadCharts, CHART_REFRESH_MS);
  if (!res.ok) { $('#chart-state').textContent = `Cannot load the history: ${res.error}`; return; }
  view.data = res.data;
  renderCharts();
}

function renderCharts() {
  const data = state.charts.data;
  $('#chart-ranges').innerHTML = data.ranges.map((r) =>
    `<button class="${r === state.chartRange ? 'on' : ''}" data-action="chart-range" data-range="${esc(r)}">${esc(r)}</button>`).join('');
  $('#chart-state').textContent = data.points.length ? ''
    : `No samples in this range yet: one is stored every ${data.interval / 60} min.`;
  $('#charts').innerHTML = data.points.length ? CHARTS.map((c) => chartHtml(c, data)).join('') : '';
}

/** One metric as a line chart. The line breaks where samples are missing (Pi or agent down). */
function chartHtml(c, data) {
  const pts = data.points.filter((p) => p[c.key] != null);
  if (!pts.length) {
    return `<div class="chart"><div class="gh"><b>${esc(c.title)}</b><span>–</span></div>
            <div class="plot none">Not reported by this Pi</div></div>`;
  }

  const top = c.max || niceMax(Math.max(...pts.map((p) => p[c.key])));
  const x = (t) => ((t - data.from) / (data.to - data.from)) * PLOT_W;
  const y = (v) => PLOT_H - (Math.min(v, top) / top) * PLOT_H;

  const runs = [[]];
  pts.forEach((p, i) => {
    if (i && p.t - pts[i - 1].t > data.interval * 2.5) runs.push([]);
    runs[runs.length - 1].push([x(p.t), y(p[c.key])]);
  });
  const xy = ([a, b]) => `${a.toFixed(1)},${b.toFixed(1)}`;
  let line = '';
  let area = '';
  for (const run of runs) {
    if (run.length === 1) run.push([run[0][0] + 2, run[0][1]]);    // a lone sample still shows
    line += `M${run.map(xy).join('L')}`;
    area += `M${run[0][0].toFixed(1)},${PLOT_H}L${run.map(xy).join('L')}L${run[run.length - 1][0].toFixed(1)},${PLOT_H}Z`;
  }
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${PLOT_W}" y1="${PLOT_H * f}" y2="${PLOT_H * f}"/>`).join('');
  const latest = fmtMetric(c, pts[pts.length - 1][c.key]);

  return `
    <div class="chart" data-key="${c.key}" data-top="${top}">
      <div class="gh"><b>${esc(c.title)}</b><span class="cv" data-latest="${esc(latest)}">${esc(latest)}</span></div>
      <div class="plot">
        <svg viewBox="0 0 ${PLOT_W} ${PLOT_H}" preserveAspectRatio="none" aria-hidden="true">
          <g class="grid">${grid}</g>
          <line class="base" x1="0" x2="${PLOT_W}" y1="${PLOT_H}" y2="${PLOT_H}"/>
          <path class="area" d="${area}"/><path class="line" d="${line}"/>
        </svg>
        <span class="ymax">${esc(fmtMetric(c, top))}</span>
        <div class="cursor" hidden><i></i></div>
      </div>
      <div class="ax"><span>${fmtChartTime(data.from, data)}</span>
        <span>${fmtChartTime((data.from + data.to) / 2, data)}</span><span>Now</span></div>
    </div>`;
}

/** Hovering one chart shows the same moment on all of them. */
function showChartCursor(plot, clientX) {
  const data = state.charts && state.charts.data;
  if (!data || !data.points.length) return;
  const box = plot.getBoundingClientRect();
  const t = data.from + ((clientX - box.left) / box.width) * (data.to - data.from);
  const p = data.points.reduce((a, b) => (Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a));
  const left = ((p.t - data.from) / (data.to - data.from)) * 100;
  $$('#charts .chart[data-key]').forEach((card) => {
    const c = CHARTS.find((m) => m.key === card.dataset.key);
    const v = p[c.key];
    const top = Number(card.dataset.top);
    const cursor = $('.cursor', card);
    const dot = $('i', cursor);
    cursor.hidden = false;
    cursor.style.left = `${left}%`;
    dot.hidden = v == null;
    if (v != null) dot.style.top = `${100 - (Math.min(v, top) / top) * 100}%`;
    $('.cv', card).textContent = `${fmtMetric(c, v)} · ${fmtChartTime(p.t, data)}`;
  });
}

function hideChartCursor() {
  $$('#charts .chart[data-key]').forEach((card) => {
    $('.cursor', card).hidden = true;
    $('.cv', card).textContent = $('.cv', card).dataset.latest;
  });
}
