"""Read playback statistics out of a running mpv.

The GStreamer runner is instrumented with pad probes, but local playback goes
through mpv, which was previously a black box — "local playback does not report
frame statistics here" was the honest but unhelpful message in the GUI.

mpv exposes everything over a JSON IPC socket, including the two things that
actually matter and cannot be inferred from outside: the real output frame rate
and whether hardware decoding engaged. So we ask it.

Deliberately synchronous and short-lived per poll: connect, ask, close. A
persistent connection would need reconnection logic for every mpv restart, and
mpv restarts on every playlist segment.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Properties worth having. Each is optional — mpv answers with an error for
# anything not applicable to the current file (no video track, etc.).
PROPERTIES: List[str] = [
    "filename",
    "media-title",
    "duration",
    "time-pos",
    "percent-pos",
    "estimated-vf-fps",
    "container-fps",
    "width",
    "height",
    "video-format",
    "hwdec-current",
    "frame-drop-count",
    "decoder-frame-drop-count",
    "audio-codec-name",
    "audio-device",
    "volume",
    "mute",
    "playlist-pos-1",
    "playlist-count",
    "paused-for-cache",
    "core-idle",
]


def query(socket_path: str, properties: Optional[List[str]] = None,
          timeout: float = 1.5) -> Dict[str, Any]:
    """Ask a running mpv for properties. Returns {} if it is not reachable."""
    props = properties or PROPERTIES
    out: Dict[str, Any] = {}
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
    except (OSError, socket.timeout):
        return out

    try:
        # Send every request up front, then read the replies. mpv tags each
        # reply with the request_id we supplied, so order does not matter.
        payload = "".join(
            json.dumps({"command": ["get_property", name], "request_id": i + 1}) + "\n"
            for i, name in enumerate(props)
        )
        sock.sendall(payload.encode())

        buffer = b""
        seen = 0
        while seen < len(props):
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rid = message.get("request_id")
                if rid is None:
                    continue  # an asynchronous event, not our reply
                seen += 1
                if message.get("error") == "success":
                    out[props[rid - 1]] = message.get("data")
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return out


def command(socket_path: str, *args: Any, timeout: float = 2.0) -> Dict[str, Any]:
    """Send one command to a running mpv and return its reply.

    Used for the things that cannot be expressed as a launch flag because they
    have to happen at a precise moment or in response to another node: leaving
    pause exactly on the beat, seeking to correct drift, and putting the
    identify caption up without restarting playback.

    Returns {} when mpv is not reachable, so callers can treat "no player" and
    "player refused" the same way — neither is worth taking playback down for.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(socket_path)
    except (OSError, socket.timeout):
        return {}
    try:
        payload = json.dumps({"command": list(args), "request_id": 1}) + "\n"
        sock.sendall(payload.encode())
        buffer = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout):
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Events carry no request_id; keep reading past them.
                if message.get("request_id") == 1:
                    return message
        return {}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def position(socket_path: str) -> Optional[float]:
    """Current playhead, asked for fresh.

    Drift correction must not use the once-a-second stats cache: a position
    that is up to a second old is indistinguishable from a second of drift,
    which would make the corrector fight itself.
    """
    reply = command(socket_path, "get_property", "time-pos")
    value = reply.get("data")
    return value if isinstance(value, (int, float)) else None


def to_stats(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape mpv's properties into the same shape the runner reports.

    Keeping one vocabulary means the GUI and the diagnosis code do not need to
    care which backend is playing.
    """
    if not raw:
        return {}

    fps = raw.get("estimated-vf-fps")
    dropped = raw.get("frame-drop-count")
    decoder_dropped = raw.get("decoder-frame-drop-count")
    hwdec = raw.get("hwdec-current")
    # mpv reports "no" rather than null when software decoding.
    hardware = bool(hwdec) and hwdec not in ("no", "none")

    return {
        "backend": "mpv",
        "file": raw.get("filename") or raw.get("media-title") or "",
        "duration": raw.get("duration"),
        "position": raw.get("time-pos"),
        "percent": raw.get("percent-pos"),
        "fps": round(fps, 2) if isinstance(fps, (int, float)) else None,
        "declared_fps": raw.get("container-fps"),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "format": raw.get("video-format"),
        "decoder": hwdec if hwdec not in (None, "no") else "software",
        "hardware_decode": hardware,
        "dropped": dropped if isinstance(dropped, int) else None,
        "decoder_dropped": decoder_dropped if isinstance(decoder_dropped, int) else None,
        "audio_codec": raw.get("audio-codec-name"),
        "audio_device": raw.get("audio-device"),
        "volume": raw.get("volume"),
        "muted": bool(raw.get("mute")),
        "playlist_position": raw.get("playlist-pos-1"),
        "playlist_count": raw.get("playlist-count"),
        "buffering": bool(raw.get("paused-for-cache")),
        "idle": bool(raw.get("core-idle")),
    }
