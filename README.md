# EE Time-Series Viewer

Web app for viewing Landsat, Sentinel-2 and HLS time series at any location on Earth. The data come from Google Earth Engine and are extracted and cloud-masked on the fly; no local data are needed.
<img width="1882" height="905" alt="ee" src="https://github.com/user-attachments/assets/366d1aa5-d338-4d3c-9bf6-f478a238704d" />


## Installation

**New to Earth Engine, or stuck at the login step** (e.g. *"Project has an incompatible OAuth2 Client configuration"*)? Follow the step-by-step [installation guide](INSTALL.md).

### 1. Earth Engine access
You need a Google Cloud project [registered for Earth Engine](https://code.earthengine.google.com/register) (free for non-commercial use). Log in once with your Google account; the credentials are saved in `~/.config/earthengine`. The simplest way is on your own computer, with Python:
```
pip install earthengine-api
earthengine authenticate --auth_mode=localhost
```
or, with Docker only:
```
docker run -it --rm \
  -v ~/.config/earthengine:/root/.config/earthengine \
  vudongpham/eetsviewer earthengine authenticate --auth_mode=notebook
```
If the Docker login fails with *"incompatible OAuth2 Client configuration"*, choose a different (e.g. new, empty) project on the authenticator page, or use the Python login above; see the [guide](INSTALL.md#project-has-an-incompatible-oauth2-client-configuration).
Alternatively, use a service account JSON key (see *Options* and the [guide](INSTALL.md#option-c--service-account-key)).

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
- `--cloudmask-buffered-download`: grow the cloud mask by this many native pixels (10 m Sentinel-2, 30 m Landsat / HLS) in cloud-masked downloads; `0` = no buffer. Default: `2`
- `--log-requests`: also log every HTTP request
- `--debug`: run Flask in debug mode

With `-t`, the terminal shows coloured output and a live progress bar; without it, one line per job. Set `NO_COLOR=1` to turn colours off.
