/*
 * Size estimate of a scene download, computed in the browser the same way
 * the server plans the GeoTIFF (eetsviewer/export.py, plan_export):
 *   1. footprint vertices -> the scene's UTM projection -> bounding box
 *   2. "Map view": the view (edges densified) -> UTM -> bounding box,
 *      intersected with the scene box
 *   3. snapped outwards to the scene's native pixel grid
 * Scenes in a projection other than UTM (WGS 84) fall back to a local
 * equirectangular approximation.
 *
 * Works in the browser (window.Grid) and in Node (module.exports).
 */
(function (root) {
  'use strict';

  // WGS 84 -> UTM (transverse Mercator, Snyder 1987); sub-metre accuracy
  // within a few degrees of the zone, plenty for a pixel count
  const A = 6378137, F = 1 / 298.257223563, K0 = 0.9996;
  const E2 = F * (2 - F), EP2 = E2 / (1 - E2);
  const E4 = E2 * E2, E6 = E4 * E2;

  function utm(lon, lat, zone, south) {
    const phi = lat * Math.PI / 180;
    const lam0 = ((zone - 1) * 6 - 180 + 3) * Math.PI / 180;
    const sin = Math.sin(phi), cos = Math.cos(phi), tan = Math.tan(phi);
    const N = A / Math.sqrt(1 - E2 * sin * sin);
    const T = tan * tan, C = EP2 * cos * cos, a = cos * (lon * Math.PI / 180 - lam0);
    const M = A * ((1 - E2 / 4 - 3 * E4 / 64 - 5 * E6 / 256) * phi
      - (3 * E2 / 8 + 3 * E4 / 32 + 45 * E6 / 1024) * Math.sin(2 * phi)
      + (15 * E4 / 256 + 45 * E6 / 1024) * Math.sin(4 * phi)
      - (35 * E6 / 3072) * Math.sin(6 * phi));
    const x = K0 * N * (a + (1 - T + C) * a ** 3 / 6
      + (5 - 18 * T + T * T + 72 * C - 58 * EP2) * a ** 5 / 120) + 500000;
    let y = K0 * (M + N * tan * (a * a / 2 + (5 - T + 9 * C + 4 * C * C) * a ** 4 / 24
      + (61 - 58 * T + T * T + 600 * C - 330 * EP2) * a ** 6 / 720));
    if (south) y += 10000000;
    return [x, y];
  }

  // projection function for a CRS like "EPSG:32632" (UTM north 32) or
  // "EPSG:32733" (UTM south 33); null for anything else
  function projector(crs) {
    const mm = /^EPSG:32([67])(\d{2})$/.exec(crs || '');
    if (!mm) return null;
    const south = mm[1] === '7', zone = Number(mm[2]);
    if (zone < 1 || zone > 60) return null;
    return (lon, lat) => utm(lon, lat, zone, south);
  }

  function coords(c, out = []) {
    if (typeof c[0] === 'number') out.push(c);
    else for (const part of c) coords(part, out);
    return out;
  }

  function boxOf(points) {
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const [x, y] of points) {
      if (x < minx) minx = x; if (x > maxx) maxx = x;
      if (y < miny) miny = y; if (y > maxy) maxy = y;
    }
    return [minx, miny, maxx, maxy];
  }

  // view rectangle (west, south, east, north) as points along its edges,
  // like rasterio.warp.transform_bounds(densify_pts=21)
  function densify([w, s, e, n], k = 21) {
    const pts = [];
    for (let i = 0; i <= k + 1; i++) {
      const f = i / (k + 1);
      pts.push([w + f * (e - w), s], [w + f * (e - w), n], [w, s + f * (n - s)], [e, s + f * (n - s)]);
    }
    return pts;
  }

  /**
   * info: {footprint (GeoJSON), bbox, crs, transform, resolution, bands, max_side}
   * view: [west, south, east, north] for "Map view", or null for the full scene
   * -> {width, height, bytes, tooLarge} or {empty: true}
   */
  function estimate(info, view) {
    let project = projector(info.crs);
    let px, py, ox, oy;
    if (project && info.transform && info.transform[1] === 0 && info.transform[3] === 0) {
      [px, , ox, , py, oy] = info.transform;
      py = -py;
    } else {
      // fallback: local equirectangular metres around the scene centre
      const [w, s, e, n] = info.bbox;
      const lat0 = ((s + n) / 2) * Math.PI / 180;
      project = (lon, lat) => [lon * 111320 * Math.cos(lat0), lat * 110574];
      px = py = info.resolution;
      ox = 0; oy = 0;
    }
    const fp = info.footprint ? coords(info.footprint.coordinates)
      : densify(info.bbox, 1);
    let box = boxOf(fp.map(([lon, lat]) => project(lon, lat)));
    if (view) {
      const v = boxOf(densify(view).map(([lon, lat]) => project(lon, lat)));
      box = [Math.max(box[0], v[0]), Math.max(box[1], v[1]), Math.min(box[2], v[2]), Math.min(box[3], v[3])];
      if (box[0] >= box[2] || box[1] >= box[3]) return { empty: true };
    }
    // snap outwards to the native pixel grid (same tolerance as the server)
    const eps = 1e-3;
    const c0 = Math.floor((box[0] - ox) / px + eps), c1 = Math.ceil((box[2] - ox) / px - eps);
    const r0 = Math.floor((oy - box[3]) / py + eps), r1 = Math.ceil((oy - box[1]) / py - eps);
    const width = Math.max(0, c1 - c0), height = Math.max(0, r1 - r0);
    if (!width || !height) return { empty: true };
    return { width, height, bytes: width * height * info.bands * 2,
             tooLarge: width > info.max_side || height > info.max_side };
  }

  const api = { estimate, utm, projector };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Grid = api;
})(typeof window !== 'undefined' ? window : this);
