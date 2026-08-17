"""Named playlists of media files and NDI sources.

A playlist item is a *segment*: either a local file or an NDI source, with an
optional duration. That mix is why playback cannot always be handed to mpv —
mpv knows nothing about NDI — so there are two play strategies:

  * all-file playlist, no explicit durations -> one mpv with an m3u, which
    gives the smoothest transitions
  * anything else -> the player sequences segments itself, spawning the right
    backend per segment and advancing on a timer or on process exit

The sequencer costs a brief black frame between segments, since each one is a
separate process holding DRM. That is the price of mixing NDI into a playlist,
and it is why the smooth path is kept for the common case.

Legacy playlists stored items as bare filename strings; those are migrated to
segments on read, so old files keep working.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, media

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")


SEGMENT_TYPES = ("file", "ndi")


@dataclass
class Playlist:
    name: str
    # Each item: {"type": "file"|"ndi", "target": str, "duration": int|None}
    items: List[Dict[str, Any]] = field(default_factory=list)
    loop: bool = True
    shuffle: bool = False
    # Default seconds a still image is held when it has no explicit duration.
    image_duration: int = 10

    def to_dict(self) -> dict:
        return asdict(self)

    def needs_sequencer(self) -> bool:
        """True when mpv alone cannot play this."""
        return any(
            item.get("type") == "ndi" or item.get("duration") for item in self.items
        )


def normalise_items(raw: Any, image_duration: int = 10) -> List[Dict[str, Any]]:
    """Coerce stored or submitted items into segments.

    Accepts bare strings (the original format) as file segments so playlists
    written by an older version keep working.
    """
    out: List[Dict[str, Any]] = []
    for entry in raw or []:
        if isinstance(entry, str):
            out.append({"type": "file", "target": entry, "duration": None})
            continue
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type", "file"))
        if kind not in SEGMENT_TYPES:
            continue
        duration = entry.get("duration")
        try:
            duration = int(duration) if duration not in (None, "", 0) else None
        except (TypeError, ValueError):
            duration = None
        if duration is not None:
            duration = max(1, min(86400, duration))
        out.append(
            {"type": kind, "target": str(entry.get("target", "")), "duration": duration}
        )
    return out


def store_path() -> Path:
    return config.STATE_DIR / "playlists.json"


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def _load_raw() -> Dict[str, dict]:
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("playlists.json unreadable (%s); starting empty", exc)
        return {}


def _save_raw(data: Dict[str, dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".playlists-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def all_playlists() -> List[Playlist]:
    out = []
    for name, raw in sorted(_load_raw().items()):
        duration = int(raw.get("image_duration", 10))
        out.append(
            Playlist(
                name=name,
                items=normalise_items(raw.get("items"), duration),
                loop=bool(raw.get("loop", True)),
                shuffle=bool(raw.get("shuffle", False)),
                image_duration=duration,
            )
        )
    return out


def get(name: str) -> Optional[Playlist]:
    return next((p for p in all_playlists() if p.name == name), None)


def save(playlist: Playlist) -> Playlist:
    if not valid_name(playlist.name):
        raise ValueError("playlist names may use letters, numbers, spaces, _ and - only")
    playlist.items = normalise_items(playlist.items, playlist.image_duration)
    if not playlist.items:
        raise ValueError("a playlist needs at least one item")
    # Silently dropping a bad entry would hide a typo; reject instead.
    missing = [
        i["target"] for i in playlist.items
        if i["type"] == "file" and media.resolve(i["target"]) is None
    ]
    if missing:
        raise ValueError(f"not in the media library: {', '.join(missing)}")
    nameless = [i for i in playlist.items if i["type"] == "ndi" and not i["target"]]
    if nameless:
        raise ValueError("every NDI item needs a source name")
    # An NDI segment has no natural end, so it must say how long to hold it.
    endless = [
        i["target"] for i in playlist.items
        if i["type"] == "ndi" and not i["duration"]
    ]
    if endless:
        raise ValueError(
            f"NDI items need a duration in seconds: {', '.join(endless)}"
        )
    playlist.image_duration = max(1, min(3600, playlist.image_duration))
    data = _load_raw()
    data[playlist.name] = playlist.to_dict()
    data[playlist.name].pop("name", None)
    _save_raw(data)
    return playlist


def delete(name: str) -> bool:
    data = _load_raw()
    if name not in data:
        return False
    del data[name]
    _save_raw(data)
    return True


def resolved_segments(name: str) -> List[Dict[str, Any]]:
    """Playable segments in play order, dropping anything that has vanished.

    Files can disappear between saving a playlist and playing it, so this
    filters at play time rather than trusting the stored list.
    """
    playlist = get(name)
    if playlist is None:
        return []
    out: List[Dict[str, Any]] = []
    for item in playlist.items:
        if item["type"] == "file":
            resolved = media.resolve(item["target"])
            if resolved is None:
                log.warning("playlist %r: %s is missing, skipping", name, item["target"])
                continue
            duration = item["duration"]
            is_image = resolved.suffix.lower() in media.IMAGE_EXTS
            if duration is None and is_image:
                duration = playlist.image_duration
            out.append({
                "type": "file", "target": item["target"], "path": str(resolved),
                "duration": duration, "image": is_image,
            })
        else:
            out.append({
                "type": "ndi", "target": item["target"],
                "duration": item["duration"] or 30, "image": False,
            })
    if playlist.shuffle:
        random.shuffle(out)
    return out


def resolved_files(name: str) -> List[str]:
    """File paths only — used by the smooth single-mpv path."""
    return [s["path"] for s in resolved_segments(name) if s["type"] == "file"]


def write_m3u(name: str) -> Optional[Path]:
    """Write the playlist for mpv to read. Returns the file path."""
    paths = resolved_files(name)
    if not paths:
        return None
    out = config.STATE_DIR / "current-playlist.m3u"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(paths) + "\n")
    return out
