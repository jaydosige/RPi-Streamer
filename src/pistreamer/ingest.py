"""Turning what people actually upload into something the screen can show.

A phone takes HEIC. An office sends a PDF. Neither is a thing mpv will put on
a display, and teaching the whole playback path two new formats is far more
work — and more to go wrong on a show night — than converting once, on arrival,
into the formats that already work everywhere.

So everything here produces JPEGs, and after that it is an ordinary image as
far as the library, playlists, the standby screen and the preview are
concerned. Nothing downstream learns a new format.

A document becomes one image per page rather than only its first: silently
dropping pages puts half a notice on a wall and gives no clue why. Pages are
capped, because a hundred-page PDF dropped into a media library is not a thing
anybody meant to do.

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
DOC_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv"}
CONVERTIBLE = HEIC_EXTS | DOC_EXTS | TEXT_EXTS

# A page beyond this is somebody uploading the wrong thing.
MAX_PAGES = 20
# Rendered pages are shown full-screen on a 1080p display.
PAGE_W, PAGE_H = 1920, 1080
JPEG_QUALITY = 88

_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def needs_conversion(name: str) -> bool:
    return Path(name).suffix.lower() in CONVERTIBLE


def tools() -> dict:
    """Which converters this node actually has."""
    return {
        "heic": bool(shutil.which("heif-convert")) or _ffmpeg_has_heif(),
        "pdf": bool(shutil.which("pdftoppm")),
        "text": _pillow_available(),
    }


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


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
        if suffix in DOC_EXTS:
            return _convert_pdf(path)
        if suffix in TEXT_EXTS:
            return _convert_text(path)
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


def _convert_pdf(path: Path) -> Tuple[List[Path], str]:
    if not shutil.which("pdftoppm"):
        return [], ("this node cannot read PDFs — install it with "
                    "'sudo apt install poppler-utils'")
    prefix = path.with_suffix("")
    ok, err = _run([
        "pdftoppm", "-jpeg", "-r", "150",
        # Scale to the display's height, not its width. Fitting the width
        # makes a portrait A4 nearly 2700px tall — taller than the screen it
        # is going on, so it is scaled down again at playback having cost the
        # memory and the file size. Height is the dimension both a portrait
        # page and a landscape slide have to fit into.
        "-scale-to-y", str(PAGE_H), "-scale-to-x", "-1",
        "-l", str(MAX_PAGES), str(path), str(prefix),
    ], timeout=300)
    pages = sorted(prefix.parent.glob(f"{glob_escape(prefix.name)}-*.jpg"))
    if not ok and not pages:
        return [], f"pdftoppm could not read it: {err}"
    return _finish(path, pages)


def glob_escape(text: str) -> str:
    """Escape a literal filename for use inside a glob pattern."""
    return "".join("[" + ch + "]" if ch in "*?[]" else ch for ch in text)


def _convert_text(path: Path) -> Tuple[List[Path], str]:
    """Render plain text onto pages, monospaced, for reading across a room."""
    if not _pillow_available():
        return [], ("this node cannot render text — install it with "
                    "'sudo apt install python3-pil'")
    from PIL import Image, ImageDraw, ImageFont

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], str(exc)

    size = 34
    font = None
    for candidate in _FONTS:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except OSError:
            continue
    if font is None:
        # Pillow's built-in default is a small bitmap face that ignores the
        # size entirely, so text rendered with it is unreadable across a room.
        # Newer Pillow can scale it; older cannot, and then the page is at
        # least legible rather than right.
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
        log.warning("no DejaVu font found; text pages will use a fallback face")

    margin = 60
    # Wrap on the real glyph width rather than a character count, so the text
    # fills the page instead of stopping short of it.
    probe = Image.new("RGB", (10, 10))
    char_w = max(1, ImageDraw.Draw(probe).textlength("M", font=font))
    cols = max(20, int((PAGE_W - 2 * margin) / char_w))
    line_h = int(size * 1.35)
    rows = max(4, (PAGE_H - 2 * margin) // line_h)

    import textwrap
    lines: List[str] = []
    for raw in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(raw, cols) or [""])
    if not lines:
        lines = ["(empty file)"]

    produced: List[Path] = []
    for index in range(0, min(len(lines), rows * MAX_PAGES), rows):
        page = Image.new("RGB", (PAGE_W, PAGE_H), (12, 14, 18))
        draw = ImageDraw.Draw(page)
        for row, line in enumerate(lines[index:index + rows]):
            draw.text((margin, margin + row * line_h), line,
                      font=font, fill=(232, 236, 243))
        out = path.with_name(f"{path.stem}-{len(produced) + 1:02d}.jpg")
        page.save(out, "JPEG", quality=JPEG_QUALITY)
        produced.append(out)
    return _finish(path, produced)
