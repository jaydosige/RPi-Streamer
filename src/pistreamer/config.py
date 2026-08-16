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
    # Fall back to shelling out to gst-launch-1.0 instead of the instrumented
    # runner. Loses all stream telemetry; kept as an escape hatch.
    use_gst_launch: bool = False

    # --- system ---
    device_name: str = "pistreamer"
    web_port: int = 80

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
