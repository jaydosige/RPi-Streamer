"""NDI source discovery.

Discovery is a long-lived background activity, not a per-request scan.

The NDI SDK's finder needs several seconds to build a picture of the network,
and it only accumulates while it is running. Starting a fresh finder on every
GUI poll and tearing it down two seconds later means it never gets far enough
to see anything — which is exactly why a short per-request scan found nothing
on a network where other NDI apps found everything. Those apps hold a finder
open for the life of the process; so do we.

One GstDeviceMonitor is started at service startup and runs under its own GLib
main loop for the life of the process, adding and removing sources as senders
appear and disappear. Callers read the current snapshot instantly.

Falls back to a persistent `gst-device-monitor-1.0 -f` subprocess if python3-gi
is unavailable.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# The device provider in gst-plugin-ndi registers itself as
# "Source/Audio/Video/Network" and advertises ndisrc's src pad caps,
# "application/x-ndi". A filter matches when every class in the filter is
# present on the device, so "Source/Network" is the correct narrow filter.
DEVICE_CLASSES = "Source/Network"
DEVICE_CAPS = "application/x-ndi"


@dataclass
class NdiSource:
    name: str  # full NDI name, e.g. "STUDIO-PC (OBS)"
    host: str = ""
    stream: str = ""
    url: str = ""
    first_seen: float = field(default=0.0)

    @classmethod
    def from_name(cls, name: str, url: str = "") -> "NdiSource":
        m = re.match(r"^(?P<host>[^(]+?)\s*\((?P<stream>.+)\)\s*$", name)
        if m:
            return cls(name=name, host=m.group("host"), stream=m.group("stream"), url=url)
        return cls(name=name, host=name, stream="", url=url)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "host": self.host,
            "stream": self.stream,
            "url": self.url,
        }


class _Discovery:
    """Owns the long-lived finder and the current set of visible senders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sources: Dict[str, NdiSource] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._monitor = None  # Gst.DeviceMonitor
        self._loop = None  # GLib.MainLoop
        self._proc: Optional[subprocess.Popen] = None
        self._backend = "none"
        self._error = ""
        self._started_at: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ndi-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop is not None:
            try:
                self._loop.quit()
            except Exception:  # noqa: BLE001
                pass
        if self._monitor is not None:
            try:
                self._monitor.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()

    def _run(self) -> None:
        try:
            self._run_gi()
        except Exception as exc:  # noqa: BLE001
            log.info("gi-based NDI discovery unavailable (%s); falling back to CLI", exc)
            try:
                self._run_cli()
            except Exception as exc2:  # noqa: BLE001
                self._error = f"discovery unavailable: {exc2}"
                log.error("%s", self._error)

    # -- primary backend: GstDeviceMonitor ---------------------------------

    def _run_gi(self) -> None:
        import gi  # type: ignore

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst  # type: ignore

        if not Gst.is_initialized():
            Gst.init(None)

        # Fail loudly and specifically if the plugin never made it onto the box.
        if Gst.ElementFactory.find("ndisrc") is None:
            raise RuntimeError(
                "the ndisrc element is not registered — check GST_PLUGIN_PATH "
                "and that libgstndi.so loads (gst-inspect-1.0 ndisrc)"
            )

        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter(DEVICE_CLASSES, Gst.Caps.new_empty_simple(DEVICE_CAPS))

        bus = monitor.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        if not monitor.start():
            raise RuntimeError("GstDeviceMonitor.start() returned false")

        self._monitor = monitor
        self._backend = "gi"
        self._started_at = time.monotonic()
        self._error = ""
        log.info("NDI discovery running (GstDeviceMonitor)")

        # Seed from anything the provider already knows about.
        for device in monitor.get_devices() or []:
            self._add_device(device)

        self._loop = GLib.MainLoop()
        self._loop.run()

    def _on_bus_message(self, _bus, message) -> None:
        from gi.repository import Gst  # type: ignore

        if message.type == Gst.MessageType.DEVICE_ADDED:
            self._add_device(message.parse_device_added())
        elif message.type == Gst.MessageType.DEVICE_REMOVED:
            self._remove_device(message.parse_device_removed())

    @staticmethod
    def _device_fields(device) -> tuple[str, str]:
        name, url = "", ""
        props = device.get_properties()
        if props:
            name = props.get_string("ndi-name") or ""
            url = props.get_string("url-address") or ""
        if not name:
            name = device.get_display_name() or ""
        return name, url

    def _add_device(self, device) -> None:
        name, url = self._device_fields(device)
        if not name:
            return
        with self._lock:
            if name not in self._sources:
                log.info("NDI source appeared: %s", name)
            self._sources[name] = NdiSource.from_name(name, url)
            self._sources[name].first_seen = time.time()

    def _remove_device(self, device) -> None:
        name, _ = self._device_fields(device)
        with self._lock:
            if self._sources.pop(name, None) is not None:
                log.info("NDI source vanished: %s", name)

    # -- fallback backend: persistent CLI monitor --------------------------

    def _run_cli(self) -> None:
        if not shutil.which("gst-device-monitor-1.0"):
            raise RuntimeError("gst-device-monitor-1.0 not installed")

        self._backend = "cli"
        self._started_at = time.monotonic()
        log.info("NDI discovery running (gst-device-monitor-1.0 -f)")

        while not self._stop.is_set():
            self._proc = subprocess.Popen(  # noqa: S603
                ["gst-device-monitor-1.0", "-f", f"{DEVICE_CLASSES}:{DEVICE_CAPS}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            removing = False
            try:
                for line in self._proc.stdout or []:
                    if self._stop.is_set():
                        break
                    stripped = line.strip()
                    if stripped.startswith("Device removed:"):
                        removing = True
                        continue
                    if stripped.startswith("Device found:") or stripped.startswith("Device added:"):
                        removing = False
                        continue
                    m = re.match(r'^ndi-name\s*=\s*"?(.+?)"?$', stripped)
                    if not m:
                        continue
                    name = m.group(1)
                    with self._lock:
                        if removing:
                            self._sources.pop(name, None)
                        else:
                            src = NdiSource.from_name(name)
                            src.first_seen = time.time()
                            self._sources[name] = src
            finally:
                if self._proc and self._proc.poll() is None:
                    self._proc.terminate()
            if self._stop.wait(5):
                break  # monitor died; restart it after a pause

    # -- reads -------------------------------------------------------------

    def snapshot(self) -> List[NdiSource]:
        with self._lock:
            return sorted(self._sources.values(), key=lambda s: s.name.lower())

    def status(self) -> dict:
        with self._lock:
            count = len(self._sources)
        running = bool(self._thread and self._thread.is_alive())
        return {
            "running": running,
            "backend": self._backend,
            "error": self._error,
            "count": count,
            "uptime": (
                round(time.monotonic() - self._started_at, 1) if self._started_at else None
            ),
        }


_discovery = _Discovery()


def start() -> None:
    _discovery.start()


def stop() -> None:
    _discovery.stop()


def discover(timeout: float = 0.0, use_cache: bool = True) -> List[NdiSource]:
    """Current snapshot of visible NDI senders.

    Returns immediately — the finder runs continuously in the background. The
    `timeout` argument is kept for callers that want to block on a cold start;
    it waits at most that long for the first source to turn up.
    """
    _discovery.start()
    if timeout > 0:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not _discovery.snapshot():
            time.sleep(0.2)
    return _discovery.snapshot()


def status() -> dict:
    return _discovery.status()
