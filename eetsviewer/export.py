"""On-the-fly GeoTIFF export of one scene.

The scene is read from Earth Engine with `ee.data.computePixels` in blocks
of about 1024 x 1024 pixels on its native pixel grid (several blocks in
parallel). Each block is written into a compressed int16 GeoTIFF as soon as
it arrives, so memory use stays small even for a full scene. Only the
final file is kept, in a temporary directory, until it is downloaded.
"""

import math
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

import ee
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.warp import transform as warp_transform, transform_bounds

from . import console
from .earthengine import IMAGE_ID, RETRY_PATTERNS
from .sensors import (CLOUD_SCORE_PLUS, CS_AGGRESSIVE, CS_DEFAULT, LANDSAT_GAIN, LANDSAT_OFFSET,
                      MASK_AGGRESSIVE, MASK_DEFAULT, SCL_AGGRESSIVE, SCL_DEFAULT,
                      SENSORS, export_name)

NODATA = -9999
BLOCK = 1024                     # block edge in pixels (12 bands int16 = 25 MB < 48 MB limit)
MAX_SIDE = 12000                 # largest width / height in pixels (a Sentinel-2 tile is 10980)
EXPORT_WORKERS = 6               # parallel computePixels requests per export
FILE_TTL = 1800                  # seconds a finished file is kept if it is not downloaded


class ExportError(Exception):
    pass


# ---------------------------------------------------------------------------
# Plan: which bands, which grid
# ---------------------------------------------------------------------------

class Plan:
    def __init__(self, sensor, image_id, crs, transform, width, height, date, source_id=None,
                 mask=None):
        s = SENSORS[sensor]
        self.sensor = sensor
        self.image_id = image_id
        self.mask = mask                  # None = raw, else MASK_DEFAULT / MASK_AGGRESSIVE
        self.crs = crs                    # dict with 'crs' (EPSG code) or 'wkt'
        self.transform = transform        # rasterio Affine of the output grid
        self.width = width
        self.height = height
        self.date = date                  # YYYYMMDD
        self.bands = [b for b, _ in s["export"]]
        self.descriptions = [f"{b} ({name})" for b, name in s["export"]]
        self.name = export_name(sensor, date, source_id, mask)  # {product}_{sensor}_{YYYYMMDD}[_masked].tif

    @property
    def blocks(self):
        return [(c, r, min(BLOCK, self.width - c), min(BLOCK, self.height - r))
                for r in range(0, self.height, BLOCK)
                for c in range(0, self.width, BLOCK)]

    def rio_crs(self):
        if self.crs.get("crs"):
            return CRS.from_user_input(self.crs["crs"])
        return CRS.from_wkt(self.crs["wkt"])

    def ee_crs(self):
        if self.crs.get("crs"):
            return {"crsCode": self.crs["crs"]}
        return {"crsWkt": self.crs["wkt"]}


def snap_to_grid(native, bounds):
    """Output grid covering `bounds` (minx, miny, maxx, maxy, native CRS units),
    aligned with the native pixel grid `native` (Affine, north-up).

    Returns (Affine, width, height)."""
    px, py = native.a, -native.e
    ox, oy = native.c, native.f
    minx, miny, maxx, maxy = bounds
    # tolerance of 1/1000 pixel: coordinates that went through a lon/lat
    # round trip are off by tiny amounts and must not add a pixel row
    eps = 1e-3
    c0 = math.floor((minx - ox) / px + eps)
    c1 = math.ceil((maxx - ox) / px - eps)
    r0 = math.floor((oy - maxy) / py + eps)
    r1 = math.ceil((oy - miny) / py - eps)
    width, height = max(0, c1 - c0), max(0, r1 - r0)
    return Affine(px, 0, ox + c0 * px, 0, -py, oy - r0 * py), width, height


def _coords(c):
    """All (lon, lat) vertices of nested GeoJSON coordinates."""
    if c and isinstance(c[0], (int, float)):
        return [c[0]], [c[1]]
    lons, lats = [], []
    for part in c:
        a, b = _coords(part)
        lons += a
        lats += b
    return lons, lats


def _intersect(a, b):
    """Intersection of two (minx, miny, maxx, maxy) boxes, or None."""
    w, s = max(a[0], b[0]), max(a[1], b[1])
    e, n = min(a[2], b[2]), min(a[3], b[3])
    return (w, s, e, n) if w < e and s < n else None


def _bits(img, start, n=2):
    return img.rightShift(start).bitwiseAnd((1 << n) - 1)


def _in(img, values):
    """1 where `img` equals one of `values`."""
    out = None
    for v in sorted(values):
        out = img.eq(v) if out is None else out.Or(img.eq(v))
    return out


def clear_mask(sensor, scene, image_id, level):
    """Per-pixel version of sensors.is_clear(): 1 = clear, 0 = masked.

    The rules are the same as for the time series (Default / Aggressive),
    so a downloaded masked scene matches the observations in the chart.
    """
    s = SENSORS[sensor]
    family = s["family"]
    if family == "LND":
        qa = scene.select("QA_PIXEL")
        ok = qa.bitwiseAnd(1).eq(0)                          # fill
        if level >= MASK_DEFAULT:
            ok = ok.And(qa.bitwiseAnd(0b111110).eq(0))       # dilated cloud, cirrus, cloud, shadow, snow
        if level >= MASK_AGGRESSIVE:
            ok = (ok.And(_bits(qa, 8).lt(2)).And(_bits(qa, 14).lt(2))     # cloud / cirrus confidence
                    .And(_bits(qa, 10).neq(3)).And(_bits(qa, 12).neq(3))  # shadow / snow confidence
                    .And(scene.select("QA_RADSAT").unmask(0).eq(0)))      # saturation
            if "AEROSOL" in s["qa"]:
                ok = ok.And(_bits(scene.select("SR_QA_AEROSOL").unmask(0), 6).neq(3))
            if "OPACITY" in s["qa"]:
                ok = ok.And(scene.select("SR_ATMOS_OPACITY").unmask(0).multiply(0.001).lte(0.3))
        return ok
    if family == "HLS":
        fm = scene.select("Fmask")
        ok = fm.neq(255)                                     # fill
        if level >= MASK_DEFAULT:
            ok = ok.And(fm.bitwiseAnd(0b11110).eq(0))        # cloud, adjacent, shadow, snow
        if level >= MASK_AGGRESSIVE:
            ok = ok.And(_bits(fm, 6).neq(3))                 # high aerosol
        return ok
    # Sentinel-2: SCL + Cloud Score+ (if the scene has no Cloud Score+ image,
    # only SCL is used, as for the time series)
    scl = scene.select("SCL")
    ok = scl.neq(0)
    if level >= MASK_DEFAULT:
        csp = ee.ImageCollection(CLOUD_SCORE_PLUS).filter(ee.Filter.eq("system:index", image_id))
        cs = ee.Image(ee.Algorithms.If(csp.size().gt(0),
                                       csp.first().select("cs_cdf"),
                                       ee.Image.constant(1).rename("cs_cdf"))).unmask(1)
        classes, threshold = ((SCL_AGGRESSIVE, CS_AGGRESSIVE) if level >= MASK_AGGRESSIVE
                              else (SCL_DEFAULT, CS_DEFAULT))
        ok = ok.And(_in(scl, classes)).And(cs.gte(threshold))
    return ok


def export_image(sensor, image_id, mask=None):
    """Earth Engine expression of the exported bands: int16, reflectance
    x 10000 (the same scale as the time series), masked pixels = -9999.

    mask: None for the raw scene, or MASK_DEFAULT / MASK_AGGRESSIVE to set
    cloudy pixels to nodata as well."""
    s = SENSORS[sensor]
    scene = ee.Image(f"{s['collection']}/{image_id}")
    img = scene.select([b for b, _ in s["export"]])
    if mask is not None:
        img = img.updateMask(clear_mask(sensor, scene, image_id, mask))
    if s["family"] == "LND":
        img = img.multiply(LANDSAT_GAIN).add(LANDSAT_OFFSET)
    elif s["family"] == "HLS":
        img = img.multiply(10000)
    return (img.round()
            .clamp(NODATA + 1, 32767)
            .toInt16()
            .unmask(NODATA, False))


_meta_cache = OrderedDict()
_meta_lock = threading.Lock()


def scene_meta(sensor, image_id):
    """Acquisition time and (HLS) source product id of one scene, cached."""
    key = (sensor, image_id)
    with _meta_lock:
        hit = _meta_cache.get(key)
    if hit is not None:
        return hit
    s = SENSORS[sensor]
    img = ee.Image(f"{s['collection']}/{image_id}")
    fields = {"time": img.get("system:time_start")}
    prop = s.get("spacecraft_property")
    if prop:
        fields["source"] = ee.Algorithms.If(img.propertyNames().contains(prop), img.get(prop), "")
    meta = _retry(lambda: ee.Dictionary(fields).getInfo())
    with _meta_lock:
        _meta_cache[key] = meta
        while len(_meta_cache) > 1000:
            _meta_cache.popitem(last=False)
    return meta


def _check(sensor, image_id, mask):
    if sensor not in SENSORS:
        raise ExportError(f"Unknown sensor {sensor!r}")
    if not IMAGE_ID.match(image_id or ""):
        raise ExportError("Invalid image id")
    if mask not in (None, MASK_DEFAULT, MASK_AGGRESSIVE):
        raise ExportError(f"Unknown cloud mask {mask!r}")


def _yyyymmdd(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d")


CLOUD_SCALE = 100   # m; grid of the cloud-cover estimate (fast, ample precision)
_cloud_cache = OrderedDict()


def cloud_cover(sensor, image_id, level):
    """Cloud cover of the whole scene in percent: pixels removed by the
    cloud mask `level` (clouds, shadows, snow, ...) / valid pixels (pixels
    with data, i.e. not fill / no-data), on a 100 m grid. Cached."""
    _check(sensor, image_id, level)
    if level is None:
        raise ExportError("A cloud mask level is required")
    key = (sensor, image_id, level)
    with _meta_lock:
        hit = _cloud_cache.get(key)
    if hit is not None:
        return hit

    s = SENSORS[sensor]
    scene = ee.Image(f"{s['collection']}/{image_id}")
    valid = clear_mask(sensor, scene, image_id, 0)          # data, no fill
    clear = clear_mask(sensor, scene, image_id, level)
    # mean of "clear" over the valid pixels = clear fraction
    frac = (clear.And(valid).rename("clear").updateMask(valid)
            .reduceRegion(reducer=ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True),
                          geometry=scene.geometry(), scale=CLOUD_SCALE,
                          crs=scene.select(s["bands"]["BLUE"]).projection(),
                          maxPixels=1e9))
    info = _retry(lambda: frac.getInfo())
    mean, count = info.get("clear_mean"), info.get("clear_count") or 0
    result = {
        "cloud": None if mean is None or not count else round(100 * (1 - mean), 1),
        "valid_pixels": int(count),
        "scale": CLOUD_SCALE,
    }
    with _meta_lock:
        _cloud_cache[key] = result
        while len(_cloud_cache) > 1000:
            _cloud_cache.popitem(last=False)
    return result


def file_name(sensor, image_id, mask=None):
    """Name of the file a download of this scene will get."""
    _check(sensor, image_id, mask)
    meta = scene_meta(sensor, image_id)
    return export_name(sensor, _yyyymmdd(meta["time"]), meta.get("source"), mask)


def plan_export(sensor, image_id, extent, view_bounds=None, mask=None):
    """Work out the output grid. One Earth Engine round trip.

    extent: "scene" (footprint of the scene) or "view" (the part of the
    scene inside `view_bounds` = (west, south, east, north) in degrees).
    """
    _check(sensor, image_id, mask)
    s = SENSORS[sensor]
    img = ee.Image(f"{s['collection']}/{image_id}")
    grid_band = s["bands"]["BLUE"]  # finest native resolution of the sensor
    fields = {
        "proj": img.select(grid_band).projection(),
        "footprint": img.geometry(),
        "time": img.get("system:time_start"),
    }
    prop = s.get("spacecraft_property")  # HLS: which satellite took the scene
    if prop:
        fields["source"] = ee.Algorithms.If(img.propertyNames().contains(prop), img.get(prop), "")
    info = _retry(lambda: ee.Dictionary(fields).getInfo())

    proj = info["proj"]
    t = proj.get("transform")
    if not t or t[1] != 0 or t[3] != 0:
        raise ExportError("The scene has no north-up native grid")
    native = Affine(t[0], t[1], t[2], t[3], t[4], t[5])
    crs = {"crs": proj.get("crs"), "wkt": proj.get("wkt")}

    # footprint vertices in the scene's own projection: their bounding box
    # is the native extent of the scene (a lon/lat box would be rotated
    # and inflated, especially near the edge of a UTM zone)
    rio_crs = CRS.from_user_input(crs["crs"]) if crs["crs"] else CRS.from_wkt(crs["wkt"])
    lons, lats = _coords(info["footprint"]["coordinates"])
    xs, ys = warp_transform("EPSG:4326", rio_crs, lons, lats)
    box = (min(xs), min(ys), max(xs), max(ys))
    if extent == "view":
        if not view_bounds:
            raise ExportError("Map bounds are missing")
        view = transform_bounds("EPSG:4326", rio_crs, *(float(v) for v in view_bounds),
                                densify_pts=21)
        box = _intersect(box, view)
        if box is None:
            raise ExportError("The scene does not cover the current map view")
    elif extent != "scene":
        raise ExportError(f"Unknown extent {extent!r}")

    transform, width, height = snap_to_grid(native, box)
    if width == 0 or height == 0:
        raise ExportError("The selected area is empty")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ExportError(f"The area is too large ({width} x {height} pixels, "
                          f"at most {MAX_SIDE} x {MAX_SIDE}): zoom in")

    date = _yyyymmdd(info["time"])
    return Plan(sensor, image_id, crs, transform, width, height, date, info.get("source"), mask)


# ---------------------------------------------------------------------------
# Reading blocks
# ---------------------------------------------------------------------------

def _retry(fn, retries=4):
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return fn()
        except ee.EEException as exc:
            if attempt == retries or not RETRY_PATTERNS.search(str(exc)):
                raise
        except (OSError, ConnectionError):
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2


def fetch_block(plan, expression, col, row, width, height):
    """Pixels of one block as an int16 array (bands, height, width)."""
    t = plan.transform * Affine.translation(col, row)
    grid = {
        "dimensions": {"width": width, "height": height},
        "affineTransform": {"scaleX": t.a, "shearX": t.b, "translateX": t.c,
                            "shearY": t.d, "scaleY": t.e, "translateY": t.f},
        **plan.ee_crs(),
    }
    arr = _retry(lambda: ee.data.computePixels({
        "expression": expression, "fileFormat": "NUMPY_NDARRAY", "grid": grid,
    }))
    return np.stack([arr[b] for b in plan.bands]).astype(np.int16, copy=False)


# ---------------------------------------------------------------------------
# Export jobs
# ---------------------------------------------------------------------------

class ExportJob:
    def __init__(self, plan):
        self.id = uuid.uuid4().hex
        self.plan = plan
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.status = "running"
        self.error = None
        self.current = 0
        self.total = len(plan.blocks)
        self.started = time.time()
        self.finished = None
        self.dir = tempfile.mkdtemp(prefix="eetsviewer-")
        self.path = os.path.join(self.dir, plan.name)

    def snapshot(self):
        with self.lock:
            return {
                "job": self.id, "status": self.status, "error": self.error,
                "current": self.current, "total": self.total, "name": self.plan.name,
                "width": self.plan.width, "height": self.plan.height,
                "bands": len(self.plan.bands), "mask": self.plan.mask,
                "elapsed": round((self.finished or time.time()) - self.started, 2),
            }

    def finish(self, status, error=None):
        with self.lock:
            changed = self.status == "running"
            if changed:
                self.status, self.error, self.finished = status, error, time.time()
        if changed:
            report_finished(self)
        if status != "done":
            self.cleanup()

    def cancel(self):
        self.cancelled.set()
        self.finish("cancelled")

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def _profile(plan):
    return {
        "driver": "GTiff", "dtype": "int16", "nodata": NODATA,
        "count": len(plan.bands), "width": plan.width, "height": plan.height,
        "crs": plan.rio_crs(), "transform": plan.transform,
        "tiled": True, "blockxsize": 256, "blockysize": 256,
        "compress": "deflate", "predictor": 2, "zlevel": 6,
        "BIGTIFF": "IF_SAFER", "interleave": "band",
    }


def run_export(job, fetch=None):
    """Fetch all blocks (in parallel) and write them into the GeoTIFF.

    The writing happens in this thread only (GDAL datasets are not thread
    safe); the pool only downloads."""
    plan = job.plan
    fetch = fetch or fetch_block
    pool = ThreadPoolExecutor(EXPORT_WORKERS, thread_name_prefix="export")
    try:
        expression = export_image(plan.sensor, plan.image_id, plan.mask)
        with rasterio.open(job.path, "w", **_profile(plan)) as dst:
            for i, desc in enumerate(plan.descriptions, start=1):
                dst.set_band_description(i, desc)
            pending = {pool.submit(fetch, plan, expression, *b): b for b in plan.blocks}
            while pending:
                if job.cancelled.is_set():
                    return
                done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in done:
                    col, row, w, h = pending.pop(f)
                    dst.write(f.result(), window=((row, row + h), (col, col + w)))
                    with job.lock:
                        job.current += 1
                    console.progress(job.id, job.current, job.total,
                                     f"{job.current}/{job.total} blocks · {plan.name}",
                                     force=job.current >= job.total)
        if not job.cancelled.is_set():
            job.finish("done")
    except Exception as exc:
        job.finish("error", str(exc))
    finally:
        # do not wait for blocks still downloading after a cancel or error
        pool.shutdown(wait=False, cancel_futures=True)


export_jobs = OrderedDict()
export_lock = threading.Lock()
MAX_EXPORT_JOBS = 8


# ---------------------------------------------------------------------------
# Terminal output (one start and one result line per download)
# ---------------------------------------------------------------------------

MASK_KINDS = {None: "raw", MASK_DEFAULT: "cloud masked (Default)",
              MASK_AGGRESSIVE: "cloud masked (Aggressive)"}


def report_started(job):
    p = job.plan
    console.info("Download", f"{p.name} · {MASK_KINDS.get(p.mask, p.mask)} · "
                             f"{p.width} × {p.height} px · {len(p.bands)} bands "
                             + console.style(f"({job.total} blocks)", "grey"))


def report_finished(job):
    console.end_progress(job.id)
    elapsed = console.fmt_seconds(job.finished - job.started)
    name = job.plan.name
    if job.status == "done":
        size = console.fmt_bytes(os.path.getsize(job.path)) if os.path.exists(job.path) else "?"
        console.ok("Download", f"{name} ready · {size} · {elapsed}")
    elif job.status == "error":
        one = " ".join((job.error or "unknown error").split())
        console.error("Download", f"{name} failed: {one[:160]}")
    else:
        console.stopped("Download", f"{name} cancelled after {job.current}/{job.total} blocks")


def start_export(plan):
    job = ExportJob(plan)
    report_started(job)
    with export_lock:
        _expire()
        export_jobs[job.id] = job
        while len(export_jobs) > MAX_EXPORT_JOBS:
            _, old = export_jobs.popitem(last=False)
            old.cancel()
            old.cleanup()
    threading.Thread(target=run_export, args=(job,), daemon=True,
                     name=f"export-{job.id[:8]}").start()
    return job


def get_export(job_id):
    with export_lock:
        return export_jobs.get(job_id)


def forget_export(job_id):
    with export_lock:
        job = export_jobs.pop(job_id, None)
    if job:
        job.cleanup()


def _expire():
    """Remove finished files nobody downloaded (call with export_lock held)."""
    now = time.time()
    for jid in [j for j, job in export_jobs.items()
                if job.finished and now - job.finished > FILE_TTL]:
        export_jobs.pop(jid).cleanup()
