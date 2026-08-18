"""Overlays on the mpv side, checked by looking at the screen.

Two bugs live here, and neither is visible from the API:

  * The identify caption was pushed into mpv over IPC, once. mpv is not one
    long-lived process — it is replaced on every file, every playlist segment
    and every standby switch — so the caption survived exactly until the next
    item began. Which is when somebody hunting for a node is most likely to be
    looking at the screen.
  * The guest QR appeared in the operator's browser and nowhere the room could
    see it.

Both are claims about pixels, so this drives real mpv under Xvfb, takes real
screenshots through mpv's own IPC, and decodes the QR out of them. The only
surgery is swapping the DRM video output for X11 — everything else is the
player's own code path.

    python3 tests/test_mpvoverlay.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-mpvov-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PASS, FAIL = [], []
SCREEN_W, SCREEN_H = 1280, 720


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}{'  — ' + detail if detail and not cond else ''}")


def need(*tools):
    missing = [t for t in tools if shutil.which(t) is None]
    return missing


missing = need("mpv", "Xvfb", "ffmpeg")
if missing:
    print(f"SKIP: not installed: {', '.join(missing)}. "
          f"This test needs mpv, Xvfb and ffmpeg to look at a real screen.")
    sys.exit(0)
try:
    import cv2  # noqa: E402
    import numpy as np  # noqa: E402
except ImportError as exc:
    print(f"SKIP: {exc}. Install opencv-python-headless and numpy to decode "
          f"the QR out of a screenshot.")
    sys.exit(0)

from pistreamer import config, guest, mpvipc  # noqa: E402
from pistreamer.player import MODE_IDLE, MODE_LOCAL, Player, mpv_socket  # noqa: E402

ok, why = guest.overlay_available()
if not ok:
    print(f"SKIP: {why}")
    sys.exit(0)


def make_clip(path: Path, seconds: int = 30) -> None:
    subprocess.run(
        ["ffmpeg", "-v", "quiet", "-y", "-f", "lavfi",
         "-i", f"color=c=black:s=640x360:d={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def x11_only(real):
    """The player's own mpv command line, with the DRM output swapped for X11.

    Not a rewrite of the command: everything that matters here — the OSD flags,
    the IPC socket, the caption baked in at spawn — comes through untouched.
    """
    def patched(cfg, image_duration):
        out = []
        for arg in real(cfg, image_duration):
            if arg == "--vo=gpu":
                out.append("--vo=x11")
            elif arg == "--gpu-context=drm" or arg.startswith("--drm-"):
                continue
            else:
                out.append(arg)
        return out + ["--force-window=yes"]
    return patched


def shot(name: str):
    """A screenshot of the window, OSD and overlays included."""
    path = TMP / f"{name}.png"
    path.unlink(missing_ok=True)
    for _ in range(30):
        reply = mpvipc.command(str(mpv_socket()), "screenshot-to-file",
                               str(path), "window")
        if isinstance(reply, dict) and reply.get("error") == "success":
            break
        time.sleep(0.3)
    for _ in range(30):
        if path.exists() and path.stat().st_size > 0:
            time.sleep(0.2)
            img = cv2.imread(str(path))
            if img is not None:
                return img
        time.sleep(0.2)
    return None


def bright_in(img, y0, y1, x0, x1) -> int:
    if img is None:
        return -1
    region = img[y0:y1, x0:x1]
    return int((region.max(axis=2) > 170).sum())


def wait_for(fn, timeout=15.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def main() -> int:  # noqa: C901
    print(f"\nworkspace: {TMP}\n")
    (TMP / "media").mkdir(parents=True, exist_ok=True)
    first = TMP / "media" / "first.mp4"
    second = TMP / "media" / "second.mp4"
    make_clip(first)
    make_clip(second)

    xvfb = subprocess.Popen(
        ["Xvfb", ":91", "-screen", "0", f"{SCREEN_W}x{SCREEN_H}x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    os.environ["DISPLAY"] = ":91"
    time.sleep(1.5)

    config.update(device_name="STAGE-LEFT", audio_enabled=False, web_port=8080,
                  local_playlist="", autostart=False, guest_overlay=True,
                  identify=False)
    player = Player()
    player._mpv_base = x11_only(player._mpv_base)
    player.start()
    try:
        print("the identify caption")
        player.apply(MODE_LOCAL, "first.mp4")
        check("mpv is playing", wait_for(lambda: player.status()["running"]),
              player.status()["last_error"])
        check("its IPC socket came up",
              wait_for(lambda: mpv_socket().exists(), timeout=20))
        time.sleep(2.5)
        base = shot("plain")
        check("a screenshot could be taken", base is not None)
        top_left_plain = bright_in(base, 0, 200, 0, 640)
        check("nothing is captioned to start with", top_left_plain < 50,
              str(top_left_plain))

        player.set_identify(True, "STAGE-LEFT\n10.42.7.13")
        time.sleep(2.0)
        with_caption = shot("caption")
        top_left = bright_in(with_caption, 0, 200, 0, 640)
        check("the caption appears without restarting playback",
              top_left > 400, f"{top_left_plain} -> {top_left}")

        # The bug. A new item is a new mpv process, and a caption pushed over
        # IPC into the old one does not come with it.
        print("\nand it survives the next item")
        player.apply(MODE_LOCAL, "second.mp4")
        check("a new mpv is playing",
              wait_for(lambda: player.status()["running"]),
              player.status()["last_error"])
        wait_for(lambda: mpv_socket().exists(), timeout=20)
        time.sleep(3.0)
        after = shot("after-switch")
        top_left_after = bright_in(after, 0, 200, 0, 640)
        check("the caption is still on screen after the content changed",
              top_left_after > 400, f"{top_left} -> {top_left_after}")

        player.set_identify(False, "")
        time.sleep(2.0)
        cleared = bright_in(shot("uncaptioned"), 0, 200, 0, 640)
        check("switching identify off clears it", cleared < 50, str(cleared))

        print("\nthe guest QR panel")
        session = guest.open_session(30, note="Sam & Rowan")
        check("the panel is drawn for the screen",
              wait_for(lambda: guest.overlay_png_path().exists(), timeout=10))
        url = guest.share_url("", 0) or (guest.overlay_meta() or {}).get("url", "")
        check("...and knows the share URL", bool(url), url)
        check("mpv was given it",
              wait_for(lambda: player._mpv_panel is not None, timeout=15),
              str(player._mpv_panel))
        time.sleep(2.0)
        panel = shot("panel")
        check("a screenshot could be taken with the panel up", panel is not None)
        if panel is not None:
            grey = cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY)
            text, _, _ = cv2.QRCodeDetector().detectAndDecode(grey)
            check("the QR on the output actually scans",
                  text == (guest.overlay_meta() or {}).get("url"),
                  f"{text!r} vs {(guest.overlay_meta() or {}).get('url')!r}")
            h, w = grey.shape
            check("it is in the bottom-right corner, clear of the caption",
                  bright_in(panel, h // 2, h, w // 2, w) > 5000
                  and bright_in(panel, 0, h // 3, 0, w // 3) < 200,
                  f"br={bright_in(panel, h // 2, h, w // 2, w)} "
                  f"tl={bright_in(panel, 0, h // 3, 0, w // 3)}")

        print("\nboth at once")
        player.set_identify(True, "STAGE-LEFT\n10.42.7.13")
        time.sleep(2.0)
        both = shot("both")
        if both is not None:
            h, w = both.shape[:2]
            check("the caption and the panel do not fight over the screen",
                  bright_in(both, 0, 200, 0, 640) > 400
                  and bright_in(both, h // 2, h, w // 2, w) > 5000,
                  f"tl={bright_in(both, 0, 200, 0, 640)} "
                  f"br={bright_in(both, h // 2, h, w // 2, w)}")
        player.set_identify(False, "")

        print("\nand it goes when sharing closes")
        guest.close_session()
        check("the panel file is removed",
              wait_for(lambda: not guest.overlay_png_path().exists(), timeout=10))
        check("mpv is told to drop it",
              wait_for(lambda: player._mpv_panel is None, timeout=15),
              str(player._mpv_panel))
        time.sleep(2.0)
        gone = shot("gone")
        if gone is not None:
            h, w = gone.shape[:2]
            check("nothing is left in the corner",
                  bright_in(gone, h // 2, h, w // 2, w) < 200,
                  str(bright_in(gone, h // 2, h, w // 2, w)))

        print("\nan expired session takes its code down on its own")
        # The reason the panel is published from the supervisor and not from
        # the API: nobody presses anything when a session simply runs out.
        guest.open_session(30)
        wait_for(lambda: guest.overlay_png_path().exists(), timeout=10)
        s = guest.session()
        s.expires = time.time() - 1
        guest._save(s)
        check("the code disappears without anybody touching anything",
              wait_for(lambda: not guest.overlay_png_path().exists(), timeout=10))
        guest.close_session()
    finally:
        try:
            player.apply(MODE_IDLE)
        except Exception:  # noqa: BLE001
            pass
        player.shutdown()
        try:
            os.killpg(xvfb.pid, 15)
        except OSError:
            pass

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
