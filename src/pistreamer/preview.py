"""A look at what is actually on the screen, from the browser.

The obvious worry with a preview is that watching the output changes the
output. Measured on a Pi 4 the encode itself is not the problem: scaling and
JPEG-encoding a frame costs about 6ms at 1080p and 3ms at preview size, so even
once a second that is well under one percent of one core.

What does matter is where the frames go. The node already wrote a full-size
JPEG to the SD card every three seconds, for ever, which is around 4 GB of
writes a day; a one-second preview at full size would have been 12 GB a day.
Flash wears out on writes, and an appliance that quietly eats its own boot
media in the background is a worse bug than a slow GUI. So previews live in
RAM — a tmpfs — and nothing about this feature touches the card.

The second protection is that capture follows demand. A browser showing the
preview says so each time it asks for a frame; when nobody has asked for a
while the rate drops back to the slow cadence the standby screen needs, and
nothing extra is spent. The toggle in the GUI is therefore about what you want
to look at, not about damage control — turning it off is the same as closing
the tab.

Not every source can be captured. NDI, local files, streams and the standby
screen all can. A web page and an AirPlay mirror cannot: those are drawn by
Chromium and uxplay straight onto the display, and there is no way to read a
frame back out of another process's scanout buffer. Saying so is better than
showing a stale picture that looks live.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)

# Where a frame is kept. RAM, in preference order: systemd's own runtime
# directory, then the shared-memory tmpfs every Linux has, and only then the
# state directory — which is on the SD card and is the thing being avoided.
_TMPFS_CANDIDATES = ("/run/pistreamer", "/dev/shm/pistreamer")

# Named rates. The GUI offers these rather than a free number: the useful
# choices are "roughly live", "ticking over" and "off", and a box that lets
# someone type 0.05 is a box that lets someone hurt themselves.
RATES = {
    "off": 0.0,
    "slow": 5.0,
    "fast": 1.0,
}
DEFAULT_RATE = "slow"

# How long a request keeps capture running after the last frame was asked for.
# Long enough to ride out a slow poll or a moment of network trouble, short
# enough that a closed laptop stops costing anything almost at once.
DEMAND_TTL_S = 12.0

# Preview frames are scaled down before encoding: it is cheaper, and a 14 KB
# frame instead of a 137 KB one matters on the event Wi-Fi the GUI is usually
# reached over.
WIDTH = 640
JPEG_QUALITY = 70

_lock = threading.Lock()
_demand: Dict[str, Any] = {"until": 0.0, "interval": 0.0, "asks": 0}


def _writable_dir() -> Path:
    for candidate in _TMPFS_CANDIDATES:
        path = Path(candidate)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".probe"
            probe.write_bytes(b"")
            probe.unlink()
            return path
        except OSError:
            continue
    # The SD card. Works, wears the card; only reached where there is no tmpfs.
    return config.STATE_DIR


_dir_cache: Optional[Path] = None


def directory() -> Path:
    global _dir_cache
    if _dir_cache is None:
        _dir_cache = _writable_dir()
        if _dir_cache == config.STATE_DIR:
            log.warning("no tmpfs available for previews; frames will be written "
                        "to %s, which is usually the SD card", _dir_cache)
    return _dir_cache


def frame_path() -> Path:
    return directory() / "preview.jpg"


def rate_path() -> Path:
    """The wanted capture interval, read by the GStreamer runner.

    A file rather than a control channel for the same reason the identify
    overlay uses one: the runner is a separate process, and a file it polls is
    the simplest thing that survives either end restarting.
    """
    return directory() / "preview.rate"


def on_tmpfs() -> bool:
    return directory() != config.STATE_DIR


# ----------------------------------------------------------------------
# Demand
# ----------------------------------------------------------------------


def request(rate: str = DEFAULT_RATE) -> float:
    """Register that somebody is watching, and how closely. Returns the interval."""
    interval = RATES.get(rate, RATES[DEFAULT_RATE])
    with _lock:
        if interval <= 0:
            _demand.update(until=0.0, interval=0.0)
        else:
            # The fastest current watcher wins while their request is live; two
            # browsers should not halve each other's frame rate.
            live = time.monotonic() < _demand["until"]
            current = _demand["interval"] if live else 0.0
            _demand.update(
                until=time.monotonic() + DEMAND_TTL_S,
                interval=min(current, interval) if current else interval,
                asks=_demand["asks"] + 1,
            )
    _publish()
    return interval


def wanted_interval() -> float:
    """Seconds between captures right now. 0 means nobody is watching."""
    with _lock:
        if time.monotonic() >= _demand["until"]:
            return 0.0
        return float(_demand["interval"])


def watching() -> bool:
    return wanted_interval() > 0


def _publish() -> None:
    """Write the wanted interval where the runner can see it.

    Written unconditionally rather than only on change: it is a handful of
    bytes to a tmpfs, and a runner that started after the last change would
    otherwise never learn the current rate.
    """
    try:
        path = rate_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(f"{wanted_interval():.3f}\n")
        os.replace(tmp, path)
    except OSError as exc:  # noqa: BLE001 - a preview is never worth an error
        log.debug("could not publish the preview rate: %s", exc)


def release() -> None:
    """Stop capturing now, without waiting for the request to age out."""
    with _lock:
        _demand.update(until=0.0, interval=0.0)
    _publish()


def age_s() -> Optional[float]:
    """How old the frame on disk is, or None if there is not one."""
    try:
        return max(0.0, time.time() - frame_path().stat().st_mtime)
    except OSError:
        return None


def summary(mode: str = "") -> Dict[str, Any]:
    supported, reason = supports(mode)
    return {
        "supported": supported,
        "reason": reason,
        "watching": watching(),
        "interval_s": wanted_interval(),
        "rates": {k: v for k, v in RATES.items()},
        "age_s": age_s(),
        "tmpfs": on_tmpfs(),
        "width": WIDTH,
    }


# Modes whose frames can be read back. Kept here rather than in the player so
# the GUI can grey the control out before it asks for a frame that will not
# arrive.
_CAPTURABLE = {"ndi", "idle", "local", "stream"}
_WHY_NOT = {
    "web": "A web page is drawn straight to the display by Chromium, which "
           "cannot be asked for a copy of what it drew.",
    "airplay": "An AirPlay mirror is drawn by the receiver straight to the "
               "display, which cannot be asked for a copy of what it drew.",
}


def supports(mode: str) -> tuple[bool, str]:
    if not mode:
        return False, "nothing is playing"
    if mode in _CAPTURABLE:
        return True, ""
    return False, _WHY_NOT.get(mode, f"{mode} cannot be previewed")
