"""HEIC uploads get converted on arrival.

A phone takes HEIC: one photograph, in a container nothing on this board can
read. It becomes a JPEG at the door and is an ordinary image from then on.

Documents are deliberately not converted here — see test_documents.py.

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
    # A HEIC is one photograph in a container nothing here reads, so it is
    # converted. Documents are many pages and stay whole — documents.py
    # rasterises them at playback instead.
    check("a HEIC is converted on arrival", ingest.needs_conversion("a.heic"))
    for name in ("a.pdf", "a.txt", "a.jpg", "a.mp4"):
        check(f"{name} is left alone", not ingest.needs_conversion(name))

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

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
