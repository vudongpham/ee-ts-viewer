"""The browser's size estimate (static/js/grid.js) must give the same pixel
grid as the server's export planner. Skipped when Node is not installed."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from rasterio.warp import transform

from eetsviewer import export as ex

NODE = shutil.which("node")
RUNNER = Path(__file__).parent / "js" / "run_grid.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


class FakeEE:
    """ee stand-in for plan_export: returns the given projection/footprint."""
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
            def get(self, k): return None
            def propertyNames(self): return self
            def contains(self, k): return None
        return Img()

    def Dictionary(self, d):
        info = self.info

        class D:
            def getInfo(self): return info
        return D()


def scene(epsg, x0, y0, size, res):
    """A square scene in UTM (like a Sentinel-2 tile / Landsat scene)."""
    xs = [x0, x0 + size, x0 + size, x0, x0]
    ys = [y0, y0, y0 - size, y0 - size, y0]
    lons, lats = transform(epsg, "EPSG:4326", xs, ys)
    footprint = {"type": "Polygon", "coordinates": [[[a, b] for a, b in zip(lons, lats)]]}
    proj = {"type": "Projection", "crs": epsg, "transform": [res, 0, x0, 0, -res, y0]}
    return footprint, proj


def compare(sensor, image_id, epsg, x0, y0, size, res, views):
    footprint, proj = scene(epsg, x0, y0, size, res)
    ex_info = {"proj": proj, "footprint": footprint, "time": 1707732621000}
    lons, lats = zip(*footprint["coordinates"][0])
    browser_info = {"footprint": footprint, "bbox": [min(lons), min(lats), max(lons), max(lats)],
                    "crs": epsg, "transform": proj["transform"], "resolution": res,
                    "bands": 12, "max_side": 1_000_000}
    out = subprocess.run([NODE, str(RUNNER)], input=json.dumps({"info": browser_info, "views": views}),
                         capture_output=True, text=True, check=True)
    estimates = json.loads(out.stdout)
    orig_ee, orig_max = ex.ee, ex.MAX_SIDE
    ex.ee, ex.MAX_SIDE = FakeEE(ex_info), 1_000_000
    try:
        for view, est in zip(views, estimates):
            try:
                plan = ex.plan_export(sensor, image_id, "scene" if view is None else "view", view)
            except ex.ExportError as exc:
                assert est.get("empty"), (view, exc)
                continue
            assert not est.get("empty"), view
            # identical grid (at most one pixel apart from rounding in the projection)
            assert abs(est["width"] - plan.width) <= 1 and abs(est["height"] - plan.height) <= 1, \
                (view, est, plan.width, plan.height)
    finally:
        ex.ee, ex.MAX_SIDE = orig_ee, orig_max


def random_views(bbox, n, seed):
    rng = np.random.default_rng(seed)
    w, s, e, n_ = bbox
    views = [None]                                   # full scene
    for _ in range(n):
        cx, cy = rng.uniform(w - 0.2, e + 0.2), rng.uniform(s - 0.2, n_ + 0.2)
        dx, dy = rng.uniform(0.005, 1.5), rng.uniform(0.005, 1.0)
        views.append([cx - dx, cy - dy, cx + dx, cy + dy])
    views.append([w - 20, s - 20, w - 10, s - 10])   # far away
    return views


def test_estimate_matches_server_grid_sentinel2_zone_edge():
    # T32UQD: eastern edge of UTM zone 32, grid rotated ~3 deg against north
    footprint, _ = scene("EPSG:32632", 699960, 5900040, 109800, 10)
    lons, lats = zip(*footprint["coordinates"][0])
    views = random_views([min(lons), min(lats), max(lons), max(lats)], 60, 1)
    compare("SEN2A", "20240212T101021_20240212T101019_T32UQD",
            "EPSG:32632", 699960, 5900040, 109800, 10, views)


def test_estimate_matches_server_grid_southern_hemisphere_30m():
    footprint, _ = scene("EPSG:32733", 300000, 7400000, 185000, 30)
    lons, lats = zip(*footprint["coordinates"][0])
    views = random_views([min(lons), min(lats), max(lons), max(lats)], 40, 2)
    compare("LND08", "LC08_176075_20240212", "EPSG:32733", 300000, 7400000, 185000, 30, views)
