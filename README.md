# EE Time-Series Viewer

Web app for viewing Landsat, Sentinel-2 and HLS time series at any location on Earth. The data come from Google Earth Engine and are extracted and cloud-masked on the fly; no local data are needed.

## Installation

### 1. Earth Engine access
You need a Google Cloud project [registered for Earth Engine](https://code.earthengine.google.com/register) (free for non-commercial use). Log in once with your Google account; the credentials are saved in `~/.config/earthengine`:
```
docker run -it --rm \
  -v ~/.config/earthengine:/root/.config/earthengine \
  vudongpham/eetsviewer earthengine authenticate --auth_mode=notebook
```
Alternatively, use a service account JSON key (see *Options*).

### 2. Pull the image
```
docker pull vudongpham/eetsviewer:latest
```
or install with pip: `pip install git+https://github.com/vudongpham/ee-ts-viewer.git`

### 3. Run
```
docker run --rm -t \
  -v ~/.config/earthengine:/root/.config/earthengine \
  -p 2742:2742 \
  vudongpham/eetsviewer eetsviewer --project your-gcp-project
```
Then open `http://localhost:2742` in a web browser.

To use another port, change only the host side of `-p` and pass it as `HOST_PORT`, so the start-up message shows the right address:
```
docker run --rm -t -e HOST_PORT=8080 \
  -v ~/.config/earthengine:/root/.config/earthengine \
  -p 8080:2742 \
  vudongpham/eetsviewer eetsviewer --project your-gcp-project
```

### Options
- `--project`: Google Cloud project used for Earth Engine. Default: `$EE_PROJECT`, or the project of the service account key
- `--key`: service account JSON key, instead of your own login, e.g. `-v /path/to/key.json:/key.json:ro` and `--key /key.json`. Default: `$EE_SERVICE_ACCOUNT_KEY`
- `-w` | `--workers`: number of parallel Earth Engine requests. Default: `12`. Lower it if you see "Too many concurrent aggregations"
- `--high-volume`: use the Earth Engine high-volume endpoint
- `-p` | `--port`: port the app listens on. Default: `2742`
- `--host-port`: port shown in the start-up message. Default: `$HOST_PORT`
- `--log-requests`: also log every HTTP request
- `--debug`: run Flask in debug mode

With `-t`, the terminal shows coloured output and a live progress bar; without it, one line per job. Set `NO_COLOR=1` to turn colours off.

## Functions

**Location**: click on the map, or type latitude / longitude (pasting `lat,lng` fills both).

**Base map**: *Satellite* (default), *OpenStreetMap* or *Blank* (white), switchable at the top left of the map; the choice is remembered in the browser.

**Sensors**, in two groups (selecting one greys out the other):
- *Individual Sensors*: Landsat 4, 5, 7, 8, 9 (Collection 2 Level-2 surface reflectance) and Sentinel-2A, 2B, 2C (L2A surface reflectance, harmonized)
- *Harmonized Sensors*: HLS Landsat 8/9 (HLSL30) and HLS Sentinel-2 (HLSS30)

**Bands**: Blue, Green, Red, Red edge 1–3 and Broad NIR (Sentinel-2 only), NIR, SWIR1, SWIR2, and the indices NDVI, EVI, NBR, NDMI, NDWI and Tasseled Cap Brightness, Greenness and Wetness. All values are × 10000.

**Cloud mask**: *None*, *Default* or *Aggressive*, using the quality bands of each product (Landsat QA, Sentinel-2 scene classification + Cloud Score+, HLS Fmask).

**Chart**
- Landsat points in magenta, Sentinel-2 in green, in both sensor groups
- *Line*: linear interpolation, spline, moving average, Savitzky–Golay, RBF ensemble, harmonic regression (basic / advanced / full) or no line. The moving average, Savitzky–Golay and RBF adapt to the density of the clear observations and do not bridge long gaps. Switching is instant.
- The y axis fits the data, between −10000 and 10000
- *Copy dates array*, *Copy reflectance array* and *Download CSV* (with the image ID of every observation)
- *Stop* cancels a running extraction; a new request also cancels the previous one

**Scene overlay**: click an observation in the chart to show that scene on the map (true color, false color or SWIR, adjustable opacity). The selected observation gets a yellow edge.

**Download selected image**: the block at the top right of the map saves the overlaid scene as a GeoTIFF.
- Shows the selected image (sensor, date, image ID, file name) and its cloud cover: pixels removed by the selected cloud mask / valid pixels of the whole scene, in %
- *Data*: *Raw*, or *Cloud masked* with the selected cloud mask (cloudy pixels set to nodata)
- *Area*: *Map view* or *Full scene*
- All reflective bands, int16, reflectance × 10000, nodata −9999, native projection and resolution
- File name `{product}_{sensor}_{YYYYMMDD}.tif`, with `_masked` / `_masked_aggressive` for cloud-masked downloads, e.g. `S2SR_SEN2A_20240212.tif`, `HLSL30_LND09_20240212_masked.tif`
