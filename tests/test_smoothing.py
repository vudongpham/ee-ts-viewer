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
        if "curve" not in r:
            continue
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


# ---------------------------------------------------------------------------
# lines over the whole date range: copies of the series before and after it
# ---------------------------------------------------------------------------

DAY_MS = 86400000


def ms(date):
    return float(np.datetime64(date, "ms").astype(np.int64))


def day(date):
    return ms(date) / DAY_MS


def raw(*calls):
    req = {"x": [], "y": [], "calls": [{"fn": fn, "args": list(args), "raw": True}
                                       for fn, *args in calls]}
    out = subprocess.run([NODE, str(RUNNER)], input=json.dumps(req), capture_output=True,
                         text=True, check=True)
    return [r["value"] for r in json.loads(out.stdout)]


def test_shift_by_calendar_years_with_leap_days():
    shifted = raw(("shiftYears", ms("2018-03-15"), -3), ("shiftYears", ms("2018-03-15"), 3),
                  ("shiftYears", ms("2020-02-29"), 1), ("shiftYears", ms("2019-12-31"), 1),
                  ("shiftYears", ms("2019-03-01"), 1))
    assert shifted == [ms("2015-03-15"), ms("2021-03-15"), ms("2021-02-28"),
                       ms("2020-12-31"), ms("2020-03-01")]


def test_years_covering_the_date_range():
    assert raw(("yearsCovering", ms("2018-01-01"), ms("2020-12-31")),
               ("yearsCovering", ms("2018-01-01"), ms("2021-01-01")),
               ("yearsCovering", ms("2018-04-01"), ms("2018-09-30")),
               ("yearsCovering", ms("2018-04-01"), ms("2019-03-31"))) == [3, 4, 1, 1]


def test_series_is_copied_back_and_forward():
    # a 2018-2020 series gets copies in 2015-2017 and 2021-2023
    dates = ["2018-02-10", "2019-07-01", "2020-02-29", "2020-11-20"]
    x = [day(d) for d in dates]
    y = [100, 200, 300, 400]
    (r,) = run(x, y, ("extendPeriodic", ms("2018-01-01"), ms("2020-12-31")))
    expected = ([day(d) for d in ["2015-02-10", "2016-07-01", "2017-02-28", "2017-11-20"]]
                + x + [day(d) for d in ["2021-02-10", "2022-07-01", "2023-02-28", "2023-11-20"]])
    assert list(r["x"]) == expected
    assert list(r["y"]) == y * 3


def test_clip_interpolates_at_both_ends():
    (r,) = run([10, 20, 30, 40], [0, 100, 200, 300], ("clipCurve", 15 * DAY_MS, 35 * DAY_MS))
    assert list(r["x"]) == [15, 20, 30, 35]
    assert list(r["y"]) == [50, 100, 200, 250]


def test_lines_span_the_whole_date_range():
    # observations only from April to October; the line must still reach the
    # start (1 Jan) and end date (31 Dec) of each year of the range
    days = np.array([d for d in np.arange(day("2018-04-05"), day("2020-10-25"), 5)
                     if 90 <= (d - day("2018-01-01")) % 365.25 <= 300])
    _, y, _ = seasonal(days, noise=100)
    from_, to = ms("2018-01-01"), ms("2020-12-31")
    (ext,) = run(days, y, ("extendPeriodic", from_, to))
    rng = {"from": from_, "to": to}
    for r in run(ext["x"], ext["y"], ("movingAverage", rng), ("savitzkyGolay", rng), ("rbf", rng)):
        assert r["x"][0] == day("2018-01-01") and r["x"][-1] == day("2020-12-31")
    # linear interpolation over the copies, cut at the start and end date
    (lin,) = run(ext["x"], ext["y"], ("clipCurve", from_, to))
    assert lin["x"][0] == day("2018-01-01") and lin["x"][-1] == day("2020-12-31")
    assert not np.isnan(lin["y"]).any()


def _null_at(r, date):
    return bool(np.isnan(r["y"][int(np.argmin(np.abs(np.asarray(r["x"]) - day(date))))]))


def test_lines_reach_both_dates_across_winter_seams():
    # April-October only: the seams with the copies (Nov-Mar) are longer than
    # the gap limit, but the lines must still have values at both dates;
    # the winters inside the range stay broken
    days = np.array([d for d in np.arange(day("2018-04-05"), day("2020-10-25"), 5)
                     if 90 <= (d - day("2018-01-01")) % 365.25 <= 300])
    _, y, _ = seasonal(days, noise=100)
    from_, to = ms("2018-01-01"), ms("2020-12-31")
    (ext,) = run(days, y, ("extendPeriodic", from_, to))
    rng = {"from": from_, "to": to}
    for r in run(ext["x"], ext["y"], ("movingAverage", rng), ("savitzkyGolay", rng), ("rbf", rng)):
        assert not _null_at(r, "2018-01-01") and not _null_at(r, "2020-12-31")
        assert not _null_at(r, "2018-02-15") and not _null_at(r, "2020-12-01")
        assert _null_at(r, "2019-01-15")          # winter inside the range


def test_lines_reach_an_end_date_in_the_future():
    # range 2026-01-01 .. 2026-12-31 with data only mid-Feb .. early Nov
    # (the rest is not available yet): the copies of 2026 in 2025 and 2027
    # fill both ends
    days = np.arange(day("2026-02-14"), day("2026-11-05"), 5).astype(float)
    _, y, _ = seasonal(days, noise=50)
    from_, to = ms("2026-01-01"), ms("2026-12-31")
    (ext,) = run(days, y, ("extendPeriodic", from_, to))
    rng = {"from": from_, "to": to}
    for r in run(ext["x"], ext["y"], ("movingAverage", rng), ("savitzkyGolay", rng), ("rbf", rng)):
        assert r["x"][0] == day("2026-01-01") and r["x"][-1] == day("2026-12-31")
        assert not np.isnan(r["y"]).any()
