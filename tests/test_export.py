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
    monkeypatch.setattr(ex, "export_image", lambda sensor, image_id: None)
    plan = make_plan()
    assert plan.name == "SEN2A_20240212.tif"
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
    monkeypatch.setattr(ex, "export_image", lambda sensor, image_id: None)
    monkeypatch.setattr(ex, "fetch_block", fake_fetch)
    monkeypatch.setattr(ex, "plan_export",
                        lambda sensor, image_id, extent, bounds: make_plan("LND08", 300, 200))
    client = appmod.app.test_client()
    d = client.post("/export", json={"sensor": "LND08", "id": "LC08_193023_20240212",
                                     "extent": "view", "bounds": [13, 52, 14, 53]}).get_json()
    for _ in range(100):
        d = client.get(f"/export/{d['job']}").get_json()
        if d["status"] != "running":
            break
        time.sleep(0.05)
    assert d["status"] == "done" and d["name"] == "LND08_20240212.tif" and d["bands"] == 7
    r = client.get(f"/export/{d['job']}/file")
    assert r.status_code == 200
    assert "LND08_20240212.tif" in r.headers["Content-Disposition"]
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

    def Image(self, asset):
        class Img:
            def select(self, b): return self
            def projection(self): return None
            def geometry(self): return self
            def bounds(self, e): return None
            def get(self, k): return None
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
    assert view.name == "SEN2A_20240212.tif"
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
