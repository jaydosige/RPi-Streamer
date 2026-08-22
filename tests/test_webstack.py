"""Chromium needs something to draw onto, and Debian does not give it one.

A real node came back restarting the same fatal eleven times in four minutes:

    FATAL:ui/ozone/platform_selection.cc:46] Invalid ozone platform: drm

Debian builds chromium with the x11, wayland and headless ozone backends and
not drm, so on a box with no desktop it cannot reach the screen at all. cage —
a kiosk compositor that puts one window full screen straight onto KMS — is what
it draws onto. Installed is therefore not the same as usable, which is the
third time that distinction has cost something in this project.

    python3 tests/test_webstack.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-webstack-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config  # noqa: E402
from pistreamer.player import Player  # noqa: E402

PASS, FAIL = [], []
REAL_WHICH = shutil.which


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def pretend(chromium=True, cage=True):
    def fake(name):
        if name in ("chromium", "chromium-browser", "chromium-browser-stable"):
            return "/usr/bin/chromium" if chromium else None
        if name == "cage":
            return "/usr/bin/cage" if cage else None
        return REAL_WHICH(name)
    shutil.which = fake


def main() -> int:
    player = Player.__new__(Player)
    cfg = config.Config()

    print("with a compositor")
    pretend(chromium=True, cage=True)
    plan = player.browser_plan(cfg)
    check("it is usable", plan["ok"], str(plan))
    check("...via cage", plan["compositor"] == "cage")
    cmd = player._web_command(cfg, "http://d.local/x")
    check("cage runs the show", cmd[0] == "cage", str(cmd[:3]))
    check("...with chromium after --", cmd[1] == "--" and "chromium" in cmd[2])
    # wayland, because that is a backend Debian actually builds.
    check("chromium is told to use wayland", "--ozone-platform=wayland" in cmd)
    check("...and not drm", "--ozone-platform=drm" not in cmd)
    check("the URL is still last, after its own --",
          cmd[-1] == "http://d.local/x" and cmd[-2] == "--")

    print("\nwithout one")
    pretend(chromium=True, cage=False)
    plan = player.browser_plan(cfg)
    # The distinction that matters: the browser is right there, and it still
    # cannot show anything.
    check("chromium alone is NOT usable", not plan["ok"], str(plan))
    check("...though the browser is found", plan["browser"] is not None)
    check("...and the reason names the fix",
          "cage" in plan["reason"], plan["reason"])

    print("\nwith no browser at all")
    pretend(chromium=False, cage=False)
    plan = player.browser_plan(cfg)
    check("not usable", not plan["ok"])
    check("...and says so plainly", "chromium is not installed" in plan["reason"],
          plan["reason"])

    print("\ngiving up rather than looping")
    # The node in the bundle restarted the same fatal eleven times. A browser
    # that dies in under a second died of its configuration and will do so
    # again identically.
    from pistreamer import player as player_mod
    check("there is a fast-fail limit", player_mod._WEB_FAST_FAIL_LIMIT >= 2)
    check("...measured in seconds, not minutes", player_mod._WEB_FAST_FAIL <= 5)

    pretend(chromium=True, cage=False)
    player._logs = ["21:29:30 [1677:1677:0822/212930:FATAL:ui/ozone/"
                    "platform_selection.cc:46] Invalid ozone platform: drm"]
    player._status = type("S", (), {"since": __import__("time").time()})()
    player._web_fast_fails = player_mod._WEB_FAST_FAIL_LIMIT - 1
    proc = type("P", (), {"returncode": -6})()
    reason = player._web_exit_reason(proc)
    check("the ozone fatal is translated, not echoed",
          "cage" in reason and "-6" not in reason, reason)
    check("...and it stops trying", player._web_stuck is True)

    shutil.which = REAL_WHICH
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
