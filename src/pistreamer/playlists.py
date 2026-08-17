"""Named playlists of local media.

Stored as one JSON file alongside the media, so a playlist survives a service
restart and can be hand-edited over SSH. Playback itself is mpv's job — it
handles ordering, looping and shuffling natively, so all we do is write it a
plain-text playlist file and set the right flags.

Per-item dwell time for stills is deliberately *not* supported: mpv's
--image-display-duration is a global setting, not per-entry, and faking
per-item timing would mean driving playback ourselves. One duration per
playlist is honest about what the backend actually does.
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
from typing import Dict, List, Optional

from . import config, media

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9 _-]{1,60}$")


@dataclass
class Playlist:
    name: str
    items: List[str] = field(default_factory=list)
    loop: bool = True
    shuffle: bool = False
    # Seconds each still image is held. Videos play to their natural end.
    image_duration: int = 10

    def to_dict(self) -> dict:
        return asdict(self)


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
        out.append(
            Playlist(
                name=name,
                items=[str(i) for i in raw.get("items", [])],
                loop=bool(raw.get("loop", True)),
                shuffle=bool(raw.get("shuffle", False)),
                image_duration=int(raw.get("image_duration", 10)),
            )
        )
    return out


def get(name: str) -> Optional[Playlist]:
    return next((p for p in all_playlists() if p.name == name), None)


def save(playlist: Playlist) -> Playlist:
    if not valid_name(playlist.name):
        raise ValueError("playlist names may use letters, numbers, spaces, _ and - only")
    # Silently dropping missing files would hide a typo; reject instead.
    missing = [i for i in playlist.items if media.resolve(i) is None]
    if missing:
        raise ValueError(f"not in the media library: {', '.join(missing)}")
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


def resolved_files(name: str) -> List[str]:
    """Absolute paths for a playlist, in play order, skipping anything gone.

    Files can disappear between saving a playlist and playing it, so this
    filters at play time rather than trusting the stored list.
    """
    playlist = get(name)
    if playlist is None:
        return []
    paths = []
    for item in playlist.items:
        resolved = media.resolve(item)
        if resolved is None:
            log.warning("playlist %r: %s is missing, skipping", name, item)
            continue
        paths.append(str(resolved))
    if playlist.shuffle:
        random.shuffle(paths)
    return paths


def write_m3u(name: str) -> Optional[Path]:
    """Write the playlist for mpv to read. Returns the file path."""
    paths = resolved_files(name)
    if not paths:
        return None
    out = config.STATE_DIR / "current-playlist.m3u"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(paths) + "\n")
    return out
