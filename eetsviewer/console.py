"""Readable terminal output.

One start line and one result line per time series and per download,
instead of a log line for every HTTP request. On an interactive terminal
(`docker run -t`, or running eetsviewer directly) a live progress bar is
drawn as well and the output is coloured; in a plain log (docker without
-t, redirected output) only the start / result lines are written, so the
log stays clean. Set NO_COLOR=1 to disable colours.
"""

import os
import shutil
import sys
import threading
import time

_lock = threading.Lock()
_tty = sys.stdout.isatty()
_color = _tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"

_live = None          # (key, text) of the progress bar shown on the last line
_last_draw = 0.0
REDRAW_EVERY = 0.15   # seconds between progress bar redraws
TITLE_WIDTH = 12      # "Earth Engine", "Time series", "Download", ...

_STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
           "blue": "34", "cyan": "36", "grey": "90"}


def style(text, *names):
    if not _color or not names:
        return text
    codes = ";".join(_STYLES[n] for n in names)
    return f"\033[{codes}m{text}\033[0m"


def _width():
    return shutil.get_terminal_size((100, 20)).columns


def _write(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def _clear_live():
    if _live is not None:
        _write("\r\033[K")


def _draw_live():
    if _live is not None:
        _write(_live[1][: _width() - 1] if not _color else _live[1])


def line(symbol, title, text, color=None):
    """Print one log line: `HH:MM:SS  ▶ Title  text`."""
    stamp = style(time.strftime("%H:%M:%S"), "grey")
    head = f"{symbol} {title:<{TITLE_WIDTH}}"
    head = style(head, color, "bold") if color else head
    with _lock:
        _clear_live()
        _write(f"{stamp}  {head} {text}\n")
        _draw_live()


def info(title, text):
    line("▶", title, text, "cyan")


def ok(title, text):
    line("✔", title, text, "green")


def warn(title, text):
    line("!", title, text, "yellow")


def error(title, text):
    line("✖", title, text, "red")


def stopped(title, text):
    line("■", title, text, "grey")


def bar(fraction, width=24):
    fraction = min(1.0, max(0.0, fraction))
    full = int(round(fraction * width))
    return style("█" * full, "cyan") + style("░" * (width - full), "grey")


def progress(key, current, total, text="", force=False):
    """Live progress bar (interactive terminals only; one at a time)."""
    global _live, _last_draw
    if not _tty:
        return
    now = time.monotonic()
    with _lock:
        if not force and _live is not None and _live[0] == key and now - _last_draw < REDRAW_EVERY:
            return
        _last_draw = now
        pct = f"{int(100 * current / total) if total else 0:3d} %"
        content = " " * (12 + TITLE_WIDTH) + f"{bar(current / total if total else 0)} {pct}  {text}"
        _clear_live()
        _live = (key, content)
        _draw_live()


def end_progress(key):
    """Remove the progress bar of `key` (before its result line is printed)."""
    global _live
    with _lock:
        if _live is not None and _live[0] == key:
            _clear_live()
            _live = None


def banner(rows):
    """Start-up banner: a title and aligned `label  value` rows."""
    width = max(len(k) for k, _ in rows)
    out = ["", "  " + style("EE Time-Series Viewer", "bold", "cyan"),
           "  " + style("Sentinel-2 / Landsat / HLS time series from Google Earth Engine", "dim"), ""]
    out += [f"  {style(k.ljust(width), 'grey')}  {v}" for k, v in rows]
    out += ["", "  " + style("Press Ctrl+C to quit", "dim"), ""]
    with _lock:
        _write("\n".join(out) + "\n")


def fmt_seconds(s):
    return f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60):02d} s"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
