import time

import numpy as np
import rasterio
from rasterio.transform import Affine

from eetsviewer import app as appmod
from eetsviewer import export as ex


def test_snap_to_grid():
    native = Affine(30, 0, 399960, 0, -30, 5900040)
    t, w, h = ex.snap_to_grid(native, (400000, 5800000, 400100, 5800050))
    # aligned with the native grid and covering the bounds
    assert (t.c - 399960) % 30 == 0 and (5900040 - t.f) % 30 == 0
    assert t.c <= 400000 and t.c + w * 30 >= 400100
    assert t.f >= 5800050 and t.f - h * 30 <= 5800000
    assert (w, h) == (4, 2)


def make_plan(sensor="SEN2A", width=2500, height=1100):
    native = Affine(10, 0, 300000, 0, -10, 5800000)
    return ex.Plan(sensor, "20240212T101021_20240212T101019_T32UQD",
                   {"crs": "EPSG:32632", "wkt": None}, native, width, height, "20240212")


def fake_fetch(plan, expression, col, row, w, h):
    # value encodes the band and the global pixel position; one nodata corner
    b = np.arange(len(plan.bands), dtype=np.int16)[:, None, None]
    rr, cc = np.mgrid[row:row + h, col:col + w]
    data = (b * 1000 + (rr % 10) * 10 + cc % 10).astype(np.int16)
    if col == 0 and row == 0:
        data[:, 0, 0] = ex.NODATA
    return data


def test_run_export_writes_geotiff(monkeypatch):
    monkeypatch.setattr(ex, "export_image", lambda *a: None)
    plan = make_plan()
    assert plan.name == "S2SR_SEN2A_20240212.tif"
    job = ex.ExportJob(plan)
    ex.run_export(job, fetch=fake_fetch)
    assert job.status == "done", job.error
    assert job.current == job.total == 3 * 2   # 1024-pixel blocks
    with rasterio.open(job.path) as src:
        assert src.count == 12 and src.dtypes[0] == "int16" and src.nodata == -9999
        assert (src.width, src.height) == (2500, 1100)
        assert src.crs.to_epsg() == 32632 and src.transform == plan.transform
        assert src.descriptions[8] == "B8A (NIR)"
        a = src.read()
    assert a[0, 0, 0] == -9999
    rr, cc = np.mgrid[0:1100, 0:2500]
    expected = (np.arange(12)[:, None, None] * 1000 + (rr % 10) * 10 + cc % 10)
    expected[:, 0, 0] = -9999
    assert np.array_equal(a, expected)
    job.cleanup()


def test_export_routes(monkeypatch):
    monkeypatch.setattr(ex, "export_image", lambda *a: None)
    monkeypatch.setattr(ex, "fetch_block", fake_fetch)
    monkeypatch.setattr(ex, "plan_export",
                        lambda sensor, image_id, extent, bounds, mask=None: make_plan("LND08", 300, 200))
    client = appmod.app.test_client()
    d = client.post("/export", json={"sensor": "LND08", "id": "LC08_193023_20240212",
                                     "extent": "view", "bounds": [13, 52, 14, 53]}).get_json()
    for _ in range(100):
        d = client.get(f"/export/{d['job']}").get_json()
        if d["status"] != "running":
            break
        time.sleep(0.05)
    assert d["status"] == "done" and d["name"] == "C2L2_LND08_20240212.tif" and d["bands"] == 7
    r = client.get(f"/export/{d['job']}/file")
    assert r.status_code == 200
    assert "C2L2_LND08_20240212.tif" in r.headers["Content-Disposition"]
    assert r.data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00")
    r.close()
    # the file is removed once it was sent
    assert client.get(f"/export/{d['job']}/file").status_code == 404


def test_export_rejects_bad_input():
    client = appmod.app.test_client()
    r = client.post("/export", json={"sensor": "LND08", "id": "../etc", "extent": "view"})
    assert r.status_code == 400
    r = client.post("/export", json={"sensor": "NOPE", "id": "x", "extent": "view"})
    assert r.status_code == 400


class FakeEE:
    """Stands in for the few ee objects plan_export uses."""
    EEException = Exception

    def __init__(self, info):
        self.info = info

    class Algorithms:
        @staticmethod
        def If(cond, a, b): return None

    def Image(self, asset):
        class Img:
            def select(self, b): return self
            def projection(self): return None
            def geometry(self): return self
            def bounds(self, e): return None
            def get(self, k): return None
            def propertyNames(self): return self
            def contains(self, k): return None
        return Img()

    def Dictionary(self, d):
        info = self.info

        class D:
            def getInfo(self): return info
        return D()


def tile_ring():
    """Corners of the 109.8 km UTM 32N tile at (699960, 5900040) in lon/lat."""
    from rasterio.warp import transform
    xs = [699960, 809760, 809760, 699960, 699960]
    ys = [5900040, 5900040, 5790240, 5790240, 5900040]
    lons, lats = transform("EPSG:32632", "EPSG:4326", xs, ys)
    return [[a, b] for a, b in zip(lons, lats)]


def s2_info():
    # Sentinel-2 tile T32UQD (UTM 32N), about 13.1..14.6 E, 52.2..53.2 N
    return {
        "proj": {"type": "Projection", "crs": "EPSG:32632",
                 "transform": [10, 0, 699960, 0, -10, 5900040]},
        "footprint": {"type": "Polygon", "coordinates": [tile_ring()]},
        "time": 1707732621000,   # 2024-02-12 10:10 UTC
    }


def test_plan_export_view_and_scene(monkeypatch):
    monkeypatch.setattr(ex, "ee", FakeEE(s2_info()))
    sid = "20240212T101021_20240212T101019_T32UQD"
    view = ex.plan_export("SEN2A", sid, "view", [12.60, 52.60, 12.70, 52.65])
    assert view.name == "S2SR_SEN2A_20240212.tif"
    assert view.transform.a == 10 and (view.transform.c - 699960) % 10 == 0
    # ~0.1 deg x 0.05 deg at 52.6 N is ~6.8 km x 5.6 km at 10 m (a bit more:
    # the box is rotated in UTM)
    assert 600 < view.width < 950 and 500 < view.height < 750
    assert len(view.bands) == 12

    # the view is clipped to the scene
    clipped = ex.plan_export("SEN2A", sid, "view", [10.0, 52.45, 13.60, 52.50])
    assert clipped.transform.c == 699960 and clipped.width == 10980

    # the full scene is exactly the 10980 x 10980 px tile, although the tile
    # is strongly rotated in lon/lat at the eastern edge of the UTM zone
    scene = ex.plan_export("SEN2A", sid, "scene")
    assert (scene.width, scene.height) == (10980, 10980)
    assert (scene.transform.c, scene.transform.f) == (699960, 5900040)


def test_plan_export_errors(monkeypatch):
    monkeypatch.setattr(ex, "ee", FakeEE(s2_info()))
    sid = "20240212T101021_20240212T101019_T32UQD"
    for args, msg in [(("view", [0, 0, 1, 1]), "does not cover"),
                      (("view", None), "bounds are missing"),
                      (("everything", None), "Unknown extent")]:
        try:
            ex.plan_export("SEN2A", sid, *args)
        except ex.ExportError as exc:
            assert msg in str(exc)
        else:
            raise AssertionError(args)
    monkeypatch.setattr(ex, "MAX_SIDE", 1000)
    try:
        ex.plan_export("SEN2A", sid, "scene")
    except ex.ExportError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("expected too large")


def test_hls_names_use_the_satellite(monkeypatch):
    info = s2_info()
    info["source"] = "S2B_MSIL1C_20240212T101019_N0510_R022_T32UQD_20240212T121006.SAFE"
    monkeypatch.setattr(ex, "ee", FakeEE(info))
    plan = ex.plan_export("HLSS30", "T32UQD_20240212T101019", "scene")
    assert plan.name == "HLSS30_SEN2B_20240212.tif"


# ---------------------------------------------------------------------------
# The Earth Engine cloud mask must match the time-series screening exactly.
# clear_mask() is run on a NumPy stand-in for ee.Image and compared, pixel by
# pixel, with sensors.is_clear() on random QA values.
# ---------------------------------------------------------------------------

class NpImage:
    def __init__(self, bands):
        self.bands = bands            # name -> np array (masked values: np.ma)

    def _one(self):
        return next(iter(self.bands.values()))

    def _op(self, other, fn):
        b = other._one() if isinstance(other, NpImage) else other
        return NpImage({"x": fn(self._one(), b)})

    def select(self, name):
        return NpImage({name: self.bands[name]})

    def rightShift(self, n): return self._op(n, lambda a, b: a >> b)
    def bitwiseAnd(self, n): return self._op(n, lambda a, b: a & b)
    def eq(self, v): return self._op(v, lambda a, b: (a == b).astype(int))
    def neq(self, v): return self._op(v, lambda a, b: (a != b).astype(int))
    def lt(self, v): return self._op(v, lambda a, b: (a < b).astype(int))
    def lte(self, v): return self._op(v, lambda a, b: (a <= b).astype(int))
    def gte(self, v): return self._op(v, lambda a, b: (a >= b).astype(int))
    def And(self, o): return self._op(o, lambda a, b: a & b)
    def Or(self, o): return self._op(o, lambda a, b: a | b)
    def multiply(self, v): return self._op(v, lambda a, b: a * b)
    def rename(self, n): return NpImage({n: self._one()})

    def unmask(self, v):
        a = self._one()
        return NpImage({"x": np.ma.filled(a, v) if np.ma.isMaskedArray(a) else a})


class NpEE:
    """ee namespace for clear_mask(); `cs` is the Cloud Score+ cs_cdf array
    (None = the scene has no Cloud Score+ image)."""

    def __init__(self, cs):
        self.cs = cs
        outer = self

        class Filter:
            @staticmethod
            def eq(k, v): return None

        class Algorithms:
            @staticmethod
            def If(cond, a, b): return a if cond else b

        class Collection:
            def filter(self, f): return self
            def size(self): return self
            def gt(self, n): return outer.cs is not None
            def first(self): return NpImage({"cs_cdf": outer.cs})

        class Image:
            def __new__(cls, x): return x
            @staticmethod
            def constant(v): return NpImage({"x": np.float64(v)})

        self.Filter, self.Algorithms, self.Image = Filter, Algorithms, Image
        self.ImageCollection = lambda name: Collection()


def _check_mask(monkeypatch, sensor, bands, cs, n):
    from eetsviewer import sensors as S
    monkeypatch.setattr(ex, "ee", NpEE(cs))
    scene = NpImage(bands)
    for level in (S.MASK_DEFAULT, S.MASK_AGGRESSIVE):
        got = np.asarray(ex.clear_mask(sensor, scene, "id", level)._one()).astype(bool)
        for i in range(n):
            qa = {k: (None if np.ma.is_masked(v[i]) else v[i].item()) for k, v in {
                **{name: bands[src] for name, src in S.SENSORS[sensor]["qa"].items()},
                **({"cs_cdf": cs} if cs is not None else {})}.items()}
            assert bool(got[i]) == S.is_clear(sensor, qa, level), (sensor, level, qa)


def test_cloud_mask_matches_time_series_rules(monkeypatch):
    rng = np.random.default_rng(1)
    n = 4000
    # Landsat: random QA_PIXEL bits; some RADSAT / aerosol / opacity values
    qa = rng.integers(0, 1 << 16, n)
    qa[: n // 2] &= ~0b111111          # half of them pass the Default flags
    radsat = np.where(rng.random(n) < 0.2, 4, 0)
    aer = rng.integers(0, 256, n)
    opa = np.ma.masked_array(rng.integers(0, 600, n), mask=rng.random(n) < 0.3)
    _check_mask(monkeypatch, "LND08", {"QA_PIXEL": qa, "QA_RADSAT": radsat, "SR_QA_AEROSOL": aer}, None, n)
    _check_mask(monkeypatch, "LND05", {"QA_PIXEL": qa, "QA_RADSAT": radsat, "SR_ATMOS_OPACITY": opa}, None, n)
    # HLS: random Fmask incl. fill
    fm = rng.integers(0, 256, n)
    fm[: n // 2] &= ~0b11110
    fm[:50] = 255
    _check_mask(monkeypatch, "HLSS30", {"Fmask": fm}, None, n)
    # Sentinel-2: random SCL and cs_cdf, with and without Cloud Score+
    scl = rng.integers(0, 12, n)
    cs = rng.random(n)
    _check_mask(monkeypatch, "SEN2B", {"SCL": scl}, cs, n)
    _check_mask(monkeypatch, "SEN2B", {"SCL": scl}, None, n)


def test_masked_plan_name(monkeypatch):
    monkeypatch.setattr(ex, "ee", FakeEE(s2_info()))
    sid = "20240212T101021_20240212T101019_T32UQD"
    assert ex.plan_export("SEN2A", sid, "scene", mask=1).name == "S2SR_SEN2A_20240212_masked.tif"
    assert ex.plan_export("SEN2A", sid, "scene", mask=2).name == "S2SR_SEN2A_20240212_masked_aggressive.tif"
    assert ex.plan_export("SEN2A", sid, "scene").name == "S2SR_SEN2A_20240212.tif"


# ---------------------------------------------------------------------------
# Cloud cover: masked pixels / valid pixels, on NumPy stand-ins for ee
# ---------------------------------------------------------------------------

class NpMaskImage:
    """Minimal ee.Image stand-in with a value array and a mask."""

    def __init__(self, v, mask=None):
        self.v = np.asarray(v, dtype=float)
        self.m = np.ones(self.v.shape, bool) if mask is None else np.asarray(mask, bool)

    def And(self, o): return NpMaskImage((self.v > 0) & (o.v > 0), self.m & o.m)
    def rename(self, name): return self
    def updateMask(self, o): return NpMaskImage(self.v, self.m & (o.v > 0) & o.m)
    def select(self, b): return self
    def projection(self): return None
    def geometry(self): return None

    def reduceRegion(self, **kw):
        v = self.v[self.m]
        info = {"clear_mean": float(v.mean()) if v.size else None, "clear_count": int(v.size)}

        class R:
            def getInfo(self): return info
        return R()


class CloudEE:
    class Reducer:
        @staticmethod
        def mean():
            class M:
                def combine(self, *a, **k): return None
            return M()

        @staticmethod
        def count():
            return None

    def Image(self, asset):
        return NpMaskImage([0])


def test_cloud_cover_is_masked_over_valid(monkeypatch):
    # 10 x 10 scene: 20 fill pixels, 80 valid of which 20 masked -> 25 %
    valid = np.ones((10, 10)); valid[:2] = 0
    clear = np.ones((10, 10)); clear[2:4] = 0
    monkeypatch.setattr(ex, "ee", CloudEE())
    monkeypatch.setattr(ex, "clear_mask",
                        lambda sensor, scene, iid, level: NpMaskImage(valid if level == 0 else clear))
    ex._cloud_cache.clear()
    r = ex.cloud_cover("SEN2A", "20240212T101021_20240212T101019_T32UQD", 1)
    assert r == {"cloud": 25.0, "valid_pixels": 80, "scale": ex.CLOUD_SCALE}
    # cached: a second call does not compute again
    monkeypatch.setattr(ex, "clear_mask", None)
    assert ex.cloud_cover("SEN2A", "20240212T101021_20240212T101019_T32UQD", 1)["cloud"] == 25.0


def test_cloud_cover_route(monkeypatch):
    monkeypatch.setattr(ex, "cloud_cover", lambda s, i, level: {"cloud": 12.5, "valid_pixels": 10, "scale": 100,
                                                               "level": level})
    client = appmod.app.test_client()
    r = client.get("/export/cloud?sensor=LND08&id=LC08_193023_20240212&mask=2").get_json()
    assert r["cloud"] == 12.5 and r["level"] == 2
    monkeypatch.undo()
    assert client.get("/export/cloud?sensor=LND08&id=../x&mask=1").status_code == 400
    assert client.get("/export/cloud?sensor=LND08&id=LC08_193023_20240212&mask=0").status_code == 400
    assert client.get("/export/cloud?sensor=NOPE&id=x&mask=1").status_code == 400
