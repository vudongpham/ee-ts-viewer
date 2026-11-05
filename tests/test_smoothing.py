"""Tests of the adaptive line modes (eetsviewer/static/js/smoothing.js),
run with Node. Skipped when Node is not installed."""

import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

NODE = shutil.which("node")
RUNNER = Path(__file__).parent / "js" / "run_smoothing.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(x, y, *calls):
    req = {"x": list(map(float, x)), "y": list(map(float, y)),
           "calls": [{"fn": fn, "args": list(args)} for fn, *args in calls]}
    out = subprocess.run([NODE, str(RUNNER)], input=json.dumps(req), capture_output=True,
                         text=True, check=True)
    res = json.loads(out.stdout)
    for r in res:
        r["x"] = np.array(r["curve"]["x"])
        r["y"] = np.array([np.nan if v is None else v for v in r["curve"]["y"]], dtype=float)
    return res


# reference implementation (the RBF interpolation script this is based on)
def reference_rbf(x, y, sigmas, step):
    x_int = np.arange(np.min(x), np.max(x) + step, step)
    ys, ws = [], []
    for sigma in sigmas:
        yi, wi = np.full(len(x_int), np.nan), np.zeros(len(x_int))
        for i, xi in enumerate(x_int):
            dx = x - xi
            keep = np.abs(dx) <= 3 * sigma
            if np.any(keep):
                w = np.exp(-0.5 * (dx[keep] / sigma) ** 2)
                yi[i] = np.sum(w * y[keep]) / np.sum(w)
                wi[i] = np.sum(keep)
        ys.append(yi)
        ws.append(wi)
    ys, ws = np.vstack(ys), np.vstack(ws)
    out = np.full(len(x_int), np.nan)
    for i in range(len(x_int)):
        ok = ~np.isnan(ys[:, i]) & (ws[:, i] > 0)
        if np.any(ok):
            out[i] = np.sum(ys[ok, i] * ws[ok, i]) / np.sum(ws[ok, i])
    return x_int, out


def seasonal(days, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    days = np.sort(np.asarray(days, dtype=float))
    clean = 4000 + 3000 * np.sin(2 * np.pi * (days - 18000) / 365.25)
    return days, clean + rng.normal(0, noise, len(days)), clean


def test_rbf_matches_reference_with_count_weighting():
    rng = np.random.default_rng(3)
    x = np.concatenate([rng.choice(np.arange(18000, 18300), 40, replace=False),
                        rng.choice(np.arange(18420, 18800), 50, replace=False)])
    x, y, _ = seasonal(x, noise=300, seed=3)
    xr, yr = reference_rbf(x, y, [24, 40, 56], 8)
    (r,) = run(x, y, ("rbfEnsemble", [24, 40, 56], {"weighting": "count", "step": 8}))
    assert np.allclose(r["x"], xr)
    assert np.allclose(r["y"], yr, rtol=0, atol=1e-6, equal_nan=True)


def test_parameters_follow_observation_density():
    dense, y_d, _ = seasonal(np.arange(18000, 18730, 5))       # e.g. Sentinel-2A + 2B
    sparse, y_s, _ = seasonal(np.arange(18000, 18730, 16))     # e.g. one Landsat
    calls = (("movingAverage",), ("savitzkyGolay",), ("rbf",))
    rd, rs = run(dense, y_d, *calls), run(sparse, y_s, *calls)
    assert rd[0]["params"]["spacing"] == 5 and rs[0]["params"]["spacing"] == 16
    for d, s in zip(rd[:2], rs[:2]):          # wider windows for sparse data
        assert d["params"]["hMax"] < s["params"]["hMax"]
    assert rd[2]["params"]["sigmas"] == [10, 18, 25]
    assert rs[2]["params"]["sigmas"] == [32, 56, 80]


def test_gaps_are_not_bridged():
    x = np.concatenate([np.arange(18000, 18200, 8), np.arange(18600, 18800, 8)])   # 400-day gap
    x, y, _ = seasonal(x, noise=200)
    for r in run(x, y, ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        inside = (r["x"] > 18300) & (r["x"] < 18500)
        assert inside.any() and np.isnan(r["y"][inside]).all()
        outside = (r["x"] < 18150) | (r["x"] > 18650)
        assert not np.isnan(r["y"][outside]).any()


def test_smoothing_reduces_noise():
    x, y, clean = seasonal(np.arange(18000, 18730, 6), noise=500, seed=1)
    for r in run(x, y, ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        fitted = np.interp(x, r["x"], r["y"])
        assert np.sqrt(np.mean((fitted - clean) ** 2)) < 0.6 * 500


def test_savitzky_golay_does_not_overshoot():
    # few, clustered observations with a jump: a free quadratic would overshoot
    x = np.array([18000, 18003, 18006, 18100, 18103, 18106, 18200, 18203], dtype=float)
    y = np.array([1000, 1050, 980, 7000, 7100, 6950, 1200, 1100], dtype=float)
    (r,) = run(x, y, ("savitzkyGolay",))
    v = r["y"][~np.isnan(r["y"])]
    pad = 0.25 * (y.max() - y.min())
    assert v.min() >= y.min() - pad and v.max() <= y.max() + pad


def test_too_few_observations():
    for r in run([18000, 18010], [1000, 2000], ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        assert r["params"] is None and len(r["x"]) == 0


def test_same_gap_rule_for_all_methods():
    # 9-day spacing -> gaps longer than 72 days are not bridged by any method
    x = np.concatenate([np.arange(18000, 18300, 9), np.arange(18390, 18700, 9)])  # 99-day gap
    x, y, _ = seasonal(x, noise=200)
    for r in run(x, y, ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        assert r["params"]["gap"] == 72
        inside = (r["x"] > 18300) & (r["x"] < 18390)
        assert np.isnan(r["y"][inside]).all()
    # a 50-day gap is bridged
    x = np.concatenate([np.arange(18000, 18300, 9), np.arange(18341, 18700, 9)])
    x, y, _ = seasonal(x, noise=200)
    for r in run(x, y, ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        inside = (r["x"] > 18300) & (r["x"] < 18341)
        assert not np.isnan(r["y"][inside]).any()


def gentle_season(days):
    d = np.asarray(days, dtype=float) % 365
    return 2000 + 6000 * (1 / (1 + np.exp(-(d - 120) / 20)) - 1 / (1 + np.exp(-(d - 270) / 25)))


def test_curves_have_no_kinks():
    # noise-free season, every 5 days with missing days: the curves must be
    # smooth everywhere, including the ends of the series (no jumps from
    # switching window size or polynomial degree)
    days = np.array([d for d in range(0, 730, 5) if (d * 7919) % 10 >= 3], dtype=float) + 18000
    y = gentle_season(days - 18000)
    for r in run(days, y, ("movingAverage",), ("savitzkyGolay",), ("rbf",)):
        v = r["y"][~np.isnan(r["y"])]
        second = np.abs(np.diff(v, 2))
        assert second.max() < 25, second.max()


def test_savitzky_golay_resists_cloud_outliers():
    rng = np.random.default_rng(5)
    days = np.arange(0, 730, 5, dtype=float)
    y = gentle_season(days) + rng.normal(0, 300, len(days))
    clouds = rng.random(len(days)) < 0.08
    y[clouds] -= 2500                                  # undetected clouds
    (robust, plain) = run(days + 18000, y, ("savitzkyGolay",), ("savitzkyGolay", {"robust": 0}))
    truth = gentle_season(robust["x"] - 18000)
    bias = lambda r: np.nanmean(r["y"] - truth)
    assert abs(bias(robust)) < 0.5 * abs(bias(plain))
    assert abs(bias(robust)) < 120
