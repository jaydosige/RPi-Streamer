"""Formats that are one image wearing an unfamiliar container.

A phone takes HEIC. It is a single photograph, and nothing on this board can
read it, so it becomes a JPEG on arrival and is an ordinary image from then on
— no part of the library, the playlists or the player learns a new format.

Documents are deliberately NOT handled here. A PDF is many pages and stays one
file; see documents.py, which rasterises it at playback instead. Converting one
at upload turned a twenty-page notice into twenty library entries, which is
worse than the problem it solved.

Conversion is best-effort. If the tool is missing the original is kept as it
was and the caller is told why, which is better than refusing an upload that
somebody is standing next to the screen waiting for.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

HEIC_EXTS = {".heic", ".heif"}
CONVERTIBLE = set(HEIC_EXTS)

JPEG_QUALITY = 88


def needs_conversion(name: str) -> bool:
    return Path(name).suffix.lower() in CONVERTIBLE


def tools() -> dict:
    """Which converters this node actually has."""
    return {"heic": bool(shutil.which("heif-convert")) or _ffmpeg_has_heif()}


def _ffmpeg_has_heif() -> bool:
    """ffmpeg only grew a HEIF demuxer in 7.0, so Bookworm's has none."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-h", "demuxer=heif"],
                              capture_output=True, text=True, timeout=15)
        return "Demuxer heif" in (proc.stdout + proc.stderr)
    except (subprocess.SubprocessError, OSError):
        return False


def _run(args: List[str], timeout: int = 120) -> Tuple[bool, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()[:300]
    except subprocess.TimeoutExpired:
        return False, f"{args[0]} timed out"
    except OSError as exc:
        return False, str(exc)


def convert(path: Path) -> Tuple[List[Path], str]:
    """Convert an uploaded file in place. Returns (new files, problem).

    On success the original is removed and the returned paths replace it. On
    failure nothing is touched and the problem is described, so the caller can
    keep the upload and say what happened.
    """
    suffix = path.suffix.lower()
    try:
        if suffix in HEIC_EXTS:
            return _convert_heic(path)
    except Exception as exc:  # noqa: BLE001 - an upload must not take the node down
        log.exception("converting %s failed", path.name)
        return [], str(exc)
    return [path], ""


def _finish(path: Path, produced: List[Path]) -> Tuple[List[Path], str]:
    """Drop the original once its replacements are actually on disk."""
    produced = [p for p in produced if p.is_file() and p.stat().st_size > 0]
    if not produced:
        return [], "the converter produced nothing"
    path.unlink(missing_ok=True)
    return sorted(produced), ""


def _convert_heic(path: Path) -> Tuple[List[Path], str]:
    out = path.with_suffix(".jpg")
    # heif-convert first: ffmpeg only handles HEIF from 7.0, which Bookworm is
    # not, and a wrong answer here is a photo that will not display.
    if shutil.which("heif-convert"):
        ok, err = _run(["heif-convert", "-q", str(JPEG_QUALITY),
                        str(path), str(out)])
        if ok:
            return _finish(path, [out])
        log.warning("heif-convert failed on %s: %s", path.name, err)
    if _ffmpeg_has_heif():
        ok, err = _run(["ffmpeg", "-v", "error", "-y", "-i", str(path),
                        "-q:v", "3", str(out)])
        if ok:
            return _finish(path, [out])
        return [], f"ffmpeg could not read it: {err}"
    return [], ("this node cannot read HEIC — install it with "
                "'sudo apt install libheif-examples'")
