'use strict';

const DAY_MS = 24 * 60 * 60 * 1000;
// y axis: 110 % of the highest / lowest value (rounded outwards to 100),
// cut off at +/-10000 (reflectance / index x 10000)
const Y_LIMIT = 10000;
const Y_PAD = 1.1;
const Y_ROUND = 100;
const INDEX_BANDS = new Set(['NDVI', 'EVI', 'NBR', 'NDMI', 'NDWI', 'MNDWI', 'AWEI', 'MSI', 'TCB', 'TCG', 'TCW']);
const BAND_TITLES = { TCB: 'TC Brightness', TCG: 'TC Greenness', TCW: 'TC Wetness' };
const POLL_MS = 120;          // status poll interval while a job is running

// Landsat is magenta and Sentinel-2 green in both sensor modes: the
// harmonized HLS L30 / S30 points use the colours of their satellites
const COLORS = {
  LND: { face: 'rgb(197, 27, 138)', edge: 'rgb(0, 0, 0)' },
  SEN: { face: 'rgb(35, 139, 69)', edge: 'rgb(0, 0, 0)' },
};
COLORS.HLSL30 = COLORS.LND;
COLORS.HLSS30 = COLORS.SEN;

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

const map = L.map('map', { preferCanvas: true, zoomSnap: 0.5 })
  .setView([52.640564, 13.374481], 11);

// Base maps, switchable in the top-left corner. The choice is remembered in
// this browser. The scene overlay (Earth Engine tiles) always stays on top.
const TILE_OPTIONS = {
  maxZoom: 19,
  keepBuffer: 4,               // keep more tiles around the view -> smoother panning
  updateWhenZooming: false,
};
const BASE_MAPS = {
  Satellite: L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { ...TILE_OPTIONS, attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics' }),
  OpenStreetMap: L.tileLayer(
    'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    { ...TILE_OPTIONS, attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }),
  Blank: L.layerGroup(),       // no tiles: the map background is white
};
const BASE_MAP_KEY = 'eetsviewer.baseMap';

function storedBaseMap() {
  try {
    const name = localStorage.getItem(BASE_MAP_KEY);
    return BASE_MAPS[name] ? name : 'Satellite';
  } catch (err) {
    return 'Satellite';
  }
}

BASE_MAPS[storedBaseMap()].addTo(map);
map.getContainer().classList.toggle('blank-map', storedBaseMap() === 'Blank');
L.control.layers(BASE_MAPS, null, { position: 'topleft', collapsed: false }).addTo(map);
map.on('baselayerchange', (e) => {
  map.getContainer().classList.toggle('blank-map', e.name === 'Blank');
  try { localStorage.setItem(BASE_MAP_KEY, e.name); } catch (err) { /* not stored */ }
});

let clickMarker = null;
let pulseRing = null;
let overlay = null;          // Earth Engine scene shown on the map

function showLocation(latlng) {
  if (clickMarker) {
    clickMarker.setLatLng(latlng);
    pulseRing.setLatLng(latlng);
  } else {
    clickMarker = L.marker(latlng).addTo(map);
    // SVG renderer so the ring has a DOM element we can animate with CSS
    pulseRing = L.circleMarker(latlng, {
      radius: 10, color: '#fff', weight: 2, fill: false, interactive: false,
      renderer: L.svg(),
    }).addTo(map);
  }
}

function setLoading(on) {
  const el = pulseRing && pulseRing.getElement && pulseRing.getElement();
  if (el) el.classList.toggle('pulse', on);
  $('progressWrap').classList.toggle('active', on);
  $('stopButton').disabled = !on;
}

// ---------------------------------------------------------------------------
// Scene overlay: clicking an observation in the chart renders that scene
// (Earth Engine map tiles) on top of the basemap.
// ---------------------------------------------------------------------------

let overlayPoint = null;     // observation currently shown
let overlayRequest = 0;      // ignore responses of superseded requests

function fmtDate(ms) {
  return new Date(ms).toLocaleDateString('en-GB').replaceAll('/', '-');
}

function setOverlayInfo(text, isError) {
  const el = $('overlayInfo');
  el.textContent = text;
  el.classList.toggle('error', !!isError);
}

// Progress of the overlay: a box on the map and a bar under the chart.
// Step 1 is the Earth Engine request for the scene (no percentage known),
// step 2 the tiles that Earth Engine renders for the current view.
const OverlayStatus = L.Control.extend({
  options: { position: 'topright' },
  onAdd() {
    const div = L.DomUtil.create('div', 'overlay-status');
    div.hidden = true;
    div.innerHTML = '<span class="spinner"></span><span class="text"></span>';
    L.DomEvent.disableClickPropagation(div);
    return div;
  },
});
// the download block (#downloadBox in the page) lives on the map, top right
const DownloadBox = L.Control.extend({
  options: { position: 'topright' },
  onAdd() {
    const div = $('downloadBox');
    // clicks / scrolling inside the block must not reach the map
    // (a map click would select a new location)
    L.DomEvent.disableClickPropagation(div);
    L.DomEvent.disableScrollPropagation(div);
    return div;
  },
});
new DownloadBox().addTo(map);

const overlayStatus = new OverlayStatus().addTo(map);

function setOverlayProgress(text, fraction) {
  const box = overlayStatus.getContainer();
  const wrap = $('ovProgressWrap');
  const bar = $('ovProgressBar');
  if (text == null) {
    box.hidden = true;
    wrap.classList.remove('active', 'indeterminate');
    bar.style.transform = 'scaleX(0)';
    return;
  }
  box.hidden = false;
  box.querySelector('.text').textContent = text;
  wrap.classList.add('active');
  wrap.classList.toggle('indeterminate', fraction == null);
  bar.style.transform = `scaleX(${fraction == null ? 1 : fraction})`;
}

// count the tiles of the overlay layer while they load
function trackTiles(layer, what) {
  let total = 0, done = 0, failed = 0;
  const update = () => {
    const pct = total ? Math.round((100 * done) / total) : 0;
    setOverlayProgress(`Rendering ${what}: ${pct} % (${done} / ${total} tiles)`, total ? done / total : 0);
  };
  layer.on('loading', () => { total = 0; done = 0; failed = 0; update(); });
  layer.on('tileloadstart', () => { total++; update(); });
  layer.on('tileload', () => { done++; update(); });
  layer.on('tileerror', () => { done++; failed++; update(); });
  layer.on('load', () => {
    setOverlayProgress(null);
    if (layer !== overlay) return;
    if (failed) setOverlayInfo(`${what}: ${failed} of ${total} tiles failed to load`, true);
  });
}

function removeOverlay() {
  overlayRequest++;
  overlayPoint = null;
  if (overlay) { map.removeLayer(overlay); overlay = null; }
  $('clearOverlay').disabled = true;
  updateExportControls();
  setOverlayInfo('');
  setOverlayProgress(null);
  chart.update('none');
}

async function showOverlay(p) {
  const req = ++overlayRequest;
  overlayPoint = p;
  $('clearOverlay').disabled = false;
  updateExportControls();
  const what = `${p.s} ${fmtDate(p.x)}`;
  setOverlayInfo(`Loading ${what}…`);
  setOverlayProgress(`Requesting ${what} from Earth Engine…`, null);
  chart.update('none');
  try {
    const params = new URLSearchParams({ sensor: p.s, id: p.id, vis: $('visMode').value });
    const r = await fetch(`/tiles?${params}`);
    const d = await r.json();
    if (req !== overlayRequest) return;
    if (!d.url) throw new Error(d.error || 'no tile url');
    // a new layer per scene, so tile events of the previous scene cannot
    // be mixed into the progress of this one
    if (overlay) map.removeLayer(overlay);
    overlay = L.tileLayer(d.url, {
      maxZoom: 19, opacity: Number($('opacity').value), zIndex: 10,
      attribution: 'Imagery: Google Earth Engine (Copernicus Sentinel-2 / USGS Landsat / NASA HLS)',
    });
    trackTiles(overlay, what);
    overlay.addTo(map);
    setOverlayInfo(`${what} · ${p.id}`);
  } catch (err) {
    if (req !== overlayRequest) return;
    console.error('Overlay failed:', err);
    setOverlayProgress(null);
    setOverlayInfo(`Could not load ${what}: ${err.message}`, true);
  }
}

// ---------------------------------------------------------------------------
// Chart (created once, then updated in place -> smooth transitions)
// ---------------------------------------------------------------------------

// Hover/tooltip interaction that only considers the observation datasets
// (Landsat, Sentinel-2) and never the line: with dense lines (harmonic fits,
// moving average) the line would otherwise almost always be the nearest
// element, leaving the tooltip with nothing to show.
const LINE_DS = 4;
const HLS_DS = [2, 3];       // datasets of the harmonized group
Chart.Interaction.modes.nearestObservation = (chart, e) => {
  const pos = Chart.helpers.getRelativePosition(e, chart);
  let best = [], bestDist = Infinity;
  chart.data.datasets.forEach((ds, di) => {
    if (di === LINE_DS || !chart.isDatasetVisible(di)) return;
    chart.getDatasetMeta(di).data.forEach((el, index) => {
      if (!el || el.skip) return;
      const area = chart.chartArea;
      if (el.y < area.top || el.y > area.bottom) return;   // cut off: not visible
      const d = Math.abs(el.x - pos.x);
      if (d < bestDist) { bestDist = d; best = [{ element: el, datasetIndex: di, index }]; }
      else if (d === bestDist) best.push({ element: el, datasetIndex: di, index });
    });
  });
  return best;
};

// the observation shown as map overlay is drawn larger, with a thick
// bright-yellow edge that stands out against all point colours
const SELECTED_EDGE = '#ffd400';
const isSelected = (ctx) => !!ctx.raw && ctx.raw === overlayPoint;
function highlightRadius(ctx) { return isSelected(ctx) ? 9 : 5; }
function highlightEdge(ctx) { return isSelected(ctx) ? SELECTED_EDGE : 'rgb(0, 0, 0)'; }
function highlightEdgeWidth(ctx) { return isSelected(ctx) ? 3 : 1; }

const chart = new Chart($('myChart').getContext('2d'), {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: 'Landsat',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.LND.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
        order: 1,
      },
      {
        label: 'Sentinel-2',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.SEN.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
        order: 2,
      },
      {
        label: 'HLS L30 (Landsat)',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.HLSL30.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
        order: 3,
      },
      {
        label: 'HLS S30 (Sentinel-2)',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: (ctx) => (isSelected(ctx) ? 10 : 8),
        backgroundColor: COLORS.HLSS30.face,
        borderColor: highlightEdge,
        borderWidth: highlightEdgeWidth,
        hoverBorderColor: highlightEdge,
        hoverBorderWidth: highlightEdgeWidth,
        order: 4,
      },
      {
        label: 'Linear interpolation',
        type: 'line',
        data: [],
        pointRadius: 0,
        pointHoverRadius: 0,
        borderWidth: 1.5,
        borderColor: 'rgba(0, 0, 0, 0.75)',
        tension: 0,
        order: 5,
      },
    ],
  },
  options: {
    parsing: false,     // data is already {x, y} with numeric x
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 350, easing: 'easeOutCubic' },
    transitions: {
      active: { animation: { duration: 120 } },
    },
    interaction: { mode: 'nearestObservation', intersect: false },
    onClick: (e, elements) => {
      if (!elements.length) return;
      const { datasetIndex, index } = elements[0];
      const p = chart.data.datasets[datasetIndex].data[index];
      if (p && p.id) showOverlay(p);
    },
    scales: {
      x: {
        type: 'time',
        time: { unit: 'day', displayFormats: { day: 'dd-MM-yyyy', month: 'MMM yyyy' } },
        ticks: { maxTicksLimit: 10, autoSkip: true, maxRotation: 0 },
      },
      y: {
        type: 'linear',
        title: { display: true, text: 'NDVI × 10000' },
      },
    },
    plugins: {
      decimation: { enabled: false },
      legend: {
        labels: {
          filter: (item, data) => (item.datasetIndex === LINE_DS
            ? $('lineMode').value !== 'none'
            : (sensorMode() === 'harmonized') === HLS_DS.includes(item.datasetIndex)),
        },
      },
      tooltip: {
        filter: (item) => item.datasetIndex !== LINE_DS,
        callbacks: {
          title: (items) => (items.length ? fmtDate(items[0].parsed.x) : ''),
          label: (item) => `${item.raw.s || item.dataset.label}: ${item.parsed.y}`,
          footer: () => 'Click to show this scene on the map',
        },
      },
    },
  },
});

// raw points of the current job, kept sorted by date. Points are collected
// silently while the extraction runs; the chart is drawn only once it ends.
let series = [];

function insertSorted(points) {
  for (const p of points) {
    const pt = { x: p[0] * DAY_MS, y: p[1], s: p[2], d: p[0], id: p[3] };
    // binary search insertion (points mostly arrive in date order -> cheap)
    let lo = 0, hi = series.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (series[mid].x <= pt.x) lo = mid + 1; else hi = mid;
    }
    series.splice(lo, 0, pt);
  }
}

// ---------------------------------------------------------------------------
// Line modes: how the line through the observations is drawn. All are O(n)
// on the already sorted series, so switching is instant.
//
// Every line spans the whole selected date range (start to end date), not
// only the first to the last clear observation: the series is copied N
// calendar years back and forward (N = length of the date range rounded up
// to whole years, e.g. a 2018-2020 series gets copies in 2015-2017 and
// 2021-2023), the line is computed on the extended series and cut at the
// start and end dates (see Smoothing.extendPeriodic). Only the real
// observations are drawn as points and exported.
// ---------------------------------------------------------------------------

// The moving average, Savitzky–Golay and RBF lines are adaptive: their
// windows / kernel widths follow the density and distribution of the clear
// observations (see static/js/smoothing.js).
const solve = Smoothing.solve;

const days = (v) => `${Math.round(v)} d`;

// line modes whose curve comes with the parameters it used (shown in the legend)
function adaptiveMode(fit, describe) {
  return { tension: 0, adaptive: true, fit, describe };
}

// Harmonic regression (Zhu et al. 2015, https://doi.org/10.1016/j.rse.2015.02.009):
//   y = c0 + c1 cos(wt) + c2 sin(wt) + c3 t            (basic)
//       + c4 cos(2wt) + c5 sin(2wt)                     (advanced)
//       + c6 cos(3wt) + c7 sin(3wt)                     (full)
// with w = 2*pi / 365.25 and t in days. Fitted by least squares on the
// extended series (observations and their copies) and evaluated on a daily
// grid from the start to the end date.
const HARMONIC_T = 365.25;
const HARMONIC_ORDERS = { basic: 1, advanced: 2, full: 3 };
const HARMONIC_MAX_SAMPLES = 2000;   // max evaluation points (keeps long series fast)

function harmonicRow(t, nHarm) {
  // t is in days relative to the start date (better conditioning than
  // days since 1970; the shift is absorbed by the intercept and the phases).
  // The trend column is in years so all columns have similar magnitude; this
  // only rescales its coefficient, the fitted curve is identical.
  const w = 2 * Math.PI / HARMONIC_T;
  const row = [1, Math.cos(w * t), Math.sin(w * t), t / HARMONIC_T];
  for (let k = 2; k <= nHarm; k++) row.push(Math.cos(k * w * t), Math.sin(k * w * t));
  return row;
}

function harmonicFit(pts, model, range) {
  const n = pts.length;
  const nHarm = HARMONIC_ORDERS[model];
  const m = 2 + 2 * nHarm;            // number of coefficients
  if (n < m) return null;             // under-determined: not enough observations

  const t0 = range.from;
  const A = Array.from({ length: m }, () => new Float64Array(m));
  const b = new Float64Array(m);
  for (const p of pts) {
    const r = harmonicRow((p.x - t0) / DAY_MS, nHarm);
    for (let i = 0; i < m; i++) {
      b[i] += r[i] * p.y;
      for (let j = i; j < m; j++) A[i][j] += r[i] * r[j];
    }
  }
  for (let i = 0; i < m; i++) for (let j = 0; j < i; j++) A[i][j] = A[j][i];
  const c = solve(A, b);
  if (!c || !c.every(Number.isFinite)) return null;

  const spanDays = (range.to - t0) / DAY_MS;
  const step = Math.max(1, Math.ceil(spanDays / HARMONIC_MAX_SAMPLES));
  const value = (d) => {
    const r = harmonicRow(d, nHarm);
    let y = 0;
    for (let i = 0; i < m; i++) y += c[i] * r[i];
    return { x: t0 + d * DAY_MS, y: Math.round(y) };
  };
  const out = [];
  for (let d = 0; d <= spanDays; d += step) out.push(value(d));
  if (out[out.length - 1].x < range.to) out.push(value(spanDays));   // end exactly at the end date
  return out;
}

function harmonicMode(model, label) {
  return {
    label,
    tension: 0,
    data: (ext, range) => harmonicFit(ext, model, range) || [],
    // shown in the status line when the fit is not possible
    minObs: 2 + 2 * HARMONIC_ORDERS[model],
  };
}

// the observations within the range plus `k` on each side beyond it: the
// chart cuts the line off at the axis (= start and end date), and the
// points beyond give the monotone cubic its proper shape at both ends
function withMargin(pts, range, k) {
  let lo = pts.findIndex((p) => p.x >= range.from);
  if (lo < 0) lo = pts.length;
  let hi = pts.length;
  while (hi > 0 && pts[hi - 1].x > range.to) hi--;
  return pts.slice(Math.max(0, lo - k), Math.min(pts.length, hi + k));
}

const LINE_MODES = {
  linear: { label: 'Linear interpolation', tension: 0,
            data: (ext, range) => Smoothing.clipCurve(ext, range.from, range.to) },
  // Chart.js monotone cubic interpolation: smooth, passes through every
  // observation and never overshoots between them
  spline: { label: 'Spline (monotone cubic)', cubic: 'monotone',
            data: (ext, range) => withMargin(ext, range, 2) },
  movavg: adaptiveMode(
    (ext, range) => Smoothing.movingAverage(ext, range),
    (p) => `Moving average (adaptive: window ±${days(p.hMin)}–${days(p.hMax)})`),
  savgol: adaptiveMode(
    (ext, range) => Smoothing.savitzkyGolay(ext, range),
    (p) => `Savitzky–Golay (adaptive, robust: window ±${days(p.hMin)}–${days(p.hMax)})`),
  rbf: adaptiveMode(
    (ext, range) => Smoothing.rbf(ext, range),
    (p) => `RBF ensemble (σ = ${p.sigmas.join('/')} d)`),
  harmBasic:    harmonicMode('basic', 'Harmonic basic'),
  harmAdvanced: harmonicMode('advanced', 'Harmonic advanced'),
  harmFull:     harmonicMode('full', 'Harmonic full'),
  none:   { label: 'Line', tension: 0, data: () => [] },
};

// date range of the current job (ms, UTC days like the observations); the
// lines are drawn from `from` to `to`
let lineRange = null;

function currentRange() {
  if (lineRange) return lineRange;
  return series.length ? { from: series[0].x, to: series[series.length - 1].x } : null;
}

function applyLineMode(ds) {
  const key = $('lineMode').value;
  const mode = LINE_MODES[key] || LINE_MODES.linear;
  ds.cubicInterpolationMode = mode.cubic || 'default';
  ds.tension = mode.tension || 0;
  const range = currentRange();
  // observations with their copies before and after the date range
  const ext = range ? Smoothing.extendPeriodic(series, range.from, range.to) : [];
  // the minimum number of observations counts the real ones only
  const minObs = mode.adaptive ? 3 : mode.minObs || 1;
  const enough = series.length >= minObs;
  if (mode.adaptive) {
    // the curve has null values in gaps: the line is broken there
    const { data, params } = enough ? mode.fit(ext, range) : { data: [], params: null };
    ds.data = Smoothing.clipCurve(data, range ? range.from : 0, range ? range.to : 0);
    ds.label = params ? mode.describe(params) : 'Line (needs ≥ 3 obs.)';
  } else {
    ds.label = mode.label;
    ds.data = enough ? mode.data(ext, range) : [];
    if (mode.minObs && series.length && !ds.data.length) {
      ds.label += ` (needs ≥ ${mode.minObs} obs.)`;
    }
  }
  // also overrides a legend click that had hidden the line
  chart.setDatasetVisibility(LINE_DS, key !== 'none');
}

function render() {
  const lnd = [], sen = [], l30 = [], s30 = [];
  for (const p of series) {
    if (p.s.startsWith('LND')) lnd.push(p);
    else if (p.s === 'HLSL30') l30.push(p);
    else if (p.s === 'HLSS30') s30.push(p);
    else sen.push(p);
  }
  const ds = chart.data.datasets;
  ds[0].data = lnd;
  ds[1].data = sen;
  ds[2].data = l30;
  ds[3].data = s30;
  // only show the sensor datasets of the selected group
  const harmonized = sensorMode() === 'harmonized';
  for (let i = 0; i < LINE_DS; i++) chart.setDatasetVisibility(i, HLS_DS.includes(i) === harmonized);
  applyLineMode(ds[LINE_DS]);

  const { min, max } = axisRange(series.map((p) => p.y));
  chart.options.scales.y.min = min;
  chart.options.scales.y.max = max;
  chart.update();
}

// Axis range: 0 .. 110 % of the highest value for positive data, down to
// 110 % of the lowest value when there are negative values. Values beyond
// +/-10000 are cut off (they are not drawn and cannot be hovered).
function axisRange(ys) {
  if (!ys.length) return { min: 0, max: Y_LIMIT };
  let lo = Infinity, hi = -Infinity;
  for (const y of ys) { if (y < lo) lo = y; if (y > hi) hi = y; }
  const max = hi > 0 ? Math.min(Y_LIMIT, Math.ceil((hi * Y_PAD) / Y_ROUND) * Y_ROUND) : 0;
  const min = lo < 0 ? Math.max(-Y_LIMIT, Math.floor((lo * Y_PAD) / Y_ROUND) * Y_ROUND) : 0;
  return { min, max: max > min ? max : min + Y_ROUND };
}

function showChart(on) {
  $('chartWrap').hidden = !on;
  if (on) chart.resize();
}

// after "View time series", bring the chart into view if it is outside
// the visible part of the panel
function revealChart() {
  const panel = $('info-panel');
  const box = $('chartWrap').getBoundingClientRect();
  const view = panel.getBoundingClientRect();
  if (box.top < view.top || box.bottom > view.bottom) {
    $('chartWrap').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function resetChart(startDate, endDate, band) {
  series = [];
  // drop any hovered point of the previous location: its index may not exist
  // in the new data
  chart.setActiveElements([]);
  chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  const o = chart.options.scales;
  o.x.min = startDate;
  o.x.max = endDate;
  lineRange = { from: Date.parse(startDate), to: Date.parse(endDate) };
  const name = BAND_TITLES[band] || band;
  o.y.title.text = INDEX_BANDS.has(band) ? `${name} × 10000` : `${name} reflectance × 10000`;
  showChart(false);
}

// draw the collected points (called once, when the extraction has ended)
function showResults() {
  showChart(true);
  $('downloadBox').hidden = false;                // download block on the map
  render();
  revealChart();
}

// ---------------------------------------------------------------------------
// Jobs: start, stream results, cancel
// ---------------------------------------------------------------------------

let currentJob = null;
let pollTimer = null;

function readOptions() {
  const lat = $('lat').value.trim();
  const lng = $('lng').value.trim();
  const startDate = $('startDate').value;
  const endDate = $('endDate').value;
  // only the sensors of the active group (the other fieldset is disabled)
  const sensorList = Array.from(
    document.querySelectorAll('#sGroups input[name="selectedSensors"]:checked:enabled')
  ).map((el) => el.value);
  const band = $('Bands').value;
  const cloudMask = document.querySelector('input[name="cloudOption"]:checked').value;
  return { lat, lng, startDate, endDate, sensorList, band, cloudMask };
}

function validate(o, silent) {
  let msg = null;
  if (!o.lat || !o.lng) msg = 'No coordinates selected!';
  else if (!o.startDate || !o.endDate) msg = 'Please select dates';
  else if (o.startDate > o.endDate) msg = 'Start date cannot be after End date';
  else if (o.sensorList.length === 0) msg = 'Sensor list cannot be empty';
  else if ($('Bands').selectedOptions[0].hasAttribute('data-s2only')
           && !o.sensorList.some((s) => s.startsWith('SEN') || s === 'HLSS30')) {
    msg = `${o.band} exists for Sentinel-2 only: please select a Sentinel-2 sensor (Sentinel-2A/B/C or HLSS30)`;
  }
  if (msg && !silent) alert(msg);
  return !msg;
}

function setStatus(text, isError) {
  const prog = $('prog');
  prog.textContent = text;
  prog.classList.toggle('error', !!isError);
}

function setProgress(current, total) {
  const f = total > 0 ? current / total : 0;
  $('progressBar').style.transform = `scaleX(${f})`;
}

function setExportEnabled(on) {
  ['copyXbutton', 'copyYbutton', 'csvButton'].forEach((id) => { $(id).disabled = !on; });
}

async function startJob(silent) {
  const o = readOptions();
  if (!validate(o, silent)) return;

  const previous = currentJob;
  currentJob = null;
  clearTimeout(pollTimer);

  resetChart(o.startDate, o.endDate, o.band);
  setExportEnabled(false);
  setProgress(0, 1);
  setLoading(true);
  setStatus('Sending request to Earth Engine…');
  removeOverlay();

  let data;
  try {
    const r = await fetch('/sendData', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...o, cancel: previous }),
    });
    data = await r.json();
  } catch (err) {
    console.error('Error starting job:', err);
    setLoading(false);
    setStatus('Failed to start processing on the server.', true);
    return;
  }

  if (!data.job) {
    setLoading(false);
    setStatus(data.name || 'Error while processing', true);
    return;
  }
  currentJob = data.job;
  handleStatus(data, 0);
}

function handleStatus(data, cursor) {
  if (data.job !== currentJob) return;   // stale response of a superseded job

  if (data.points && data.points.length) insertSorted(data.points);
  cursor = data.cursor;
  setProgress(data.current, data.total);

  if (data.status === 'running') {
    setStatus(`${data.name}: ${data.current} / ${data.total} requests · ${data.images} images · ${data.valid} clear`);
    pollTimer = setTimeout(() => poll(data.job, cursor), POLL_MS);
    return;
  }

  setLoading(false);
  const t = data.elapsed != null ? ` in ${data.elapsed.toFixed(2)} s` : '';
  const failed = data.failed
    ? ` · ${data.failed} of ${data.total} requests failed (${data.error})` : '';
  if (data.status === 'done') {
    let msg;
    if (!data.total) msg = data.name || 'No images found';
    else if (!data.images) msg = `No images found${t}${failed}`;
    else msg = `${data.valid} clear observations out of ${data.images} images${t}${failed}`;
    setStatus(msg, !!data.failed);
    showResults();
    setExportEnabled(series.length > 0);
  } else if (data.status === 'cancelled') {
    setStatus(`Stopped: ${series.length} clear observations from ${data.current} / ${data.total} requests${t}`);
    if (series.length) showResults();
    setExportEnabled(series.length > 0);
  } else if (data.status === 'error') {
    setStatus(data.name || 'Error while processing', true);
  }
}

function poll(jobId, cursor) {
  fetch(`/status/${jobId}?since=${cursor}`)
    .then((r) => r.json())
    .then((data) => handleStatus(data, cursor))
    .catch((err) => {
      if (jobId !== currentJob) return;
      console.error(err);
      setLoading(false);
      setStatus('Lost connection to the server.', true);
    });
}

// ---------------------------------------------------------------------------
// UI wiring
// ---------------------------------------------------------------------------

function selectLocation(latlng, { pan = false } = {}) {
  $('lat').value = latlng.lat.toFixed(6);
  $('lng').value = latlng.lng.toFixed(6);
  $('lat').classList.remove('invalid');
  $('lng').classList.remove('invalid');
  showLocation(latlng);
  if (pan) map.setView(latlng, Math.max(map.getZoom(), 14));
}

map.on('click', (e) => selectLocation(e.latlng));

// Manual coordinate entry: the "Go to" button pins the typed location.
// Pasting "lat,lng" (e.g. 52.11354,8.27282) into either field fills both.
function splitPair(input) {
  const parts = input.value.split(/[,;\s]+/).filter(Boolean);
  if (parts.length === 2) {
    $('lat').value = parts[0];
    $('lng').value = parts[1];
    return true;
  }
  return false;
}

['lat', 'lng'].forEach((id) => {
  $(id).addEventListener('input', (e) => {
    e.target.classList.remove('invalid');
    if (e.target.value.includes(',')) splitPair(e.target);
  });
});

// Validate the typed coordinates; returns an L.LatLng or null (and flags errors).
function readTypedCoords() {
  splitPair($('lat'));
  const latStr = $('lat').value.trim();
  const lngStr = $('lng').value.trim();
  const num = /^[-+]?\d+(\.\d+)?$/;
  const lat = Number(latStr);
  const lng = Number(lngStr);
  const latOk = num.test(latStr) && lat >= -90 && lat <= 90;
  const lngOk = num.test(lngStr) && lng >= -180 && lng <= 180;
  $('lat').classList.toggle('invalid', !latOk);
  $('lng').classList.toggle('invalid', !lngOk);
  if (!latOk || !lngOk) {
    setStatus('Invalid coordinates: latitude must be -90..90 and longitude -180..180 (decimal degrees)', true);
    return null;
  }
  return L.latLng(lat, lng);
}

$('coordForm').addEventListener('submit', (e) => {
  e.preventDefault();
  const latlng = readTypedCoords();
  if (!latlng) return;
  setStatus('');
  selectLocation(latlng, { pan: true });
});

$('getData').addEventListener('click', () => {
  // If coordinates were typed but not pinned yet, pin them first.
  if ($('lat').value.trim() || $('lng').value.trim()) {
    const latlng = readTypedCoords();
    if (!latlng) return;
    if (!clickMarker || !clickMarker.getLatLng().equals(latlng, 1e-6)) {
      selectLocation(latlng, { pan: true });
    }
  }
  startJob(false);
});

// Enter anywhere on the page acts like clicking "View time series".
// Enter on a focused button or link keeps its own action, and the "Go to"
// button still only pins the location. Enter in the lat/lng fields now
// starts the extraction (it pins the typed coordinates first).
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' || e.repeat || e.isComposing) return;
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
  const t = e.target;
  if (t instanceof Element) {
    if (t.closest('button, a[href], textarea, summary, [contenteditable=""], [contenteditable="true"], #map')) return;
  }
  e.preventDefault(); // stops the coordinate form's implicit "Go to" submit
  $('getData').click();
});

// the band box is narrow: show the full name of the chosen band on hover
function syncBandTitle() {
  const sel = $('Bands');
  sel.title = sel.options[sel.selectedIndex]?.text || '';
}
$('Bands').addEventListener('change', syncBandTitle);
syncBandTitle();

// redraw the line in the chosen mode; no new extraction needed
$('lineMode').addEventListener('change', () => {
  if (!$('chartWrap').hidden) render();
});

// Stop: ask the server to cancel; the polling loop then receives the
// 'cancelled' status and draws whatever was extracted so far.
$('stopButton').addEventListener('click', async () => {
  const jobId = currentJob;
  if (!jobId) return;
  $('stopButton').disabled = true;
  setStatus('Stopping…');
  try {
    await fetch(`/cancel/${jobId}`, { method: 'POST' });
  } catch (err) {
    console.error('Error stopping job:', err);
  }
});

// export buttons: one listener each, reading the *current* data on click
function flash(button, text) {
  // keep the original content (text and icon) to restore it
  const original = button.dataset.html || button.innerHTML;
  button.dataset.html = original;
  button.textContent = text;
  button.classList.add('copied');
  setTimeout(() => {
    button.innerHTML = original;
    button.classList.remove('copied');
  }, 1000);
}

async function copyArray(button, values) {
  try {
    await navigator.clipboard.writeText(`[${values.join(', ')}]`);
    flash(button, 'Copied!');
  } catch (err) {
    console.error('Copy failed:', err);
  }
}

$('copyXbutton').addEventListener('click', (e) => copyArray(e.currentTarget, series.map((p) => p.d)));
$('copyYbutton').addEventListener('click', (e) => copyArray(e.currentTarget, series.map((p) => p.y)));

// Save the graph as an image: drawn once more at (at least) 2 x resolution
// on a white background, then the chart goes back to screen resolution.
const IMAGE_SCALE = 2;

function exportBaseName() {
  const o = readOptions();
  return `eetsviewer_${o.lat}_${o.lng}_${o.band}_${o.startDate}_${o.endDate}`;
}

function saveChartImage(format, button) {
  const type = format === 'jpeg' ? 'image/jpeg' : 'image/png';
  const previous = chart.options.devicePixelRatio;
  // no tooltip or hover highlight in the image
  chart.setActiveElements([]);
  chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  chart.options.devicePixelRatio = Math.max(IMAGE_SCALE, window.devicePixelRatio || 1);
  chart.resize();
  chart.update('none');
  const out = document.createElement('canvas');
  out.width = chart.canvas.width;
  out.height = chart.canvas.height;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.drawImage(chart.canvas, 0, 0);
  // back to screen resolution
  chart.options.devicePixelRatio = previous;
  chart.resize();
  chart.update('none');
  out.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${exportBaseName()}.${format === 'jpeg' ? 'jpg' : 'png'}`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    flash(button, 'Saved!');
  }, type, 0.95);
}

$('savePng').addEventListener('click', (e) => saveChartImage('png', e.currentTarget));
$('saveJpeg').addEventListener('click', (e) => saveChartImage('jpeg', e.currentTarget));

$('csvButton').addEventListener('click', (e) => {
  const o = readOptions();
  const rows = ['date,days_since_epoch,sensor,image_id,' + o.band];
  for (const p of series) {
    rows.push(`${new Date(p.x).toISOString().slice(0, 10)},${p.d},${p.s},${p.id},${p.y}`);
  }
  const blob = new Blob([rows.join('\n') + '\n'], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `eetsviewer_${o.lat}_${o.lng}_${o.band}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  flash(e.currentTarget, 'Saved!');
});

// overlay controls
$('visMode').addEventListener('change', () => { if (overlayPoint) showOverlay(overlayPoint); });
$('opacity').addEventListener('input', (e) => { if (overlay) overlay.setOpacity(Number(e.target.value)); });
$('clearOverlay').addEventListener('click', removeOverlay);

// sensor groups: Individual or Harmonized; the other group is greyed out
function sensorMode() {
  const el = document.querySelector('input[name="sensorMode"]:checked');
  return el ? el.value : 'individual';
}

function applySensorMode() {
  const mode = sensorMode();
  document.querySelectorAll('#sGroups .sensorGroup').forEach((fs) => {
    fs.disabled = fs.dataset.group !== mode;
  });
}

document.querySelectorAll('input[name="sensorMode"]').forEach((el) => {
  el.addEventListener('change', applySensorMode);
});
applySensorMode();

// ---------------------------------------------------------------------------
// Download of the overlaid scene as GeoTIFF: the server reads it from Earth
// Engine block by block and writes the file; the browser only polls the
// progress and saves the finished file.
// ---------------------------------------------------------------------------

let exportJob = null;      // id of the running export on the server
let exportBusy = false;    // a download is being prepared or running
let exportToken = 0;       // bumped by Cancel: ignores a request still being prepared
let exportTimer = null;

// A final message (saved / cancelled / failed) stays until the next action
// anywhere on the page: a click, a key press or zooming with the wheel.
let clearInfoOnNextAction = null;

function setFinalExportInfo(text, isError) {
  setExportInfo(text, isError);
  // registered after this event loop turn, so the click that ended the
  // download (e.g. Cancel) does not clear its own message
  setTimeout(() => {
    if (clearInfoOnNextAction) return;
    clearInfoOnNextAction = () => {
      for (const ev of ['pointerdown', 'keydown', 'wheel']) {
        document.removeEventListener(ev, clearInfoOnNextAction, true);
      }
      clearInfoOnNextAction = null;
      if (!exportBusy) setExportInfo('');
    };
    for (const ev of ['pointerdown', 'keydown', 'wheel']) {
      document.addEventListener(ev, clearInfoOnNextAction, true);
    }
  }, 0);
}

function setExportInfo(text, isError) {
  const el = $('exportInfo');
  el.textContent = text;
  el.classList.toggle('error', !!isError);
}

// fraction: 0..1, null = not known yet (sliding bar), false = hidden
function setExportProgress(fraction) {
  const wrap = $('exProgressWrap');
  wrap.classList.toggle('active', fraction !== false);
  wrap.classList.toggle('indeterminate', fraction === null);
  const scale = typeof fraction === 'number' ? fraction : (fraction === null ? 1 : 0);
  $('exProgressBar').style.transform = `scaleX(${scale})`;
}

// Download controls: usable only while a scene is overlaid. While a
// download runs, the button cancels it and the options are locked.
// Scene overlay card: greyed out with disabled controls while no scene is
// selected (like the download card)
function updateOverlayCard() {
  const scene = !!overlayPoint;
  $('overlayRow').classList.toggle('inactive', !scene);
  $('visMode').disabled = !scene;
  $('opacity').disabled = !scene;
}

function updateExportControls() {
  const running = exportBusy;
  const scene = !!overlayPoint;
  updateOverlayCard();
  updateSelectedInfo();
  $('downloadOverlay').disabled = !running && !scene;
  $('exportData').disabled = running || !scene;
  $('exportExtent').disabled = running || !scene;
}

// sensor code -> label shown in the sensor checkboxes (e.g. "Sentinel-2A")
const SENSOR_LABELS = Object.fromEntries(
  Array.from(document.querySelectorAll('input[name="selectedSensors"]'))
    .map((el) => [el.value, el.parentElement.textContent.trim()]));

let shownScene = null;     // scene + options the file name was fetched for
let nameRequest = 0;

// long ids and file names wrap after an underscore, not inside a word
function setBreakable(el, text) {
  el.textContent = '';
  String(text).split(/(?<=_)/).forEach((part, i) => {
    if (i) el.appendChild(document.createElement('wbr'));
    el.appendChild(document.createTextNode(part));
  });
}

// sensor chip in the card header, in the colour of its points in the chart
function setBadge(sensor) {
  const b = $('dbBadge');
  b.textContent = sensor ? (SENSOR_LABELS[sensor] || sensor) : '';
  b.classList.toggle('lnd', !!sensor && (sensor.startsWith('LND') || sensor === 'HLSL30'));
  b.classList.toggle('sen', !!sensor && (sensor.startsWith('SEN') || sensor === 'HLSS30'));
}

// name of the selected image in the download block: sensor and date, the
// Earth Engine image id and the name of the file the download will get
function updateSelectedInfo() {
  const box = $('downloadBox');
  const p = overlayPoint;
  box.classList.toggle('empty', !p);
  if (!p) {
    shownScene = null;
    nameRequest++;
    $('dbScene').textContent = 'No image selected';
    setBadge(null);
    $('dbId').textContent = '';
    setFileName('');
    updateCloudCover();
    sceneRequest++;
    setSize('');
    return;
  }
  $('dbScene').textContent = new Date(p.x).toLocaleDateString('en-GB',
    { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  setBadge(p.s);
  setBreakable($('dbId'), p.id);
  $('dbId').title = p.id;
  updateCloudCover();
  loadSceneInfo(p);
  const mask = exportMask();
  const key = `${p.s}/${p.id}/${mask}`;
  if (key === shownScene) return;
  shownScene = key;
  const req = ++nameRequest;
  setFileName('…');
  const params = new URLSearchParams({ sensor: p.s, id: p.id, mask: mask == null ? 'raw' : mask });
  fetch(`/export/name?${params}`)
    .then((r) => r.json())
    .then((d) => {
      if (req !== nameRequest) return;
      if (d.name) setFileName(d.name);
      else setFileName(`unavailable: ${d.error || 'unknown error'}`, true);
    })
    .catch((err) => { if (req === nameRequest) setFileName(`unavailable: ${err.message}`, true); });
}

// Cloud cover of the selected scene: pixels removed by the Cloud mask of
// the panel (None -> Default, as for the cloud-masked download) / valid
// pixels, computed by Earth Engine for the whole scene
const MASK_LABELS = { 1: 'Default', 2: 'Aggressive' };
let shownCloud = null;
let cloudRequest = 0;
const cloudResults = new Map();   // scene/level -> result, so switching back is instant

function showCloud(el, label, d) {
  if (d.cloud != null) {
    el.innerHTML = `<div class="db-cloud-row"><span class="db-cloud-value">${d.cloud.toFixed(1)} %</span>` +
      `<span class="db-cloud-mask">${label}</span></div>` +
      `<div class="db-meter"><span style="width:${Math.min(100, Math.max(0, d.cloud))}%"></span></div>`;
    el.title = `${d.valid_pixels.toLocaleString('en')} valid pixels on a ${d.scale} m grid; ` +
               'masked pixels (clouds, shadows, snow, ...) / valid pixels of the whole scene';
  } else {
    el.textContent = `unavailable (${d.error || 'no valid pixels'})`;
    el.classList.add('error');
  }
}

function updateCloudCover() {
  const el = $('dbCloud');
  const p = overlayPoint;
  if (!p) {
    shownCloud = null;
    cloudRequest++;
    el.textContent = '';
    return;
  }
  const panel = Number(document.querySelector('input[name="cloudOption"]:checked').value);
  const level = panel >= 1 ? panel : 1;
  const key = `${p.s}/${p.id}/${level}`;
  if (key === shownCloud) return;
  shownCloud = key;
  const req = ++cloudRequest;
  const label = `${MASK_LABELS[level]} mask`;
  el.classList.remove('error');
  el.title = '';
  if (cloudResults.has(key)) { showCloud(el, label, cloudResults.get(key)); return; }
  el.innerHTML = `<div class="db-cloud-row"><span class="db-cloud-mask">computing…</span></div>`;
  const params = new URLSearchParams({ sensor: p.s, id: p.id, mask: level });
  fetch(`/export/cloud?${params}`)
    .then((r) => r.json())
    .then((d) => {
      if (d.cloud != null) cloudResults.set(key, d);
      if (req !== cloudRequest) return;
      showCloud(el, label, d);
    })
    .catch((err) => {
      if (req !== cloudRequest) return;
      el.textContent = `unavailable (${err.message})`;
      el.classList.add('error');
    });
}

// Size of the download: the pixel grid the server would write for the
// selected Area, computed in the browser (static/js/grid.js) from the
// scene's footprint and projection, so it follows panning / zooming
// instantly. The size in MB is uncompressed (the file is compressed).
const sceneInfos = new Map();     // sensor/id -> {bbox, resolution, bands, max_side}
let sceneRequest = 0;

function loadSceneInfo(p) {
  const key = `${p.s}/${p.id}`;
  if (sceneInfos.has(key)) { updateSizeEstimate(); return; }
  const req = ++sceneRequest;
  setSize('Size: …');
  fetch(`/export/scene?${new URLSearchParams({ sensor: p.s, id: p.id })}`)
    .then((r) => r.json())
    .then((d) => {
      if (d.bbox) sceneInfos.set(key, d);
      if (req !== sceneRequest) return;
      if (d.bbox) updateSizeEstimate();
      else setSize(`Size unavailable: ${d.error || 'unknown error'}`, true);
    })
    .catch((err) => { if (req === sceneRequest) setSize(`Size unavailable: ${err.message}`, true); });
}

function setSize(text, isError) {
  const el = $('dbSize');
  el.textContent = text;
  el.classList.toggle('error', !!isError);
}

function updateSizeEstimate() {
  const p = overlayPoint;
  if (!p) { setSize(''); return; }
  const info = sceneInfos.get(`${p.s}/${p.id}`);
  if (!info) return;
  const fullScene = $('exportExtent').value === 'scene';
  let view = null;
  if (!fullScene) {
    const v = map.getBounds();
    view = [Math.max(-180, v.getWest()), Math.max(-90, v.getSouth()),
            Math.min(180, v.getEast()), Math.min(90, v.getNorth())];
  }
  const what = fullScene ? 'Full scene' : 'Map view';
  const r = Grid.estimate(info, view);
  if (r.empty) { setSize(`${what}: the view does not cover this scene`, true); return; }
  const fmt = (v) => v.toLocaleString('en');
  const mb = r.bytes / 1e6;
  const size = mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.max(1, Math.round(mb))} MB`;
  if (r.tooLarge) {
    setSize(`${what}: ${fmt(r.width)} × ${fmt(r.height)} px, larger than the ` +
            `${fmt(info.max_side)} px limit${fullScene ? '' : ': zoom in'}`, true);
  } else {
    setSize(`${what}: ${fmt(r.width)} × ${fmt(r.height)} px · ${info.bands} bands · ${size}`);
  }
}

map.on('moveend', () => { if (overlayPoint) updateSizeEstimate(); });

function setFileName(text, isError) {
  const el = $('dbFile');
  setBreakable(el, text);
  el.classList.toggle('error', !!isError);
}

function setExportRunning(on) {
  const b = $('downloadOverlay');
  b.classList.toggle('running', on);
  $('downloadLabel').textContent = on ? 'Cancel download' : 'Download selected image';
  exportBusy = on;
  updateExportControls();
}

function mapBounds() {
  const b = map.getBounds();
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  return [clamp(b.getWest(), -180, 180), clamp(b.getSouth(), -90, 90),
          clamp(b.getEast(), -180, 180), clamp(b.getNorth(), -90, 90)];
}

const MASK_NAMES = { 1: 'Default', 2: 'Aggressive' };

// cloud mask of the download: null = original, otherwise the Cloud mask of the
// panel (None -> Default)
function exportMask() {
  if ($('exportData').value !== 'masked') return null;
  const level = Number(document.querySelector('input[name="cloudOption"]:checked').value);
  return level >= 1 ? level : 1;
}

async function startExport() {
  const p = overlayPoint;
  if (!p) return;
  const mask = exportMask();
  const token = ++exportToken;
  setExportRunning(true);
  setExportProgress(null);
  setExportInfo(`Preparing ${p.s} ${fmtDate(p.x)}…`);
  let d;
  try {
    const r = await fetch('/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sensor: p.s, id: p.id, extent: $('exportExtent').value, bounds: mapBounds(), mask }),
    });
    d = await r.json();
  } catch (err) {
    d = { status: 'error', error: err.message };
  }
  if (token !== exportToken) {           // cancelled while it was being prepared
    if (d.job) fetch(`/export/${d.job}/cancel`, { method: 'POST' }).catch(() => {});
    return;
  }
  if (!d.job) {
    setExportRunning(false);
    setExportProgress(false);
    setFinalExportInfo(`Download failed: ${d.error || 'unknown error'}`, true);
    return;
  }
  exportJob = d.job;
  exportStatus(d);
}

function exportStatus(d) {
  if (d.job !== exportJob) return;
  const kind = d.mask ? `cloud masked (${MASK_NAMES[d.mask]}${d.buffer ? `, +${d.buffer} px buffer` : ''})` : 'original';
  if (d.status === 'running') {
    const pct = d.total ? Math.round((100 * d.current) / d.total) : 0;
    setExportProgress(d.total ? d.current / d.total : 0);
    setExportInfo(`Reading from Earth Engine · ${pct} % · ${d.current} / ${d.total} blocks`);
    exportTimer = setTimeout(() => pollExport(d.job), 500);
    return;
  }
  exportJob = null;
  setExportRunning(false);
  setExportProgress(false);
  if (d.status === 'done') {
    setFinalExportInfo(`Saved ${d.name} (${kind}, ${d.width} × ${d.height} px) in ${d.elapsed.toFixed(1)} s`);
    const a = document.createElement('a');
    a.href = `/export/${d.job}/file`;
    a.download = d.name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (d.status === 'error') {
    setFinalExportInfo(`Download failed: ${d.error}`, true);
  } else {
    setFinalExportInfo('Download cancelled');
  }
}

function pollExport(jobId) {
  fetch(`/export/${jobId}`)
    .then((r) => r.json())
    .then(exportStatus)
    .catch((err) => {
      if (jobId !== exportJob) return;
      exportJob = null;
      setExportRunning(false);
      setExportProgress(false);
      setFinalExportInfo(`Lost connection to the server: ${err.message}`, true);
    });
}

$('downloadOverlay').addEventListener('click', async () => {
  if (!exportBusy) { startExport(); return; }
  const jobId = exportJob;
  exportToken++;
  exportJob = null;
  clearTimeout(exportTimer);
  setExportRunning(false);
  setExportProgress(false);
  setFinalExportInfo('Download cancelled');
  if (!jobId) return;                     // still preparing: startExport cancels it
  try { await fetch(`/export/${jobId}/cancel`, { method: 'POST' }); } catch (err) { /* ignore */ }
});

// the file name depends on Original / Cloud masked and on the Cloud mask level
$('exportData').addEventListener('change', updateSelectedInfo);
document.querySelectorAll('input[name="cloudOption"]').forEach((el) => {
  el.addEventListener('change', updateSelectedInfo);
});

// the size estimate depends on Map view / Full scene
$('exportExtent').addEventListener('change', updateSizeEstimate);
