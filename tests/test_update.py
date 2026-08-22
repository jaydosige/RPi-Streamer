"""The updater, against real git repositories.

Updating is the one feature that can brick a node from the GUI, so it is tested
against actual clones rather than mocks: a real remote, a real working copy, a
real `git fetch`, and a stand-in for install.sh that can be made to fail on
demand.

What matters here:
  * it reports honestly whether there is anything to update to
  * it applies only what was asked for, and records where it came from
  * local hand-edits on a node are parked, never lost
  * a failed install is reported as failed rather than as success
  * rollback returns the node to the commit it was on
  * a request from the web GUI cannot smuggle anything onto a git command line

    python3 tests/test_update.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib

if not os.access("/etc/systemd/system", os.W_OK):
    # The helper unit lives in /etc/systemd/system and this checks for it by
    # creating one. Without write access there is nothing to check.
    print("skipping: /etc/systemd/system is not writable here")
    raise SystemExit(0)
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "pistreamer-update"
TMP = Path(tempfile.mkdtemp(prefix="pistreamer-update-"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, **kw)


def commit(repo, message, **files):
    for name, body in files.items():
        (repo / name).write_text(body)
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e.st", "-c", "user.name=Test", "commit", "-m", message)


def run(action, *extra, state=None, conf=None, env=None):
    e = dict(os.environ)
    e.update({
        "PISTREAMER_STATE": str(state),
        "PISTREAMER_CONFIG_DIR": str(conf),
        # sudo is not available (and not needed) in a test container; the
        # updater only uses it to drop from root to the repo's owner.
        "PATH": f"{TMP}/fakebin:" + e["PATH"],
    })
    e.update(env or {})
    return subprocess.run([str(UPDATER), action, *extra],
                          capture_output=True, text=True, env=e, timeout=180)


def status(state):
    try:
        return json.loads((Path(state) / "update.status").read_text())
    except (OSError, ValueError):
        return {}


# A `sudo -u <user> <cmd>` stand-in: this test is not root, and the real thing
# only uses sudo to become the working copy's owner, which here is us already.
(TMP / "fakebin").mkdir()
(TMP / "fakebin" / "sudo").write_text(
    '#!/usr/bin/env bash\n'
    '# strip "-u <user>" and run the rest as ourselves\n'
    'while [[ "$1" == "-u" ]]; do shift 2; done\n'
    'exec "$@"\n')
(TMP / "fakebin" / "sudo").chmod(0o755)


def make_world(name, install_body='#!/usr/bin/env bash\necho "==> installing"\nexit 0\n'):
    """A remote, a clone of it, and the config the updater reads."""
    world = TMP / name
    origin, work = world / "origin.git", world / "work"
    state, conf = world / "state", world / "conf"
    for d in (state, conf):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)],
                   check=True)
    subprocess.run(["git", "clone", "--quiet", str(origin), str(work)],
                   capture_output=True, check=True)
    (work / "scripts").mkdir(exist_ok=True)
    (work / "install.sh").write_text(install_body)
    (work / "install.sh").chmod(0o755)
    commit(work, "first release", VERSION="1")
    git(work, "branch", "-M", "main")
    git(work, "push", "--quiet", "origin", "HEAD:main")
    git(work, "branch", "--set-upstream-to=origin/main", "main")
    (conf / "install.conf").write_text(
        f"REPO_DIR={work}\nREPO_USER={os.environ.get('USER', 'root')}\nDEFAULT_BRANCH=main\n")
    return world, origin, work, state, conf


def publish(origin, message, **files):
    """Push a new commit to the remote, as somebody else would."""
    clone = Path(tempfile.mkdtemp(dir=TMP))
    subprocess.run(["git", "clone", "--quiet", str(origin), str(clone / "c")],
                   capture_output=True, check=True)
    commit(clone / "c", message, **files)
    out = git(clone / "c", "push", "--quiet", "origin", "HEAD:main")
    assert out.returncode == 0, f"publishing {message!r} failed: {out.stderr}"


print("checking for updates")
world, origin, work, state, conf = make_world("check")
r = run("check", state=state, conf=conf)
st = status(state)
check("a check with nothing new succeeds", r.returncode == 0, r.stderr[-300:])
check("...and reports nothing to do", st.get("behind") == 0, json.dumps(st.get("behind")))
check("...and says so in the message", "up to date" in (st.get("message") or ""),
      st.get("message", ""))
check("it records what is running now", (st.get("current") or {}).get("subject") == "first release",
      json.dumps(st.get("current")))
check("it records when it looked", isinstance(st.get("checked_at"), (int, float)))

publish(origin, "fix the thing that broke", VERSION="2")
publish(origin, "and another fix", VERSION="3")
r = run("check", state=state, conf=conf)
st = status(state)
check("two new commits are counted", st.get("behind") == 2, json.dumps(st.get("behind")))
check("the newest is offered", (st.get("available") or {}).get("subject") == "and another fix",
      json.dumps(st.get("available")))
check("what is in the update is listed",
      [c["subject"] for c in st.get("commits", [])] == ["and another fix", "fix the thing that broke"],
      json.dumps(st.get("commits")))
check("checking does not move the working copy",
      (st.get("current") or {}).get("subject") == "first release",
      json.dumps(st.get("current")))

print("\napplying an update")
r = run("apply", state=state, conf=conf)
st = status(state)
check("apply succeeds", r.returncode == 0, r.stdout[-400:] + r.stderr[-400:])
check("it reports done", st.get("phase") == "done" and st.get("ok") is True, json.dumps(st.get("phase")))
check("the working copy moved to the newest commit",
      (work / "VERSION").read_text() == "3", (work / "VERSION").read_text())
check("the installer was run", any("installing" in line for line in st.get("log", [])),
      json.dumps(st.get("log")))
check("nothing is outstanding afterwards", st.get("behind") == 0, json.dumps(st.get("behind")))
check("where it came from is recorded for rollback",
      (st.get("previous") or {}).get("subject") == "first release",
      json.dumps(st.get("previous")))

print("\nrolling back")
r = run("rollback", state=state, conf=conf)
st = status(state)
check("rollback succeeds", r.returncode == 0, r.stderr[-300:])
check("the working copy is back where it was",
      (work / "VERSION").read_text() == "1", (work / "VERSION").read_text())
check("it says it rolled back", "rolled back" in (st.get("message") or ""), st.get("message", ""))

print("\nlocal edits on a node")
world2, origin2, work2, state2, conf2 = make_world("dirty")
publish(origin2, "upstream change", VERSION="2")
(work2 / "install.sh").write_text('#!/usr/bin/env bash\necho "==> installing"\n# hand fix\nexit 0\n')
(work2 / "notes.txt").write_text("something someone typed at 5pm")
r = run("apply", state=state2, conf=conf2)
st = status(state2)
check("an update over local edits still succeeds", r.returncode == 0, r.stderr[-300:])
check("the update was applied", (work2 / "VERSION").read_text() == "2")
stash = git(work2, "stash", "list").stdout
check("the local edits were parked, not discarded", "pistreamer update" in stash, stash)
restored = git(work2, "stash", "show", "-p", "stash@{0}").stdout
check("...and the parked copy still holds them",
      "hand fix" in restored or "notes.txt" in git(work2, "stash", "show", "--include-untracked",
                                                   "--name-only", "stash@{0}").stdout,
      restored[:200])

print("\nwhen the install fails")
world3, origin3, work3, state3, conf3 = make_world(
    "failing", install_body='#!/usr/bin/env bash\necho "==> installing"\necho "boom" >&2\nexit 1\n')
publish(origin3, "a release that will not install", VERSION="2")
r = run("apply", state=state3, conf=conf3)
st = status(state3)
check("a failed install exits non-zero", r.returncode != 0, str(r.returncode))
check("...and is reported as failed, not done",
      st.get("phase") == "failed" and st.get("ok") is False, json.dumps(st.get("phase")))
check("...with a message that says what to do",
      "roll back" in (st.get("message") or "").lower(), st.get("message", ""))
check("...and the previous version is still recorded",
      (st.get("previous") or {}).get("subject") == "first release",
      json.dumps(st.get("previous")))

print("\nrequests from the web GUI")
world4, origin4, work4, state4, conf4 = make_world("requests")
publish(origin4, "newer", VERSION="2")
# The request file is written by an unauthenticated GUI on an event network, so
# what it carries has to be treated as hostile: it ends up on a git command line.
(state4 / "update.request").write_text(json.dumps(
    {"action": "apply", "ref": "main; touch /tmp/pwned-by-update"}))
r = run("--from-request", state=state4, conf=conf4)
check("a ref containing a shell command is refused",
      not Path("/tmp/pwned-by-update").exists())
check("...and the node is still updated to the branch head",
      (work4 / "VERSION").read_text() == "2", (work4 / "VERSION").read_text())
check("the request file is consumed", not (state4 / "update.request").exists())

(state4 / "update.request").write_text(json.dumps({"action": "wat"}))
r = run("--from-request", state=state4, conf=conf4)
check("an unknown action fails cleanly", r.returncode != 0)
check("...and says which one", "wat" in (status(state4).get("message") or ""),
      status(state4).get("message", ""))

print("\ninstalled from a tarball")
world5 = TMP / "tarball"
(world5 / "state").mkdir(parents=True)
(world5 / "conf").mkdir(parents=True)
(world5 / "notrepo").mkdir(parents=True)
(world5 / "conf" / "install.conf").write_text(
    f"REPO_DIR={world5 / 'notrepo'}\nREPO_USER=root\n")
r = run("check", state=world5 / "state", conf=world5 / "conf")
check("a node with no git working copy refuses politely", r.returncode != 0)
check("...and explains why", "tarball" in r.stderr, r.stderr[-200:])

print("\nthe API half, without a root job")
# The service can only do two things: write a request and read a status. Both
# halves are exercised here against the real endpoints, with the root job stood
# in for by this test — which is exactly the separation the real system has.
import shutil  # noqa: E402

os.environ["PISTREAMER_CONFIG_DIR"] = str(TMP / "apiconf")
os.environ["PISTREAMER_STATE"] = str(TMP / "apistate")
os.environ["PISTREAMER_CONFIG"] = str(TMP / "apiconf" / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "apimedia")
for d in ("apiconf", "apistate", "apimedia"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402
from pistreamer import updates as U  # noqa: E402
from pistreamer.web import app  # noqa: E402

(TMP / "apiconf" / "build.json").write_text(json.dumps({
    "source": "git", "sha": "a" * 40, "short": "aaaaaaa",
    "subject": "the running version", "date": "2026-08-01T10:00:00+01:00",
    "repo": "/home/x/RPi-Streamer"}))

with TestClient(app) as client:
    r = client.get("/api/update")
    check("GET update -> 200", r.status_code == 200, r.text)
    body = r.json()
    check("it reports what is running",
          (body["current"] or {}).get("subject") == "the running version",
          json.dumps(body.get("current")))
    check("a git install is updatable", body["updatable"] is True)
    check("...but not while the helper is missing", body["helper"] is False)

    r = client.post("/api/update/check")
    check("check refuses when the helper is absent -> 409", r.status_code == 409, r.text)
    check("...and says how to fix it", "install.sh" in r.text, r.text[:200])

    # Arm the helper the way install.sh does.
    helper = pathlib.Path("/etc/systemd/system/pistreamer-update.path")
    made = False
    if not helper.exists():
        helper.parent.mkdir(parents=True, exist_ok=True)
        helper.write_text("# test\n")
        made = True
    try:
        r = client.post("/api/update/check")
        check("check is accepted once armed -> 200", r.status_code == 200, r.text)
        req = json.loads((TMP / "apistate" / "update.request").read_text())
        check("...and a request file is written for the root job",
              req["action"] == "check", json.dumps(req))

        # A node that is playing refuses to update without being told twice.
        (TMP / "apistate" / "update.request").unlink()
        from pistreamer.player import player as live_player  # noqa: E402
        live_player._status.running = True
        live_player._status.target = "opening-titles.mp4"
        r = client.post("/api/update/apply", json={"force": False})
        check("a node that is on air refuses to update -> 409", r.status_code == 409, r.text)
        check("...and names what is playing", "opening-titles" in r.text, r.text[:200])
        check("...and writes no request",
              not (TMP / "apistate" / "update.request").exists())

        r = client.post("/api/update/apply", json={"force": True})
        check("forcing it through is accepted", r.status_code == 200, r.text)
        req = json.loads((TMP / "apistate" / "update.request").read_text())
        check("...and asks for an apply", req["action"] == "apply", json.dumps(req))
        live_player._status.running = False

        # A running update must not be started twice.
        (TMP / "apistate" / "update.status").write_text(json.dumps(
            {"phase": "installing", "updated_at": time.time()}))
        r = client.post("/api/update/check")
        check("a second update while one runs -> 409", r.status_code == 409, r.text)
        check("the GUI is told it is busy", client.get("/api/update").json()["busy"] is True)

        # A phase left behind by a job that died must not lock the button.
        (TMP / "apistate" / "update.status").write_text(json.dumps(
            {"phase": "installing", "updated_at": time.time() - 7200}))
        check("a stale 'installing' does not lock updates for ever",
              client.get("/api/update").json()["busy"] is False)

        # Rollback needs somewhere to roll back to.
        (TMP / "apistate" / "update.status").write_text(json.dumps({"phase": "idle"}))
        r = client.post("/api/update/rollback")
        check("rollback with no recorded version -> 404", r.status_code == 404, r.text)
        (TMP / "apistate" / "update.status").write_text(json.dumps(
            {"phase": "idle", "previous": {"sha": "b" * 40, "short": "bbbbbbb"}}))
        r = client.post("/api/update/rollback")
        check("rollback with one recorded is accepted", r.status_code == 200, r.text)

        # The status poll carries the count, which is what drives the badge.
        (TMP / "apistate" / "update.status").write_text(json.dumps(
            {"phase": "idle", "behind": 3}))
        check("the status poll carries the update count",
              client.get("/api/status").json()["update"]["behind"] == 3)
    finally:
        if made:
            helper.unlink()

    # A node installed from a tarball has no remote to update from.
    (TMP / "apiconf" / "build.json").write_text(json.dumps({"source": "archive"}))
    check("an archive install reports itself as not updatable",
          client.get("/api/update").json()["updatable"] is False)
    r = client.post("/api/update/apply", json={"force": True})
    check("...and refuses to try -> 409", r.status_code == 409, r.text)
    check("...explaining why", "archive" in r.text, r.text[:200])

print("\nthe whole loop, end to end")
# systemd is not available here, so the path unit is stood in for by a thread
# that does exactly what it does: notice the request file and run the updater.
# Everything else is real — a real clone, a real remote, real HTTP.
import threading  # noqa: E402

world6, origin6, work6, state6, conf6 = make_world("e2e")
publish(origin6, "the update everyone is waiting for", VERSION="2")
(conf6 / "build.json").write_text(json.dumps({"source": "git"}))

watching = threading.Event()


def path_unit():
    """What pistreamer-update.path does, in fifteen lines."""
    while not watching.is_set():
        if (state6 / "update.request").exists():
            run("--from-request", state=state6, conf=conf6)
        time.sleep(0.2)


threading.Thread(target=path_unit, daemon=True).start()

# Those modules read their paths at import time, and the app half above has
# already imported them — so point them at this world directly rather than
# fighting the import system.
import importlib  # noqa: E402
from pistreamer import config as C  # noqa: E402
from pistreamer import updates as U2  # noqa: E402

C.STATE_DIR = state6
U2.CONFIG_DIR = conf6

helper = pathlib.Path("/etc/systemd/system/pistreamer-update.path")
made = False
if not helper.exists():
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("# test\n")
    made = True
try:
    U2.request("check")
    deadline = time.time() + 60
    while time.time() < deadline and not U2.status().get("checked_at"):
        time.sleep(0.2)
    st = U2.summary()
    check("a check asked for by the app comes back answered",
          st["behind"] == 1, json.dumps(st.get("behind")))
    check("...naming what the update contains",
          (st["commits"] or [{}])[0].get("subject") == "the update everyone is waiting for",
          json.dumps(st.get("commits")))

    U2.request("apply")
    deadline = time.time() + 120
    while time.time() < deadline and U2.status().get("phase") not in ("done", "failed"):
        time.sleep(0.2)
    st = U2.summary()
    check("an apply asked for by the app completes", st["phase"] == "done", json.dumps(st))
    check("...and the working copy really moved",
          (work6 / "VERSION").read_text() == "2", (work6 / "VERSION").read_text())
    check("...and the app now reports nothing outstanding", st["behind"] == 0)
    check("...and offers a way back", (st.get("previous") or {}).get("short") is not None,
          json.dumps(st.get("previous")))
    check("the log the GUI shows has real lines in it",
          any("install" in line for line in st.get("log", [])), json.dumps(st.get("log"))[:200])
finally:
    watching.set()
    if made:
        helper.unlink()

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
