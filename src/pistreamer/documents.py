"""Showing a document without shredding it into the media library.

A PDF is one thing to an operator, so it stays one entry in the library. It is
still true that nothing on this board can put a PDF on a DRM display, so its
pages are rasterised — but at playback, into scratch space, not at upload into
the library. Uploading a twenty-page notice used to produce twenty files an
operator then had to scroll past for ever.

Pages become an ordinary mpv playlist, which is what makes paging free: next,
previous and jump-to-page are `playlist-next`, `playlist-prev` and setting
`playlist-pos` on the mpv that is already running, over the socket that is
already open. mpv also reports which entry it is on, so "page 4 of 12" needs no
bookkeeping here at all.

The cache lives in RAM alongside the preview frames. Pages are derived data —
losing them on reboot costs one re-render — and writing a pile of JPEGs to the
SD card every time somebody opens a document is how flash wears out.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

PDF_EXTS = {".pdf"}
TEXT_EXTS = {".txt", ".md", ".log", ".csv"}
DOC_EXTS = PDF_EXTS | TEXT_EXTS

PAGE_W, PAGE_H = 1920, 1080
JPEG_QUALITY = 88
# No cap on what can be shown — the whole point of the rework. This only stops
# a runaway render if something hands us a pathological file.
SAFETY_LIMIT = 2000

_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def is_document(name: str) -> bool:
    return Path(name).suffix.lower() in DOC_EXTS


def available(name: str) -> Tuple[bool, str]:
    """Can this node rasterise that kind of document?"""
    suffix = Path(name).suffix.lower()
    if suffix in PDF_EXTS and not shutil.which("pdftoppm"):
        return False, ("this node cannot show PDFs — install it with "
                       "'sudo apt install poppler-utils'")
    if suffix in TEXT_EXTS:
        try:
            import PIL  # noqa: F401
        except ImportError:
            return False, ("this node cannot render text — install it with "
                           "'sudo apt install python3-pil'")
    return True, ""


def cache_dir(path: Path) -> Path:
    """Where a document's pages live.

    Keyed on the file's identity — name, size, mtime — so editing a document
    and re-uploading it under the same name renders again instead of showing
    the pages from before.
    """
    try:
        stat = path.stat()
        token = f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        token = path.name
    digest = hashlib.sha256(token.encode()).hexdigest()[:16]
    return config.runtime_dir() / "documents" / digest


def pages(path: Path, force: bool = False) -> Tuple[List[Path], str]:
    """The document's pages as images, rendering them the first time.

    Returns (pages, problem). A document already rendered is free to reopen.
    """
    ok, reason = available(path.name)
    if not ok:
        return [], reason

    out = cache_dir(path)
    existing = sorted(out.glob("page-*.jpg")) if out.is_dir() else []
    if existing and not force:
        return existing, ""

    if out.is_dir():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    try:
        suffix = path.suffix.lower()
        if suffix in PDF_EXTS:
            problem = _render_pdf(path, out)
        else:
            problem = _render_text(path, out)
    except Exception as exc:  # noqa: BLE001 - a bad document must not take the node down
        log.exception("rendering %s failed", path.name)
        problem = str(exc)

    rendered = sorted(out.glob("page-*.jpg"))
    if problem and not rendered:
        shutil.rmtree(out, ignore_errors=True)
        return [], problem
    log.info("rendered %s into %d page(s)", path.name, len(rendered))
    return rendered, ""


def count(path: Path) -> int:
    found, _problem = pages(path)
    return len(found)


def forget(path: Path) -> None:
    shutil.rmtree(cache_dir(path), ignore_errors=True)


def _render_pdf(path: Path, out: Path) -> str:
    try:
        proc = subprocess.run(
            ["pdftoppm", "-jpeg", "-jpegopt", f"quality={JPEG_QUALITY}",
             # Scale to the display's height. Fitting the width instead makes a
             # portrait A4 taller than the screen it is going on, so it is
             # scaled down again at playback having cost the size.
             "-scale-to-y", str(PAGE_H), "-scale-to-x", "-1",
             "-l", str(SAFETY_LIMIT),
             str(path), str(out / "page")],
            capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "the document took too long to render"
    except OSError as exc:
        return str(exc)
    if proc.returncode != 0 and not list(out.glob("page-*.jpg")):
        return f"pdftoppm could not read it: {(proc.stderr or '').strip()[:200]}"
    _pad_names(out)
    return ""


def _pad_names(out: Path) -> None:
    """pdftoppm numbers pages without padding past its own width.

    page-9 and page-10 then sort in the wrong order as strings, which is how a
    ten-page document ends up showing page 10 second. Renaming once here keeps
    every later sort — the playlist, the page list, the cache scan — correct
    without any of them needing to know.
    """
    for page in list(out.glob("page-*.jpg")):
        stem = page.stem.split("-", 1)[-1]
        if stem.isdigit():
            padded = out / f"page-{int(stem):04d}.jpg"
            if padded != page:
                page.rename(padded)


def _render_text(path: Path, out: Path) -> str:
    from PIL import Image, ImageDraw, ImageFont

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return str(exc)

    size = 34
    font = None
    for candidate in _FONTS:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except OSError:
            continue
    if font is None:
        # Pillow's built-in face ignores the size and renders far too small to
        # read across a room. Newer Pillow can scale it; older cannot.
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
        log.warning("no DejaVu font found; text pages will use a fallback face")

    margin = 60
    # Wrap on the real glyph width, not a character count, so the text fills
    # the page instead of stopping short of it.
    probe = Image.new("RGB", (10, 10))
    char_w = max(1.0, ImageDraw.Draw(probe).textlength("M", font=font))
    cols = max(20, int((PAGE_W - 2 * margin) / char_w))
    line_h = int(size * 1.35)
    rows = max(4, (PAGE_H - 2 * margin) // line_h)

    lines: List[str] = []
    for raw in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(raw, cols) or [""])
    if not lines:
        lines = ["(empty file)"]

    number = 0
    for index in range(0, min(len(lines), rows * SAFETY_LIMIT), rows):
        number += 1
        page = Image.new("RGB", (PAGE_W, PAGE_H), (12, 14, 18))
        draw = ImageDraw.Draw(page)
        for row, line in enumerate(lines[index:index + rows]):
            draw.text((margin, margin + row * line_h), line,
                      font=font, fill=(232, 236, 243))
        page.save(out / f"page-{number:04d}.jpg", "JPEG", quality=JPEG_QUALITY)
    return ""
