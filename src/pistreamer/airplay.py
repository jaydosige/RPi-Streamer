"""AirPlay receiving: an iPhone, iPad or Mac mirroring onto the node.

The receiver is `uxplay`, run as a supervised subprocess like every other
playback backend. That is not an accident of convenience — an AirPlay session
takes the display, and this project has exactly one rule about the display:
one process owns it at a time. Making AirPlay a *mode* rather than a background
service means the existing state machine already handles taking the screen off
NDI, giving it back, and cleaning up if the receiver wedges.

What this module owns is the difference between what uxplay is and what an
operator needs:

  * **uxplay expects a terminal.** It prints the pairing PIN to stdout and
    assumes somebody is looking at it. On a headless node nobody is, so the
    PIN feature is unusable unless something reads the output and puts it on
    the GUI. That is `observe()`.
  * **uxplay expects a home directory.** It stores its keypair in
    `$HOME/.uxplay.pem`. Our unit sets `ProtectHome=yes`, so `$HOME` is not
    there — the same shape of trap as `NoNewPrivileges` and sudo. Paths are
    passed explicitly instead.
  * **uxplay expects a DNS-SD server.** With no Avahi it prints one error and
    exits, which to a supervisor looks like a crash worth retrying forever.
    Better to check first and say what is wrong.

None of this parsing is load-bearing for playback: if a log line changes
wording in a future uxplay, mirroring still works and the GUI just knows less.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

# uxplay writes these into $HOME by default. Ours live with the rest of the
# node's state so they survive a restart and exist at all under ProtectHome.
KEY_FILE = "uxplay.pem"
REGISTER_FILE = "uxplay.register"

# Sockets Avahi listens on. Checked directly rather than by asking systemd,
# because the answer we actually want is "can uxplay register a service", and
# `systemctl is-active` is a proxy for that which is wrong inside a container
# and on any host where the daemon is socket-activated.
_AVAHI_SOCKETS = ("/var/run/avahi-daemon/socket", "/run/avahi-daemon/socket")


# ----------------------------------------------------------------------
# Availability
# ----------------------------------------------------------------------


def binary() -> Optional[str]:
    return shutil.which("uxplay")


def version() -> str:
    """The uxplay version string, or "" if it cannot be determined."""
    exe = binary()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "-v"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (out.stdout or "") + (out.stderr or "")
    m = re.search(r"UxPlay(?:\s+version)?\s+v?([0-9][0-9.]*)", text)
    return m.group(1) if m else ""


_element_cache: Dict[str, bool] = {}


def element_available(name: str) -> bool:
    """Does this GStreamer element exist on this box?

    Asked before putting an element on uxplay's command line, because uxplay
    validates the sink and decoder strings at startup and *aborts* on a bad one
    — SIGTRAP, in about 40 ms, with the reason on stderr and nothing in the
    exit code but -5. A supervisor left to itself simply restarts that forever.

    The specific case this exists for: `v4l2h264dec` is the Pi's GPU h264
    decoder and is on by default, but it is not there if the bcm2835-codec
    module has not loaded, and it is not there at all on a box that is not a
    Pi. Falling back to software is right; crash-looping is not.
    """
    if name in _element_cache:
        return _element_cache[name]
    exe = shutil.which("gst-inspect-1.0")
    ok = False
    if exe:
        try:
            ok = subprocess.run([exe, name], capture_output=True,
                                timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    _element_cache[name] = ok
    if not ok:
        log.info("GStreamer element %s is not available on this box", name)
    return ok


def dns_sd_available() -> Tuple[bool, str]:
    """Can a service be advertised on the LAN?

    Without this, uxplay prints `No DNS-SD Server found` and exits — and a
    supervisor reads that as a crash and retries it on a backoff for the rest
    of the evening. Checking first turns a mystery into a sentence.
    """
    for path in _AVAHI_SOCKETS:
        if os.path.exists(path):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(path)
                s.close()
                return True, ""
            except OSError:
                return False, ("Avahi is installed but not answering. "
                               "Try: sudo systemctl restart avahi-daemon")
    return False, ("AirPlay needs Avahi to advertise the node on the network. "
                   "Install it with: sudo apt install avahi-daemon")


def available() -> Tuple[bool, str]:
    """Can this box receive AirPlay at all?"""
    if not binary():
        return False, ("uxplay is not installed. Install it with: "
                       "sudo apt install uxplay avahi-daemon")
    return dns_sd_available()


def capabilities() -> Dict[str, Any]:
    ok, reason = available()
    return {
        "available": ok,
        "reason": reason,
        "binary": binary() or "",
        "version": version(),
        "dns_sd": dns_sd_available()[0],
        "software_decode": software_forced(),
    }


# ----------------------------------------------------------------------
# Session state, read out of uxplay's own output
# ----------------------------------------------------------------------


@dataclass
class Session:
    """What the operator needs to know about the AirPlay receiver right now."""

    # Advertising and waiting for someone to pick us.
    listening: bool = False
    # A device is actually mirroring: the display is theirs.
    mirroring: bool = False
    client: str = ""       # the device's name, as it announced itself
    model: str = ""        # e.g. "iPhone14,5"
    device_id: str = ""
    # The pairing code uxplay would otherwise have printed to a terminal that
    # does not exist on this node.
    pin: str = ""
    since: Optional[float] = None
    last_error: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "listening": self.listening,
            "mirroring": self.mirroring,
            "client": self.client,
            "model": self.model,
            "device_id": self.device_id,
            "pin": self.pin,
            "since": self.since,
            "connected_for": round(time.time() - self.since, 1) if self.since else None,
            "last_error": self.last_error,
        }


_lock = threading.Lock()
_session = Session()
# Set when the GPU decoder has been proved not to work *on this stream*, which
# only happens once a phone actually connects. Startup tells you nothing.
_force_software = False
_restart_wanted = False


def reset(keep_degrade: bool = False) -> None:
    """Forget everything. Called when the receiver is (re)started or stopped."""
    global _session, _force_software, _restart_wanted
    with _lock:
        _session = Session()
        _restart_wanted = False
        if not keep_degrade:
            _force_software = False


def software_forced() -> bool:
    with _lock:
        return _force_software


def restart_wanted() -> bool:
    """Has something happened that only a restart of the receiver can fix?"""
    global _restart_wanted
    with _lock:
        wanted = _restart_wanted
        _restart_wanted = False
        return wanted


def session() -> Session:
    with _lock:
        return _session


def summary() -> Dict[str, Any]:
    return session().to_dict()


# Matched against uxplay's own messages. Deliberately loose: substring and
# tolerant groups rather than a strict reproduction of its format strings, so
# a reworded message costs us a field rather than the feature.
_RE_REQUEST = re.compile(
    r"connection request from\s+(?P<name>.*?)\s*\((?P<model>[^)]*)\)"
    r"(?:\s+with deviceID\s*=\s*(?P<id>\S+))?", re.I)
_RE_PIN = re.compile(r'ENTER PIN\s*=\s*"?(?P<pin>[0-9]{4})"?', re.I)
_RE_UA = re.compile(r"Client identified as User-Agent:\s*(?P<ua>.+)", re.I)


def observe(line: str) -> Optional[str]:
    """Fold one line of uxplay output into the session state.

    Returns a short event name when something happened worth logging, or None.
    Never raises: this runs on the process's output-draining thread, and a
    thread that dies there takes the GUI's log with it.
    """
    try:
        return _observe(line)
    except Exception:  # noqa: BLE001
        return None


def _observe(line: str) -> Optional[str]:
    global _session
    text = line.strip()
    if not text:
        return None
    with _lock:
        s = _session
        low = text.lower()

        if "initialized server socket" in low or "advertised airplay service" in low:
            s.listening = True
            return "listening"

        m = _RE_PIN.search(text)
        if m:
            s.pin = m.group("pin")
            log.info("AirPlay pairing PIN is %s", s.pin)
            return "pin"

        m = _RE_REQUEST.search(text)
        if m:
            s.client = (m.group("name") or "").strip()[:60]
            s.model = (m.group("model") or "").strip()[:40]
            s.device_id = (m.group("id") or "").strip()[:40]
            s.last_error = ""
            return "request"

        m = _RE_UA.search(text)
        if m and not s.client:
            s.client = m.group("ua").strip()[:60]
            return "client"

        if "mirroring initialized successfully" in low:
            s.mirroring = True
            s.since = s.since or time.time()
            return "mirroring"

        # uxplay says "Stopping..." on its own shutdown as well as at the end
        # of a session, so the end of mirroring is inferred from the messages
        # that only appear when a client goes away.
        if ("client stopped mirroring" in low
                or "connection closed for socket" in low
                or "lost connection with client" in low
                or "no-response limit" in low):
            if s.mirroring:
                log.info("AirPlay client disconnected")
            s.mirroring = False
            s.client = ""
            s.model = ""
            s.since = None
            return "disconnected"

        if "denied" in low and "blocked client" in low:
            s.last_error = "a blocked device tried to connect"
            return "denied"
        if "authentication failure" in low:
            s.last_error = "the device gave the wrong PIN"
            return "auth-failed"
        if "no dns-sd server" in low:
            s.last_error = ("Avahi is not running, so the node cannot advertise "
                            "itself. Try: sudo systemctl restart avahi-daemon")
            return "no-dnssd"
        if "nameconflict" in low.replace("_", "").replace(" ", ""):
            s.last_error = ("another device on this network is already using "
                            "this AirPlay name")
            return "name-conflict"
        # The one that matters in the field. Everything is healthy until a
        # phone connects; then the decoder rejects Apple's stream and the
        # pipeline collapses at the first frame. uxplay prints its own advice
        # about this, which is the tell.
        if ("unable to construct a working video pipeline" in low
                or "internal data stream error" in low):
            global _force_software, _restart_wanted
            if not _force_software:
                _force_software = True
                _restart_wanted = True
                s.last_error = ("the GPU decoder could not play that stream, so "
                                "the receiver has switched to software decoding "
                                "and restarted. Ask the device to connect again.")
                log.warning("AirPlay: hardware decoding failed on a live "
                            "stream; falling back to software")
                return "degrade-to-software"
            s.last_error = ("the video pipeline failed even in software — see "
                            "the log")
            return "video-pipeline-failed"

        if "failed to initialize gstreamer video renderer" in low:
            # uxplay says this and then sits there, alive and useless. Catch it
            # here so the GUI has the reason immediately rather than waiting for
            # the supervisor to notice nothing is being advertised.
            s.last_error = ("the video output could not be opened — check the "
                            "display connector and that nothing else has the "
                            "screen")
            return "video-failed"
        m = re.search(r'no element "([^"]+)"', text)
        if m:
            s.last_error = (
                f"the video pipeline asked for '{m.group(1)}', which this box "
                "does not have. Turn off hardware decoding, or check the "
                "GStreamer plugins are installed.")
            return "missing-element"
        if "get_parse_launch error" in low:
            s.last_error = s.last_error or (
                "the video or audio output could not be built — see the log")
            return "parse-error"
        if "gstreamer error" in low:
            s.last_error = text[-160:]
            return "gst-error"
        return None


# ----------------------------------------------------------------------
# Building the command
# ----------------------------------------------------------------------


def key_path() -> Path:
    return config.STATE_DIR / KEY_FILE


def register_path() -> Path:
    return config.STATE_DIR / REGISTER_FILE


def receiver_name(cfg: Optional[config.Config] = None) -> str:
    """What the node calls itself in the AirPlay picker.

    Defaults to the node name, because on a multi-node rig the whole point is
    telling STAGE-LEFT from STAGE-RIGHT on a phone held at the back of a room.
    """
    cfg = cfg or config.load()
    name = (cfg.airplay_name or cfg.device_name or "pistreamer").strip()
    # AirPlay names travel through mDNS; keep them to something a phone will
    # render and a person will recognise.
    return re.sub(r"[^\w .\-]", "", name)[:40] or "pistreamer"


def _rotation_args(rotation: int) -> List[str]:
    """uxplay rotates in its own vocabulary, not videoflip's."""
    return {90: ["-r", "R"], 180: ["-f", "I"], 270: ["-r", "L"]}.get(int(rotation), [])


def reset_units(seconds: int) -> int:
    """`-reset n` is in units of three seconds, not seconds.

    Reading it as seconds gives a receiver that gives up on a phone that went
    behind somebody's back for four seconds.
    """
    if seconds <= 0:
        return 0
    return max(1, round(seconds / 3))


def build_command(cfg: Optional[config.Config] = None,
                  video_sink: str = "",
                  width: Optional[int] = None,
                  height: Optional[int] = None,
                  refresh: Optional[int] = None) -> List[str]:
    """The uxplay argv for this node's configuration.

    Pure, and takes the display details as arguments, so it can be tested
    without a DRM device — which is the only way any of this could have been
    written without a Pi and an iPhone on the desk.
    """
    cfg = cfg or config.load()
    exe = binary() or "uxplay"

    # stdbuf, and not for tidiness: uxplay block-buffers stdout when it is a
    # pipe, so without this the GUI log stays empty for a kilobyte at a time
    # and the pairing PIN can arrive minutes after the guest needed it.
    cmd: List[str] = ["stdbuf", "-oL", "-eL", exe]

    cmd += ["-n", receiver_name(cfg), "-nh"]
    # Explicit paths: $HOME does not exist under ProtectHome=yes.
    cmd += ["-key", str(key_path()), "-reg", str(register_path())]

    if video_sink:
        cmd += ["-vs", video_sink]
    if width and height:
        # kmssink only sets a mode that matches the frame exactly, so the
        # client is asked to send frames the size of the screen. This is the
        # same constraint that produced the "DUMB buffer has a size of..."
        # failure on the NDI path.
        size = f"{width}x{height}"
        if refresh:
            size += f"@{refresh}"
        cmd += ["-s", size]

    if cfg.audio_enabled:
        sink = "alsasink"
        if cfg.audio_device:
            sink += f" device={cfg.audio_device}"
        cmd += ["-as", sink]
    else:
        cmd += ["-as", "0"]

    hardware = (cfg.airplay_hw_decode and not software_forced()
                and element_available("v4l2h264dec"))
    if hardware:
        # -v4l2 rather than picking the elements by hand: verified to build the
        # identical pipeline (h264parse ! v4l2h264dec ! v4l2convert), but it is
        # the option uxplay supports, so it stays right if that chain changes.
        cmd += ["-v4l2"]
        if cfg.airplay_bt709:
            # Adds `capssetter caps="video/x-h264, colorimetry=bt709"` ahead of
            # the decoder — checked by reading back the pipeline uxplay prints,
            # not from the documentation.
            cmd += ["-bt709"]
    else:
        # Software h264. A Pi 4 manages 720p30 and struggles above it, which is
        # why this is a fallback and not the default.
        cmd += ["-avdec"]

    cmd += _rotation_args(cfg.rotation)

    if cfg.airplay_fps:
        cmd += ["-fps", str(int(cfg.airplay_fps))]
    if cfg.airplay_pin:
        # No fixed PIN: uxplay 1.68 rejects the `-pinNNNN` form that later
        # versions accept, and a receiver that refuses to start because of a
        # version difference is worse than a code the operator reads off the
        # GUI.
        cmd += ["-pin"]
    if cfg.airplay_port:
        cmd += ["-p", str(int(cfg.airplay_port))]
    if cfg.airplay_hold_last_frame:
        # Leave the last frame up when the phone stops mirroring, rather than
        # dropping to black mid-show.
        cmd += ["-nc"]
    if cfg.airplay_timeout_s:
        cmd += ["-reset", str(reset_units(cfg.airplay_timeout_s))]
    return cmd


def ports(cfg: Optional[config.Config] = None) -> Dict[str, Any]:
    """Which ports need opening, for the network people who always ask."""
    cfg = cfg or config.load()
    base = int(cfg.airplay_port or 0)
    if not base:
        return {"fixed": False, "note": "chosen dynamically; mDNS advertises them"}
    return {
        "fixed": True,
        "tcp": [base, base + 1, base + 2],
        "udp": [base, base + 1, base + 2],
        # Confirmed by reading the advertisement back off the network: with
        # `-p n` the AirPlay service is advertised on n+2, not n.
        "airplay": base + 2,
        "mdns": 5353,
    }
