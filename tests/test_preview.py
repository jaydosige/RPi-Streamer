"""Tests for the output preview.

Two things decide whether this feature is acceptable on an appliance, and both
are asserted here rather than assumed.

The first is that watching costs nothing when nobody is watching. Capture
follows demand: a request for a frame is what keeps it running, and it stops on
its own shortly after the last one. If that ever stopped working the node would
quietly encode frames all night for a laptop somebody shut at 6pm.

The second is that frames never land on the SD card if there is anywhere else
to put them. The node already wrote a full-size JPEG there every three seconds,
which is around 4 GB a day; making that worse in the name of a nicer GUI would
be a poor trade. Where no tmpfs exists it still works, and says so.

    python3 tests/test_preview.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-preview-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import preview  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def main() -> int:
    print("what can be captured")
    # NDI and standby come off a branch of the GStreamer pipeline; local files
    # and streams are screenshotted out of mpv over its IPC socket.
    for mode in ("ndi", "idle", "local", "stream"):
        check(f"{mode} can be previewed", preview.supports(mode)[0])
    # Chromium and uxplay draw straight to the display. There is no way to read
    # a frame back out, and pretending otherwise would show a stale picture.
    for mode in ("web", "airplay"):
        ok, why = preview.supports(mode)
        check(f"{mode} cannot", not ok)
        check(f"...and says why in words an operator can act on", len(why) > 30, why)
    check("nothing playing is not previewable", not preview.supports("")[0])

    print("\ncapture follows demand")
    preview.release()
    check("nothing is captured before anyone asks", preview.wanted_interval() == 0.0)
    preview.request("fast")
    check("asking starts it", preview.wanted_interval() == 1.0)
    check("the runner is told the rate through a file it polls",
          preview.rate_path().read_text().strip() == "1.000")
    preview.request("slow")
    check("a second, slower watcher does not slow the first down",
          preview.wanted_interval() == 1.0, str(preview.wanted_interval()))
    preview.request("off")
    check("off means off", preview.wanted_interval() == 0.0)
    check("...and the runner is told to stop",
          preview.rate_path().read_text().strip() == "0.000")

    print("\nit stops on its own")
    preview.request("fast")
    # The case that matters: a browser that goes away without saying so. Nothing
    # sends a message here — the request simply ages out.
    preview._demand["until"] = time.monotonic() - 1
    check("a watcher that vanishes stops costing anything",
          preview.wanted_interval() == 0.0)
    preview.request("fast")
    preview.release()
    check("and it can be given up immediately", preview.wanted_interval() == 0.0)

    print("\nframes stay off the SD card where possible")
    # On a Pi this picks /run/pistreamer (systemd RuntimeDirectory) or /dev/shm.
    # This machine may have neither, in which case the fallback is what is
    # being checked instead.
    if preview.on_tmpfs():
        check(f"a tmpfs is in use ({preview.directory()})", True)
    else:
        check("no tmpfs here, so the fallback is reported honestly",
              preview.on_tmpfs() is False)
        check("...and summary() surfaces it so the GUI can warn",
              preview.summary("ndi")["tmpfs"] is False)

    print("\nthe frame itself")
    if not shutil.which("ffmpeg"):
        print("  (skipping — ffmpeg is not installed here)")
    else:
        preview.frame_path().parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc2=size={preview.WIDTH}x360:rate=1:duration=1",
             "-frames:v", "1", str(preview.frame_path())],
            check=True, capture_output=True, timeout=60)
        size = preview.frame_path().stat().st_size
        # A preview goes out over event Wi-Fi once a second. A full-size frame
        # is ~137 KB; scaled down it is nearer 15 KB, which is the whole reason
        # the branch scales before it encodes.
        check(f"a preview frame is small ({size} bytes)", size < 60_000, str(size))
        age = preview.age_s()
        check("its age is reported, so a frozen preview is visible as one",
              age is not None and age < 5, str(age))

    print("\nsummary is complete enough to drive the GUI")
    s = preview.summary("ndi")
    for key in ("supported", "watching", "interval_s", "rates", "age_s", "tmpfs"):
        check(f"summary has {key}", key in s)
    check("the rates offered are off/slow/fast", set(s["rates"]) == {"off", "slow", "fast"})

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
