"""Earth Engine access: initialisation, pixel time-series requests and map tiles."""

import json
import re
import threading
import time
from collections import OrderedDict
from datetime import date

import ee

from .sensors import CLOUD_SCORE_PLUS, LANDSAT_GAIN, LANDSAT_OFFSET, SENSORS

DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def initialize(project=None, key_file=None, high_volume=False):
    """Initialise the Earth Engine client.

    * key_file: service account JSON key (the project defaults to the
      key's project_id)
    * otherwise the persistent credentials written by
      `earthengine authenticate` are used.
    """
    url = "https://earthengine-highvolume.googleapis.com" if high_volume else None
    credentials = "persistent"
    if key_file:
        with open(key_file) as f:
            info = json.load(f)
        credentials = ee.ServiceAccountCredentials(info["client_email"], key_file)
        project = project or info.get("project_id")
    if not project:
        raise SystemExit(
            "A Google Cloud project with the Earth Engine API enabled is required: "
            "pass --project (or set EE_PROJECT)."
        )
    ee.Initialize(credentials=credentials, project=project, url=url)
    return project


# ---------------------------------------------------------------------------
# Pixel time series
# ---------------------------------------------------------------------------

def _with_cloud_score(img):
    """Make sure every Sentinel-2 image has a cs_cdf band (masked = unknown)."""
    img = ee.Image(img)
    missing = img.addBands(ee.Image.constant(0).float().updateMask(0).rename("cs_cdf"))
    return ee.Image(ee.Algorithms.If(img.bandNames().contains("cs_cdf"), img, missing))


def build_collection(sensor, point, start, end):
    """Collection of `sensor` over `point` with canonical band names."""
    s = SENSORS[sensor]
    col = (ee.ImageCollection(s["collection"])
           .filterBounds(point)
           .filterDate(start, end))
    src = list(s["bands"].values()) + list(s["qa"].values())
    dst = list(s["bands"].keys()) + list(s["qa"].keys())
    if s["family"] == "SEN":
        col = col.filter(ee.Filter.eq("SPACECRAFT_NAME", s["spacecraft"]))
        col = col.linkCollection(ee.ImageCollection(CLOUD_SCORE_PLUS), ["cs_cdf"])
        col = col.map(_with_cloud_score)
        src.append("cs_cdf")
        dst.append("cs_cdf")
    return col.select(src, dst)


RETRY_PATTERNS = re.compile(
    r"too many|rate limit|quota|429|timed out|deadline|unavailable|internal error|503|500",
    re.IGNORECASE,
)


EMPTY_COLLECTION = re.compile(r"no bands in collection", re.IGNORECASE)


def _get_info(obj, retries=4):
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            return obj.getInfo()
        except ee.EEException as exc:
            if attempt == retries or not RETRY_PATTERNS.search(str(exc)):
                raise
        except (OSError, ConnectionError):  # transient network errors
            if attempt == retries:
                raise
        time.sleep(delay)
        delay *= 2


def fetch_period(sensor, lng, lat, p0, p1):
    """All observations of `sensor` at (lat, lng) acquired in [p0, p1).

    Returns a list of dicts {id, day, values: {band: value or None}} sorted by
    date; `values` holds the raw DNs of the reflectance and QA bands.
    """
    s = SENSORS[sensor]
    point = ee.Geometry.Point([lng, lat])
    col = build_collection(sensor, point, p0.isoformat(), p1.isoformat())
    try:
        region = _get_info(col.getRegion(point, s["scale"]))
    except ee.EEException as exc:
        # getRegion fails on an empty collection: no scene of this sensor
        # covers the location in this period, which is not an error
        if EMPTY_COLLECTION.search(str(exc)):
            return []
        raise
    if not region or len(region) < 2:
        return []
    header = region[0]
    i_id, i_time = header.index("id"), header.index("time")
    names = [(i, h) for i, h in enumerate(header) if h not in ("id", "longitude", "latitude", "time")]
    rows = []
    for rec in region[1:]:
        if rec[i_time] is None:
            continue
        values = {h: rec[i] for i, h in names}
        if all(v is None for v in values.values()):
            continue  # point falls in the no-data part of the scene
        rows.append({"id": rec[i_id], "day": int(rec[i_time] // DAY_MS), "values": values})
    rows.sort(key=lambda r: (r["day"], r["id"]))
    return rows


class PeriodCache:
    """LRU of (sensor, lng, lat, year, start month) -> rows.

    Periods that may still receive new acquisitions (this year and last
    year) expire after `recent_ttl` seconds; older ones are kept until
    evicted.
    """

    def __init__(self, maxsize=4000, recent_ttl=3600):
        self.maxsize = maxsize
        self.recent_ttl = recent_ttl
        self._data = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(sensor, lng, lat, p0):
        return sensor, round(float(lng), 6), round(float(lat), 6), p0.year, p0.month

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            stamp, rows = hit
            recent = key[3] >= date.today().year - 1
            if recent and time.time() - stamp > self.recent_ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return rows

    def put(self, key, rows):
        with self._lock:
            self._data[key] = (time.time(), rows)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


period_cache = PeriodCache()


def fetch_period_cached(sensor, lng, lat, p0, p1):
    key = PeriodCache.key(sensor, lng, lat, p0)
    rows = period_cache.get(key)
    if rows is None:
        rows = fetch_period(sensor, lng, lat, p0, p1)
        period_cache.put(key, rows)
    return rows


# ---------------------------------------------------------------------------
# Image overlay (map tiles)
# ---------------------------------------------------------------------------

VIS_PRESETS = {
    "true":  (("RED", "GREEN", "BLUE"), 0, 3000),
    "false": (("NIR", "RED", "GREEN"), 0, 5000),
    "swir":  (("SWIR2", "NIR", "RED"), 0, 5000),
}

IMAGE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")  # e.g. HLS ids contain dots

_tile_cache = OrderedDict()
_tile_lock = threading.Lock()
TILE_TTL = 3600  # map ids are only valid for a limited time


def tile_url(sensor, image_id, vis="true"):
    """XYZ tile URL template of one scene rendered with a preset."""
    if sensor not in SENSORS:
        raise ValueError(f"Unknown sensor {sensor!r}")
    if not IMAGE_ID.match(image_id or ""):
        raise ValueError("Invalid image id")
    if vis not in VIS_PRESETS:
        raise ValueError(f"Unknown visualisation {vis!r}")

    key = (sensor, image_id, vis)
    with _tile_lock:
        hit = _tile_cache.get(key)
        if hit and time.time() - hit[0] < TILE_TTL:
            return hit[1]

    s = SENSORS[sensor]
    bands, vmin, vmax = VIS_PRESETS[vis]
    img = ee.Image(f"{s['collection']}/{image_id}").select([s["bands"][b] for b in bands])
    if s["family"] == "LND":
        img = img.multiply(LANDSAT_GAIN).add(LANDSAT_OFFSET)
    elif s["family"] == "HLS":
        img = img.multiply(10000)
    map_id = img.getMapId({"min": vmin, "max": vmax, "gamma": 1.2})
    url = map_id["tile_fetcher"].url_format

    with _tile_lock:
        _tile_cache[key] = (time.time(), url)
        while len(_tile_cache) > 256:
            _tile_cache.popitem(last=False)
    return url
