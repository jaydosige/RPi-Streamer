"""Playlist segments reuse the mpv already on screen instead of respawning it.

The black frame between items was process teardown plus handing DRM to a fresh
mpv, not decoding. This checks the decision that avoids it: file-after-file
reuses, anything crossing a backend boundary does not.

    python3 tests/test_gapless.py
"""
import os, sys, tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-gapless-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, mpvipc  # noqa: E402
from pistreamer.player import Player  # noqa: E402

sent = []
mpvipc.command = lambda sock, *a, **k: (sent.append(a),
                                        {"error": "success"})[1]

pl = Player.__new__(Player)
pl._proc = type("P", (), {"poll": lambda self: None})()   # a live process
seg = lambda **k: {"type": "file", "path": "/m/a.mp4", "duration": None,
                   "image": False, "target": "a.mp4", **k}

pl._segment_backend = "mpv"
assert pl._load_into_running_mpv(seg()) is True, "file after file must reuse"
assert sent[-1][0] == "loadfile" and sent[-1][2] == "replace", sent[-1]

sent.clear()
pl._load_into_running_mpv(seg(image=True, duration=7))
assert ("set_property", "image-display-duration", 7) in sent, sent
# ...set before the load, or the still is held for the previous item's time.
assert sent.index(("set_property", "image-display-duration", 7)) < \
       next(i for i, c in enumerate(sent) if c[0] == "loadfile"), sent

pl._segment_backend = "ndi"
assert pl._load_into_running_mpv(seg()) is False, "after NDI there is no mpv to reuse"
pl._segment_backend = "mpv"
assert pl._load_into_running_mpv(seg(type="ndi")) is False, "NDI needs the runner"
pl._proc = None
assert pl._load_into_running_mpv(seg()) is False, "nothing running to reuse"
pl._proc = type("P", (), {"poll": lambda self: 0})()      # exited
assert pl._load_into_running_mpv(seg()) is False, "a dead process is not reusable"

# A reused mpv must outlive its file, and --length cannot be set per loadfile.
cmd = Player._segment_command(pl, config.Config(), seg(duration=5))
assert "--idle=yes" in cmd, cmd
assert not any(c.startswith("--length") for c in cmd), cmd
assert "--prefetch-playlist=yes" in Player._mpv_base(pl, config.Config(), 10)

print("ok")
