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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import config

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm", ".ts", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
# A HEIC is one photograph in a container nothing here can read, so it is
# converted to JPEG on arrival — see ingest.py — and never seen again.
HEIC_EXTS = {".heic", ".heif"}
# Documents stay whole. They are many pages and one library entry; the pages
# are rasterised at playback — see documents.py.
DOC_EXTS = {".pdf", ".txt", ".md", ".log", ".csv"}
ALLOWED_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS | HEIC_EXTS | DOC_EXTS

_SAFE_RE = re.compile(r"[^A-Za-z0-9._ -]")


# What the Pi 4's video block can actually decode in hardware, and the limits
# it does it within. These are properties of the SoC, not of the software: the
# H.264 decoder is 1080p-capable and 8-bit 4:2:0 only, and the separate HEVC
# block goes to 4K. Everything else — VP8, VP9, AV1, MPEG-4 part 2, VC-1 — has
# no hardware path at all on this board and falls to the CPU.
#
# Deliberately conservative. Claiming hardware decode that then does not happen
# is worse than not claiming it: the file plays badly on the day instead of
# being flagged the moment it was uploaded.
HW_DECODE_LIMITS = {
    "h264": {"max_pixels": 1920 * 1080, "max_fps": 60,
             "pix_fmts": {"yuv420p", "yuvj420p"}},
    "hevc": {"max_pixels": 3840 * 2160, "max_fps": 60,
             "pix_fmts": {"yuv420p", "yuvj420p"}},
}
# The shape a file should be in to play well. Anything outside this is what the
# transcode button exists to fix.
# How codecs are written when a human reads them. ffprobe's names are lowercase
# and punctuation-free, which looks like a typo in a sentence.
CODEC_LABELS = {
    "h264": "H.264", "hevc": "H.265", "vp8": "VP8", "vp9": "VP9",
    "av1": "AV1", "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4",
    "vc1": "VC-1", "prores": "ProRes", "dnxhd": "DNxHD", "theora": "Theora",
}


def codec_label(codec: str) -> str:
    return CODEC_LABELS.get(codec.lower(), codec.upper() or "This codec")


TARGET_CODEC = "h264"
TARGET_WIDTH = 1920
TARGET_HEIGHT = 1080
TARGET_FPS = 60
TARGET_PIX_FMT = "yuv420p"


@dataclass
class VideoInfo:
    """What ffprobe found in a file's video stream."""

    codec: str = ""
    profile: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    bit_rate: int = 0
    audio_codec: str = ""

    def to_dict(self) -> dict:
        return {
            "codec": self.codec, "profile": self.profile,
            "width": self.width, "height": self.height,
            "fps": round(self.fps, 3) if self.fps else 0.0,
            "pix_fmt": self.pix_fmt, "bit_rate": self.bit_rate,
            "audio_codec": self.audio_codec,
        }


@dataclass
class MediaFile:
    name: str
    size: int
    kind: str  # "video" | "audio" | "image"
    duration: Optional[float] = None
    video: Optional[VideoInfo] = None
    playback: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "duration": self.duration,
            "video": self.video.to_dict() if self.video else None,
            "playback": self.playback,
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
    if s in DOC_EXTS:
        return "document"
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


def _parse_fps(rate: str) -> float:
    """ffprobe gives frame rates as the exact fraction, e.g. '30000/1001'."""
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return 0.0


# Probe results by path, keyed on the file's size and mtime so a replaced file
# is re-read but an unchanged one is never probed twice. The media tab polls the
# library, and ffprobe on a folder of multi-gigabyte videos is seconds of work
# per listing — without this, opening that tab stalls the GUI every time.
_probe_cache: Dict[tuple, tuple] = {}
_probe_lock = threading.Lock()
# A library of a few hundred files is already unusual; this only exists so a
# long-running node that has had thousands of files through it cannot grow the
# cache without bound.
_PROBE_CACHE_MAX = 512


def probe_file(path: Path) -> tuple[Optional[float], Optional[VideoInfo]]:
    """Duration and video stream details via ffprobe.

    One ffprobe call for both, because it is the slow part of listing a media
    folder and it was already being run for the duration alone. Results are
    cached against size and mtime, so a file is probed once and a file that has
    been replaced is probed again.
    """
    if not shutil.which("ffprobe"):
        return None, None
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None, None
    with _probe_lock:
        hit = _probe_cache.get(key)
    if hit is not None:
        return hit
    result = _probe_uncached(path)
    with _probe_lock:
        if len(_probe_cache) >= _PROBE_CACHE_MAX:
            _probe_cache.clear()
        _probe_cache[key] = result
    return result


def _probe_uncached(path: Path) -> tuple[Optional[float], Optional[VideoInfo]]:
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode != 0:
            return None, None
        data = json.loads(proc.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None, None

    duration: Optional[float] = None
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        duration = None

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        return duration, None

    # avg_frame_rate is 0/0 on some containers; r_frame_rate is the fallback.
    fps = _parse_fps(str(video.get("avg_frame_rate") or "")) or \
        _parse_fps(str(video.get("r_frame_rate") or ""))
    try:
        bit_rate = int(video.get("bit_rate") or data["format"].get("bit_rate") or 0)
    except (KeyError, TypeError, ValueError):
        bit_rate = 0

    return duration, VideoInfo(
        codec=str(video.get("codec_name") or ""),
        profile=str(video.get("profile") or ""),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        pix_fmt=str(video.get("pix_fmt") or ""),
        bit_rate=bit_rate,
        audio_codec=str(audio.get("codec_name") or "") if audio else "",
    )


def _probe_duration(path: Path) -> Optional[float]:
    """Best-effort duration via ffprobe. Returns None if unavailable."""
    return probe_file(path)[0]


def assess(info: Optional[VideoInfo],
           hw_codecs: Optional[set] = None) -> dict:
    """Judge how well a video will play on this node, and why.

    `hw_codecs` is what the box reports it can decode in hardware; None means
    "assume the Pi 4's own block", which is what this runs on. Passing the real
    set matters because HEVC hardware decode depends on the rpivid driver being
    enabled, so the same file is a different answer on two Pis.

    Returns a verdict of "good", "fair" or "poor", the reasons behind it, and
    whether transcoding would actually help — which is not the same question:
    a 4K HEVC file decodes in hardware and still drops frames, and a file that
    is merely 1080p50 H.264 is fine as it is.
    """
    if info is None:
        return {"verdict": "unknown", "hardware": False, "reasons": [],
                "transcode": False,
                "summary": "Could not be probed — install ffmpeg to check."}

    codec = info.codec.lower()
    label = codec_label(info.codec)
    limits = HW_DECODE_LIMITS.get(codec)
    supported = limits is not None and (hw_codecs is None or codec in hw_codecs)
    pixels = info.width * info.height
    reasons: List[str] = []
    hardware = False

    if not supported:
        why = (f"{label} has no hardware decoder on this node — "
               f"the CPU has to do it")
        if limits is not None and hw_codecs is not None:
            # The board can do it but nothing has registered a decoder, which on
            # a Pi almost always means the rpivid overlay is not enabled.
            why = (f"{label} decoding is not registered on this node — check the "
                   f"rpivid overlay is enabled in /boot/config.txt")
        reasons.append(why)
    else:
        hardware = True
        if info.pix_fmt and info.pix_fmt not in limits["pix_fmts"]:
            hardware = False
            depth = "10-bit" if "10" in info.pix_fmt else info.pix_fmt
            reasons.append(
                f"{depth} colour ({info.pix_fmt}) — the hardware decoder only "
                f"takes 8-bit 4:2:0, so this falls back to the CPU")
        if pixels > limits["max_pixels"]:
            hardware = False
            cap = limits["max_pixels"]
            reasons.append(
                f"{info.width}x{info.height} is above the "
                f"{'1080p' if cap == 1920 * 1080 else '4K'} hardware limit for "
                f"{label}")
        if info.fps > limits["max_fps"] + 1:
            hardware = False
            reasons.append(f"{info.fps:.0f}fps is above the "
                           f"{limits['max_fps']}fps hardware limit")

    # Above the display's own resolution the Pi scales every frame down, which
    # costs real time even when the decode itself was free.
    oversized = pixels > TARGET_WIDTH * TARGET_HEIGHT
    if hardware and oversized:
        reasons.append(
            f"{info.width}x{info.height} is larger than the 1080p output, so "
            f"every frame is scaled down before it is shown")
    if info.bit_rate > 30_000_000:
        reasons.append(f"{info.bit_rate / 1_000_000:.0f} Mbps is a very high "
                       f"bitrate to read and decode continuously")

    if hardware and not reasons:
        verdict, summary = "good", "Decodes in hardware — plays smoothly"
    elif hardware:
        verdict, summary = "fair", "Decodes in hardware, but has to be scaled"
    else:
        verdict, summary = "poor", "Falls back to software decoding — expect stutter"

    # Transcoding is only offered where it would change the answer. Re-encoding
    # a file that already plays well costs quality for nothing.
    return {
        "verdict": verdict,
        "hardware": hardware,
        "reasons": reasons,
        "transcode": verdict != "good",
        "summary": summary,
    }


def list_media(probe: bool = False, hw_codecs: Optional[set] = None) -> List[MediaFile]:
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
        duration, info, playback = None, None, None
        # `probe` is opt-in because ffprobe on a folder of large files is
        # seconds of work and the GUI polls this endpoint.
        if probe:
            duration, info = probe_file(path)
            if kind == "video":
                playback = assess(info, hw_codecs)
        out.append(
            MediaFile(name=path.name, size=size, kind=kind,
                      duration=duration, video=info, playback=playback)
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
    return [str(config.MEDIA_DIR / m.name) for m in list_media()
            if m.kind not in ("image", "document")]
