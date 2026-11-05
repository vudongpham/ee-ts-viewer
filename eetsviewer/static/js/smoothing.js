/*
 * Adaptive smoothing / interpolation of irregular time series.
 *
 * All methods take observations [{x: time in ms, y}, ...] sorted by x and
 * return a curve [{x, y}, ...] on a regular grid of days, with y = null in
 * gaps that have too few observations (Chart.js then breaks the line instead
 * of drawing across the gap).
 *
 * Their parameters are derived from the data: the median spacing of the
 * clear observations (in days) sets the window and kernel sizes, and around
 * every point the window grows until it holds enough observations. So a
 * dense Sentinel-2 + Landsat series is smoothed with short windows, a sparse
 * Landsat 5 series with longer ones, and a winter gap is not bridged.
 *
 * Works in the browser (window.Smoothing) and in Node (module.exports), so
 * it can be tested outside the browser.
 */
(function (root) {
  'use strict';

  const DAY_MS = 24 * 60 * 60 * 1000;
  const MAX_SAMPLES = 1500;          // max evaluation points of a curve

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const tricube = (u) => { const a = 1 - Math.abs(u) ** 3; return a > 0 ? a * a * a : 0; };

  // ------------------------------------------------------------------------
  // helpers
  // ------------------------------------------------------------------------

  // observations as days (Float64Array), sorted
  function toDays(pts) {
    const t = new Float64Array(pts.length);
    const y = new Float64Array(pts.length);
    pts.forEach((p, i) => { t[i] = p.x / DAY_MS; y[i] = p.y; });
    return { t, y, n: pts.length };
  }

  // median gap (days) between distinct observation days
  function medianSpacing(t) {
    const gaps = [];
    for (let i = 1; i < t.length; i++) {
      const d = Math.round(t[i]) - Math.round(t[i - 1]);
      if (d > 0) gaps.push(d);
    }
    if (!gaps.length) return null;
    gaps.sort((a, b) => a - b);
    const m = gaps.length >> 1;
    return gaps.length % 2 ? gaps[m] : (gaps[m - 1] + gaps[m]) / 2;
  }

  // first index with t[i] >= v
  function lowerBound(t, v) {
    let lo = 0, hi = t.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (t[mid] < v) lo = mid + 1; else hi = mid; }
    return lo;
  }

  // distance (days) from `at` to its k-th nearest observation
  function kthDistance(t, at, k) {
    let r = lowerBound(t, at), l = r - 1, d = 0;
    for (let found = 0; found < k; found++) {
      const dl = l >= 0 ? at - t[l] : Infinity;
      const dr = r < t.length ? t[r] - at : Infinity;
      if (dl === Infinity && dr === Infinity) break;
      if (dl <= dr) { d = dl; l--; } else { d = dr; r++; }
    }
    return d;
  }

  // regular grid of days from the first to the last observation
  function dayGrid(t0, t1, step) {
    step = step || Math.max(1, Math.ceil((t1 - t0) / MAX_SAMPLES));
    const out = [];
    for (let d = t0; d <= t1 + 1e-9; d += step) out.push(d);
    if (out[out.length - 1] < t1 - 1e-9) out.push(t1);
    return out;
  }

  // tiny Gaussian elimination with partial pivoting (m <= 8)
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

  // window of observations around `at`: wide enough to hold k observations,
  // but at least hMin and at most hMax days (half width)
  function adaptiveWindow(t, at, k, hMin, hMax) {
    const h = clamp(kthDistance(t, at, k), hMin, hMax);
    // +0.5 day so the k-th observation itself gets a (small) weight
    const hw = h + 0.5;
    return { h: hw, lo: lowerBound(t, at - hw), hi: lowerBound(t, at + hw + 1e-9) };
  }

  // A gap between two consecutive observations is not bridged by any method
  // if it is longer than 8 x the median spacing (at least 60, at most 365
  // days), e.g. 72 days for a 9-day series, 128 days for 16-day Landsat.
  const gapLimit = (dt) => clamp(8 * dt, 60, 365);

  function inGap(t, day, limit) {
    const i = lowerBound(t, day);
    if (i === 0 || i >= t.length || t[i] === day) return false;
    return t[i] - t[i - 1] > limit;
  }

  // set the curve to null inside long gaps
  function breakGaps(data, t, dt) {
    const limit = gapLimit(dt);
    return data.map((p) => (inGap(t, p.x / DAY_MS, limit) ? { x: p.x, y: null } : p));
  }

  const out = (day, y) => ({ x: day * DAY_MS, y: y == null || !Number.isFinite(y) ? null : Math.round(y) });

  // ------------------------------------------------------------------------
  // Adaptive moving average: tricube-weighted mean over a window that holds
  // `k` observations (local regression of degree 0, adaptive span)
  // ------------------------------------------------------------------------

  function movingAverage(pts, opts = {}) {
    const k = opts.k || 5;
    const { t, y, n } = toDays(pts);
    const dt = medianSpacing(t);
    if (n < 3 || !dt) return { data: [], params: null };
    const hMin = Math.max(4, dt);
    const hMax = clamp(4 * dt, 30, 120);
    const data = dayGrid(t[0], t[n - 1]).map((g) => {
      const w = adaptiveWindow(t, g, k, hMin, hMax);
      let sw = 0, swy = 0, used = 0;
      for (let i = w.lo; i < w.hi; i++) {
        const wi = tricube((t[i] - g) / w.h);
        if (wi > 0) { sw += wi; swy += wi * y[i]; used++; }
      }
      return out(g, used >= 2 ? swy / sw : null);
    });
    return { data: breakGaps(data, t, dt), params: { k, spacing: dt, hMin, hMax, gap: gapLimit(dt) } };
  }

  // ------------------------------------------------------------------------
  // Adaptive Savitzky–Golay: local polynomial (tricube-weighted least
  // squares) over a window that holds `k` observations. The degree adapts
  // too: quadratic with >= 6 observations and >= 2 on each side, linear with
  // >= 3 and >= 1 on each side, otherwise the weighted mean. So the curve
  // does not overshoot at the ends of the series or at the edge of a gap.
  // ------------------------------------------------------------------------

  function savitzkyGolay(pts, opts = {}) {
    const k = opts.k || 7;
    const maxDeg = opts.degree == null ? 2 : opts.degree;
    const { t, y, n } = toDays(pts);
    const dt = medianSpacing(t);
    if (n < 3 || !dt) return { data: [], params: null };
    const hMin = Math.max(8, 1.5 * dt);
    const hMax = clamp(6 * dt, 45, 150);
    const data = dayGrid(t[0], t[n - 1]).map((g) => {
      const w = adaptiveWindow(t, g, k, hMin, hMax);
      let used = 0, left = 0, right = 0, ylo = Infinity, yhi = -Infinity;
      let sw = 0, swy = 0;
      const ws = [];
      for (let i = w.lo; i < w.hi; i++) {
        const wi = tricube((t[i] - g) / w.h);
        ws.push(wi);
        if (wi <= 0) continue;
        used++; sw += wi; swy += wi * y[i];
        if (t[i] < g - 0.5) left++; else if (t[i] > g + 0.5) right++;
        if (y[i] < ylo) ylo = y[i];
        if (y[i] > yhi) yhi = y[i];
      }
      if (used < 2) return out(g, null);
      let deg = 0;
      if (maxDeg >= 2 && used >= 6 && left >= 2 && right >= 2) deg = 2;
      else if (maxDeg >= 1 && used >= 3 && left >= 1 && right >= 1) deg = 1;
      if (deg === 0) return out(g, swy / sw);

      // weighted least squares in u = (t - g) / h  (in [-1, 1]: well conditioned)
      const m = deg + 1;
      const A = Array.from({ length: m }, () => new Float64Array(m));
      const b = new Float64Array(m);
      for (let i = w.lo, j = 0; i < w.hi; i++, j++) {
        const wi = ws[j];
        if (wi <= 0) continue;
        const u = (t[i] - g) / w.h;
        const p = [1, u, u * u, u * u * u, u * u * u * u];
        for (let r = 0; r < m; r++) {
          b[r] += wi * p[r] * y[i];
          for (let c = 0; c < m; c++) A[r][c] += wi * p[r + c];
        }
      }
      const c = solve(A, b);
      if (!c || !Number.isFinite(c[0])) return out(g, swy / sw);
      // never further outside the local data range than 25 % of it
      const pad = 0.25 * (yhi - ylo);
      return out(g, clamp(c[0], ylo - pad, yhi + pad));
    });
    return { data: breakGaps(data, t, dt), params: { k, spacing: dt, hMin, hMax, gap: gapLimit(dt) } };
  }

  // ------------------------------------------------------------------------
  // RBF ensemble (after Schwieder et al. 2016, doi:10.1016/j.jag.2016.06.019):
  // Gaussian kernels of several widths, each truncated at +/- 3 sigma, are
  // combined with a weight per kernel and evaluation day.
  //   weighting 'count'  : number of observations within +/- 3 sigma (as in
  //                        the reference script; favours the widest kernel)
  //   weighting 'density': sum of the kernel weights / sigma, i.e. the local
  //                        data density at the scale of that kernel. Equal for
  //                        all kernels in evenly sampled stretches; in sparse
  //                        stretches the narrow kernels fade out and the wide
  //                        ones take over.
  //   minMass: a kernel only contributes where the sum of its weights is at
  //            least this (0.5 = half the weight of one observation right at
  //            that day). Without it (0, the reference behaviour) the widest
  //            kernel draws a flat line up to 3 sigma into every gap.
  // ------------------------------------------------------------------------

  function rbfEnsemble(pts, sigmas, opts = {}) {
    const weighting = opts.weighting || 'density';
    const minMass = opts.minMass || 0;
    const { t, y, n } = toDays(pts);
    if (!n) return [];
    const step = opts.step;
    // same grid as numpy.arange(min, max + step, step) when a step is given
    const grid = [];
    if (step) for (let d = t[0]; d < t[n - 1] + step - 1e-9; d += step) grid.push(d);
    else grid.push(...dayGrid(t[0], t[n - 1]));
    return grid.map((g) => {
      let num = 0, den = 0;
      for (const sigma of sigmas) {
        const lo = lowerBound(t, g - 3 * sigma);
        const hi = lowerBound(t, g + 3 * sigma + 1e-9);
        if (hi <= lo) continue;
        let sw = 0, swy = 0;
        for (let i = lo; i < hi; i++) {
          const d = (t[i] - g) / sigma;
          const wi = Math.exp(-0.5 * d * d);
          sw += wi; swy += wi * y[i];
        }
        if (sw <= 0 || sw < minMass) continue;
        const weight = weighting === 'count' ? hi - lo : sw / sigma;
        num += weight * (swy / sw);
        den += weight;
      }
      return { x: g * DAY_MS, y: den > 0 ? num / den : null };
    });
  }

  // kernel widths from the median observation spacing: 2, 3.5 and 5 times
  // the spacing (e.g. 24 / 42 / 60 days for Landsat 8 + 9 at 12 days),
  // at least 6 and at most 90 days
  const RBF_FACTORS = [2, 3.5, 5];

  function rbf(pts, opts = {}) {
    const { t, n } = toDays(pts);
    const dt = medianSpacing(t);
    if (n < 3 || !dt) return { data: [], params: null };
    const sigmas = [...new Set((opts.factors || RBF_FACTORS)
      .map((f) => Math.round(clamp(f * dt, 6, 90))))];
    const data = rbfEnsemble(pts, sigmas, { weighting: opts.weighting || 'density',
                                             minMass: opts.minMass == null ? 0.5 : opts.minMass })
      .map((p) => out(p.x / DAY_MS, p.y));
    return { data: breakGaps(data, t, dt), params: { sigmas, spacing: dt, gap: gapLimit(dt) } };
  }

  const api = {
    DAY_MS, medianSpacing, kthDistance, solve,
    movingAverage, savitzkyGolay, rbfEnsemble, rbf,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Smoothing = api;
})(typeof window !== 'undefined' ? window : this);
