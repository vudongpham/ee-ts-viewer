from datetime import date

from eetsviewer import sensors as S
from eetsviewer.app import observations


def test_landsat_scaling():
    # rho = DN * 0.0000275 - 0.2  ->  DN 21818 ~ 0.4
    assert S.compute_value("LND08", {"NIR": 21818}, "NIR") == 4000
    ndvi = S.compute_value("LND08", {"NIR": 21818, "RED": 10909}, "NDVI")
    assert ndvi == round((4000 - 1000) / (4000 + 1000) * 10000)


def test_sentinel2_values_and_indices():
    raw = {"BLUE": 500, "GREEN": 800, "RED": 600, "NIR": 3000, "SWIR1": 2000, "SWIR2": 1000}
    assert S.compute_value("SEN2A", raw, "RED") == 600
    assert S.compute_value("SEN2A", raw, "NDVI") == 6667
    assert S.compute_value("SEN2A", raw, "NBR") == 5000
    assert S.compute_value("SEN2A", raw, "NDMI") == 2000
    assert S.compute_value("SEN2A", raw, "NDWI") == round((800 - 3000) / 3800 * 10000)
    evi = 2.5 * (0.3 - 0.06) / (0.3 + 6 * 0.06 - 7.5 * 0.05 + 1) * 10000
    assert S.compute_value("SEN2A", raw, "EVI") == round(evi)
    assert S.compute_value("LND08", raw, "RE1") is None  # S2-only band
    assert S.compute_value("SEN2A", {"NIR": 0, "RED": 0}, "NDVI") is None


CLEAR = 1 << 6 | 1 << 8          # clear, low cloud confidence
CLOUD = 1 << 3 | 3 << 8          # cloud, high confidence
SHADOW = 1 << 4
MEDIUM_CONF = 1 << 6 | 2 << 8    # clear but medium cloud confidence


def test_landsat_mask():
    assert S.landsat_clear({"QA_PIXEL": CLEAR}, S.MASK_DEFAULT)
    assert not S.landsat_clear({"QA_PIXEL": CLOUD}, S.MASK_DEFAULT)
    assert not S.landsat_clear({"QA_PIXEL": SHADOW}, S.MASK_DEFAULT)
    assert S.landsat_clear({"QA_PIXEL": CLOUD}, S.MASK_NONE)
    assert not S.landsat_clear({"QA_PIXEL": 1}, S.MASK_NONE)  # fill
    assert S.landsat_clear({"QA_PIXEL": MEDIUM_CONF}, S.MASK_DEFAULT)
    assert not S.landsat_clear({"QA_PIXEL": MEDIUM_CONF}, S.MASK_AGGRESSIVE)
    assert not S.landsat_clear({"QA_PIXEL": CLEAR, "QA_RADSAT": 4}, S.MASK_AGGRESSIVE)
    assert not S.landsat_clear({"QA_PIXEL": CLEAR, "AEROSOL": 0b11000000}, S.MASK_AGGRESSIVE)
    assert not S.landsat_clear({"QA_PIXEL": CLEAR, "OPACITY": 500}, S.MASK_AGGRESSIVE)
    assert S.landsat_clear({"QA_PIXEL": CLEAR, "QA_RADSAT": 0, "OPACITY": 100}, S.MASK_AGGRESSIVE)


def test_sentinel2_mask():
    assert S.sentinel2_clear({"SCL": 4, "cs_cdf": 0.9}, S.MASK_DEFAULT)
    assert S.sentinel2_clear({"SCL": 4, "cs_cdf": None}, S.MASK_DEFAULT)
    assert not S.sentinel2_clear({"SCL": 4, "cs_cdf": 0.3}, S.MASK_DEFAULT)
    assert not S.sentinel2_clear({"SCL": 9, "cs_cdf": 0.9}, S.MASK_DEFAULT)
    assert S.sentinel2_clear({"SCL": 7, "cs_cdf": 0.6}, S.MASK_DEFAULT)
    assert not S.sentinel2_clear({"SCL": 7, "cs_cdf": 0.9}, S.MASK_AGGRESSIVE)
    assert not S.sentinel2_clear({"SCL": 4, "cs_cdf": 0.6}, S.MASK_AGGRESSIVE)
    assert S.sentinel2_clear({"SCL": 9}, S.MASK_NONE)
    assert not S.sentinel2_clear({"SCL": 0}, S.MASK_NONE)


def test_sensor_years():
    assert S.sensor_years("LND05", date(2010, 5, 1), date(2015, 1, 1)) == [2010, 2011, 2012]
    assert S.sensor_years("LND09", date(2010, 1, 1), date(2020, 1, 1)) == []
    assert S.sensor_years("SEN2B", date(2016, 1, 1), date(2018, 1, 1)) == [2017, 2018]


def test_observations_dedupe_same_day():
    cloudy = {"SCL": 9, "cs_cdf": 0.1, "NIR": 3000, "RED": 600}
    clear = {"SCL": 4, "cs_cdf": 0.9, "NIR": 3000, "RED": 600}
    rows = [
        {"id": "a_T32UPU", "day": 100, "values": cloudy},
        {"id": "a_T32UPV", "day": 100, "values": clear},   # overlapping granule
        {"id": "b_T32UPU", "day": 105, "values": clear},
        {"id": "c_T32UPU", "day": 200, "values": clear},   # outside the range
    ]
    images, points = observations("SEN2A", rows, 0, 150, "NDVI", S.MASK_DEFAULT)
    assert images == 2
    assert points == [[100, 6667, "SEN2A", "a_T32UPV"], [105, 6667, "SEN2A", "b_T32UPU"]]


def test_hls_scaling_and_mask():
    raw = {"RED": 0.05, "NIR": 0.30, "FMASK": 0}
    assert S.compute_value("HLSL30", raw, "RED") == 500
    assert S.compute_value("HLSL30", raw, "NDVI") == round(0.25 / 0.35 * 10000)
    assert S.compute_value("HLSL30", {"RED": -0.9999}, "RED") is None   # fill
    assert S.hls_clear({"FMASK": 0}, S.MASK_DEFAULT)
    assert S.hls_clear({"FMASK": 1 << 5 | 1}, S.MASK_DEFAULT)          # water, reserved bit
    for bit in (1, 2, 3, 4):                                            # cloud, adjacent, shadow, snow
        assert not S.hls_clear({"FMASK": 1 << bit}, S.MASK_DEFAULT)
        assert S.hls_clear({"FMASK": 1 << bit}, S.MASK_NONE)
    assert S.hls_clear({"FMASK": 3 << 6}, S.MASK_DEFAULT)               # high aerosol
    assert not S.hls_clear({"FMASK": 3 << 6}, S.MASK_AGGRESSIVE)
    assert not S.hls_clear({"FMASK": 255}, S.MASK_NONE)                 # fill
    assert S.sensor_years("HLSL30", date(2010, 1, 1), date(2014, 6, 1)) == [2013, 2014]


def test_tasseled_cap():
    raw = {"BLUE": 500, "GREEN": 800, "RED": 600, "NIR": 3000, "SWIR1": 2000, "SWIR2": 1000}
    vals = [raw[b] for b in S.TC_BANDS]
    for band in ("TCB", "TCG", "TCW"):
        expected = round(sum(c * v for c, v in zip(S.TC_COEFFS[band], vals)))
        assert S.compute_value("SEN2A", raw, band) == expected
    # same reflectances in HLS float units give the same result
    hls = {b: v / 10000 for b, v in raw.items()}
    assert S.compute_value("HLSL30", hls, "TCG") == S.compute_value("SEN2A", raw, "TCG")
    assert S.compute_value("SEN2A", {"NIR": 3000}, "TCW") is None


def test_hlss30():
    raw = {"RED": 0.05, "NIR": 0.30, "RE1": 0.12, "FMASK": 0}
    assert S.compute_value("HLSS30", raw, "RE1") == 1200
    assert S.compute_value("HLSS30", raw, "NDVI") == S.compute_value("HLSL30", raw, "NDVI")
    assert "RE1" not in S.SENSORS["HLSL30"]["bands"]        # no red edge on OLI
    assert not S.is_clear("HLSS30", {"FMASK": 1 << 1}, S.MASK_DEFAULT)
    assert S.SENSORS["HLSS30"]["bands"]["NIR"] == "B8A"
    assert S.sensor_years("HLSS30", date(2014, 1, 1), date(2016, 6, 1)) == [2015, 2016]


# band names as listed in the Earth Engine catalog
CATALOG_BANDS = {
    "NASA/HLS/HLSL30/v002": {"B1", "B2", "B3", "B4", "B5", "B6", "B7", "B9", "B10", "B11",
                             "Fmask", "SZA", "SAA", "VZA", "VAA"},
    "NASA/HLS/HLSS30/v002": {"B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B9", "B10",
                             "B11", "B12", "Fmask", "SZA", "SAA", "VZA", "VAA"},
}


def test_hls_band_names_exist():
    for code in ("HLSL30", "HLSS30"):
        s = S.SENSORS[code]
        used = set(s["bands"].values()) | set(s["qa"].values())
        assert used <= CATALOG_BANDS[s["collection"]], (code, used - CATALOG_BANDS[s["collection"]])
    assert S.SENSORS["HLSL30"]["bands"]["NIR"] == "B5"
