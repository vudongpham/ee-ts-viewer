"""Sensor definitions, quality screening and band / index computation.

This module is pure Python (no Earth Engine calls) so the masking and index
logic can be tested without credentials.

All reflectances are returned as integers scaled by 10000 (the same
convention as FORCE BOA products), indices are scaled by 10000 too.
"""

from datetime import date

# Canonical band names used throughout the app. Sentinel-2 has more bands
# than Landsat; the red-edge bands and the broad NIR (B8) exist for
# Sentinel-2 only.
REFLECTANCE_BANDS = ("BLUE", "GREEN", "RED", "RE1", "RE2", "RE3", "BNIR", "NIR", "SWIR1", "SWIR2")
SEN2_ONLY = ("RE1", "RE2", "RE3", "BNIR")
INDICES = ("NDVI", "EVI", "NBR", "NDMI", "NDWI", "TCB", "TCG", "TCW")
ALL_BANDS = REFLECTANCE_BANDS + INDICES

# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

# Landsat Collection 2, Level-2 surface reflectance (Tier 1)
TM_BANDS = {  # Landsat 4/5 TM and Landsat 7 ETM+
    "BLUE": "SR_B1", "GREEN": "SR_B2", "RED": "SR_B3",
    "NIR": "SR_B4", "SWIR1": "SR_B5", "SWIR2": "SR_B7",
}
OLI_BANDS = {  # Landsat 8/9 OLI
    "BLUE": "SR_B2", "GREEN": "SR_B3", "RED": "SR_B4",
    "NIR": "SR_B5", "SWIR1": "SR_B6", "SWIR2": "SR_B7",
}
TM_QA = {"QA_PIXEL": "QA_PIXEL", "QA_RADSAT": "QA_RADSAT", "OPACITY": "SR_ATMOS_OPACITY"}
OLI_QA = {"QA_PIXEL": "QA_PIXEL", "QA_RADSAT": "QA_RADSAT", "AEROSOL": "SR_QA_AEROSOL"}

S2_BANDS = {
    "BLUE": "B2", "GREEN": "B3", "RED": "B4",
    "RE1": "B5", "RE2": "B6", "RE3": "B7",
    "BNIR": "B8", "NIR": "B8A", "SWIR1": "B11", "SWIR2": "B12",
}
S2_QA = {"SCL": "SCL"}  # cs_cdf (Cloud Score+) is linked in separately

# HLS-2 (Harmonized Landsat Sentinel-2, v2.0): surface reflectance as
# floats (0..1), BRDF-normalised and bandpass-adjusted to a common
# reference, on the 30 m Sentinel-2 MGRS grid.
#   HLSL30: Landsat 8/9 OLI, HLSS30: Sentinel-2A/B/C MSI
HLSL30_BANDS = OLI_BANDS
HLSS30_BANDS = {
    "BLUE": "B2", "GREEN": "B3", "RED": "B4",
    "RE1": "B5", "RE2": "B6", "RE3": "B7",
    "BNIR": "B8", "NIR": "B8A", "SWIR1": "B11", "SWIR2": "B12",
}
HLS_QA = {"FMASK": "Fmask"}
HLSL30_COLLECTION = "NASA/HLS/HLSL30/v002"
HLSS30_COLLECTION = "NASA/HLS/HLSS30/v002"

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
CLOUD_SCORE_PLUS = "GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED"

SENSORS = {
    "LND04": dict(group="individual", label="Landsat 4", family="LND", collection="LANDSAT/LT04/C02/T1_L2",
                  bands=TM_BANDS, qa=TM_QA, scale=30, start=date(1982, 8, 1), end=date(1993, 12, 31)),
    "LND05": dict(group="individual", label="Landsat 5", family="LND", collection="LANDSAT/LT05/C02/T1_L2",
                  bands=TM_BANDS, qa=TM_QA, scale=30, start=date(1984, 3, 1), end=date(2012, 12, 31)),
    "LND07": dict(group="individual", label="Landsat 7", family="LND", collection="LANDSAT/LE07/C02/T1_L2",
                  bands=TM_BANDS, qa=TM_QA, scale=30, start=date(1999, 5, 1), end=None),
    "LND08": dict(group="individual", label="Landsat 8", family="LND", collection="LANDSAT/LC08/C02/T1_L2",
                  bands=OLI_BANDS, qa=OLI_QA, scale=30, start=date(2013, 3, 1), end=None),
    "LND09": dict(group="individual", label="Landsat 9", family="LND", collection="LANDSAT/LC09/C02/T1_L2",
                  bands=OLI_BANDS, qa=OLI_QA, scale=30, start=date(2021, 10, 1), end=None),
    "SEN2A": dict(group="individual", label="Sentinel-2A", family="SEN", collection=S2_COLLECTION, spacecraft="Sentinel-2A",
                  bands=S2_BANDS, qa=S2_QA, scale=10, start=date(2015, 6, 1), end=None),
    "SEN2B": dict(group="individual", label="Sentinel-2B", family="SEN", collection=S2_COLLECTION, spacecraft="Sentinel-2B",
                  bands=S2_BANDS, qa=S2_QA, scale=10, start=date(2017, 3, 1), end=None),
    "SEN2C": dict(group="individual", label="Sentinel-2C", family="SEN", collection=S2_COLLECTION, spacecraft="Sentinel-2C",
                  bands=S2_BANDS, qa=S2_QA, scale=10, start=date(2024, 9, 1), end=None),
    "HLSL30": dict(group="harmonized", label="HLS Landsat 8/9 OLI (HLSL30)", family="HLS",
                   collection=HLSL30_COLLECTION, bands=HLSL30_BANDS, qa=HLS_QA, scale=30,
                   start=date(2013, 4, 1), end=None),
    "HLSS30": dict(group="harmonized", label="HLS Sentinel-2 MSI (HLSS30)", family="HLS",
                   collection=HLSS30_COLLECTION, bands=HLSS30_BANDS, qa=HLS_QA, scale=30,
                   start=date(2015, 11, 1), end=None),
}

SENSOR_GROUPS = {
    "individual": "Individual Sensors",
    "harmonized": "Harmonized Sensors",
}

# Landsat C2 L2 surface reflectance scaling: rho = DN * 0.0000275 - 0.2
# -> rho * 10000 = DN * 0.275 - 2000
LANDSAT_GAIN = 0.275
LANDSAT_OFFSET = -2000.0


def sensor_years(sensor, start, end):
    """Calendar years in [start, end] (datetime.date) in which `sensor` flew."""
    s = SENSORS[sensor]
    first = max(start.year, s["start"].year)
    last = min(end.year, (s["end"] or date.today()).year, date.today().year)
    return list(range(first, last + 1))


# ---------------------------------------------------------------------------
# Quality screening
# ---------------------------------------------------------------------------

MASK_NONE, MASK_DEFAULT, MASK_AGGRESSIVE = 0, 1, 2

# Cloud Score+ cs_cdf thresholds (0 = cloudy, 1 = clear)
CS_DEFAULT = 0.50
CS_AGGRESSIVE = 0.65

# Sentinel-2 scene classification (SCL)
#   0 no data, 1 saturated/defective, 2 dark area, 3 cloud shadow,
#   4 vegetation, 5 bare soil, 6 water, 7 unclassified, 8 cloud medium prob.,
#   9 cloud high prob., 10 thin cirrus, 11 snow/ice
SCL_DEFAULT = frozenset({2, 4, 5, 6, 7})
SCL_AGGRESSIVE = frozenset({4, 5, 6})


def _bit(v, b):
    return (v >> b) & 1


def _bits(v, b, n=2):
    return (v >> b) & ((1 << n) - 1)


def landsat_clear(qa, level):
    """Screen one Landsat C2 L2 observation.

    QA_PIXEL bits: 0 fill, 1 dilated cloud, 2 cirrus (OLI), 3 cloud,
    4 cloud shadow, 5 snow, 6 clear, 7 water, 8-9 cloud conf.,
    10-11 shadow conf., 12-13 snow conf., 14-15 cirrus conf.
    """
    px = qa.get("QA_PIXEL")
    if px is None:
        return False
    px = int(px)
    if _bit(px, 0):
        return False
    if level == MASK_NONE:
        return True
    # default: no (dilated) cloud, cirrus, cloud shadow or snow
    if px & 0b111110:
        return False
    if level == MASK_DEFAULT:
        return True
    # aggressive: also reject medium/high cloud and cirrus confidence,
    # high shadow / snow confidence, saturation and hazy atmosphere
    if _bits(px, 8) >= 2 or _bits(px, 14) >= 2:
        return False
    if _bits(px, 10) == 3 or _bits(px, 12) == 3:
        return False
    if qa.get("QA_RADSAT") not in (None, 0, 0.0):
        return False
    aerosol = qa.get("AEROSOL")
    if aerosol is not None and _bits(int(aerosol), 6) == 3:  # high aerosol (OLI)
        return False
    opacity = qa.get("OPACITY")
    if opacity is not None and opacity * 0.001 > 0.3:  # hazy (TM/ETM+)
        return False
    return True


def sentinel2_clear(qa, level):
    """Screen one Sentinel-2 L2A observation with SCL and Cloud Score+."""
    scl = qa.get("SCL")
    if scl is None:
        return False
    scl = int(scl)
    if scl == 0:
        return False
    if level == MASK_NONE:
        return True
    cs = qa.get("cs_cdf")  # None if Cloud Score+ is not available
    if level == MASK_DEFAULT:
        return scl in SCL_DEFAULT and (cs is None or cs >= CS_DEFAULT)
    return scl in SCL_AGGRESSIVE and (cs is None or cs >= CS_AGGRESSIVE)


def hls_clear(qa, level):
    """Screen one HLS observation with its Fmask band.

    Fmask bits: 0 reserved, 1 cloud, 2 adjacent to cloud/shadow,
    3 cloud shadow, 4 snow/ice, 5 water, 6-7 aerosol level
    (0 climatology, 1 low, 2 moderate, 3 high). 255 = fill.
    """
    fm = qa.get("FMASK")
    if fm is None:
        return False
    fm = int(fm)
    if fm == 255:
        return False
    if level == MASK_NONE:
        return True
    if fm & 0b11110:  # cloud, adjacent, shadow, snow
        return False
    if level == MASK_DEFAULT:
        return True
    return _bits(fm, 6) != 3  # aggressive: also no high aerosol


def is_clear(sensor, qa, level):
    family = SENSORS[sensor]["family"]
    if family == "LND":
        return landsat_clear(qa, level)
    if family == "HLS":
        return hls_clear(qa, level)
    return sentinel2_clear(qa, level)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def reflectance(sensor, raw, band):
    """Reflectance x 10000 (float) of canonical `band`, or None."""
    v = raw.get(band)
    if v is None:
        return None
    family = SENSORS[sensor]["family"]
    if family == "LND":
        return v * LANDSAT_GAIN + LANDSAT_OFFSET
    if family == "HLS":
        if v < -0.5:  # fill value (-9999 / -0.9999) that was not masked
            return None
        return v * 10000.0
    return float(v)


# Tasseled Cap, reflectance-factor coefficients of Crist (1985) for
# BLUE, GREEN, RED, NIR, SWIR1, SWIR2 (as used by FORCE for Landsat and
# Sentinel-2). Linear, so they apply directly to reflectance x 10000.
TC_BANDS = ("BLUE", "GREEN", "RED", "NIR", "SWIR1", "SWIR2")
TC_COEFFS = {
    "TCB": (0.2043, 0.4158, 0.5524, 0.5741, 0.3124, 0.2303),
    "TCG": (-0.1603, -0.2819, -0.4934, 0.7940, -0.0002, -0.1446),
    "TCW": (0.0315, 0.2021, 0.3102, 0.1594, -0.6806, -0.6109),
}


def _nd(a, b):
    if a is None or b is None or a + b == 0:
        return None
    return (a - b) / (a + b) * 10000.0


def compute_value(sensor, raw, band):
    """Integer value (x 10000) of `band` (reflectance band or index)."""
    def r(b):
        return reflectance(sensor, raw, b)

    if band == "NDVI":
        v = _nd(r("NIR"), r("RED"))
    elif band == "NBR":
        v = _nd(r("NIR"), r("SWIR2"))
    elif band == "NDMI":
        v = _nd(r("NIR"), r("SWIR1"))
    elif band == "NDWI":
        v = _nd(r("GREEN"), r("NIR"))
    elif band == "EVI":
        n, red, blue = r("NIR"), r("RED"), r("BLUE")
        v = None
        if None not in (n, red, blue):
            n, red, blue = n / 1e4, red / 1e4, blue / 1e4
            den = n + 6.0 * red - 7.5 * blue + 1.0
            if den != 0:
                v = 2.5 * (n - red) / den * 10000.0
                if not -20000 <= v <= 20000:  # unstable denominator
                    v = None
    elif band in TC_COEFFS:
        vals = [r(b) for b in TC_BANDS]
        v = None if None in vals else sum(c * x for c, x in zip(TC_COEFFS[band], vals))
    elif band in REFLECTANCE_BANDS:
        v = r(band)
    else:
        raise ValueError(f"Unknown band {band!r}")
    return None if v is None else int(round(v))
