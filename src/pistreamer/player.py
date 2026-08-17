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
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, List, Optional

from . import config, display, media, playlists, sources

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
# Prefix the runner uses to mark a machine-readable stats line on stdout.
STATS_PREFIX = "@STATS "


@dataclass
class PlayerStatus:
    mode: str = MODE_IDLE
    target: str = ""  # NDI source name or media filename
    running: bool = False
    pid: Optional[int] = None
    since: Optional[float] = None
    restarts: int = 0
    last_error: str = ""
    # Standby is on screen because the wanted source went away.
    fallback: bool = False

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
            "fallback": self.fallback,
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


def snapshot_path() -> Path:
    """Where the running pipeline keeps the most recent frame."""
    return config.STATE_DIR / "lastframe.jpg"


def _usable_jpeg(path: Path) -> bool:
    """Is this a complete JPEG?

    multifilesink rewrites the snapshot in place, so a read can land
    mid-write. Checking for the end-of-image marker is cheap and rules that
    out; a torn file just means we fall back to black for a few seconds.
    """
    try:
        if not path.is_file() or path.stat().st_size < 1024:
            return False
        with path.open("rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return False
            fh.seek(-2, os.SEEK_END)
            return fh.read(2) == b"\xff\xd9"
    except OSError:
        return False


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
        self._stats_reader: Optional[threading.Thread] = None
        self._stream_stats: dict = {}
        # True while the standby screen is covering for a lost source.
        self._fallback = False
        self._next_attempt = 0.0

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
        # A named playlist wins over a single file or the whole folder, and
        # brings its own loop/shuffle/dwell settings with it.
        playlist = None
        if cfg.local_playlist:
            playlist = playlists.get(cfg.local_playlist)
            if playlist is None:
                raise RuntimeError(f"playlist not found: {cfg.local_playlist}")
            files = playlists.resolved_files(cfg.local_playlist)
            if not files:
                raise RuntimeError(
                    f"playlist {cfg.local_playlist!r} has no playable files left"
                )
        else:
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
            f"--image-display-duration={playlist.image_duration if playlist else 10}",
        ]
        if conn:
            cmd.append(f"--drm-connector={conn.name}")
        if cfg.video_mode:
            # mpv takes the mode as WxH@R via drm-mode when the connector
            # supports it; otherwise it silently keeps the preferred mode.
            cmd.append(f"--drm-mode={cfg.video_mode}")
        if cfg.rotation:
            cmd.append(f"--video-rotate={cfg.rotation}")
        if (playlist.loop if playlist else cfg.loop):
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

    def _runner_command(self, cfg: config.Config, source: str) -> List[str]:
        """Spawn the instrumented runner, which builds the pipeline itself."""
        ok, reason = sources.ndi_available()
        if not ok:
            raise RuntimeError(reason)
        conn = display.pick_connector(cfg.connector)
        conn_name = conn.name if conn else ""
        mode = display.target_mode(conn, cfg.video_mode)
        spec = {
            "source": source,
            "bandwidth": _bandwidth_value(cfg.ndi_bandwidth),
            "timestamp_mode": cfg.ndi_timestamp_mode,
            "latency_ms": cfg.ndi_latency_ms,
            "receiver_name": cfg.device_name or "pistreamer",
            "rotation": cfg.rotation,
            "width": mode.width if mode else None,
            "height": mode.height if mode else None,
            "connector_id": _connector_id(conn_name) if conn_name else None,
            "driver_name": display.drm_driver_for(conn_name) if conn_name else None,
            "audio": cfg.audio_enabled,
            "audio_device": cfg.audio_device,
            "stats_interval": 1.0,
            "color_format": cfg.ndi_color_format,
            "max_queue": cfg.ndi_max_queue,
            "connect_timeout_ms": cfg.ndi_connect_timeout_ms,
            "timeout_ms": cfg.ndi_timeout_ms,
            "sink_sync": cfg.sink_sync,
            "sink_qos": cfg.sink_qos,
            "sink_max_lateness_ms": cfg.sink_max_lateness_ms,
            "scale_method": cfg.scale_method,
            "video_format": cfg.video_format,
            "queue_leaky": cfg.queue_leaky,
            "queue_max_buffers": cfg.queue_max_buffers,
            "convert_threads": cfg.convert_threads,
            "match_source": cfg.match_source,
            "url_address": cfg.ndi_url_address,
            "snapshot_path": str(snapshot_path()) if cfg.snapshot_enabled else None,
            "snapshot_interval_s": cfg.snapshot_interval_s,
        }
        return [sys.executable, "-m", "pistreamer.runner", json.dumps(spec)]

    def _idle_command(self, cfg: config.Config) -> List[str]:
        """The standby screen. An appliance should never show a console.

        Note this means idle is an *active* pipeline holding the display, not
        the absence of one — that is the whole point.
        """
        image: Optional[str] = None
        if cfg.idle_mode == "image" and cfg.standby_file:
            path = media.resolve(cfg.standby_file)
            if path is not None:
                if path.suffix.lower() in media.VIDEO_EXTS:
                    # A standby video is just local playback that happens to
                    # be the fallback, so reuse the mpv path and loop it.
                    return self._local_command(cfg, cfg.standby_file)
                image = str(path)
        elif cfg.idle_mode == "lastframe":
            snap = snapshot_path()
            if _usable_jpeg(snap):
                image = str(snap)

        conn = display.pick_connector(cfg.connector)
        conn_name = conn.name if conn else ""
        mode_ = display.target_mode(conn, cfg.video_mode)
        spec = {
            "source_type": "idle",
            "idle_image": image,
            "width": mode_.width if mode_ else None,
            "height": mode_.height if mode_ else None,
            "connector_id": _connector_id(conn_name) if conn_name else None,
            "driver_name": display.drm_driver_for(conn_name) if conn_name else None,
            "rotation": cfg.rotation,
            "audio": False,
            "video_format": cfg.video_format,
            "scale_method": cfg.scale_method,
            "sink_sync": True,
            "sink_qos": False,
            "stats_interval": 5.0,
        }
        return [sys.executable, "-m", "pistreamer.runner", json.dumps(spec)]

    def _build_command(self, cfg: config.Config, mode: str, target: str) -> List[str]:
        if mode == MODE_IDLE:
            if cfg.use_gst_launch:
                raise RuntimeError("standby screen requires the instrumented runner")
            ok, reason = sources.gstreamer_available()
            if not ok:
                raise RuntimeError(reason)
            return self._idle_command(cfg)
        if mode == MODE_NDI:
            if not target:
                raise RuntimeError("no NDI source selected")
            if cfg.use_gst_launch:
                if not shutil.which("gst-launch-1.0"):
                    raise RuntimeError("gst-launch-1.0 not installed")
                return self._ndi_command(cfg, target)
            return self._runner_command(cfg, target)
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

    def _drain_stats(self, proc: subprocess.Popen) -> None:
        """Read the runner's stdout, splitting stats lines from plain output."""
        stream = proc.stdout
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                if not line:
                    continue
                if line.startswith(STATS_PREFIX):
                    try:
                        with self._lock:
                            self._stream_stats = json.loads(line[len(STATS_PREFIX):])
                    except json.JSONDecodeError:
                        pass
                else:
                    self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        except (OSError, ValueError):
            pass

    def stream_stats(self) -> dict:
        """Latest per-second stats from the runner, or {} on the fallback path."""
        with self._lock:
            stats = dict(self._stream_stats)
        # Stats go stale if the runner dies without us noticing.
        if stats and (time.time() - stats.get("t", 0)) > 10:
            return {}
        return stats

    def _spawn(self, cfg: config.Config, mode: str, target: str) -> None:
        self._spawn_command(self._build_command(cfg, mode, target), mode)

    def _spawn_command(self, cmd: List[str], what: str) -> None:
        log.info("starting %s: %s", what, " ".join(shlex.quote(c) for c in cmd))
        self._logs.append(f"{time.strftime('%H:%M:%S')} $ {' '.join(shlex.quote(c) for c in cmd)}")

        env = dict(os.environ)
        env.setdefault("GST_DEBUG", "1")

        proc = subprocess.Popen(  # noqa: S603 - argv list, never shell=True
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,  # own process group, so we can kill children
        )
        self._proc = proc
        self._status.pid = proc.pid
        self._status.running = True
        self._status.since = time.time()
        self._stream_stats = {}
        self._reader = threading.Thread(target=self._drain_output, args=(proc,), daemon=True)
        self._reader.start()
        self._stats_reader = threading.Thread(
            target=self._drain_stats, args=(proc,), daemon=True
        )
        self._stats_reader.start()

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
            self._fallback = False
            self._status.fallback = False
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
            self._status.fallback = self._fallback
            return self._status.to_dict()

    def logs(self) -> List[str]:
        return list(self._logs)

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------

    def _backoff_delay(self) -> int:
        delay = _BACKOFF[min(self._backoff_idx, len(_BACKOFF) - 1)]
        self._backoff_idx += 1
        return delay

    def _enter_fallback(self, cfg: config.Config) -> None:
        """Put the standby screen up while we wait for the source to return.

        This is the fix for a real design hole: the supervisor used to retry
        the dead source directly, which left the display showing whatever the
        console had — so "hold the last frame" only ever worked on an explicit
        stop, never on the case that actually matters, a feed dropping
        mid-show. Standby is a real pipeline holding DRM, so it has to be
        started and torn down like any other mode.
        """
        self._terminate()
        self._fallback = True
        try:
            cmd = self._idle_command(cfg)
            log.info("source lost — showing standby while retrying")
            self._spawn_command(cmd, "standby")
        except Exception as exc:  # noqa: BLE001
            self._status.last_error = f"standby screen failed: {exc}"
            log.error("%s", self._status.last_error)
        self._next_attempt = time.monotonic() + self._backoff_delay()

    def _source_ready(self, mode: str, target: str) -> bool:
        """Is it worth tearing down standby to try the source again?

        For a named NDI source we can ask the finder, which runs continuously
        and needs no display — so we only interrupt standby once the sender is
        actually visible again. With an explicit address there is nothing to
        ask, so we just retry on the backoff schedule.
        """
        if mode != MODE_NDI:
            return True
        if config.load().ndi_url_address.strip():
            return True
        try:
            names = {s.name for s in sources.discover(timeout=0.0)}
        except Exception:  # noqa: BLE001
            return True
        return target in names

    def _try_resume(self, cfg: config.Config, mode: str, target: str) -> None:
        self._terminate()
        self._fallback = False
        try:
            self._spawn(cfg, mode, target)
        except Exception as exc:  # noqa: BLE001
            self._status.last_error = str(exc)
            log.error("resume failed: %s", exc)
            self._enter_fallback(cfg)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(1.0):
                break
            try:
                self._supervise()
            except Exception:  # noqa: BLE001 - a supervisor must never die
                log.exception("supervisor tick failed")

    def _supervise(self) -> None:
        with self._lock:
            mode, target = self._wanted_mode, self._wanted_target
            cfg = config.load()
            proc = self._proc
            alive = proc is not None and proc.poll() is None

            if alive:
                if self._fallback:
                    # Standby is on screen. Check periodically whether the
                    # real source is back.
                    if time.monotonic() >= self._next_attempt:
                        if self._source_ready(mode, target):
                            log.info("source available again; leaving standby")
                            self._try_resume(cfg, mode, target)
                        else:
                            self._next_attempt = time.monotonic() + self._backoff_delay()
                elif self._status.since and (time.time() - self._status.since) > _HEALTHY_AFTER:
                    self._backoff_idx = 0  # been up a while; forget the backoff
                return

            # Whatever was running has exited.
            if proc is not None:
                self._status.last_error = (
                    f"standby screen exited with code {proc.returncode}"
                    if self._fallback
                    else f"player exited with code {proc.returncode}"
                )
            self._status.running = False
            self._proc = None

            if mode == MODE_IDLE:
                # The standby screen is the point of idle; bring it back.
                self._next_attempt = time.monotonic() + self._backoff_delay()
                try:
                    self._spawn(cfg, mode, target)
                except Exception as exc:  # noqa: BLE001
                    self._status.last_error = str(exc)
                return

            self._status.restarts += 1

            if self._fallback:
                # Standby died rather than the source. Try to get it back up.
                self._enter_fallback(cfg)
                return

            if mode == MODE_NDI and cfg.fallback_to_standby:
                self._enter_fallback(cfg)
                return

            # Local playback, or fallback disabled: straight retry on backoff.
            self._next_attempt = time.monotonic() + self._backoff_delay()

        # Outside the lock: wait out the backoff, then retry if nothing changed.
        delay = max(0.0, self._next_attempt - time.monotonic())
        if delay and self._stop_event.wait(delay):
            return
        with self._lock:
            if (self._wanted_mode, self._wanted_target) != (mode, target):
                return  # the user changed their mind while we waited
            if self._proc is not None and self._proc.poll() is None:
                return  # something already restarted it
            try:
                self._spawn(config.load(), self._wanted_mode, self._wanted_target)
            except Exception as exc:  # noqa: BLE001
                self._status.last_error = str(exc)
                log.error("restart failed: %s", exc)


# Module-level singleton the web layer talks to.
player = Player()
