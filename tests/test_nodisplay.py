"""A node with nothing plugged in must say so, not loop the backoff.

The symptom was "failed to set pipeline to PLAYING" repeating for ever with
width and height null: HDMI reports no modes when the cable is out, so there is
no mode to set. Nothing in that told the operator the cause.

    python3 tests/test_nodisplay.py
"""
import os, sys, tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-nodisp-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, display  # noqa: E402
from pistreamer.player import Player  # noqa: E402

pl = Player.__new__(Player)
cfg = config.Config()
conn = lambda name, modes: display.Connector(name=name, connected=bool(modes),
                                             modes=modes, current=None)
mode = display.Mode(width=1920, height=1080, refresh=60)

# Cable out: HDMI is listed but advertises nothing.
display.pick_connector = lambda pref="": conn("HDMI-A-1", [])
for m in ("idle", "local", "ndi", "stream", "web", "airplay"):
    try:
        Player._build_command(pl, cfg, m, "x")
        raise AssertionError(f"{m} should have refused with no modes")
    except RuntimeError as e:
        assert "nothing is plugged in" in str(e), e
        assert "cmdline.txt" in str(e), "must name the headless fix"
        assert "HDMI-A-1" in str(e), "must name the connector"

# No connectors at all is a machine without DRM, not a cable that fell out.
# The guard must stay quiet so the missing backend is what gets reported.
display.pick_connector = lambda pref="": None
try:
    Player._build_command(pl, cfg, "ndi", "SOME (Source)")
except RuntimeError as e:
    assert "nothing is plugged in" not in str(e), \
        f"display guard masked the real error: {e}"

def refuses_for_no_modes():
    """Did the no-modes guard fire? Anything further (no GStreamer on a dev
    box) is not what is being tested here."""
    try:
        Player._build_command(pl, cfg, "idle", "")
    except RuntimeError as e:
        return "nothing is plugged in" in str(e)
    return False


# Forced on from cmdline.txt: sysfs may say disconnected, but the kernel lists
# modes and will set them. That must not be refused — it is the headless fix.
display.pick_connector = lambda pref="": display.Connector(
    name="HDMI-A-1", connected=False, modes=[mode], current=None)
assert not refuses_for_no_modes(), "a forced connector must be allowed through"

# Writeback, which is what actually plays on a node with no cable.
display.pick_connector = lambda pref="": conn("Writeback-1", [mode])
assert not refuses_for_no_modes(), "writeback has modes and must be allowed"

print("ok")
