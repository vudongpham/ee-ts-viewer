import argparse
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from flask import Flask, Response, jsonify, render_template, request
from flask import cli as flask_cli

from . import console
from . import earthengine as eeapi
from .sensors import ALL_BANDS, SENSOR_GROUPS, SENSORS, compute_value, is_clear, sensor_periods

app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

# Every request is one Earth Engine getRegion call (one sensor, one year or
# a part of a year, see sensors.sensor_periods).
# They are network bound, so a thread pool runs them in parallel. Earth
# Engine limits concurrent requests per project, so do not go too high.
DEFAULT_WORKERS = 12
executor = None


def get_executor():
    global executor
    if executor is None:
        executor = ThreadPoolExecutor(
            max_workers=app.config.get("WORKERS", DEFAULT_WORKERS),
            thread_name_prefix="eetsviewer",
        )
    return executor


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class Job:
    def __init__(self):
        self.id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.status = "running"
        self.name = "Preparing request"
        self.current = 0    # Earth Engine requests finished
        self.total = 0      # Earth Engine requests scheduled
        self.images = 0     # acquisitions covering the location
        self.valid = 0      # clear observations
        self.failed = 0     # requests that failed after retries
        self.last_error = None
        self.points = []    # [day, value, sensor, image id] in completion order
        self.started = time.perf_counter()
        self.elapsed = 0.0
        self.futures = []

    def snapshot(self, since=0):
        with self.lock:
            return {
                "job": self.id,
                "status": self.status,
                "name": self.name,
                "current": self.current,
                "total": self.total,
                "images": self.images,
                "valid": self.valid,
                "failed": self.failed,
                "error": self.last_error,
                "elapsed": round(time.perf_counter() - self.started
                                 if self.status == "running" else self.elapsed, 3),
                "points": self.points[since:],
                "cursor": len(self.points),
            }

    def finish(self, status, name=None):
        with self.lock:
            if self.status != "running":
                return
            self.status = status
            if name is not None:
                self.name = name
            self.elapsed = time.perf_counter() - self.started
        report_finished(self)

    def cancel(self):
        self.cancelled.set()
        for f in self.futures:
            f.cancel()
        self.finish("cancelled", "Cancelled")


jobs = OrderedDict()
jobs_lock = threading.Lock()
MAX_JOBS = 32


def register_job(job):
    with jobs_lock:
        jobs[job.id] = job
        while len(jobs) > MAX_JOBS:
            _, old = jobs.popitem(last=False)
            old.cancel()


# ---------------------------------------------------------------------------
# Terminal output (one start and one result line per time series)
# ---------------------------------------------------------------------------

MASK_NAMES = {0: "no cloud mask", 1: "Default mask", 2: "Aggressive mask"}


def short_error(text, limit=160):
    """(Earth Engine) error message on one line, shortened."""
    one = " ".join(str(text).split()) or "unknown error"
    return one if len(one) <= limit else one[: limit - 1] + "…"


def sensor_summary(sensors):
    """e.g. "Landsat 5/7/8 + Sentinel-2A/B" or "HLSL30 + HLSS30"."""
    lnd = [s[-1] if s[-2] == "0" else s[-2:] for s in sensors if s.startswith("LND")]
    sen = [s[-1] for s in sensors if s.startswith("SEN")]
    parts = []
    if lnd:
        parts.append("Landsat " + "/".join(lnd))
    if sen:
        parts.append("Sentinel-2" + "/".join(sen))
    parts += [s for s in sensors if s.startswith("HLS")]
    return " + ".join(parts) or "no sensor"


def report_started(job, lat, lng, start, end, sensors, band, cloud_mask):
    console.info("Time series",
                 f"{lat:.5f}, {lng:.5f} · {start} → {end} · {band} · "
                 f"{MASK_NAMES.get(cloud_mask, cloud_mask)} · {sensor_summary(sensors)} "
                 + console.style(f"({job.total} requests)", "grey"))


def report_progress(job):
    with job.lock:
        current, total, images, valid = job.current, job.total, job.images, job.valid
    console.progress(job.id, current, total,
                     f"{current}/{total} requests · {images} images · {valid} clear",
                     force=current >= total)


def report_finished(job):
    console.end_progress(job.id)
    with job.lock:
        status, name, total, current = job.status, job.name, job.total, job.current
        images, valid, failed, err = job.images, job.valid, job.failed, job.last_error
        elapsed = console.fmt_seconds(job.elapsed)
    if status == "done" and not total:
        console.ok("Time series", name)
    elif status == "done":
        text = f"{valid} clear observations of {images} images · {elapsed}"
        if failed:
            console.warn("Time series", f"{text} · {failed} of {total} requests failed "
                                        f"({short_error(err)})")
        else:
            console.ok("Time series", text)
    elif status == "error":
        console.error("Time series", short_error(name))
    elif status == "cancelled":
        console.stopped("Time series", f"stopped after {current}/{total} requests · {elapsed}")


def observations(sensor, rows, first_day, last_day, band, cloud_mask):
    """Screen and convert the rows of one (sensor, year) request.

    Overlapping scenes of the same sensor on the same day (adjacent
    Sentinel-2 granules or Landsat rows) give one observation: the first
    clear one.
    """
    days, points, done = set(), [], set()
    for r in rows:
        day = r["day"]
        if not first_day <= day <= last_day:
            continue
        days.add(day)
        if day in done or not is_clear(sensor, r["values"], cloud_mask):
            continue
        value = compute_value(sensor, r["values"], band)
        if value is None:
            continue
        done.add(day)
        points.append([day, value, sensor, r["id"]])
    return len(days), points


def run_request(job, sensor, period, lat, lng, first_day, last_day, band, cloud_mask):
    """One Earth Engine request (one sensor, one period). Runs in the pool."""
    if job.cancelled.is_set():
        return
    images, points, error = 0, [], None
    try:
        rows = eeapi.fetch_period_cached(sensor, lng, lat, *period)
        images, points = observations(sensor, rows, first_day, last_day, band, cloud_mask)
    except Exception as exc:  # keep going, report it at the end
        error = str(exc)
        console.warn("Request", f"{sensor} from {period[0]:%Y-%m-%d} failed: {short_error(exc)}")

    with job.lock:
        job.current += 1
        job.images += images
        job.valid += len(points)
        job.points.extend(points)
        if error:
            job.failed += 1
            job.last_error = error
        done = job.current >= job.total
        failed_all = job.failed == job.total
    report_progress(job)
    if done:
        if failed_all:
            job.finish("error", f"Earth Engine error: {error}")
        else:
            job.finish("done", "Done")


def start_job(job, lat, lng, start_date, end_date, sensors, band, cloud_mask):
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        epoch = date(1970, 1, 1)
        first_day, last_day = (start - epoch).days, (end - epoch).days

        requests = [(s, p) for s in sensors for p in sensor_periods(s, start, end)]
        with job.lock:
            job.total = len(requests)
            job.name = "Querying Earth Engine"
        if not requests:
            job.finish("done", "No sensor was operating in the selected period")
            return
        report_started(job, lat, lng, start, end, sensors, band, cloud_mask)

        pool = get_executor()
        # newest periods first: they are the most interesting and least cached
        requests.sort(key=lambda r: r[1][0], reverse=True)
        job.futures = [pool.submit(run_request, job, s, p, lat, lng, first_day, last_day,
                                   band, cloud_mask)
                       for s, p in requests]
    except Exception as exc:
        job.finish("error", f"Error: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", sensors=SENSORS, groups=SENSOR_GROUPS)


@app.route("/sendData", methods=["POST"])
def send_data():
    data = request.get_json(force=True)
    try:
        lat = float(data["lat"])
        lng = float(data["lng"])
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("coordinates out of range")
        band = data.get("band", "NDVI")
        if band not in ALL_BANDS:
            raise ValueError(f"unknown band {band}")
        sensors = [s for s in (data.get("sensorList") or []) if s in SENSORS]
        if len({SENSORS[s]["group"] for s in sensors}) > 1:
            raise ValueError("individual and harmonized sensors cannot be mixed")
        cloud_mask = int(data.get("cloudMask", 1))
        start_date = data["startDate"]
        end_date = data["endDate"]
        date.fromisoformat(start_date)
        date.fromisoformat(end_date)
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"status": "error", "name": f"Bad request: {exc}"}), 400

    # cancel the job this client started before (new click supersedes it)
    prev = data.get("cancel")
    if prev:
        with jobs_lock:
            old = jobs.get(prev)
        if old:
            old.cancel()

    job = Job()
    register_job(job)
    start_job(job, lat, lng, start_date, end_date, sensors, band, cloud_mask)
    return jsonify(job.snapshot())


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "name": "Unknown job"}), 404
    since = request.args.get("since", default=0, type=int)
    return jsonify(job.snapshot(since))


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job:
        job.cancel()
    return jsonify({"status": "cancelled"})


@app.route("/tiles")
def tiles():
    """Tile URL of one scene, used to show it as an overlay on the map."""
    try:
        url = eeapi.tile_url(request.args.get("sensor"), request.args.get("id"),
                             request.args.get("vis", "true"))
        return jsonify({"url": url})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Earth Engine error: {exc}"}), 502


_export_error = None


def load_exporter():
    """The GeoTIFF export module, imported on first use.

    It needs rasterio / GDAL; if those cannot be loaded, the time-series
    viewer still works and only the download reports the problem."""
    global _export_error
    try:
        from . import export
        return export
    except ImportError as exc:
        if _export_error is None:
            console.warn("Download", f"GeoTIFF download is not available: {exc}")
        _export_error = str(exc)
        return None


def _get_export_job(job_id):
    exporter = load_exporter()
    return exporter.get_export(job_id) if exporter else None


@app.route("/export", methods=["POST"])
def export_start():
    """Start exporting the overlaid scene to a GeoTIFF."""
    exporter = load_exporter()
    if exporter is None:
        return jsonify({"status": "error",
                        "error": f"GeoTIFF download is not available on this server: {_export_error}"}), 500
    data = request.get_json(force=True)
    try:
        mask = data.get("mask")             # None = raw, 1 = Default, 2 = Aggressive
        mask = None if mask in (None, "", "raw") else int(mask)
        plan = exporter.plan_export(data.get("sensor"), data.get("id"),
                                    data.get("extent", "view"), data.get("bounds"), mask)
    except (TypeError, ValueError) as exc:
        return jsonify({"status": "error", "error": f"Bad request: {exc}"}), 400
    except exporter.ExportError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"status": "error", "error": f"Earth Engine error: {exc}"}), 502
    return jsonify(exporter.start_export(plan).snapshot())


@app.route("/export/name")
def export_name_route():
    """File name a download of this scene will get (shown in the download block)."""
    exporter = load_exporter()
    if exporter is None:
        return jsonify({"error": f"GeoTIFF download is not available on this server: {_export_error}"}), 500
    try:
        mask = request.args.get("mask")
        mask = None if mask in (None, "", "raw") else int(mask)
        name = exporter.file_name(request.args.get("sensor"), request.args.get("id"), mask)
    except (exporter.ExportError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"Earth Engine error: {exc}"}), 502
    return jsonify({"name": name})


@app.route("/export/<job_id>")
def export_status(job_id):
    job = _get_export_job(job_id)
    if job is None:
        return jsonify({"status": "error", "error": "Unknown export"}), 404
    return jsonify(job.snapshot())


@app.route("/export/<job_id>/cancel", methods=["POST"])
def export_cancel(job_id):
    job = _get_export_job(job_id)
    if job:
        job.cancel()
        load_exporter().forget_export(job_id)
    return jsonify({"status": "cancelled"})


@app.route("/export/<job_id>/file")
def export_file(job_id):
    """The finished GeoTIFF; it is deleted from the server once it is sent."""
    exporter = load_exporter()
    job = exporter.get_export(job_id) if exporter else None
    if job is None or job.status != "done" or not os.path.exists(job.path):
        return jsonify({"status": "error", "error": "File not available"}), 404
    path, name = job.path, job.plan.name

    size = os.path.getsize(path)

    def stream():
        sent = 0
        try:
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    sent += len(chunk)
                    yield chunk
        finally:  # runs when the download ends or the connection closes
            exporter.forget_export(job_id)
            if sent >= size:
                console.ok("Download", f"{name} sent to the browser")
            else:
                console.warn("Download", f"{name}: the browser stopped the transfer "
                                         f"({console.fmt_bytes(sent)} of {console.fmt_bytes(size)})")

    return Response(stream(), mimetype="image/tiff", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Content-Length": str(size),
        "Cache-Control": "no-store",
    })


def in_container():
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def browser_address(port, host_port=None):
    """Address shown in the banner. Inside a container the app cannot see
    which host port `docker run -p` maps to its port, so it has to be told
    (--host-port / HOST_PORT); otherwise the banner says so."""
    if host_port:
        return console.style(f"http://localhost:{host_port}", "bold")
    url = console.style(f"http://localhost:{port}", "bold")
    if in_container():
        return url + console.style(
            f"  (port inside the container: if you mapped another port with "
            f"docker -p PORT:{port}, open that one, or pass -e HOST_PORT=PORT to show it here)", "dim")
    return url


def main():
    parser = argparse.ArgumentParser(
        prog="eetsviewer",
        description="Web-based Sentinel-2 / Landsat time-series viewer powered by Google Earth Engine",
    )
    parser.add_argument("--project", default=os.environ.get("EE_PROJECT"),
                        help="Google Cloud project registered for Earth Engine "
                             "(default: $EE_PROJECT, or the project of the service account key)")
    parser.add_argument("--key", default=os.environ.get("EE_SERVICE_ACCOUNT_KEY"),
                        help="Service account JSON key file (default: $EE_SERVICE_ACCOUNT_KEY). "
                             "Without it the credentials of `earthengine authenticate` are used.")
    parser.add_argument("--high-volume", action="store_true",
                        help="Use the Earth Engine high-volume endpoint")
    parser.add_argument("-p", "--port", default=2742, type=int,
                        help="Port the app listens on (inside the container). Default: 2742")
    parser.add_argument("--host-port", type=int,
                        default=int(os.environ["HOST_PORT"]) if os.environ.get("HOST_PORT", "").isdigit() else None,
                        help="Port to open in the browser, if it differs from --port, e.g. the host "
                             "port of `docker run -p HOST_PORT:2742` (default: $HOST_PORT). "
                             "Only used for the address shown at start-up.")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Number of parallel Earth Engine requests. Default: {DEFAULT_WORKERS}")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode")
    parser.add_argument("--log-requests", action="store_true",
                        help="Also log every HTTP request (off by default to keep the output readable)")
    args = parser.parse_args()

    console.line("…", "Earth Engine", "connecting…")
    try:
        project = eeapi.initialize(args.project, args.key, args.high_volume)
    except SystemExit as exc:          # no project given
        console.error("Earth Engine", str(exc))
        raise SystemExit(1)
    except Exception as exc:
        console.error("Earth Engine", short_error(exc, 300))
        hint = ("check the service account key and that it has access to the project"
                if args.key else
                "log in once with `earthengine authenticate` (see README, section 1), "
                "and check that the project is registered for Earth Engine")
        console.line(" ", "", console.style(hint, "dim"))
        raise SystemExit(1)

    if args.key:
        with open(args.key) as f:
            who = f"service account {json.load(f).get('client_email', '?')}"
    else:
        who = "your Google account (earthengine authenticate)"

    app.config["WORKERS"] = max(1, args.workers)
    console.banner([
        ("Open in browser", browser_address(args.port, args.host_port)),
        ("Earth Engine", f"project {project}" + (" · high-volume endpoint" if args.high_volume else "")),
        ("Credentials", who),
        ("Parallel requests", str(app.config["WORKERS"])),
    ])
    if not console._tty:
        console.line(" ", "", console.style(
            "tip: run docker with -t to see a live progress bar", "dim"))

    if not (args.debug or args.log_requests):
        # no "GET /status/... 200" line for every poll of the browser, and no
        # Flask development-server banner (the banner above replaces it)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        flask_cli.show_server_banner = lambda *a, **k: None

    app.run(host="0.0.0.0", port=args.port, debug=args.debug, threaded=True,
            use_reloader=args.debug)


if __name__ == "__main__":
    main()
