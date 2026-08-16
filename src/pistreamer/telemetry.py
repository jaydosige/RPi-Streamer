"""Rolling telemetry history.

A single instantaneous reading tells you very little during a show. What you
want to know is whether the temperature has been climbing for ten minutes,
whether frame rate dipped when someone plugged in a switch, whether
under-voltage flickered once an hour ago. So a background thread samples at a
fixed interval and keeps a bounded window in memory.

Nothing is written to disk: this is a node with an SD card, and the whole
point of the journald-to-RAM setting elsewhere is to stop writing to it.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from . import system

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = 2.0
WINDOW_SECONDS = 600  # ten minutes
MAX_SAMPLES = int(WINDOW_SECONDS / SAMPLE_INTERVAL)


class Telemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_SAMPLES)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._latest: Dict[str, Any] = {}
        # Peaks are worth keeping even after they scroll out of the window —
        # "it hit 82°C at some point" is the thing you need to know later.
        self._peaks: Dict[str, Any] = {
            "cpu_temp": None,
            "cpu_percent": None,
            "rx_mbps": None,
        }
        self._player_stats_source = None

    def bind_player(self, getter) -> None:
        """Provide a callable returning the player's current stream stats."""
        self._player_stats_source = getter

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Prime the rate-based readers so the first real sample has deltas.
        try:
            system.cpu_usage()
            system.network()
        except Exception:  # noqa: BLE001
            pass
        while not self._stop.wait(SAMPLE_INTERVAL):
            try:
                self._sample()
            except Exception as exc:  # noqa: BLE001 - a sampler must not die
                log.warning("telemetry sample failed: %s", exc)

    def _sample(self) -> None:
        summary = system.summary()
        net = summary.get("network") or []
        rx = max((n.get("rx_mbps") or 0) for n in net) if net else 0.0
        tx = max((n.get("tx_mbps") or 0) for n in net) if net else 0.0

        stream: Dict[str, Any] = {}
        if self._player_stats_source is not None:
            try:
                stream = self._player_stats_source() or {}
            except Exception:  # noqa: BLE001
                stream = {}

        sample = {
            "t": time.time(),
            "cpu_percent": summary.get("cpu_percent"),
            "cpu_temp": summary.get("cpu_temp"),
            "cpu_freq": (summary.get("cpu_freq") or {}).get("current"),
            "mem_used": (
                summary["memory"]["total"] - summary["memory"]["available"]
                if summary.get("memory", {}).get("total") is not None
                and summary.get("memory", {}).get("available") is not None
                else None
            ),
            "rx_mbps": rx,
            "tx_mbps": tx,
            "fps": stream.get("fps"),
            "dropped": stream.get("dropped"),
        }

        with self._lock:
            self._samples.append(sample)
            self._latest = summary
            for key in ("cpu_temp", "cpu_percent", "rx_mbps"):
                value = sample.get(key)
                if value is None:
                    continue
                current = self._peaks.get(key)
                if current is None or value > current:
                    self._peaks[key] = value

    # -- reads -------------------------------------------------------------

    def history(self, keys: Optional[List[str]] = None) -> Dict[str, Any]:
        """Return the sample window as parallel arrays, which is what a chart wants."""
        with self._lock:
            samples = list(self._samples)
            peaks = dict(self._peaks)
        if not samples:
            return {"t": [], "series": {}, "peaks": peaks, "interval": SAMPLE_INTERVAL}
        if keys is None:
            keys = [k for k in samples[-1].keys() if k != "t"]
        return {
            "t": [s["t"] for s in samples],
            "series": {k: [s.get(k) for s in samples] for k in keys},
            "peaks": peaks,
            "interval": SAMPLE_INTERVAL,
            "window_seconds": WINDOW_SECONDS,
        }

    def latest(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._latest)


telemetry = Telemetry()
