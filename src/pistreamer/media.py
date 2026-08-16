"""Local media library.

Files live in a single flat directory (MEDIA_DIR). Names are sanitised on
upload so a hostile or careless filename cannot escape the directory or
break the shell — the player passes paths as argv, never through a shell,
but path traversal is still a real risk on the upload and delete endpoints.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import config

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
ALLOWED_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]")


@dataclass
class MediaFile:
    name: str
    size: int
    kind: str  # "video" | "audio" | "image"
    duration: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "duration": self.duration,
        }


def sanitise_name(name: str) -> str:
    """Reduce an arbitrary upload filename to a safe flat basename."""
    # Drop any directory component the client sent.
    base = Path(name.replace("\\", "/")).name
    base = _SAFE_RE.sub("_", base).strip(". ")
    if not base:
        base = "upload"
    return base[:150]


def _kind_for(suffix: str) -> Optional[str]:
    s = suffix.lower()
    if s in VIDEO_EXTS:
        return "video"
    if s in AUDIO_EXTS:
        return "audio"
    if s in IMAGE_EXTS:
        return "image"
    return None


def resolve(name: str) -> Optional[Path]:
    """Map a client-supplied filename to a real file inside MEDIA_DIR.

    Returns None if the name escapes the media directory or does not exist.
    """
    if not name:
        return None
    candidate = (config.MEDIA_DIR / Path(name).name).resolve()
    try:
        media_root = config.MEDIA_DIR.resolve()
    except OSError:
        return None
    if media_root not in candidate.parents and candidate != media_root:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _probe_duration(path: Path) -> Optional[float]:
    """Best-effort duration via ffprobe. Returns None if unavailable."""
    if not shutil.which("ffprobe"):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        return float(data["format"]["duration"])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        return None


def list_media(probe: bool = False) -> List[MediaFile]:
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    out: List[MediaFile] = []
    for path in sorted(config.MEDIA_DIR.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        kind = _kind_for(path.suffix)
        if kind is None:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append(
            MediaFile(
                name=path.name,
                size=size,
                kind=kind,
                duration=_probe_duration(path) if probe else None,
            )
        )
    return out


def is_allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTS


def delete(name: str) -> bool:
    path = resolve(name)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def playlist_paths(selection: str = "") -> List[str]:
    """Return the argv list of files to play.

    An empty selection means "everything in the folder", which is the common
    case for digital-signage style looping.
    """
    if selection:
        path = resolve(selection)
        return [str(path)] if path else []
    return [str(config.MEDIA_DIR / m.name) for m in list_media() if m.kind != "image"]
