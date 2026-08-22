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
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from . import (airplay, config, display, favourites, guest, media, mpvipc,
               playlists, preview, sources, syncplay)

log = logging.getLogger(__name__)

MODE_IDLE = "idle"
MODE_NDI = "ndi"
MODE_LOCAL = "local"
MODE_WEB = "web"
# A live video stream pulled from a URL — HLS, DASH, UDP/RTP multicast, RTSP,
# SRT. Distinct from MODE_LOCAL because there is no file and no playlist, and
# distinct from MODE_NDI because it is mpv rather than the GStreamer runner.
MODE_STREAM = "stream"
# Receiving an AirPlay mirror. A mode, not a background service, because the
# session takes the display and the display has exactly one owner.
MODE_AIRPLAY = "airplay"
VALID_MODES = {MODE_IDLE, MODE_NDI, MODE_LOCAL, MODE_WEB, MODE_STREAM,
               MODE_AIRPLAY}

# Backoff schedule for automatic restarts, in seconds.
_BACKOFF = [1, 2, 5, 10, 15, 30]
# A process that stayed up this long is considered healthy; backoff resets.
_HEALTHY_AFTER = 30.0
_LOG_LINES = 300
# Prefix the runner uses to mark a machine-readable stats line on stdout.
STATS_PREFIX = "@STATS "
# How long the AirPlay receiver gets to start advertising itself before we
# call it broken. It normally takes well under a second.
# The mpv overlay slot the guest panel occupies. Fixed, so replacing it is a
# second overlay-add rather than a remove-then-add that flickers.
GUEST_OVERLAY_ID = 7
# Padding from the screen edge, shared with the GStreamer side so the
# panel lands in the same place whichever backend is playing. TVs overscan.
OVERLAY_PAD = 40
_AIRPLAY_LISTEN_TIMEOUT = 10.0
# uxplay validates its sink and decoder strings at startup and aborts on a bad
# one in about 40 ms. Anything that dies this fast died of its configuration,
# and restarting it will do exactly the same thing.
_AIRPLAY_FAST_FAIL = 3.0
_AIRPLAY_FAST_FAIL_LIMIT = 3
# Supervisor tick. Fast enough that a segment change is not visibly late —
# at 1s a playlist transition could sit black for most of a second.
_TICK = 0.25

# Command-line fragments that identify a player process of ours. Used only to
# clean up strays: a player that outlived its supervisor keeps its audio going
# and mixes with the next one, which is heard as two tracks at once.
_PLAYER_SIGNATURES = ("pistreamer.runner", "mpv --no-config", "gst-launch-1.0",
                      "uxplay", "chromium")


def _stray_players(exclude_pid: Optional[int] = None) -> List[int]:
    """PIDs of our own player processes that nothing is supervising.

    Scoped deliberately narrowly: same uid, command line matching one of our
    own spawn signatures, never the process we are currently tracking, and
    never this process. Anything wider risks killing a user's own SSH session.
    """
    out: List[int] = []
    me = os.getpid()
    uid = os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in (me, exclude_pid):
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError):
            continue
        if any(sig in cmdline for sig in _PLAYER_SIGNATURES):
            out.append(pid)
    return out


def reap_strays(exclude_pid: Optional[int] = None) -> List[int]:
    """Kill unsupervised player processes. Returns the PIDs it killed.

    A stray can only exist if a previous supervisor lost track of a child —
    an unclean service stop, a crash, or a player started by hand over SSH
    during testing. Whatever the cause, the audible symptom is two soundtracks
    at once and the visible one is a fight over DRM, so clean up rather than
    leaving it to chance.
    """
    killed: List[int] = []
    for pid in _stray_players(exclude_pid):
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except OSError:
            continue
    if not killed:
        return killed
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in killed):
            break
        time.sleep(0.05)
    for pid in killed:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    log.warning("cleaned up %d stray player process(es): %s", len(killed), killed)
    return killed


def _pid_alive(pid: int) -> bool:
    """Is this pid a process that could still be playing?

    A zombie counts as dead: it has released the display and the sound card and
    is only waiting to be reaped. Treating it as alive would make teardown
    spin for its full timeout every time we kill one of our own children.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b") ", 1)[-1].split(b" ", 1)
        return fields[0] != b"Z"
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    # Unsupervised player processes cleaned up so far this run.
    strays_cleaned: int = 0

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
            "strays_cleaned": self.strays_cleaned,
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


def mpv_socket() -> Path:
    """Where mpv listens for JSON IPC, so local playback can be measured."""
    return config.STATE_DIR / "mpv.sock"


def snapshot_path() -> Path:
    """Where the running pipeline keeps the most recent frame."""
    return config.STATE_DIR / "lastframe.jpg"


def overlay_path() -> Path:
    """The identify caption, read by the GStreamer runner twice a second.

    A file rather than a control channel: the runner is a separate process that
    owns the display, and an overlay must never require restarting it. Empty or
    absent means no caption.
    """
    return config.STATE_DIR / "overlay.txt"


def write_overlay(text: str) -> None:
    """Publish (or clear) the identify caption.

    Written atomically because the runner polls it: a torn read would show half
    a caption, and on a wall of screens that looks like a fault.
    """
    path = overlay_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not text:
            path.write_text("")
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("could not write the identify caption: %s", exc)


def _mpv_ok(reply: Any) -> bool:
    """Did mpv actually do it?

    `mpvipc.command` returns the whole reply, and an unreachable player gives
    {}. Both are dicts, and a reply carrying `"error": "property not found"` is
    truthy — so testing the reply for truth, as this code used to, counts a
    refusal as a success.
    """
    return isinstance(reply, dict) and reply.get("error") == "success"


def _mpv_data(reply: Any) -> Any:
    return reply.get("data") if _mpv_ok(reply) else None


def read_overlay() -> str:
    """The caption currently published, or "" if there is none.

    Needed because mpv is not one long-lived process: it is replaced on every
    file, every playlist segment and every standby switch, and each new one
    starts with a blank OSD. Setting the caption over IPC once — which is all
    that used to happen — meant identify survived exactly until the next item
    began, which is when somebody hunting for a node is most likely to be
    looking at it.
    """
    try:
        return overlay_path().read_text().strip()
    except OSError:
        return ""


def identify_text(cfg: config.Config, ip: str) -> str:
    """What an identified node shows: who it is and how to reach it.

    Name first and largest by position, address underneath — the name is what
    somebody is looking for when they are stood in front of six screens, and the
    address is what they need once they have found it.
    """
    return f"{cfg.device_name or 'pistreamer'}\n{ip}"


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
        # Playlist sequencer: used when a playlist mixes NDI with files, or
        # sets explicit durations, so mpv alone cannot play it.
        self._segments: List[dict] = []
        self._segment_idx = 0
        self._segment_deadline: Optional[float] = None
        # Which backend the current segment is using, so the next one knows
        # whether the process on screen can be reused.
        self._segment_backend = ""
        self._segment_loop = True
        # Set while a synchronised session owns playback, so the supervisor
        # does not treat a prepared-and-paused player as a crash to restart.
        self._sync_active = False
        # While a speed nudge is settling, leave it alone rather than stacking
        # corrections on top of each other.
        self._nudging_until = 0.0
        # When this node last went out of sync and started trying to get back.
        # 0 means it is not currently correcting. Nudging that never arrives is
        # invisible without this: every individual pulse looks like it is doing
        # the right thing.
        self._correcting_since = 0.0
        # The synchronised session this node is taking part in, and the one it
        # has been pulled out of by hand.
        self._sync_session = ""
        self._sync_declined = ""
        # How many unsupervised player processes we have had to clean up. If
        # this is not zero, something is escaping teardown and the user is
        # hearing two soundtracks — worth surfacing rather than hiding.
        self._stray_kills = 0
        # Consecutive AirPlay receivers that died before they could work.
        self._airplay_fast_fails = 0
        self._airplay_stuck = False
        # What has actually been pushed into the running mpv, so the poller can
        # tell a change from a repeat.
        self._mpv_caption: Optional[str] = None
        self._mpv_panel: Optional[str] = None

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

    def _airplay_command(self, cfg: config.Config) -> List[str]:
        """Run uxplay against the same display the other backends use.

        The video sink is built here rather than in `airplay.py` because it is
        the *display* half of the problem, and everything known about driving
        this display — the connector id, the DRM driver name, the fact that
        kmssink will only set a mode it was handed exactly — already lives here.
        """
        conn = display.pick_connector(cfg.connector)
        conn_name = conn.name if conn else ""
        conn_id = _connector_id(conn_name)

        sink = ["kmssink", "force-modesetting=true"]
        if conn_id is not None:
            sink.append(f"connector-id={conn_id}")
        driver = display.drm_driver_for(conn_name) if conn_name else None
        if driver:
            sink.append(f"driver-name={driver}")
        if cfg.airplay_video_sink.strip():
            sink = [cfg.airplay_video_sink.strip()]

        mode = display.target_mode(conn, cfg.video_mode)
        refresh = getattr(mode, "refresh", None) if mode else None
        return airplay.build_command(
            cfg,
            video_sink=" ".join(sink),
            width=mode.width if mode else None,
            height=mode.height if mode else None,
            refresh=int(refresh) if refresh else None,
        )

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

        cmd = self._mpv_base(cfg, playlist.image_duration if playlist else 10)
        if (playlist.loop if playlist else cfg.loop):
            cmd.append("--loop-playlist=inf")
        cmd.append("--")
        cmd += files
        return cmd

    def _web_command(self, cfg: config.Config, url: str) -> List[str]:
        """Run a kiosk browser on the display the other backends use.

        The URL is validated to http/https before it reaches argv: a value
        beginning with "-" would otherwise be read by Chromium as a flag, which
        turns "type a URL in the GUI" into "pass arbitrary switches to the
        browser".
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError("the address must start with http:// or https://")
        if not parsed.netloc:
            raise RuntimeError(f"not a usable web address: {url}")

        browser = next(
            (b for b in ("chromium-browser", "chromium", "chromium-browser-stable")
             if shutil.which(b)),
            None,
        )
        if browser is None:
            raise RuntimeError("no chromium browser installed (apt install chromium-browser)")

        conn = display.pick_connector(cfg.connector)
        mode = display.target_mode(conn, cfg.video_mode)
        cmd = [
            browser,
            "--kiosk",
            "--noerrdialogs",
            "--disable-infobars",
            "--disable-session-crashed-bubble",
            "--check-for-update-interval=31536000",
            "--autoplay-policy=no-user-gesture-required",
            f"--user-data-dir={config.STATE_DIR / 'chromium'}",
            "--ozone-platform=drm",
        ]
        if mode:
            cmd.append(f"--window-size={mode.width},{mode.height}")
        # Everything after this is a positional argument, never a switch.
        cmd.append("--")
        cmd.append(url)
        return cmd

    def _stream_command(self, cfg: config.Config, url: str) -> List[str]:
        """Play a live stream — HLS, DASH, UDP/RTP multicast, RTSP, SRT.

        mpv already speaks all of these, so this is the local-file command with
        the file swapped for a URL and the buffering changed. The buffering is
        the whole difference: a file can be re-read, a multicast packet that was
        dropped is gone. So the demuxer cache is sized in seconds of stream
        rather than left at mpv's file-oriented default, and the stream is never
        looped — there is no end to loop back from.

        The URL is validated before it reaches argv: mpv reads a leading "-" as
        an option, so an address typed into the GUI must not be able to become
        one.
        """
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() not in favourites.STREAM_SCHEMES:
            raise RuntimeError(
                f"{parsed.scheme or 'that'} is not a stream address — use one of: "
                f"{', '.join(favourites.STREAM_SCHEMES)}")
        if not parsed.netloc:
            raise RuntimeError(f"not a usable stream address: {url}")

        cmd = self._mpv_base(cfg, 10)
        cmd += [
            # Live streams have no seekable history, so a big cache buys nothing
            # but latency. These are the numbers that keep a wall responsive.
            "--cache=yes",
            f"--demuxer-readahead-secs={max(0, cfg.stream_cache_s)}",
            "--demuxer-max-bytes=64MiB",
            # Drop rather than fall behind: on a signage screen being a second
            # late for ever is worse than missing a frame once.
            "--framedrop=decoder+vo",
            # A stream that dies should be retried rather than ending playback;
            # the supervisor restarts us either way, but this rides out a blip
            # without a black frame.
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
        ]
        if cfg.stream_low_latency:
            # Trades smoothing for immediacy. Worth it for IMAG, wrong for a
            # dashboard nobody is comparing against a live stage.
            cmd += ["--profile=low-latency"]
        cmd += ["--"]
        cmd.append(url)
        return cmd

    def _mpv_base(self, cfg: config.Config, image_duration: int) -> List[str]:
        """Flags shared by single-file, whole-playlist and per-segment mpv.

        One builder on purpose: these two call sites drifted apart once
        already, and a flag that only appears on one of them is a bug that
        shows up in exactly one playback mode.
        """
        conn = display.pick_connector(cfg.connector)
        cmd = [
            "mpv",
            "--no-config",
            "--no-terminal",
            f"--input-ipc-server={mpv_socket()}",
            "--msg-level=all=warn",
            "--fullscreen",
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=auto-safe",
            "--keep-open=no",
            # A flat media folder means a stray .m4a or .srt beside a video
            # gets auto-loaded as an extra track. Play only what we asked for.
            "--audio-file-auto=no",
            "--sub-auto=no",
            # Preview frames go out over whatever Wi-Fi the GUI is reached on.
            # mpv writes a screenshot at the video's own size — there is no
            # scale option for it — so quality is the only lever on how big
            # they land, and 70 is indistinguishable in a thumbnail.
            # mpv opens the next playlist entry while the current one is
            # ending. Only helps the one-mpv path; the sequencer below reuses
            # the same process instead.
            "--prefetch-playlist=yes",
            "--screenshot-format=jpg",
            "--screenshot-jpeg-quality=70",
            f"--image-display-duration={image_duration}",
            # The identify caption, applied at birth rather than pushed in
            # afterwards. The IPC push still happens (see _sync_overlay) so a
            # caption switched on mid-item appears at once; this is what stops
            # it vanishing at the next item.
            "--osd-level=1",
            f"--osd-msg1={read_overlay()}",
            "--osd-font-size=40",
            "--osd-align-x=left",
            "--osd-align-y=top",
        ]
        if conn:
            cmd.append(f"--drm-connector={conn.name}")
        if cfg.video_mode:
            # mpv takes the mode as WxH@R via drm-mode when the connector
            # supports it; otherwise it silently keeps the preferred mode.
            cmd.append(f"--drm-mode={cfg.video_mode}")
        if cfg.rotation:
            cmd.append(f"--video-rotate={cfg.rotation}")
        cmd += self._mpv_audio_args(cfg)
        return cmd

    def _mpv_audio_args(self, cfg: config.Config) -> List[str]:
        if not cfg.audio_enabled:
            return ["--no-audio"]
        return [
            f"--volume={max(0, min(100, cfg.volume))}",
            # Straight to ALSA. mpv's default is to probe for a sound server
            # first, and a server holds its own playback buffer — so the audio
            # of an item that has been killed keeps coming out of the speakers
            # for as long as that buffer lasts, under the item that replaced
            # it. Writing to the card directly means audio stops when the
            # process does, which is what a playlist needs at every boundary.
            "--ao=alsa",
            # Bound the write-ahead for the same reason. The default is ~200ms
            # plus whatever the device buffers; this keeps the tail short.
            "--audio-buffer=0.1",
        ] + self._mpv_audio_device(cfg)

    def _mpv_audio_device(self, cfg: config.Config) -> List[str]:
        # mpv's device names are its own; if one has been chosen use it
        # verbatim, otherwise derive an alsa/ name from the ALSA device.
        if cfg.audio_device_mpv:
            return [f"--audio-device={cfg.audio_device_mpv}"]
        if cfg.audio_device:
            return [f"--audio-device=alsa/{cfg.audio_device}"]
        return []

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
            "audio_sync": cfg.audio_sync,
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
            # Always wired up; it captures nothing until somebody is watching,
            # and building it lazily would mean a pipeline restart — a black
            # frame — the first time anyone opened the preview.
            "preview_path": str(preview.frame_path()),
            "preview_rate_path": str(preview.rate_path()),
            "preview_width": preview.WIDTH,
            "overlay_file": str(overlay_path()),
            "image_overlay_file": str(guest.overlay_png_path()),
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
                    # A standby video is local playback that happens to be the
                    # fallback. Build it directly rather than going through
                    # _local_command: that honours cfg.local_playlist over its
                    # argument, so with a playlist selected the standby screen
                    # would quietly play the whole playlist instead.
                    return self._mpv_base(cfg, 10) + [
                        "--loop-file=inf", "--", str(path),
                    ]
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
            # Standby carries the caption too: "identify" has to work on a node
            # that is not playing anything, which is exactly when you are
            # hunting for which box is which.
            "overlay_file": str(overlay_path()),
            "image_overlay_file": str(guest.overlay_png_path()),
        }
        return [sys.executable, "-m", "pistreamer.runner", json.dumps(spec)]

    def _segment_command(self, cfg: config.Config, segment: dict) -> List[str]:
        """The command for a single playlist segment."""
        if segment["type"] == "ndi":
            return self._runner_command(cfg, segment["target"])
        cmd = self._mpv_base(cfg, segment["duration"] or 10)
        # Stay alive when the file ends instead of exiting, so the next segment
        # is a loadfile into this process rather than a new one. Spawning mpv
        # and handing it DRM is what the black frame between items actually
        # was; decoding the first frame is the cheap part.
        # A duration is enforced by _segment_deadline rather than --length,
        # because --length is a launch option and cannot be set per loadfile.
        cmd += ["--idle=yes", "--", segment["path"]]
        return cmd

    def _load_into_running_mpv(self, segment: dict) -> bool:
        """Hand the next file to the mpv already on screen. False if it cannot.

        Only for file segments following a file segment. NDI needs the
        GStreamer runner, so a playlist that mixes the two still swaps
        processes at those boundaries — that is a different backend, not
        avoidable churn.
        """
        if segment["type"] != "file" or self._segment_backend != "mpv":
            return False
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        sock = str(mpv_socket())
        # Set before the load so a still is held for its own time rather than
        # the previous segment's.
        mpvipc.command(sock, "set_property", "image-display-duration",
                       segment["duration"] or 10)
        reply = mpvipc.command(sock, "loadfile", segment["path"], "replace")
        return bool(reply) and reply.get("error") == "success"

    def _start_segment(self, cfg: config.Config, idx: int) -> None:
        """Play segment `idx`, wrapping round if the playlist loops."""
        if not self._segments:
            raise RuntimeError("playlist has no playable segments")
        if idx >= len(self._segments):
            if not self._segment_loop:
                log.info("playlist finished; going to standby")
                self._segments = []
                self.apply(MODE_IDLE)
                return
            idx = 0
        self._segment_idx = idx
        segment = self._segments[idx]
        label = segment["target"] or segment.get("path", "")
        log.info(
            "playlist segment %d/%d: %s %s",
            idx + 1, len(self._segments), segment["type"], label,
        )
        self._status.target = f"{label} ({idx + 1}/{len(self._segments)})"
        if not self._load_into_running_mpv(segment):
            self._terminate()
            self._spawn_command(self._segment_command(cfg, segment),
                                f"segment {idx + 1}")
            self._segment_backend = "mpv" if segment["type"] == "file" else "ndi"
        # An NDI segment never ends by itself, and a duration is now enforced
        # here rather than by --length, so anything with one gets a deadline.
        # A little slack keeps us from cutting a file off early.
        if segment["type"] == "ndi" or segment["image"] or segment["duration"]:
            self._segment_deadline = time.monotonic() + (segment["duration"] or 30)
        else:
            self._segment_deadline = None

    def _build_command(self, cfg: config.Config, mode: str, target: str) -> List[str]:
        # Every backend here drives DRM, and a connector with no modes has
        # nothing to set. Without this the failure is "failed to set pipeline
        # to PLAYING" on a backoff loop for ever, which says nothing about the
        # cable being out.
        conn = display.pick_connector(cfg.connector)
        if conn is None or not conn.modes:
            raise RuntimeError(
                f"{conn.name if conn else 'the display'} reports no modes — "
                f"nothing is plugged in. Either connect a display, or run "
                f"headless by adding video=HDMI-A-1:1920x1080@60e to "
                f"/boot/firmware/cmdline.txt and rebooting."
            )
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
        if mode == MODE_WEB:
            if not target:
                raise RuntimeError("no web address given")
            return self._web_command(cfg, target)
        if mode == MODE_STREAM:
            if not target:
                raise RuntimeError("no stream address given")
            if not shutil.which("mpv"):
                raise RuntimeError("mpv not installed")
            return self._stream_command(cfg, target)
        if mode == MODE_AIRPLAY:
            # Checked before spawning, not after: with no Avahi, uxplay prints
            # one line and exits, and a supervisor reads that as a crash worth
            # retrying every few seconds for the rest of the evening.
            ok, reason = airplay.available()
            if not ok:
                raise RuntimeError(reason)
            return self._airplay_command(cfg)
        raise RuntimeError(f"mode {mode!r} has no command")

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def _note_line(self, line: str) -> None:
        """One line of child output: into the log, and past the AirPlay reader.

        uxplay writes its status to *stdout* and its errors to stderr, and the
        two things an operator most needs — the pairing PIN and who just
        connected — arrive on stdout. Both drains come through here so it does
        not matter which stream a message chose.
        """
        self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        if self._status.mode == MODE_AIRPLAY:
            airplay.observe(line)

    def _drain_output(self, proc: subprocess.Popen) -> None:
        """Pump the child's stderr into the ring buffer for the GUI log view."""
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in stream:
                line = raw.rstrip("\n")
                if line:
                    self._note_line(line)
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
                    self._note_line(line)
        except (OSError, ValueError):
            pass

    def _sync_overlay(self, sock: str) -> None:
        """Push the caption and the guest panel into a running mpv.

        Both are files on disk that some other part of the system rewrites, so
        this compares rather than assumes — an unconditional set every second
        would re-render the OSD and re-upload the panel forever.
        """
        caption = read_overlay()
        if caption != self._mpv_caption:
            if _mpv_ok(mpvipc.command(sock, "set_property", "osd-msg1", caption)):
                self._mpv_caption = caption

        meta = guest.overlay_meta() if guest.overlay_png_path().exists() else None
        want = meta.get("url") if meta else None
        if want == self._mpv_panel:
            return
        if not meta:
            mpvipc.command(sock, "overlay-remove", GUEST_OVERLAY_ID)
            self._mpv_panel = None
            return
        if not Path(str(meta.get("bgra", ""))).exists():
            return  # half-written; the next tick will find it
        # mpv places an overlay by its top-left corner in screen pixels, so the
        # bottom-right position the GStreamer side gets for free has to be
        # worked out here — and it needs mpv's own idea of the screen, not the
        # configured mode, because a connector can end up on a different one.
        w = _mpv_data(mpvipc.command(sock, "get_property", "osd-width"))
        h = _mpv_data(mpvipc.command(sock, "get_property", "osd-height"))
        if not isinstance(w, int) or not isinstance(h, int) or w <= 0 or h <= 0:
            return  # not laid out yet; try again on the next tick
        pad = OVERLAY_PAD
        x = max(0, w - int(meta["width"]) - pad)
        y = max(0, h - int(meta["height"]) - pad)
        if _mpv_ok(mpvipc.command(
                sock, "overlay-add", GUEST_OVERLAY_ID, x, y, str(meta["bgra"]),
                0, "bgra", int(meta["width"]), int(meta["height"]),
                int(meta["stride"]))):
            self._mpv_panel = want
            log.info("guest QR panel placed on mpv at %d,%d", x, y)

    def _poll_mpv(self, proc: subprocess.Popen) -> None:
        """Fill stream stats from mpv while it runs.

        mpv creates its IPC socket a moment after starting, so the first few
        attempts are expected to fail; we simply keep trying until the process
        goes away.
        """
        sock = str(mpv_socket())
        self._mpv_caption = None
        self._mpv_panel = None
        while proc.poll() is None and not self._stop_event.is_set():
            self._sync_overlay(sock)
            stats = mpvipc.to_stats(mpvipc.query(sock))
            if stats:
                stats["t"] = time.time()
                with self._lock:
                    self._stream_stats = stats
            if self._stop_event.wait(1.0):
                break

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
        # Never start a second player over a live one. Every caller is supposed
        # to have torn the old one down already; if one ever forgets, the
        # symptom is two soundtracks at once and a DRM fight, so make the
        # invariant hold here rather than trusting call sites to remember.
        if self._proc is not None and self._proc.poll() is None:
            log.warning("spawn requested while pid %s is alive; terminating it first",
                        self._proc.pid)
            self._terminate()

        log.info("starting %s: %s", what, " ".join(shlex.quote(c) for c in cmd))
        self._logs.append(f"{time.strftime('%H:%M:%S')} $ {' '.join(shlex.quote(c) for c in cmd)}")

        if cmd and cmd[0] == "mpv":
            # Before the spawn, not after: mpv creates this socket at startup,
            # so unlinking afterwards deletes the socket of the process we just
            # started and the stats polling silently finds nothing.
            try:
                mpv_socket().unlink(missing_ok=True)
            except OSError:
                pass

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
        if cmd and cmd[0] == "mpv":
            threading.Thread(
                target=self._poll_mpv, args=(proc,), name="mpv-stats", daemon=True
            ).start()

    def _terminate(self, timeout: float = 5.0) -> None:
        """Stop the current process and wait for it to release display and audio.

        Waiting on the direct child is not enough. `proc.wait()` returns as soon
        as *our* child is reaped, but the child leads a process group, and
        anything else in that group keeps its ALSA device open — which is heard
        as the previous item still playing under the next one. So wait for the
        whole group to disappear, then sweep for strays.
        """
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            pgid = proc.pid  # start_new_session=True, so pgid == pid
            try:
                os.killpg(pgid, signal.SIGTERM)
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
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    log.error("player pid %s would not die", proc.pid)

            # The child is reaped; make sure the rest of its group has gone.
            deadline = time.monotonic() + 2.0
            while _group_alive(pgid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if _group_alive(pgid):
                log.warning("process group %s outlived its leader; killing", pgid)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass

        # Belt and braces: anything of ours still playing is unsupervised.
        strays = reap_strays()
        if strays:
            self._logs.append(
                f"{time.strftime('%H:%M:%S')} ! stopped {len(strays)} stray "
                f"player process(es) — these would have played over the next item"
            )
            self._stray_kills += len(strays)
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
        # An unclean stop (crash, SIGKILL, a player launched by hand over SSH
        # while debugging) leaves a process still holding the display and the
        # sound card. Nothing else will ever clean it up, and it would play
        # underneath everything we start from here on.
        strays = reap_strays()
        if strays:
            self._stray_kills += len(strays)
            self._logs.append(
                f"{time.strftime('%H:%M:%S')} ! cleaned up {len(strays)} "
                f"player process(es) left over from a previous run"
            )
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
            self._segments = []
            self._segment_deadline = None
            # A new mode means the old AirPlay session is over — including when
            # the new mode is AirPlay again, since restarting the receiver
            # drops whoever was mirroring.
            airplay.reset()
            self._airplay_fast_fails = 0
            self._airplay_stuck = False
            # Any explicit mode change ends a synchronised session: the operator
            # has taken local control of this node. Remember *which* session, so
            # this node refuses the rest of it but still joins the next one.
            if self._sync_active and self._sync_session:
                self._sync_declined = self._sync_session
            self._sync_active = False
            self._nudging_until = 0.0
            self._correcting_since = 0.0
            try:
                if mode == MODE_LOCAL and cfg_playlist_needs_sequencer():
                    cfg = config.load()
                    self._segments = playlists.resolved_segments(cfg.local_playlist)
                    if not self._segments:
                        raise RuntimeError(
                            f"playlist {cfg.local_playlist!r} has no playable segments"
                        )
                    playlist = playlists.get(cfg.local_playlist)
                    self._segment_loop = playlist.loop if playlist else True
                    self._start_segment(cfg, 0)
                    return
            except Exception as exc:  # noqa: BLE001
                self._status.last_error = str(exc)
                self._status.running = False
                log.error("failed to start playlist: %s", exc)
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
            self._status.fallback = self._fallback
            self._status.strays_cleaned = self._stray_kills
            return self._status.to_dict()

    def logs(self) -> List[str]:
        return list(self._logs)

    # ------------------------------------------------------------------
    # Identify
    # ------------------------------------------------------------------

    def set_identify(self, on: bool, text: str = "") -> Dict[str, Any]:
        """Show or hide the node's name and address over whatever is playing.

        Both backends have to be told, in their own idiom, and neither may be
        restarted to do it — identify is used to find a node *during* a show.
        The GStreamer runner polls a file; mpv is told over its IPC socket.
        """
        caption = text if on else ""
        write_overlay(caption)
        # The file is the source of truth; both backends poll it, and mpv is
        # also born with it. This push is only so a caption switched on during
        # an item appears now rather than within the second.
        self._mpv_caption = None
        applied = {"overlay_file": True, "mpv": False}
        if _mpv_ok(mpvipc.command(str(mpv_socket()), "set_property",
                                  "osd-msg1", caption)):
            applied["mpv"] = True
            self._mpv_caption = caption
            # The default OSD font is sized for a desktop window; on a screen
            # being read from across a room the caption needs to be larger, and
            # top-left keeps it clear of lower-third graphics in the content.
            mpvipc.command(str(mpv_socket()), "set_property", "osd-font-size", 40)
            mpvipc.command(str(mpv_socket()), "set_property", "osd-align-x", "left")
            mpvipc.command(str(mpv_socket()), "set_property", "osd-align-y", "top")
        return applied

    # ------------------------------------------------------------------
    # Synchronised playback
    # ------------------------------------------------------------------

    def prepare(self, filename: str, duration: Optional[int] = None,
                image: bool = False, session: str = "") -> Dict[str, Any]:
        """Load a file and hold on its first frame, paused, ready to be released.

        This is half of an aligned start. Spawning a player costs tens to
        hundreds of milliseconds and differs per node and per file; un-pausing
        one that has already decoded its first frame costs about a frame. So the
        expensive, variable part happens before the beat and only the cheap part
        happens on it.
        """
        with self._lock:
            # Somebody stopped this node by hand during this session. Stay
            # stopped: being pulled back into playing at every item boundary is
            # indistinguishable from stop not working. A *new* session is a
            # fresh instruction and is honoured.
            if session and session == self._sync_declined:
                return {"ready": False, "file": filename,
                        "reason": "stopped locally; will rejoin the next session"}
        path = media.resolve(filename)
        if path is None:
            raise RuntimeError(f"not in the media library: {filename}")
        cfg = config.load()
        with self._lock:
            self._wanted_mode = MODE_LOCAL
            self._wanted_target = filename
            self._segments = []
            self._segment_deadline = None
            self._fallback = False
            self._sync_active = True
            self._sync_session = session
            # Each item is its own correction problem: carrying the previous
            # one's clock over would escalate to a seek moments into a file
            # that has not had a chance to drift yet.
            self._nudging_until = 0.0
            self._correcting_since = 0.0
            # A still image has no playhead and no natural end, so the conductor
            # decides when it comes down — every node at the same instant.
            # Letting each node time out on its own dwell counter instead means
            # they blink off at slightly different moments.
            hold = "inf" if image else str(duration or 10)
            cmd = [c for c in self._mpv_base(cfg, 10)
                   if not c.startswith("--image-display-duration=")]
            cmd += [f"--image-display-duration={hold}"]
            if duration and not image:
                cmd.append(f"--length={duration}")
            cmd += [
                # Hold the first frame rather than the last: mpv paused at
                # position 0 with the frame decoded is exactly the state we
                # want to release on the beat.
                "--pause=yes",
                "--", str(path),
            ]
            self._status.mode = MODE_LOCAL
            self._status.target = filename
            self._spawn_command(cmd, f"prepare {filename}")
        ready = self._await_ready()
        return {"ready": ready, "file": filename}

    def _await_ready(self, timeout: float = 8.0) -> bool:
        """Wait until mpv has the file open and is genuinely holding a frame.

        Asking for a real property rather than sleeping: a fixed sleep is either
        too short on a cold SD card or wasted time on a warm one, and the whole
        point of preparing is to take the variance out of the start.
        """
        deadline = time.monotonic() + timeout
        sock = str(mpv_socket())
        while time.monotonic() < deadline:
            reply = mpvipc.command(sock, "get_property", "seekable")
            if reply.get("error") == "success":
                return True
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def start_at(self, at: float) -> Dict[str, Any]:
        """Release a prepared file at an instant on *this node's* clock.

        The leader converts the agreed instant into each follower's clock before
        sending it, so there is no clock arithmetic here — which is deliberate:
        every node doing its own conversion is how two nodes end up disagreeing
        about which of them is wrong.
        """
        delay = at - time.time()
        if delay > 30:
            raise RuntimeError(f"start time is {delay:.0f}s away; refusing")
        thread = threading.Thread(
            target=self._release_at, args=(at,), name="sync-start", daemon=True
        )
        thread.start()
        return {"scheduled": True, "in_ms": round(max(0.0, delay) * 1000, 1)}

    def _release_at(self, at: float) -> None:
        # Sleep most of the way, then spin for the last few milliseconds.
        # time.sleep() alone is only accurate to the scheduler's granularity,
        # and on a start that is meant to be frame-accurate that granularity is
        # the whole error budget.
        remaining = at - time.time()
        if remaining > 0.05:
            time.sleep(remaining - 0.05)
        while time.time() < at:
            time.sleep(0.0005)
        sock = str(mpv_socket())
        mpvipc.command(sock, "set_property", "pause", False)
        self._logs.append(
            f"{time.strftime('%H:%M:%S')} · released on the beat "
            f"({(time.time() - at) * 1000:+.1f}ms)"
        )

    def capture_preview(self) -> bool:
        """Take one preview frame from an mpv-backed source.

        The GStreamer runner captures on its own, from a branch of the pipeline
        it is already running. mpv has no such branch, but it will take a
        screenshot on request over the IPC socket that is already open for
        seeking and speed nudges, so a preview costs one round trip and only
        when a frame is actually wanted.

        `video` rather than the default: it captures the decoded frame without
        subtitles or OSD, so the identify caption and the guest QR panel do not
        end up baked into the preview.
        """
        if self._status.mode not in (MODE_LOCAL, MODE_STREAM):
            return False
        try:
            result = mpvipc.command(
                str(mpv_socket()), "screenshot-to-file",
                str(preview.frame_path()), "video", timeout=3.0)
        except Exception as exc:  # noqa: BLE001 - a missed frame is not news
            log.debug("preview capture failed: %s", exc)
            return False
        return bool(result) and result.get("error") in (None, "success")

    def sync_position(self) -> Optional[float]:
        """This node's playhead, asked for fresh rather than from the cache."""
        return mpvipc.position(str(mpv_socket()))

    def apply_pulse(self, pulse: Dict[str, Any]) -> Dict[str, Any]:
        """Act on the leader's position report: hold, nudge or seek."""
        own = self.sync_position()
        now = time.time()
        strength = syncplay.profile(config.load().cluster_sync_strength)
        with self._lock:
            nudging_until = self._nudging_until
            correcting_since = self._correcting_since
        decision = syncplay.decide(pulse, own, now, nudging_until, strength,
                                   correcting_since)
        sock = str(mpv_socket())
        if decision.action == "seek" and decision.seek_to is not None:
            # exact, not keyframe: a keyframe seek can land a second away, which
            # would be a correction that causes the very problem it is fixing.
            mpvipc.command(sock, "seek", decision.seek_to, "absolute+exact")
            mpvipc.command(sock, "set_property", "speed", 1.0)
            with self._lock:
                self._nudging_until = 0.0
                # A seek lands us on the leader, so the correction is over —
                # and the escalation clock has to be cleared with it or the
                # next pulse escalates again immediately.
                self._correcting_since = 0.0
        elif decision.action == "nudge":
            mpvipc.command(sock, "set_property", "speed", decision.speed)
            with self._lock:
                self._nudging_until = now + strength.hold_s
                self._correcting_since = self._correcting_since or now
        elif decision.reason == "in sync":
            mpvipc.command(sock, "set_property", "speed", 1.0)
            with self._lock:
                self._nudging_until = 0.0
                self._correcting_since = 0.0
        elif decision.reason == "nudge in progress":
            # Still out, still riding a nudge: the clock keeps running. This is
            # the branch that lets "correcting forever" be noticed at all.
            with self._lock:
                self._correcting_since = self._correcting_since or now
        out = decision.to_dict()
        out["position"] = own
        out["strength"] = strength.name
        with self._lock:
            out["correcting_for"] = (round(now - self._correcting_since, 1)
                                     if self._correcting_since else 0.0)
        return out

    # ------------------------------------------------------------------
    # Supervisor
    # ------------------------------------------------------------------

    def _airplay_exit_reason(self, proc: subprocess.Popen) -> str:
        """Did this receiver die of its configuration rather than bad luck?

        `player exited with code -5` is true and useless. uxplay puts the real
        reason on its own output, which we have already read, so hand that to
        the operator — and after a few of these, stop restarting. A typo does
        not get better by being retried every second for the rest of the show.
        """
        ran_for = time.time() - (self._status.since or time.time())
        if ran_for >= _AIRPLAY_FAST_FAIL:
            self._airplay_fast_fails = 0
            return ""
        self._airplay_fast_fails += 1
        if self._airplay_fast_fails < _AIRPLAY_FAST_FAIL_LIMIT:
            return ""
        self._airplay_stuck = True
        detail = airplay.session().last_error
        if not detail:
            tail = [l for l in list(self._logs)[-8:] if "ERROR" in l or "no element" in l]
            detail = tail[-1][-160:] if tail else f"it exited with {proc.returncode}"
        return (f"the AirPlay receiver would not stay up ({detail}). "
                "Fix that and press Start receiving again.")

    def _check_airplay_startup(self) -> None:
        """A receiver that is up but not listening is not working.

        uxplay can start, print its banner, and then wedge while building its
        video pipeline — which is what happens if the sink cannot reach a
        display. The process is alive, so every liveness check says fine, and
        the operator sees a green light and a black screen. Say so instead.
        """
        since = self._status.since
        if not since or (time.time() - since) < _AIRPLAY_LISTEN_TIMEOUT:
            return
        if airplay.session().listening or self._status.last_error:
            return
        tail = " / ".join(list(self._logs)[-2:])
        self._status.last_error = (
            "the AirPlay receiver started but never began advertising itself "
            f"after {int(_AIRPLAY_LISTEN_TIMEOUT)}s — it is usually the video "
            f"output that is wrong. Last output: {tail[-200:]}"
        )
        log.error("%s", self._status.last_error)

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
            if self._stop_event.wait(_TICK):
                break
            try:
                self._supervise()
            except Exception:  # noqa: BLE001 - a supervisor must never die
                log.exception("supervisor tick failed")

    def _publish_guest_overlay(self, cfg: config.Config) -> None:
        """Keep the on-screen QR panel in step with the guest session.

        Done from the supervisor rather than from the API endpoints because the
        session closes itself on a timer. If drawing and clearing the panel only
        happened when somebody pressed something, an expired session would leave
        a dead QR code on the screen for the rest of the evening — a code that
        still looks like an invitation and no longer works.
        """
        try:
            conn = display.pick_connector(cfg.connector)
            mode_ = display.target_mode(conn, cfg.video_mode)
            guest.publish_overlay(port=cfg.web_port,
                                  screen_h=mode_.height if mode_ else 1080)
        except Exception as exc:  # noqa: BLE001 - never take the supervisor down
            log.debug("could not publish the guest overlay: %s", exc)

    def _supervise(self) -> None:
        with self._lock:
            mode, target = self._wanted_mode, self._wanted_target
            cfg = config.load()
            self._publish_guest_overlay(cfg)
            proc = self._proc
            alive = proc is not None and proc.poll() is None

            if alive:
                # A timed segment ends on the clock, not on process exit.
                if self._segments and self._segment_deadline is not None:
                    if time.monotonic() >= self._segment_deadline:
                        self._start_segment(cfg, self._segment_idx + 1)
                        return
                # A segment of natural length used to end by the process
                # exiting. A reused mpv goes idle instead, so that is now what
                # "this item finished" looks like.
                if (self._segments and self._segment_deadline is None
                        and self._segment_backend == "mpv"):
                    idle = mpvipc.query(str(mpv_socket()), ["idle-active"])
                    if idle.get("idle-active") is True:
                        self._start_segment(cfg, self._segment_idx + 1)
                        return
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
                if mode == MODE_AIRPLAY:
                    if airplay.restart_wanted():
                        # Something the receiver cannot recover from in place —
                        # in practice, the GPU decoder failing on a live stream.
                        # Restarted here rather than from the thread reading its
                        # output, because that thread belongs to the process we
                        # are about to kill.
                        log.info("restarting the AirPlay receiver "
                                 "(software decoding)")
                        note = airplay.session().last_error
                        self._terminate()
                        airplay.reset(keep_degrade=True)
                        try:
                            self._spawn(cfg, mode, target)
                            self._status.last_error = note
                        except Exception as exc:  # noqa: BLE001
                            self._status.last_error = str(exc)
                        return
                    self._check_airplay_startup()
                return

            # Whatever was running has exited.
            if proc is not None:
                self._status.last_error = (
                    f"standby screen exited with code {proc.returncode}"
                    if self._fallback
                    else f"player exited with code {proc.returncode}"
                )
                if mode == MODE_AIRPLAY and not self._fallback:
                    reason = self._airplay_exit_reason(proc)
                    if reason:
                        self._status.last_error = reason
                        self._status.running = False
                        self._proc = None
                        return
            self._status.running = False
            self._proc = None

            if mode == MODE_AIRPLAY and self._airplay_stuck:
                return

            if mode == MODE_IDLE:
                # The standby screen is the point of idle; bring it back.
                self._next_attempt = time.monotonic() + self._backoff_delay()
                try:
                    self._spawn(cfg, mode, target)
                except Exception as exc:  # noqa: BLE001
                    self._status.last_error = str(exc)
                return

            # In playlist mode a process exiting means the segment finished.
            if self._segments:
                if proc is not None and proc.returncode not in (0, None):
                    log.warning(
                        "segment %d exited with %s; advancing anyway",
                        self._segment_idx + 1, proc.returncode,
                    )
                try:
                    self._start_segment(cfg, self._segment_idx + 1)
                except Exception as exc:  # noqa: BLE001
                    self._status.last_error = str(exc)
                    log.error("could not advance the playlist: %s", exc)
                return

            # A synchronised item that has played out is not a crash. The
            # leader decides what comes next, so restarting here would race it
            # and play the previous item again underneath the new one.
            if self._sync_active:
                self._status.last_error = ""
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


def cfg_playlist_needs_sequencer() -> bool:
    """Does the configured playlist need segment-by-segment playback?

    Kept as a helper so the decision — and the reason for it — lives in one
    place: mpv can play a list of files smoothly, but it knows nothing about
    NDI and cannot cut a still short per item.
    """
    cfg = config.load()
    if not cfg.local_playlist:
        return False
    playlist = playlists.get(cfg.local_playlist)
    return bool(playlist and playlist.needs_sequencer())


# Module-level singleton the web layer talks to.
player = Player()
