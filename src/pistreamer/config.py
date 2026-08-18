"""Configuration store for pi-streamer.

Config lives as a single JSON file so it can be hand-edited over SSH and
backed up trivially. Writes are atomic (temp file + rename) so a power cut
mid-write cannot leave a truncated config on the SD card.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict

CONFIG_PATH = Path(os.environ.get("PISTREAMER_CONFIG", "/etc/pistreamer/config.json"))
MEDIA_DIR = Path(os.environ.get("PISTREAMER_MEDIA", "/var/lib/pistreamer/media"))
STATE_DIR = Path(os.environ.get("PISTREAMER_STATE", "/var/lib/pistreamer"))


@dataclass
class Config:
    # --- what to play ---
    # mode: "idle" | "ndi" | "local"
    mode: str = "idle"
    # NDI source name exactly as advertised on the network,
    # e.g. "STUDIO-PC (OBS)"
    ndi_source: str = ""
    # Filename (not path) inside MEDIA_DIR, or "" for the whole folder
    local_file: str = ""
    loop: bool = True
    # Restore the above on boot rather than starting idle
    autostart: bool = True

    # --- display ---
    # DRM connector to drive, e.g. "HDMI-A-1". "" = first connected.
    connector: str = ""
    # "" = leave the mode the kernel picked, else e.g. "1920x1080@60"
    video_mode: str = ""
    # 0 / 90 / 180 / 270
    rotation: int = 0

    # --- audio ---
    # ALSA device string, "" = default. e.g. "hw:CARD=vc4hdmi0,DEV=0"
    audio_device: str = ""
    audio_enabled: bool = True
    # Pace audio on the pipeline clock. On means correct timing; off writes to
    # ALSA as fast as buffers arrive, which is what causes crackle and drift.
    audio_sync: bool = True
    # mpv names audio devices differently from ALSA, so local playback needs
    # its own value. Blank uses mpv's default.
    audio_device_mpv: str = ""
    volume: int = 100  # 0-100, applied to local playback only

    # --- ndi tuning ---
    # "highest" = full bandwidth, "lowest" = the proxy stream.
    # On a Pi 4, "lowest" is the safe default for full-bandwidth senders.
    ndi_bandwidth: str = "highest"
    # Latency in ms for the NDI receive queue
    ndi_latency_ms: int = 200
    # How ndisrc derives presentation timestamps. "receive-time" uses our own
    # clock and is monotonic by construction; the upstream default
    # ("receive-time-vs-timecode") trusts the sender's timecode, which stalls
    # playback if the sender emits odd or non-advancing timecodes.
    ndi_timestamp_mode: str = "receive-time"
    # Colour format the NDI receiver asks the SDK for. "uyvy-bgra" is the
    # upstream default. "fastest" tells the SDK to hand back whatever is
    # cheapest for it, which shifts work to videoconvert; "best" does the
    # opposite. On a Pi 4 this is one of the larger CPU levers.
    ndi_color_format: str = "uyvy-bgra"
    # Frames the receiver will hold. Larger rides out network jitter at the
    # cost of latency and memory.
    ndi_max_queue: int = 10
    ndi_connect_timeout_ms: int = 10000
    ndi_timeout_ms: int = 5000

    # --- NDI networking on a multi-homed node ---
    # Restrict NDI to specific NICs, given as those NICs' own IP addresses.
    # On a node with Wi-Fi for management and Ethernet for media, putting the
    # Ethernet address here stops NDI using the wrong interface.
    ndi_adapter_ips: str = ""
    # Sender IPs to probe directly, bypassing mDNS entirely. Useful when
    # discovery cannot cross the network but the route can.
    ndi_extra_ips: str = ""
    # An NDI Discovery Server, for networks where mDNS is blocked.
    ndi_discovery_server: str = ""
    # Connect straight to "host:port" instead of resolving a name. The last
    # resort that always works if the route does.
    ndi_url_address: str = ""

    # --- pipeline performance ---
    # Sink clock sync. With sync on, a frame that arrives late is dropped to
    # stay in time. Turning it off renders every frame as it arrives — motion
    # can judder slightly but nothing is discarded, which is usually what you
    # want on a signage or IMAG feed.
    sink_sync: bool = True
    # Quality-of-service. The sink reports lateness upstream and drops frames
    # to catch up. This is the single most common source of "dropped frames"
    # on a Pi that is merely a little too slow.
    sink_qos: bool = True
    # Frames later than this are dropped outright. -1 = no limit.
    sink_max_lateness_ms: int = -1
    # videoscale method: 0 nearest, 1 bilinear, 2 4-tap, 3 lanczos.
    # Nearest is dramatically cheaper and, when no scaling is needed, free.
    scale_method: int = 1
    # Pixel format handed to the sink.
    video_format: str = "BGRx"
    # Queue behaviour on the video branch. Leaking downstream drops the oldest
    # frames when the Pi falls behind, which keeps latency bounded.
    queue_leaky: str = "downstream"
    queue_max_buffers: int = 0
    # videoconvert worker threads; 0 lets GStreamer choose.
    convert_threads: int = 0
    # Let the source's own resolution through instead of scaling to a display
    # mode. Makes videoscale a passthrough — the cheapest it can be — but
    # kmssink then needs a mode matching the source exactly, so the runner
    # falls back to the pinned mode if negotiation fails.
    match_source: bool = False

    # --- standby screen ---
    # What to show when nothing is playing. An appliance should never show a
    # console. "black" | "image" | "lastframe"
    idle_mode: str = "black"
    # When an NDI sender vanishes, put the standby screen up while retrying
    # instead of leaving the display to whatever the console shows. This is
    # what makes "hold the last frame" work for the case that matters.
    fallback_to_standby: bool = True
    # Filename in the media directory used for "image" (a still or a video).
    standby_file: str = ""
    # Playlist to play in local mode. Takes precedence over local_file.
    local_playlist: str = ""
    # Run the schedule. Cues still exist when off, they just do not fire.
    schedule_enabled: bool = True
    # Continuously save the most recent NDI frame so "lastframe" has something
    # to hold when the feed stops. Cheap: one JPEG every few seconds.
    snapshot_enabled: bool = True
    snapshot_interval_s: int = 3

    # Fall back to shelling out to gst-launch-1.0 instead of the instrumented
    # runner. Loses all stream telemetry; kept as an escape hatch.
    use_gst_launch: bool = False

    # --- system ---
    device_name: str = "pistreamer"
    web_port: int = 80

    # How often to ask the remote whether there is a new version, in hours.
    # 0 turns it off. A node on a show network with no internet simply fails
    # the check quietly and carries on.
    update_check_hours: int = 24

    # --- guest sharing ---
    # A QR code a guest scans to put a photo or video on the screen. Off by
    # default and it closes itself: an upload page left open on an event
    # network is a door nobody remembers to shut.
    guest_minutes: int = 60
    guest_max_mb: int = 512
    guest_max_items: int = 20
    # Whether a guest may put their own upload on the screen. Off means it
    # queues and the operator decides, which is the right default at a job.
    guest_autoplay: bool = False
    # A line shown on the guest page, e.g. the name of the event.
    guest_note: str = ""

    # --- airplay ---
    # Receiving an iPhone/iPad/Mac screen, via uxplay. This is a playback mode
    # like NDI or local, not a background service: an AirPlay session takes the
    # display, and only one process may own the display.
    # Blank uses device_name, which is what makes STAGE-LEFT and STAGE-RIGHT
    # tellable apart in the picker on a phone at the back of a room.
    airplay_name: str = ""
    # Ask for a pairing code. The code is shown in the GUI, because a headless
    # node has no terminal for uxplay to print it to.
    airplay_pin: bool = False
    # GPU h264 decode (v4l2h264dec + v4l2convert). Off falls back to software,
    # which a Pi 4 can just about manage at 720p and not at all at 1080p60.
    airplay_hw_decode: bool = True
    # A workaround for older Video4Linux2 plugins not recognising Apple's
    # full-range colour. Needed on GStreamer < 1.22; harmless to leave off on
    # Trixie, which ships newer.
    airplay_bt709: bool = False
    # Cap the frame rate the client streams at. 30 is uxplay's own default and
    # is the difference between a Pi 4 keeping up and not.
    airplay_fps: int = 30
    # Seconds of client silence before the session is dropped. Stored in
    # seconds; uxplay counts in threes.
    airplay_timeout_s: int = 15
    # Leave the last frame on screen when the phone stops mirroring, instead of
    # dropping to black in front of a room.
    airplay_hold_last_frame: bool = True
    # Pin the ports, for networks with a firewall between the phones and the
    # node. 0 lets uxplay choose and advertise them over mDNS.
    airplay_port: int = 0
    # Escape hatch: force a GStreamer videosink instead of the DRM one built
    # for this display. Only needed off a Pi — on a desktop with X11, or on a
    # host with no DRM device at all, where the kmssink we would build hangs
    # before the receiver ever starts listening.
    airplay_video_sink: str = ""

    # --- cluster ---
    # Announce this node on the LAN and accept commands from its group. Off
    # makes the node completely invisible and uncontrollable from other nodes.
    cluster_enabled: bool = True
    # Nodes only ever see and command others in the same group. Two shows on
    # one network stay apart by using different group names.
    cluster_group: str = "default"
    # Shared secret. Every beacon is signed with it and every inbound command
    # must carry it, so a laptop on the guest VLAN cannot reboot the rig. The
    # default is deliberately not a secret — change it per install.
    cluster_key: str = "pistreamer"
    # Beacons are UDP broadcast, which some managed switches drop. Add peer
    # addresses here (comma separated) to unicast to them as well; the same
    # escape hatch NDI needs on the same networks.
    cluster_extra_ips: str = ""
    # Correct playback drift against the leader while a synchronised playlist
    # runs. Off still gives a synchronised *start*, just no correction after it.
    cluster_drift_correct: bool = True
    # Show the node name and IP over the picture. Toggled from the GUI across
    # the whole group; persisted so a reboot mid-show comes back identified.
    identify: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_lock = threading.Lock()
_cached: Config | None = None


def _known_keys() -> set[str]:
    return {f.name for f in fields(Config)}


def load() -> Config:
    """Load config from disk, filling in defaults for anything missing."""
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        data: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                # Corrupt config should not brick the node. Fall back to
                # defaults and let the user fix it from the GUI.
                data = {}
        known = _known_keys()
        clean = {k: v for k, v in data.items() if k in known}
        _cached = Config(**clean)
        return _cached


def save(cfg: Config) -> None:
    """Atomically persist config to disk."""
    global _cached
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(cfg.to_dict(), indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), prefix=".config-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, CONFIG_PATH)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        _cached = cfg


def update(**kwargs: Any) -> Config:
    """Patch specific fields and persist. Unknown keys are ignored."""
    cfg = load()
    known = _known_keys()
    for key, value in kwargs.items():
        if key in known:
            setattr(cfg, key, value)
    save(cfg)
    return cfg


def ensure_dirs() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
