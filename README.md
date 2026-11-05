# EE Time-Series Viewer

- Web app for viewing Sentinel-2 and Landsat time series at any location on Earth, with the data coming from [Google Earth Engine](https://earthengine.google.com/)
- The sister app of [FORCE Live](https://github.com/vudongpham/FORCE-Live), which reads a local FORCE datacube. This app needs no local data
- Click on the map (or type coordinates) and the pixel time series is extracted on the fly, with cloud masking

### Data

| Sensor | Earth Engine collection |
|---|---|
| Landsat 4 TM | `LANDSAT/LT04/C02/T1_L2` |
| Landsat 5 TM | `LANDSAT/LT05/C02/T1_L2` |
| Landsat 7 ETM+ | `LANDSAT/LE07/C02/T1_L2` |
| Landsat 8 OLI | `LANDSAT/LC08/C02/T1_L2` |
| Landsat 9 OLI-2 | `LANDSAT/LC09/C02/T1_L2` |
| Sentinel-2A/B/C MSI | `COPERNICUS/S2_SR_HARMONIZED` (split by `SPACECRAFT_NAME`) |
| Sentinel-2 cloud score | `GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED` (`cs_cdf`) |

Landsat is Collection 2, Level-2 surface reflectance (Tier 1). All reflectances are shown × 10000, the same as FORCE BOA. Landsat DNs are scaled with `DN × 0.0000275 − 0.2`.

| Band | Landsat 4/5/7 | Landsat 8/9 | Sentinel-2 |
|---|---|---|---|
| Blue | SR_B1 | SR_B2 | B2 |
| Green | SR_B2 | SR_B3 | B3 |
| Red | SR_B3 | SR_B4 | B4 |
| Red edge 1–3 | – | – | B5, B6, B7 |
| Broad NIR | – | – | B8 |
| NIR | SR_B4 | SR_B5 | B8A |
| SWIR1 | SR_B5 | SR_B6 | B11 |
| SWIR2 | SR_B7 | SR_B7 | B12 |

The indices NDVI, EVI, NBR, NDMI and NDWI (McFeeters) are × 10000 too. They use the NIR row above, so on Sentinel-2 that is B8A, as in FORCE.

**Cloud mask**
- *None*: only fill / no-data pixels are removed.
- *Default*
  - Landsat: `QA_PIXEL` cloud, dilated cloud, cirrus, cloud shadow and snow bits must be 0.
  - Sentinel-2: `SCL` must be 2, 4, 5, 6 or 7, and Cloud Score+ `cs_cdf` ≥ 0.50.
- *Aggressive*
  - Landsat: Default, plus no medium/high cloud or cirrus confidence, no high shadow/snow confidence, no saturation (`QA_RADSAT`), no high aerosol (L8/9 `SR_QA_AEROSOL`) and atmospheric opacity ≤ 0.3 (L4–7).
  - Sentinel-2: `SCL` must be 4, 5 or 6, and `cs_cdf` ≥ 0.65.

If several scenes of the same sensor cover the location on the same day (overlapping Sentinel-2 granules or Landsat rows), only the first clear one is kept.

### 1. Earth Engine access
You need a Google Cloud project that is [registered for Earth Engine](https://code.earthengine.google.com/register) (non-commercial use is free). You can log in in either of these two ways:

**a) Your own Google account.** Run this once. It saves the credentials in `~/.config/earthengine`:
```
docker run -it --rm \
  -v ~/.config/earthengine:/root/.config/earthengine \
  vudongpham/eetsviewer earthengine authenticate --auth_mode=notebook
```
(or run `earthengine authenticate` directly if `earthengine-api` is installed on the host).

**b) A service account** with the *Earth Engine Resource Viewer* role (or higher) and a JSON key.

### 2. Installation
Pull the docker image:
```
docker pull vudongpham/eetsviewer:latest
```
or install with pip: `pip install git+https://github.com/vudongpham/ee-ts-viewer.git`

### 3. Run
With your own account (a):
```
docker run --rm \
  -v ~/.config/earthengine:/root/.config/earthengine \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --project your-gcp-project
```
With a service account (b):
```
docker run --rm \
  -v /path/to/key.json:/key.json:ro \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --key /key.json
```

Then open `http://localhost:2742` in any web browser.

<i>Optional arguments:</i>
- `--project`: Google Cloud project used for Earth Engine. Default: `$EE_PROJECT`, or the `project_id` of the service account key
- `--key`: service account JSON key. Default: `$EE_SERVICE_ACCOUNT_KEY`
- `--high-volume`: use the Earth Engine [high-volume endpoint](https://developers.google.com/earth-engine/guides/processing_environments#high-volume_endpoint)
- `-p` | `--port`: Local port used to host the app. Default: `2742`. If you change it, change the docker `-p` to match
- `-w` | `--workers`: Number of parallel Earth Engine requests. Default: `12`
- `--debug`: Run Flask in debug mode

### Notes
- The extraction is split into one Earth Engine `getRegion` request per sensor and calendar year. These requests run in parallel, and their results stream into the progress bar. A new click cancels the previous request.
- The raw band and QA values of each sensor-year are cached in memory. Switching the band, the cloud mask or the date range at the same location is therefore almost instant. Years that can still get new acquisitions are refreshed after 1 h.
- **Click an observation in the chart** to show that scene on the map. You can show it as true color, false color (NIR-R-G) or SWIR (SWIR2-NIR-R), and set its opacity. The tiles are rendered by Earth Engine.
- The *Line* dropdown works the same as in FORCE Live. It offers linear interpolation, monotone spline, a moving average, Savitzky–Golay, harmonic regression (1/2/3 harmonics + trend, after [Zhu et al. 2015](https://doi.org/10.1016/j.rse.2015.02.009)) or no line. It is computed in the browser.
- *Download CSV* includes the Earth Engine image ID of every observation.
- Earth Engine limits concurrent requests per project. If you see "Too many concurrent aggregations", lower `--workers` (failed requests are retried with back-off first).
