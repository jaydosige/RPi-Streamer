"""Tests that a stopped player really has stopped.

These exist because of a field report of "multiple audio tracks playing at
once" during playlist playback. The sequencer's own state machine turned out to
be clean — it never starts two segments — so the remaining ways audio can
outlive its segment are all about teardown:

  * the direct child is reaped but something else in its process group is
    still holding the sound card
  * the child ignores SIGTERM
  * a player from a previous, unclean run is still going and nothing is
    supervising it
  * a caller spawns without tearing down first

Each of those is covered here with a real process, not a mock — the bug is in
process handling, so mocking the processes would test nothing. No hardware,
no GStreamer, no sound card needed.

    python3 tests/test_teardown.py
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path as _Path

if not _Path("/proc").is_dir():
    # Stray-process detection walks /proc by design — it is how a player that
    # outlived its supervisor is found. Without one there is nothing to test.
    print("skipping: this test reads /proc, which this system does not have")
    raise SystemExit(0)
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-teardown-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
(TMP / "media").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, player as P  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def script(name: str, body: str) -> str:
    path = TMP / name
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


print("teardown")

# --- a player whose process group outlives its leader ----------------------
# mpv and the runner do not fork today, but "the child exited" has never been
# the same question as "the audio stopped", and this is the difference.
holder = script("holder.sh", """#!/usr/bin/env bash
bash -c 'exec -a pistreamer-audio-holder sleep 300' &
bash -c 'exec -a pistreamer-second-holder sleep 300' &
wait
""")
player = P.Player()
player._spawn_command([holder], "group test")
lead = player._proc.pid
time.sleep(0.6)
player._terminate()
time.sleep(0.3)
check("the process we started is gone", not P._pid_alive(lead))
check("nothing is left in its process group", not P._group_alive(lead))

# --- a player that ignores SIGTERM ----------------------------------------
stubborn = script("stubborn.sh", """#!/usr/bin/env bash
trap '' TERM
while true; do sleep 0.2; done
""")
player._spawn_command([stubborn], "stubborn test")
pid = player._proc.pid
time.sleep(0.4)
player._terminate(timeout=1.0)
check("a player ignoring SIGTERM is killed anyway", not P._pid_alive(pid))

# --- spawning over a live player ------------------------------------------
# Every caller is supposed to tear down first. If one ever forgets, the
# invariant must still hold, because the symptom is two soundtracks at once.
forever = script("forever.sh", """#!/usr/bin/env bash
while true; do sleep 0.2; done
""")
player._spawn_command([forever], "first")
first = player._proc.pid
time.sleep(0.3)
player._spawn_command([forever], "second — no terminate in between")
second = player._proc.pid
time.sleep(0.3)
check("spawning over a live player stops the old one", not P._pid_alive(first))
check("the new player is the one running", P._pid_alive(second) and first != second)
player._terminate()

# --- a stray from a previous run ------------------------------------------
stray = subprocess.Popen(
    ["bash", "-c", "exec -a 'python -m pistreamer.runner {}' sleep 300"],
    start_new_session=True,
)
time.sleep(0.4)
check("a stray player is recognised as ours", stray.pid in P._stray_players())

fresh = P.Player()
fresh.start()
time.sleep(0.6)
check("startup cleans up strays", not P._pid_alive(stray.pid))
check("the cleanup is reported in status", fresh.status()["strays_cleaned"] >= 1)
fresh.shutdown()
if P._pid_alive(stray.pid):
    stray.kill()

# --- and does not touch anything else ------------------------------------
# The scan matches on our own command signatures for exactly this reason: an
# over-broad sweep would kill the user's shell session.
innocent = subprocess.Popen(
    ["bash", "-c", "exec -a 'my-own-ssh-session' sleep 300"], start_new_session=True
)
time.sleep(0.3)
P.reap_strays()
time.sleep(0.2)
check("an unrelated process is left alone", P._pid_alive(innocent.pid))
innocent.kill()

# --- a zombie is not mistaken for a player -------------------------------
# Treating one as alive would make every teardown wait out its full timeout.
z = subprocess.Popen(["true"])
time.sleep(0.2)
check("a zombie counts as stopped", not P._pid_alive(z.pid))
z.wait()

# --- the sequencer must never have two segments running -------------------
# This is the check that told us where the audio overlap was NOT: the state
# machine advances cleanly, so the problem had to be in teardown.
print("\nplaylist sequencer")
from pistreamer import playlists  # noqa: E402

config.ensure_dirs()
for name in ("clip.mp4", "still.jpg"):
    (config.MEDIA_DIR / name).write_bytes(b"\0" * 2048)
playlists.save(playlists.Playlist(name="Mixed", items=[
    {"type": "file", "target": "clip.mp4", "duration": 1},
    {"type": "ndi", "target": "SOME-PC (Test)", "duration": 1},
    {"type": "file", "target": "still.jpg", "duration": 1},
], loop=True))
cfg = config.load()
cfg.local_playlist = "Mixed"
config.save(cfg)

log_path = TMP / "segments.log"
log_path.write_text("")
fake = script("fake_player.sh", f"""#!/usr/bin/env bash
echo "START $$" >> {log_path}
trap 'echo "STOP $$" >> {log_path}; exit 0' TERM
if [[ -n "${{FAKE_DURATION:-}}" ]]; then
  sleep "$FAKE_DURATION"; echo "STOP $$" >> {log_path}
else
  while true; do sleep 0.1; done
fi
""")


def fake_segment_command(self, cfg_, segment):
    if segment["type"] == "ndi":
        return [fake]  # like the runner: runs until killed
    # like mpv --length / --image-display-duration: exits by itself
    return ["env", f"FAKE_DURATION={segment['duration'] or 5}", fake]


P.Player._segment_command = fake_segment_command
P.Player._idle_command = lambda self, cfg_: [fake]

seq = P.Player()
seq.apply("local", "")
seq.start()
time.sleep(6)
seq.shutdown()

live, overlaps, starts = 0, 0, 0
for line in log_path.read_text().splitlines():
    if line.startswith("START"):
        starts += 1
        if live:
            overlaps += 1
        live += 1
    else:
        live = max(0, live - 1)
check("the playlist advanced through several segments", starts >= 3, f"{starts} segments")
check("never two segments running at once", overlaps == 0, f"{overlaps} overlaps")
check("nothing still playing after shutdown", live == 0, f"{live} left")

# --- what prepare() actually asks mpv for -------------------------------
# The conductor decides when an item ends, so the command has to let it: a
# still image must be held open rather than timing out on its own dwell
# counter, or every node blinks off at a slightly different moment.
print("\nsynchronised prepare")
(config.MEDIA_DIR / "slide.png").write_bytes(b"\0" * 2048)
(config.MEDIA_DIR / "clip.mp4").write_bytes(b"\0" * 2048)

captured = []
prep = P.Player()
prep._spawn_command = lambda cmd, what: captured.append(cmd)
prep._await_ready = lambda timeout=8.0: True

prep.prepare("slide.png", duration=20, image=True)
cmd = captured[-1]
check("an image is held open, not timed out locally",
      "--image-display-duration=inf" in cmd, " ".join(cmd))
check("an image is not given a --length", not any(c.startswith("--length") for c in cmd))
check("it starts paused, ready for the beat", "--pause=yes" in cmd)
check("only one image-display-duration is passed",
      sum(1 for c in cmd if c.startswith("--image-display-duration=")) == 1,
      " ".join(c for c in cmd if c.startswith("--image-display-duration")))

prep.prepare("clip.mp4", duration=12, image=False)
cmd = captured[-1]
check("a video with an explicit duration is cut to it", "--length=12" in cmd,
      " ".join(cmd))

prep.prepare("clip.mp4")
cmd = captured[-1]
check("a plain video is left to play to its end",
      not any(c.startswith("--length") for c in cmd), " ".join(cmd))

# --- a node stopped by hand stays stopped -------------------------------
prep.prepare("clip.mp4", session="session-one")
check("it takes part in a session normally",
      prep.prepare("clip.mp4", session="session-one")["ready"])
prep.apply("idle")            # the operator presses stop
declined = prep.prepare("clip.mp4", session="session-one")
check("after a local stop it refuses the rest of that session",
      not declined["ready"], str(declined))
check("...and says why, so the leader can report it",
      "stopped locally" in declined.get("reason", ""), str(declined))
check("...but joins the next session",
      prep.prepare("clip.mp4", session="session-two")["ready"])
prep._terminate()

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
