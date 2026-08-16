"""NDI source discovery.

Discovery runs through the GStreamer device provider shipped with
gst-plugin-ndi, which wraps the NDI SDK's mDNS/discovery service. We prefer
the in-process GObject API (structured, no output parsing) and fall back to
shelling out to gst-device-monitor-1.0 if python3-gi is unavailable.

Discovery is inherently time-based: NDI senders announce themselves and the
list fills in over a second or two, so every call takes `timeout` seconds.
Results are cached briefly so the web GUI polling does not hammer it.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import List

log = logging.getLogger(__name__)

DEVICE_CLASSES = "Source/Network"
_CACHE_TTL = 3.0

_cache_lock = threading.Lock()
_cache: List["NdiSource"] = []
_cache_at: float = 0.0


@dataclass
class NdiSource:
    name: str  # full NDI name, e.g. "STUDIO-PC (OBS)"
    host: str = ""  # machine part, best-effort
    stream: str = ""  # stream part, best-effort

    @classmethod
    def from_name(cls, name: str) -> "NdiSource":
        m = re.match(r"^(?P<host>[^(]+?)\s*\((?P<stream>.+)\)\s*$", name)
        if m:
            return cls(name=name, host=m.group("host"), stream=m.group("stream"))
        return cls(name=name, host=name, stream="")


def _discover_gi(timeout: float) -> List[NdiSource]:
    import gi  # type: ignore

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # type: ignore

    if not Gst.is_initialized():
        Gst.init(None)

    monitor = Gst.DeviceMonitor.new()
    caps = Gst.Caps.new_empty_simple("application/x-ndi")
    monitor.add_filter(DEVICE_CLASSES, caps)

    if not monitor.start():
        raise RuntimeError("GstDeviceMonitor failed to start (is gst-plugin-ndi installed?)")

    try:
        # Give senders time to announce themselves.
        loop_end = time.monotonic() + timeout
        ctx = GLib.MainContext.default()
        while time.monotonic() < loop_end:
            while ctx.pending():
                ctx.iteration(False)
            time.sleep(0.05)

        found: List[NdiSource] = []
        seen: set[str] = set()
        for device in monitor.get_devices() or []:
            name = device.get_display_name() or ""
            props = device.get_properties()
            if props:
                # gst-plugin-ndi exposes the canonical name here; prefer it.
                for key in ("ndi-name", "ndi.name"):
                    val = props.get_string(key)
                    if val:
                        name = val
                        break
            if name and name not in seen:
                seen.add(name)
                found.append(NdiSource.from_name(name))
        return found
    finally:
        monitor.stop()


def _discover_cli(timeout: float) -> List[NdiSource]:
    """Fallback: run gst-device-monitor-1.0 in follow mode and parse its output."""
    cmd = ["gst-device-monitor-1.0", "-f", f"{DEVICE_CLASSES}:application/x-ndi"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout
    except subprocess.TimeoutExpired as exc:
        # Expected: follow mode never exits, we kill it after `timeout`.
        output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("NDI discovery via CLI failed: %s", exc)
        return []

    found: List[NdiSource] = []
    seen: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        m = re.match(r"^(?:name|ndi-name)\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if not m:
            continue
        name = m.group(1).strip().strip('"')
        if name and name not in seen and name.lower() != "ndi source":
            seen.add(name)
            found.append(NdiSource.from_name(name))
    return found


def discover(timeout: float = 2.0, use_cache: bool = True) -> List[NdiSource]:
    """Return NDI senders visible on the network."""
    global _cache, _cache_at

    with _cache_lock:
        if use_cache and _cache_at and (time.monotonic() - _cache_at) < _CACHE_TTL:
            return list(_cache)

    sources: List[NdiSource] = []
    try:
        sources = _discover_gi(timeout)
    except Exception as exc:  # noqa: BLE001 - any gi/Gst failure falls back
        log.info("gi-based NDI discovery unavailable (%s); trying CLI", exc)
        try:
            sources = _discover_cli(timeout)
        except Exception as exc2:  # noqa: BLE001
            log.warning("NDI discovery failed entirely: %s", exc2)
            sources = []

    sources.sort(key=lambda s: s.name.lower())
    with _cache_lock:
        _cache = sources
        _cache_at = time.monotonic()
    return list(sources)


def invalidate_cache() -> None:
    global _cache_at
    with _cache_lock:
        _cache_at = 0.0
