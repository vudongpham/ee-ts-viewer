'use strict';

const DAY_MS = 24 * 60 * 60 * 1000;
// y axis: 110 % of the highest / lowest value (rounded outwards to 100),
// cut off at +/-10000 (reflectance / index x 10000)
const Y_LIMIT = 10000;
const Y_PAD = 1.1;
const Y_ROUND = 100;
const INDEX_BANDS = new Set(['NDVI', 'EVI', 'NBR', 'NDMI', 'NDWI', 'TCB', 'TCG', 'TCW']);
const BAND_TITLES = { TCB: 'TC Brightness', TCG: 'TC Greenness', TCW: 'TC Wetness' };
const POLL_MS = 120;          // status poll interval while a job is running

const COLORS = {
  LND: { face: 'rgb(197, 27, 138)', edge: 'rgb(0, 0, 0)' },
  SEN: { face: 'rgb(35, 139, 69)', edge: 'rgb(0, 0, 0)' },
  HLSL30: { face: 'rgb(230, 145, 20)', edge: 'rgb(0, 0, 0)' },
  HLSS30: { face: 'rgb(33, 113, 181)', edge: 'rgb(0, 0, 0)' },
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Map
// ---------------------------------------------------------------------------

const map = L.map('map', { preferCanvas: true, zoomSnap: 0.5 })
  .setView([52.640564, 13.374481], 11);

L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {
    maxZoom: 19,
    keepBuffer: 4,               // keep more tiles around the view -> smoother panning
    updateWhenZooming: false,
    attribution: 'Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics',
  }
).addTo(map);

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
  setOverlayInfo('');
  setOverlayProgress(null);
  chart.update('none');
}

async function showOverlay(p) {
  const req = ++overlayRequest;
  overlayPoint = p;
  $('clearOverlay').disabled = false;
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

// the observation shown as map overlay is drawn larger
function highlightRadius(ctx) {
  return ctx.raw && ctx.raw === overlayPoint ? 9 : 5;
}

const chart = new Chart($('myChart').getContext('2d'), {
  type: 'scatter',
  data: {
    datasets: [
      {
        label: 'Landsat',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: 8,
        backgroundColor: COLORS.LND.face,
        borderColor: COLORS.LND.edge,
        borderWidth: 1,
        order: 1,
      },
      {
        label: 'Sentinel-2',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: 8,
        backgroundColor: COLORS.SEN.face,
        borderColor: COLORS.SEN.edge,
        borderWidth: 1,
        order: 2,
      },
      {
        label: 'HLS L30 (Landsat)',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: 8,
        backgroundColor: COLORS.HLSL30.face,
        borderColor: COLORS.HLSL30.edge,
        borderWidth: 1,
        order: 3,
      },
      {
        label: 'HLS S30 (Sentinel-2)',
        data: [],
        pointRadius: highlightRadius,
        pointHoverRadius: 8,
        backgroundColor: COLORS.HLSS30.face,
        borderColor: COLORS.HLSS30.edge,
        borderWidth: 1,
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
// ---------------------------------------------------------------------------

const MOVAVG_WINDOW = 5;     // observations, centred
const SAVGOL_WINDOW = 7;     // observations, centred
const SAVGOL_ORDER = 2;      // local polynomial degree

// centred moving average over a fixed number of observations
// (the window shrinks symmetrically at the ends of the series)
function movingAverage(pts, window) {
  const half = window >> 1;
  const n = pts.length;
  const prefix = new Float64Array(n + 1);
  for (let i = 0; i < n; i++) prefix[i + 1] = prefix[i] + pts[i].y;
  return pts.map((p, i) => {
    const h = Math.min(half, i, n - 1 - i);
    const sum = prefix[i + h + 1] - prefix[i - h];
    return { x: p.x, y: Math.round(sum / (2 * h + 1)) };
  });
}

// Savitzky–Golay style smoothing for irregularly spaced data: a least-squares
// polynomial is fitted to the observations in a centred window (using the real
// acquisition dates) and evaluated at the centre point.
function savitzkyGolay(pts, window, order) {
  const half = window >> 1;
  const n = pts.length;
  return pts.map((p, i) => {
    const lo = Math.max(0, i - half);
    const hi = Math.min(n - 1, i + half);
    const deg = Math.min(order, hi - lo);   // never more terms than points
    if (deg < 1) return { x: p.x, y: p.y };
    const m = deg + 1;
    // normal equations A c = b, x in days relative to the centre point
    const A = Array.from({ length: m }, () => new Float64Array(m));
    const b = new Float64Array(m);
    for (let k = lo; k <= hi; k++) {
      const t = (pts[k].x - p.x) / DAY_MS;
      const pw = new Float64Array(2 * m - 1);
      pw[0] = 1;
      for (let j = 1; j < pw.length; j++) pw[j] = pw[j - 1] * t;
      for (let r = 0; r < m; r++) {
        b[r] += pw[r] * pts[k].y;
        for (let c = 0; c < m; c++) A[r][c] += pw[r + c];
      }
    }
    const c = solve(A, b);
    // c[0] is the fitted value at t = 0, i.e. at the centre observation
    return { x: p.x, y: c && Number.isFinite(c[0]) ? Math.round(c[0]) : p.y };
  });
}

// tiny Gaussian elimination with partial pivoting (m <= 8 here)
function solve(A, b) {
  const m = b.length;
  for (let col = 0; col < m; col++) {
    let piv = col;
    for (let r = col + 1; r < m; r++) if (Math.abs(A[r][col]) > Math.abs(A[piv][col])) piv = r;
    if (Math.abs(A[piv][col]) < 1e-12) return null;
    [A[col], A[piv]] = [A[piv], A[col]];
    [b[col], b[piv]] = [b[piv], b[col]];
    for (let r = col + 1; r < m; r++) {
      const f = A[r][col] / A[col][col];
      for (let c = col; c < m; c++) A[r][c] -= f * A[col][c];
      b[r] -= f * b[col];
    }
  }
  const x = new Float64Array(m);
  for (let r = m - 1; r >= 0; r--) {
    let s = b[r];
    for (let c = r + 1; c < m; c++) s -= A[r][c] * x[c];
    x[r] = s / A[r][r];
  }
  return x;
}

// Harmonic regression (Zhu et al. 2015, https://doi.org/10.1016/j.rse.2015.02.009):
//   y = c0 + c1 cos(wt) + c2 sin(wt) + c3 t            (basic)
//       + c4 cos(2wt) + c5 sin(2wt)                     (advanced)
//       + c6 cos(3wt) + c7 sin(3wt)                     (full)
// with w = 2*pi / 365.25 and t in days. Fitted by least squares on all
// observations and evaluated on a daily grid between the first and last one.
const HARMONIC_T = 365.25;
const HARMONIC_ORDERS = { basic: 1, advanced: 2, full: 3 };
const HARMONIC_MAX_SAMPLES = 2000;   // max evaluation points (keeps long series fast)

function harmonicRow(t, nHarm) {
  // t is in days relative to the first observation (better conditioning than
  // days since 1970; the shift is absorbed by the intercept and the phases).
  // The trend column is in years so all columns have similar magnitude; this
  // only rescales its coefficient, the fitted curve is identical.
  const w = 2 * Math.PI / HARMONIC_T;
  const row = [1, Math.cos(w * t), Math.sin(w * t), t / HARMONIC_T];
  for (let k = 2; k <= nHarm; k++) row.push(Math.cos(k * w * t), Math.sin(k * w * t));
  return row;
}

function harmonicFit(pts, model) {
  const n = pts.length;
  const nHarm = HARMONIC_ORDERS[model];
  const m = 2 + 2 * nHarm;            // number of coefficients
  if (n < m) return null;             // under-determined: not enough observations

  const t0 = pts[0].x;
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

  const spanDays = (pts[n - 1].x - t0) / DAY_MS;
  const step = Math.max(1, Math.ceil(spanDays / HARMONIC_MAX_SAMPLES));
  const out = [];
  for (let d = 0; d <= spanDays; d += step) {
    const r = harmonicRow(d, nHarm);
    let y = 0;
    for (let i = 0; i < m; i++) y += c[i] * r[i];
    out.push({ x: t0 + d * DAY_MS, y: Math.round(y) });
  }
  return out;
}

function harmonicMode(model, label) {
  return {
    label,
    tension: 0,
    data: (pts) => harmonicFit(pts, model) || [],
    // shown in the status line when the fit is not possible
    minObs: 2 + 2 * HARMONIC_ORDERS[model],
  };
}

const LINE_MODES = {
  linear: { label: 'Linear interpolation', tension: 0, data: (pts) => pts.slice() },
  // Chart.js monotone cubic interpolation: smooth, passes through every
  // observation and never overshoots between them
  spline: { label: 'Spline (monotone cubic)', cubic: 'monotone', data: (pts) => pts.slice() },
  movavg: { label: `Moving average (${MOVAVG_WINDOW} obs.)`, tension: 0,
            data: (pts) => movingAverage(pts, MOVAVG_WINDOW) },
  savgol: { label: `Savitzky–Golay (${SAVGOL_WINDOW} obs.)`, cubic: 'monotone',
            data: (pts) => savitzkyGolay(pts, SAVGOL_WINDOW, SAVGOL_ORDER) },
  harmBasic:    harmonicMode('basic', 'Harmonic basic'),
  harmAdvanced: harmonicMode('advanced', 'Harmonic advanced'),
  harmFull:     harmonicMode('full', 'Harmonic full'),
  none:   { label: 'Line', tension: 0, data: () => [] },
};

function applyLineMode(ds) {
  const key = $('lineMode').value;
  const mode = LINE_MODES[key] || LINE_MODES.linear;
  ds.label = mode.label;
  ds.cubicInterpolationMode = mode.cubic || 'default';
  ds.tension = mode.tension || 0;
  ds.data = mode.data(series);
  if (mode.minObs && series.length && !ds.data.length) {
    ds.label += ` (needs ≥ ${mode.minObs} obs.)`;
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

function resetChart(startDate, endDate, band) {
  series = [];
  // drop any hovered point of the previous location: its index may not exist
  // in the new data
  chart.setActiveElements([]);
  chart.tooltip.setActiveElements([], { x: 0, y: 0 });
  const o = chart.options.scales;
  o.x.min = startDate;
  o.x.max = endDate;
  const name = BAND_TITLES[band] || band;
  o.y.title.text = INDEX_BANDS.has(band) ? `${name} × 10000` : `${name} reflectance × 10000`;
  showChart(false);
}

// draw the collected points (called once, when the extraction has ended)
function showResults() {
  showChart(true);
  render();
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

// Manual coordinate entry: "Go to" button or Enter in either field.
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
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.textContent = text;
  button.classList.add('copied');
  setTimeout(() => {
    button.textContent = original;
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
