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
  // Robust adaptive local regression (LOWESS-like), the core of the moving
  // average (degree 0) and Savitzky-Golay (local polynomial, degree <= 2).
  //
  //  * window: around each evaluation day it grows until it holds `k`
  //    observations, but is never shorter than hMin or longer than hMax
  //    (half widths in days, derived from the median spacing)
  //  * weights: tricube in time x robustness weight per observation
  //  * robustness: after a first fit, observations far from the curve
  //    (residual > 6 x median absolute residual, e.g. undetected clouds)
  //    get bisquare weights near 0 and the curve is fitted again
  //    (`robust` iterations), so they no longer pull the curve down
  //  * degree (Savitzky-Golay): local quadratic where both sides have
  //    enough observations, blended smoothly into a local linear fit
  //    towards the ends of the series, so the curve neither overshoots
  //    there nor gets kinks
  // ------------------------------------------------------------------------

  const smoothstep = (x) => { const s = clamp(x, 0, 1); return s * s * (3 - 2 * s); };
  // value at `at` of the robust local fit. Degree 0 (weighted mean) for
  // maxDeg 0; otherwise a blend of the local linear and the local quadratic
  // fit. The quadratic gets full weight where the window is balanced (the
  // weighted mean position of its observations, -1 = all left ... +1 = all
  // right, is within +/-0.2) and holds enough weight (>= 3), and fades out
  // smoothly towards the ends of the series (|mean position| 0.5 or less
  // than 1.5 weight: linear). Both measures change continuously with
  // `at`, so the curve has no kinks (switching the degree abruptly would
  // create them).
  function localFit(t, y, rw, at, k, hMin, hMax, maxDeg) {
    const w = adaptiveWindow(t, at, k, hMin, hMax);
    const m = maxDeg >= 2 ? 3 : 2;
    const A = Array.from({ length: m }, () => new Float64Array(m));
    const b = new Float64Array(m);
    let used = 0, sw = 0, swy = 0, swu = 0, ylo = Infinity, yhi = -Infinity;
    for (let i = w.lo; i < w.hi; i++) {
      const wi = tricube((t[i] - at) / w.h) * rw[i];
      if (wi <= 1e-12) continue;
      used++; sw += wi; swy += wi * y[i]; swu += wi * (t[i] - at) / w.h;
      if (y[i] < ylo) ylo = y[i];
      if (y[i] > yhi) yhi = y[i];
      if (maxDeg >= 1) {
        const u = (t[i] - at) / w.h;     // in [-1, 1]: well conditioned
        const p = [1, u, u * u, u * u * u, u * u * u * u];
        for (let r = 0; r < m; r++) {
          b[r] += wi * p[r] * y[i];
          for (let c = 0; c < m; c++) A[r][c] += wi * p[r + c];
        }
      }
    }
    if (used < 2 || sw <= 0) return null;
    const mean = swy / sw;
    if (maxDeg < 1) return mean;

    const sub = (n) => [A.slice(0, n).map((row) => Float64Array.from(row.slice(0, n))), Float64Array.from(b.slice(0, n))];
    const c1 = solve(...sub(2));
    const f1 = c1 && Number.isFinite(c1[0]) ? c1[0] : mean;
    let f = f1;
    if (m === 3 && used >= 3) {
      const balance = Math.abs(swu / sw);
      const alpha = smoothstep((0.5 - balance) / 0.3) * smoothstep((sw - 1.5) / 1.5);
      if (alpha > 0) {
        const c2 = solve(...sub(3));
        if (c2 && Number.isFinite(c2[0])) f = alpha * c2[0] + (1 - alpha) * f1;
      }
    }
    // never further outside the local data range than 25 % of it
    const pad = 0.25 * (yhi - ylo);
    return clamp(f, ylo - pad, yhi + pad);
  }

  function median(values) {
    const a = Float64Array.from(values).sort();
    const m = a.length >> 1;
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }

  function robustLocal(pts, cfg) {
    const { t, y, n } = toDays(pts);
    const dt = medianSpacing(t);
    if (n < 3 || !dt) return { data: [], params: null };
    const { k, maxDeg, robust } = cfg;
    const hMin = cfg.hMin(dt);
    const hMax = Math.max(hMin, cfg.hMax(dt));
    const rw = new Float64Array(n).fill(1);
    for (let it = 0; it < robust; it++) {
      const res = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        const f = localFit(t, y, rw, t[i], k, hMin, hMax, maxDeg);
        res[i] = f == null ? 0 : y[i] - f;
      }
      const s = 6 * median(Array.from(res, Math.abs));
      if (!(s > 0)) break;
      for (let i = 0; i < n; i++) {
        const u = res[i] / s;
        rw[i] = Math.abs(u) < 1 ? (1 - u * u) ** 2 : 0;
      }
    }
    const data = dayGrid(t[0], t[n - 1]).map((g) => out(g, localFit(t, y, rw, g, k, hMin, hMax, maxDeg)));
    return { data: breakGaps(data, t, dt), params: { k, robust, spacing: dt, hMin, hMax, gap: gapLimit(dt) } };
  }

  // Adaptive moving average: robust tricube-weighted mean
  function movingAverage(pts, opts = {}) {
    return robustLocal(pts, {
      k: opts.k || MA_DEFAULTS.k, maxDeg: 0,
      robust: opts.robust == null ? MA_DEFAULTS.robust : opts.robust,
      hMin: opts.hMin || MA_DEFAULTS.hMin, hMax: opts.hMax || MA_DEFAULTS.hMax,
    });
  }

  // Adaptive Savitzky-Golay: robust tricube-weighted local polynomial
  function savitzkyGolay(pts, opts = {}) {
    return robustLocal(pts, {
      k: opts.k || SG_DEFAULTS.k, maxDeg: opts.degree == null ? 2 : opts.degree,
      robust: opts.robust == null ? SG_DEFAULTS.robust : opts.robust,
      hMin: opts.hMin || SG_DEFAULTS.hMin, hMax: opts.hMax || SG_DEFAULTS.hMax,
    });
  }

  // Window settings (half widths in days, from the median spacing dt),
  // tuned on synthetic seasonal NDVI series (steep crop-like and gentle
  // seasons, 3-16 day spacing, noise and undetected clouds) for the lowest
  // error to the true curve among smooth curves:
  //   moving average : 5 obs., at least 20 days / 2 dt, at most 8 dt (45-150)
  //   Savitzky-Golay : 9 obs., at least 40 days / 2 dt, at most 10 dt
  //                    (60-180), one robustness pass against cloud outliers
  const MA_DEFAULTS = {
    k: 5, robust: 0,
    hMin: (dt) => Math.max(20, 2 * dt),
    hMax: (dt) => clamp(8 * dt, 45, 150),
  };
  const SG_DEFAULTS = {
    k: 9, robust: 1,
    hMin: (dt) => Math.max(40, 2 * dt),
    hMax: (dt) => clamp(10 * dt, 60, 180),
  };

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
    DAY_MS, medianSpacing, kthDistance, solve, MA_DEFAULTS, SG_DEFAULTS,
    movingAverage, savitzkyGolay, rbfEnsemble, rbf,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Smoothing = api;
})(typeof window !== 'undefined' ? window : this);
