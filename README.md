# EE Time-Series Viewer

- Web app for viewing Sentinel-2 and Landsat time series at any location on Earth, with the data coming from [Google Earth Engine](https://earthengine.google.com/)
- The sister app of [FORCE Live](https://github.com/vudongpham/FORCE-Live), which reads a local FORCE datacube. This app needs no local data
- Click on the map (or type coordinates) and the pixel time series is extracted on the fly, with cloud masking

### Data

The sensors come in two groups. Pick one with the radio button; the other group is greyed out.

**Individual Sensors**

| Sensor | Earth Engine collection |
|---|---|
| Landsat 4 TM | `LANDSAT/LT04/C02/T1_L2` |
| Landsat 5 TM | `LANDSAT/LT05/C02/T1_L2` |
| Landsat 7 ETM+ | `LANDSAT/LE07/C02/T1_L2` |
| Landsat 8 OLI | `LANDSAT/LC08/C02/T1_L2` |
| Landsat 9 OLI-2 | `LANDSAT/LC09/C02/T1_L2` |
| Sentinel-2A/B/C MSI | `COPERNICUS/S2_SR_HARMONIZED` (split by `SPACECRAFT_NAME`) |
| Sentinel-2 cloud score | `GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED` (`cs_cdf`) |

**Harmonized Sensors**

| Sensor | Earth Engine collection |
|---|---|
| HLS-2 Landsat 8/9 OLI | `NASA/HLS/HLSL30/v002` (HLSL30, from April 2013) |
| HLS-2 Sentinel-2A/B/C MSI | `NASA/HLS/HLSS30/v002` (HLSS30, from November 2015) |

HLS (Harmonized Landsat Sentinel-2) surface reflectance is BRDF-normalised and bandpass-adjusted to a common reference, on the 30 m Sentinel-2 MGRS tile grid, so HLSL30 and HLSS30 can be combined into one dense time series. You can select either or both. HLSL30 uses the Landsat 8/9 bands (B2–B7), HLSS30 the Sentinel-2 bands of the table above (the red-edge bands and broad NIR are available for HLSS30 only, and are not bandpass-adjusted). Both are cloud-screened with their `Fmask` band.

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

The indices NDVI, EVI, NBR, NDMI and NDWI (McFeeters) are × 10000 too, as are the Tasseled Cap Brightness, Greenness and Wetness. These use the reflectance-factor coefficients of [Crist (1985)](https://doi.org/10.1016/0034-4257(85)90102-6) on Blue, Green, Red, NIR, SWIR1 and SWIR2 for every sensor, as FORCE does. They use the NIR row above, so on Sentinel-2 that is B8A, as in FORCE.

**Cloud mask**
- *None*: only fill / no-data pixels are removed.
- *Default*
  - Landsat: `QA_PIXEL` cloud, dilated cloud, cirrus, cloud shadow and snow bits must be 0.
  - Sentinel-2: `SCL` must be 2, 4, 5, 6 or 7, and Cloud Score+ `cs_cdf` ≥ 0.50.
  - HLS: `Fmask` cloud, adjacent to cloud/shadow, cloud shadow and snow/ice bits must be 0.
- *Aggressive*
  - Landsat: Default, plus no medium/high cloud or cirrus confidence, no high shadow/snow confidence, no saturation (`QA_RADSAT`), no high aerosol (L8/9 `SR_QA_AEROSOL`) and atmospheric opacity ≤ 0.3 (L4–7).
  - Sentinel-2: `SCL` must be 4, 5 or 6, and `cs_cdf` ≥ 0.65.
  - HLS: Default, plus no high aerosol (`Fmask` bits 6–7).

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
docker run --rm -t \
  -v ~/.config/earthengine:/root/.config/earthengine \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --project your-gcp-project
```
With a service account (b):
```
docker run --rm -t \
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
- `--log-requests`: also log every HTTP request (off by default)
- `--debug`: Run Flask in debug mode

**Terminal output**: after a short start-up banner, the terminal shows one start line and one result line per time series and per download, e.g.
```
16:24:50  ▶ Time series  52.50000, 13.40000 · 2023-01-01 → 2024-12-31 · NDVI · Default mask · Landsat 7/8/9 + Sentinel-2A/B (10 requests)
16:24:53  ✔ Time series  157 clear observations of 238 images · 3.1 s
16:24:55  ▶ Download     S2SR_SEN2A_20240212.tif · cloud masked (Default) · 3000 × 2600 px · 12 bands (9 blocks)
16:24:58  ✔ Download     S2SR_SEN2A_20240212.tif ready · 41.2 MB · 3.2 s
```
With `docker run -t` (as above) the output is coloured and a live progress bar is shown while a job runs; without `-t` only these lines are written. Set `NO_COLOR=1` to turn colours off.

### Notes
- The extraction is split into one Earth Engine `getRegion` request per sensor and calendar year. HLSL30 (Landsat 8 + 9 in one collection) is split into 4-month parts and HLSS30 (Sentinel-2A/B/C in one collection) into 2-month parts, so their requests are about as small as those of the individual sensors. Only the parts that overlap the date range are requested. The requests run in parallel, and their results stream into the progress bar. A new click cancels the previous request.
- The raw band and QA values of each request are cached in memory. Switching the band, the cloud mask or the date range at the same location is therefore almost instant. Years that can still get new acquisitions are refreshed after 1 h.
- The y axis runs from 0 (or 110 % of the lowest value, if there are negative values) to 110 % of the highest value, and is cut off at −10000 and +10000.
- **Click an observation in the chart** to show that scene on the map; the selected observation gets a yellow edge. A box on the map and a bar under the chart show the progress, first of the Earth Engine request and then of the tiles being rendered. You can show it as true color, false color (NIR-R-G) or SWIR (SWIR2-NIR-R), and set its opacity. The tiles are rendered by Earth Engine.
- **Download selected image**, in the block at the top right of the map, saves the overlaid scene (the block shows its sensor, date, Earth Engine image ID and the file name) as a GeoTIFF named `{product}_{sensor}_{YYYYMMDD}.tif`, with the suffix `_masked` (Default) or `_masked_aggressive` for cloud-masked downloads:
  - *Product*: `C2L2` (Landsat Collection 2 Level-2), `S2SR` (Sentinel-2 L2A surface reflectance, `S2_SR_HARMONIZED`), `HLSL30` or `HLSS30`. *Sensor*: the satellite, `LND04`…`LND09` or `SEN2A`/`SEN2B`/`SEN2C`; for HLS it is read from the scene's source product. Examples: `C2L2_LND08_20240212.tif`, `S2SR_SEN2A_20240212.tif`, `HLSS30_SEN2B_20240212_masked.tif`, `HLSL30_LND09_20240212_masked_aggressive.tif`.
  - *Area*: *Map view* (the part of the scene visible on the map) or *Full scene*. Both are at the scene's native resolution and pixel grid (10 m for Sentinel-2, 30 m for Landsat and HLS) and in its native projection (UTM). At most 12000 × 12000 pixels; a full Sentinel-2 tile is 10980 × 10980.
  - All reflective bands of the product, with band names (e.g. `B8A (NIR)`): Landsat 4–7 SR_B1–5, 7; Landsat 8/9 SR_B1–7; Sentinel-2 B1–B8A, B9, B11, B12; HLSL30 B1–B7, B9; HLSS30 B1–B12 incl. B8A. Thermal bands and indices are not included.
  - *Data*: *Raw* (the scene as it is) or *Cloud masked*: pixels removed by the selected *Cloud mask* (Default or Aggressive; Default when None is selected) are set to nodata. The rules are the same as for the time series, applied per pixel (Landsat `QA_PIXEL` etc., Sentinel-2 `SCL` + Cloud Score+, HLS `Fmask`), so the masked scene matches the observations in the chart.
  - int16, reflectance × 10000 (the same scale as the chart), nodata = −9999 (the area outside the scene, and cloudy pixels in a cloud-masked download).
  - Everything happens on the fly: the server reads the scene from Earth Engine in blocks of 1024 × 1024 pixels (`computePixels`, 6 in parallel) and writes them straight into a compressed (deflate) GeoTIFF. The file is deleted from the server once it is downloaded. A progress bar shows the blocks read; the button turns into *Cancel download* meanwhile.
- The *Line* dropdown works the same as in FORCE Live. It offers linear interpolation, monotone spline, a moving average, Savitzky–Golay, harmonic regression (1/2/3 harmonics + trend, after [Zhu et al. 2015](https://doi.org/10.1016/j.rse.2015.02.009)) or no line. It is computed in the browser.
- *Download CSV* includes the Earth Engine image ID of every observation.
- Earth Engine limits concurrent requests per project. If you see "Too many concurrent aggregations", lower `--workers` (failed requests are retried with back-off first).
