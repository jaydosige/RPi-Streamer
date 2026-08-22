"""Uploads that are not showable get converted on arrival.

A phone takes HEIC and an office sends a PDF. Neither is something mpv will
put on a display, so both become JPEGs at the door and nothing downstream —
the library, playlists, the standby screen, the preview — ever learns a new
format.

The conversions need heif-convert, pdftoppm and Pillow. Each is skipped
individually where its tool is missing, so this still says something useful on
a machine that has only some of them.

    python3 tests/test_ingest.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-ingest-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP / "media"), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import ingest, media  # noqa: E402

PASS, FAIL = [], []
WORK = TMP / "work"
WORK.mkdir(parents=True, exist_ok=True)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def sized(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    print("what is accepted")
    for name in ("a.heic", "a.HEIF", "a.pdf", "a.txt", "a.md"):
        check(f"{name} may be uploaded", media.is_allowed(name))
    check("an executable still may not", not media.is_allowed("a.sh"))
    for name in ("a.heic", "a.pdf", "a.txt"):
        check(f"{name} is marked for conversion", ingest.needs_conversion(name))
    check("a jpeg is left alone", not ingest.needs_conversion("a.jpg"))
    check("a video is left alone", not ingest.needs_conversion("a.mp4"))

    have = ingest.tools()
    print(f"\ntools here: {have}")

    print("\nHEIC")
    if not (have["heic"] and shutil.which("heif-enc")):
        print("  (skipping — no heif tooling here)")
    else:
        png = WORK / "src.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "testsrc2=size=1600x1200:rate=1", "-frames:v", "1",
                        str(png)], check=True, timeout=60)
        heic = WORK / "photo.heic"
        subprocess.run(["heif-enc", "-q", "80", str(png), "-o", str(heic)],
                       check=True, capture_output=True, timeout=60)
        produced, problem = ingest.convert(heic)
        check("a HEIC converts", not problem and len(produced) == 1, problem)
        if produced:
            check("...to a JPEG", produced[0].suffix == ".jpg", produced[0].name)
            check("...keeping its dimensions", sized(produced[0]) == (1600, 1200),
                  str(sized(produced[0])))
        # The original must go, or the library shows a file that cannot play.
        check("the original is removed", not heic.exists())

    print("\nPDF")
    if not (have["pdf"] and have["text"]):
        print("  (skipping — no pdftoppm or Pillow here)")
    else:
        from PIL import Image, ImageDraw
        pages = []
        for i in range(3):
            im = Image.new("RGB", (1240, 1754), (255, 255, 255))
            ImageDraw.Draw(im).text((80, 80), f"PAGE {i + 1}", fill=(0, 0, 0))
            pages.append(im)
        pdf = WORK / "notice.pdf"
        pages[0].save(pdf, "PDF", save_all=True, append_images=pages[1:])
        produced, problem = ingest.convert(pdf)
        check("a PDF converts", not problem, problem)
        # Every page, not just the first: half a notice on a wall with no clue
        # why is worse than refusing it.
        check("every page becomes an image", len(produced) == 3,
              str([p.name for p in produced]))
        check("the original is removed", not pdf.exists())
        if produced:
            size = sized(produced[0])
            # Scaled to the display's height. Fitting the width instead makes a
            # portrait A4 ~2700px tall — taller than the screen it is going on.
            check(f"a portrait page fits the screen {size}",
                  size and size[0] <= 1920 and size[1] <= 1080, str(size))
            check("pages are in order",
                  [p.name for p in produced] == sorted(p.name for p in produced))

    print("\ntext")
    if not have["text"]:
        print("  (skipping — no Pillow here)")
    else:
        txt = WORK / "notice.txt"
        txt.write_text("Fire exit is via the rear door.\n\n" + ("word " * 900))
        produced, problem = ingest.convert(txt)
        check("text converts", not problem and produced, problem)
        check("long text paginates", len(produced) > 1,
              str([p.name for p in produced]))
        check("pages are display-sized",
              all(sized(p) == (ingest.PAGE_W, ingest.PAGE_H) for p in produced))
        check("the original is removed", not txt.exists())

        empty = WORK / "empty.txt"
        empty.write_text("")
        produced, problem = ingest.convert(empty)
        check("an empty file still makes a page", not problem and produced, problem)

    print("\nwhen a tool is missing")
    real = shutil.which
    shutil.which = lambda _n: None
    ingest._ffmpeg_has_heif.__defaults__ = None
    missing = WORK / "x.pdf"
    missing.write_bytes(b"%PDF-1.4 not really")
    produced, problem = ingest.convert(missing)
    shutil.which = real
    # The upload is kept and the reason is actionable, rather than a silent
    # failure or a lost file.
    check("it refuses rather than losing the file", missing.exists())
    check("...and names the package to install", "poppler-utils" in problem, problem)
    check("...and produces nothing", not produced)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
