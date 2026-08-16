"""Playback engine.

Exactly one process may hold the DRM display at a time, so this module is
built around a single-owner state machine: every mode change tears the
current process down and waits for it to release KMS before starting the
next one. A supervisor thread restarts the process with backoff if it dies,
which is what makes the node survive an NDI sender going away and coming
back without anyone touching it.

Two backends:
  * NDI    -> gst-launch-1.0 with ndisrc/ndisrcdemux into kmssink
  * local  -> mpv with the DRM video output
"""

from __future__ import annotations

import collections
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional

from . import config, display, media

log = logging.getLogger(__name__)

MODE_IDLE = "idle"
MODE_NDI = "ndi"
MODE_LOCAL = "local"
VALID_MODES = {MODE_IDLE, MODE_NDI, MODE_LOCAL}

# Backoff schedule for automatic restarts, in seconds.
_BACKOFF = [1, 2, 5, 10, 15, 30]
# A process that stayed up this long is considered healthy; backoff resets.
_HEALTHY_AFTER = 30.0
_LOG_LINES = 300


@dataclass
class PlayerStatus:
    mode: str = MODE_IDLE
    target: str = ""  # NDI source name or media filename
    running: bool = False
    pid: Optional[int] = None
    since: Optional[float] = None
    restarts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict:
        uptime = time.time() - self.since if self.since else None
        return {
            "mode": self.mode,
            "target": self.target,
            "running": self.running,
            "pid": self.pid,
            "uptime": round(uptime, 1) if uptime is not None else None,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }


def _flip_method(rotation: int) -> Optional[str]:
    return {90: "clockwise", 180: "rotate-180", 270: "counterclockwise"}.get(rotation)


def _gst_quote(value: str) -> str:
    """Quote a property value for the gst-launch parser.

    gst-launch-1.0 joins its argv with spaces and re-parses the result, so
    passing the value as a single argv element is not enough: a real NDI name
    like `STUDIO-PC (OBS)` contains a space and parentheses, and parentheses
    open a bin in gst-launch syntax. The value has to carry literal quotes.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _bandwidth_value(setting: str) -> int:
    """Map the config's friendly name onto ndisrc's integer `bandwidth`.

    ndisrc exposes bandwidth as an int in the range -10..100 mirroring the NDI
    SDK enum (-10 metadata-only, 0 lowest/proxy, 10 audio-only, 100 highest).
    It is NOT a string enum — passing "highest" fails to parse and the pipeline
    never starts.
    """
    return {"highest": 100, "lowest": 0}.get(setting, 100)


def _connector_id(connector_name: str) -> Optional[int]:
    """Read the numeric DRM connector id kmssink wants, from sysfs."""
    if not connector_name or not display.DRM_SYSFS.exists():
        return None
    for entry in display.DRM_SYSFS.iterdir():
        if not entry.name.endswith(f"-{connector_name}"):
            continue
        id_file = entry / "connector_id"
        if id_file.exists():
            try:
                return int(id_file.read_text().strip())
            except (OSError, ValueError):
                return None
    return None


class Player:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._status = PlayerStatus()
        self._logs: Deque[str] = collections.deque(maxlen=_LOG_LINES)
        self._stop_event = threading.Event()
        self._wanted_mode = MODE_IDLE
        self._wanted_target = ""
        self._backoff_idx = 0
        self._supervisor: Optional[threading.Thread] = None
        self._reader: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _ndi_command(self, cfg: config.Config, source: str) -> List[str]:
        conn = display.pick_connector(cfg.connector)
        conn_name = conn.name if conn else ""
        conn_id = _connector_id(conn_name)

        latency_ns = max(0, cfg.ndi_latency_ms) * 1_000_000

        sink = ["kmssink", "force-modesetting=true"]
        if conn_id is not None:
            sink.append(f"connector-id={conn_id}")
        # driver-name, not bus-id, and not a device path. kmssink passes
        # bus-id to drmOpen(NULL, bus_id) as a *bus* identifier; a
        # /dev/dri/cardN path there fails with "Could not open DRM module".
        # If we cannot identify the driver, say nothing and let kmssink probe
        # its built-in list.
        driver = display.drm_driver_for(conn_name) if conn_name else None
        if driver:
            sink.append(f"driver-name={driver}")

        video_chain = [
            "queue",
            f"max-size-time={latency_ns}",
            "max-size-bytes=0",
            "max-size-buffers=0",
            "leaky=downstream",
            "!",
            "videoconvert",
        ]
        flip = _flip_method(cfg.rotation)
        if flip:
            video_chain += ["!", "videoflip", f"method={flip}"]

        # Scale to a mode the connector actually advertises and pin the pixel
        # format. kmssink only sets a mode that matches the frame size
        # exactly, and allocates its mode-setting buffer at that size, so an
        # arbitrary sender resolution fails with "failed to allocate buffer
        # object for mode setting". add-borders keeps the aspect ratio and
        # pillar/letterboxes the rest.
        mode = display.target_mode(conn, cfg.video_mode)
        caps = "video/x-raw,format=BGRx"
        if mode:
            caps += f",width={mode.width},height={mode.height}"
        video_chain += ["!", "videoscale", "add-borders=true", "!", caps, "!"] + sink

        cmd: List[str] = [
            "gst-launch-1.0",
            "-q",
            "ndisrc",
            f"ndi-name={_gst_quote(source)}",
            f"bandwidth={_bandwidth_value(cfg.ndi_bandwidth)}",
            # Surface a dead sender as a pipeline error instead of hanging
            # silently; the supervisor then retries with backoff.
            "connect-timeout=10000",
            "timeout=5000",
            f"timestamp-mode={cfg.ndi_timestamp_mode}",
            f"receiver-ndi-name={_gst_quote(cfg.device_name or 'pistreamer')}",
            "!",
            "ndisrcdemux",
            "name=demux",
            "demux.video",
            "!",
        ] + video_chain

        if cfg.audio_enabled:
            # provide-clock=false is load-bearing. An audio sink is the
            # pipeline's preferred clock provider by default, and that clock
            # only advances while audio is actually being consumed by the
            # card. If the sender has no audio, or HDMI audio is not really
            # playing, the clock stalls — and the video sink, which syncs to
            # it, renders exactly one frame and then waits forever.
            # async=false keeps a sulking audio sink out of preroll too.
            audio_sink = [
                "alsasink",
                "sync=false",
                "provide-clock=false",
                "async=false",
            ]
            if cfg.audio_device:
                audio_sink.append(f"device={cfg.audio_device}")
            cmd += [
                "demux.audio",
                "!",
                "queue",
                "leaky=downstream",
                f"max-size-time={latency_ns}",
                "!",
                "audioconvert",
                "!",
                "audioresample",
                "!",
            ] + audio_sink

        return cmd

    def _local_command(self, cfg: config.Config, selection: str) -> List[str]:
        files = media.playlist_paths(selection)
        if not files:
            raise RuntimeError("no playable media files found")

        conn = display.pick_connector(cfg.connector)
        cmd = [
            "mpv",
            "--no-config",
            "--no-terminal",
            "--msg-level=all=warn",
            "--fullscreen",
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=auto-safe",
            "--keep-open=no",
            "--image-display-duration=10",
        ]
        if conn:
            cmd.append(f"--drm-connector={conn.name}")
        if cfg.video_mode:
            # mpv takes the mode as WxH@R via drm-mode when the connector
            # supports it; otherwise it silently keeps the preferred mode.
            cmd.append(f"--drm-mode={cfg.video_mode}")
        if cfg.rotation:
            cmd.append(f"--video-rotate={cfg.rotation}")
        if cfg.loop:
            cmd.append("--loop-playlist=inf")
        if cfg.audio_enabled:
            cmd.append(f"--volume={max(0, min(100, cfg.volume))}")
            if cfg.audio_device:
                cmd.append(f"--audio-device=alsa/{cfg.audio_device}")
        else:
            cmd.append("--no-audio")

        cmd.append("--")
        cmd += files
        return cmd

    def _build_command(self, cfg: config.Config, mode: str, target: str) -> List[str]:
        if mode == MODE_NDI:
            if not target:
                raise RuntimeError("no NDI source selected")
            if not shutil.which("gst-launch-1.0"):
                raise RuntimeError("gst-launch-1.0 not installed")
            return self._ndi_command(cfg, target)
        if mode == MODE_LOCAL:
            if not shutil.which("mpv"):
                raise RuntimeError("mpv not installed")
            return self._local_command(cfg, target)
        raise RuntimeError(f"mode {mode!r} has no command")

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def _drain_output(self, proc: subprocess.Popen) -> None:
        """Pump the child's stderr into the ring buffer for the GUI log view."""
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                if line:
                    self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        except (OSError, ValueError):
            pass

    def _spawn(self, cfg: config.Config, mode: str, target: str) -> None:
        cmd = self._build_command(cfg, mode, target)
        log.info("starting: %s", " ".join(shlex.quote(c) for c in cmd))
        self._logs.append(f"{time.strftime('%H:%M:%S')} $ {' '.join(shlex.quote(c) for c in cmd)}")

        env = dict(os.environ)
        env.setdefault("GST_DEBUG", "1")

        proc = subprocess.Popen(  # noqa: S603 - argv list, never shell=True
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group, so we can kill children
        )
        self._proc = proc
        self._status.pid = proc.pid
        self._status.running = True
        self._status.since = time.time()
        self._reader = threading.Thread(target=self._drain_output, args=(proc,), daemon=True)
        self._reader.start()

    def _terminate(self, timeout: float = 5.0) -> None:
        """Stop the current process and wait for it to release the display."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("player pid %s ignored SIGTERM; killing", proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                log.error("player pid %s would not die", proc.pid)
        self._status.running = False
        self._status.pid = None
        self._status.since = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the supervisor thread. Safe to call once."""
        if self._supervisor and self._supervisor.is_alive():
            return
        self._stop_event.clear()
        self._supervisor = threading.Thread(target=self._run, name="player", daemon=True)
        self._supervisor.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        with self._lock:
            self._wanted_mode = MODE_IDLE
            self._terminate()
        if self._supervisor:
            self._supervisor.join(timeout=10)

    def apply(self, mode: str, target: str = "") -> None:
        """Switch to a mode. Raises ValueError for an unknown mode."""
        if mode not in VALID_MODES:
            raise ValueError(f"unknown mode: {mode}")
        with self._lock:
            self._wanted_mode = mode
            self._wanted_target = target
            self._backoff_idx = 0
            self._status.last_error = ""
            self._status.restarts = 0
            self._terminate()
            self._status.mode = mode
            self._status.target = target
            if mode == MODE_IDLE:
                return
            try:
                self._spawn(config.load(), mode, target)
            except Exception as exc:  # noqa: BLE001 - surfaced to the GUI
                self._status.last_error = str(exc)
                self._status.running = False
                log.error("failed to start %s/%s: %s", mode, target, exc)

    def restart(self) -> None:
        with self._lock:
            self.apply(self._wanted_mode, self._wanted_target)

    def status(self) -> dict:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                self._status.running = False
            return self._status.to_dict()

    def logs(self) -> List[str]:
        return list(self._logs)

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(1.0)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._wanted_mode == MODE_IDLE:
                    continue
                proc = self._proc
                if proc is not None and proc.poll() is None:
                    # Healthy for long enough? Reset backoff.
                    if self._status.since and (time.time() - self._status.since) > _HEALTHY_AFTER:
                        self._backoff_idx = 0
                    continue

                # Process is gone (crashed, or the NDI sender vanished).
                rc = proc.returncode if proc is not None else None
                if proc is not None:
                    self._status.last_error = f"player exited with code {rc}"
                self._status.running = False
                self._proc = None

                delay = _BACKOFF[min(self._backoff_idx, len(_BACKOFF) - 1)]
                self._backoff_idx += 1
                self._status.restarts += 1
                mode, target = self._wanted_mode, self._wanted_target

            log.info("player down (%s); retrying in %ss", self._status.last_error, delay)
            if self._stop_event.wait(delay):
                break

            with self._lock:
                if self._wanted_mode != mode or self._wanted_target != target:
                    continue  # user changed their mind while we waited
                try:
                    self._spawn(config.load(), mode, target)
                except Exception as exc:  # noqa: BLE001
                    self._status.last_error = str(exc)
                    log.error("restart failed: %s", exc)


# Module-level singleton the web layer talks to.
player = Player()
