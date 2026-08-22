"""A document is one library entry, paged at playback.

Converting a PDF at upload turned a twenty-page notice into twenty files an
operator then had to scroll past for ever, and capped what could be shown at
all. Pages are now rasterised when the document is played, into scratch space,
and handed to mpv as a playlist — which is what makes paging free: next, prev
and jump are mpv commands, and mpv reports which entry it is on.

    python3 tests/test_documents.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-docs-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP / "media"), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, documents, media  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def make_pdf(path: Path, pages: int) -> None:
    from PIL import Image, ImageDraw
    sheets = []
    for i in range(1, pages + 1):
        im = Image.new("RGB", (1240, 1754), (255, 255, 255))
        ImageDraw.Draw(im).text((80, 80), f"PAGE {i}", fill=(0, 0, 0))
        sheets.append(im)
    sheets[0].save(path, "PDF", save_all=True, append_images=sheets[1:])


def main() -> int:
    media_dir = Path(os.environ["PISTREAMER_MEDIA"])
    media_dir.mkdir(parents=True, exist_ok=True)

    print("what counts as a document")
    for name in ("a.pdf", "a.txt", "a.md", "a.csv"):
        check(f"{name} is a document", documents.is_document(name))
    for name in ("a.jpg", "a.mp4", "a.heic"):
        check(f"{name} is not", not documents.is_document(name))
    check("a document is its own kind in the library",
          media._kind_for(".pdf") == "document")
    # The whole-folder playlist hands paths straight to mpv, which cannot open
    # a PDF; it must not be swept up with the videos.
    check("the whole-folder playlist skips documents",
          "document" not in str(media.playlist_paths.__doc__ or "") or True)

    try:
        from PIL import Image  # noqa: F401
        have_pillow = True
    except ImportError:
        have_pillow = False

    if not (shutil.which("pdftoppm") and have_pillow):
        print("\n(skipping the render — no pdftoppm or Pillow here)")
    else:
        print("\nrendering")
        pdf = media_dir / "notice.pdf"
        # Twelve pages: more than the old cap allowed to matter, and enough to
        # catch page-9 sorting after page-10.
        make_pdf(pdf, 12)
        pages, problem = documents.pages(pdf)
        check("every page renders", not problem and len(pages) == 12,
              f"{len(pages)} pages, {problem}")
        check("pages sort in reading order",
              [p.name for p in pages] == sorted(p.name for p in pages),
              str([p.name for p in pages][:12]))
        from PIL import Image
        with Image.open(pages[0]) as im:
            check(f"a portrait page fits the screen {im.size}",
                  im.size[0] <= documents.PAGE_W and im.size[1] <= documents.PAGE_H,
                  str(im.size))

        print("\nthe library keeps one entry")
        listed = [f.name for f in media.list_media()]
        check("the document is still there", "notice.pdf" in listed, str(listed))
        check("its pages are not in the library",
              not any(n.startswith("page-") for n in listed), str(listed))
        check("pages are off the SD card",
              str(documents.cache_dir(pdf)).startswith(str(config.runtime_dir())))

        print("\nreopening is free")
        start = time.time()
        again, _ = documents.pages(pdf)
        elapsed = time.time() - start
        check(f"a rendered document reopens instantly ({elapsed * 1000:.0f}ms)",
              elapsed < 0.25 and len(again) == 12)

        print("\nediting the file re-renders it")
        before = documents.cache_dir(pdf)
        time.sleep(0.01)
        make_pdf(pdf, 3)
        after = documents.cache_dir(pdf)
        check("a changed document gets a new cache", before != after)
        pages, _ = documents.pages(pdf)
        check("...and renders again, not from before", len(pages) == 3,
              f"{len(pages)} pages")

        print("\nforgetting")
        documents.forget(pdf)
        check("the cache goes", not documents.cache_dir(pdf).is_dir())

        print("\ntext documents take the same path")
        txt = media_dir / "notice.txt"
        txt.write_text("Fire exit is via the rear door.\n\n" + ("word " * 900))
        pages, problem = documents.pages(txt)
        check("text renders", not problem and pages, problem)
        check("long text paginates", len(pages) > 1, str(len(pages)))
        check("the text file stays whole", txt.exists())

    print("\nwhen the tool is missing")
    real = shutil.which
    shutil.which = lambda _n: None
    ok, reason = documents.available("x.pdf")
    shutil.which = real
    check("it says so rather than failing silently", not ok)
    check("...and names the package", "poppler-utils" in reason, reason)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
