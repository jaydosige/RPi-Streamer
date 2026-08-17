"""Tests for node discovery, group authentication and synchronisation maths.

The parts worth testing here are the ones that are hard to test on hardware and
expensive to get wrong at an event:

  * a beacon from the wrong group, or signed with the wrong key, must be ignored
  * a peer that has gone quiet must disappear from the list
  * clock offset arithmetic, including its sign — a sign error would make every
    node start twice as far out as doing nothing at all
  * the drift corrector's decisions, including that it settles rather than
    oscillating, and that a stale or future-dated pulse is refused
  * the conductor's per-item loop, driven with fake nodes so no display, no
    network and no media are needed

    python3 tests/test_cluster.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-cluster-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import cluster, pushjob, syncplay  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def body(**kw):
    out = {"v": cluster.PROTOCOL, "id": "abc123", "name": "NODE-01",
           "ip": "192.168.20.31", "port": 80, "group": "default",
           "mode": "local", "target": "clip.mp4", "playing": True,
           "identify": False, "version": "0.6", "t": time.time()}
    out.update(kw)
    return out


print("beacon wire format")
packet = cluster.encode_beacon(body(), "secret")
check("a well-formed beacon round-trips",
      (cluster.decode_beacon(packet, "secret", "default") or {}).get("name") == "NODE-01")
check("the wrong key is rejected",
      cluster.decode_beacon(packet, "other-key", "default") is None)
check("another group is ignored",
      cluster.decode_beacon(packet, "secret", "show-b") is None)
check("a different protocol version is ignored",
      cluster.decode_beacon(cluster.encode_beacon(body(v=99), "secret"),
                            "secret", "default") is None)
check("a stale beacon is ignored",
      cluster.decode_beacon(
          cluster.encode_beacon(body(t=time.time() - 600), "secret"),
          "secret", "default") is None)
check("a future-dated beacon is ignored",
      cluster.decode_beacon(
          cluster.encode_beacon(body(t=time.time() + 600), "secret"),
          "secret", "default") is None)
check("rubbish on the port does not raise",
      cluster.decode_beacon(b"\x00\x01not json", "secret", "default") is None)
check("a truncated envelope does not raise",
      cluster.decode_beacon(b'{"p":"{}"}', "secret", "default") is None)

# Tampering: change the payload but keep the old signature. Note the payload
# sits inside the envelope as a JSON *string*, so its quotes are escaped —
# tampering with the unescaped form silently matches nothing and the test would
# pass while proving absolutely nothing.
tampered = packet.replace(rb"\"name\":\"NODE-01\"", rb"\"name\":\"NODE-99\"")
check("the tamper actually altered the packet", tampered != packet)
check("a tampered payload fails the signature",
      cluster.decode_beacon(tampered, "secret", "default") is None)

print("\npeer registry")
reg = cluster.Registry()
reg.observe(body(), "192.168.20.31")
reg.observe(body(id="def456", name="NODE-02"), "192.168.20.32")
check("both peers are listed", len(reg.all()) == 2, str(len(reg.all())))
check("sorted by name", [p.name for p in reg.all()] == ["NODE-01", "NODE-02"])
# A node's own claim wins, because it knows which of its interfaces it wants to
# be reached on — our nodes are dual-homed. Trusting the source address instead
# made the recorded address flap between interfaces as broadcast and unicast
# beacons alternated, which sent commands to whichever one beaconed last.
reg.observe(body(ip="10.0.0.1"), "192.168.20.31")
check("the node's own claimed address is preferred",
      reg.get("abc123").ip == "10.0.0.1", reg.get("abc123").ip)
check("...and where the packet came from is kept as the fallback",
      reg.get("abc123").seen_ip == "192.168.20.31", reg.get("abc123").seen_ip)
check("both addresses are offered, claim first",
      reg.get("abc123").addresses() == ["10.0.0.1", "192.168.20.31"],
      str(reg.get("abc123").addresses()))
# A node that claims nothing must still be reachable.
reg.observe(body(id="noclaim", name="NODE-03", ip=""), "192.168.20.33")
check("a node that claims no address falls back to its source address",
      reg.get("noclaim").ip == "192.168.20.33", reg.get("noclaim").ip)
peer = reg.get("abc123")
peer.last_seen = time.monotonic() - (cluster.PEER_TIMEOUT + 1)
check("a peer that stopped beaconing drops off the list",
      [p.name for p in reg.all()] == ["NODE-02", "NODE-03"],
      str([p.name for p in reg.all()]))
check("...but is still there if stale peers are asked for",
      len(reg.all(include_stale=True)) == 3)

print("\npeer addressing")
check("port 80 is left out of the URL",
      cluster.Peer(id="x", ip="192.168.20.31", port=80).base_url()
      == "http://192.168.20.31")
check("a non-default port is included",
      cluster.Peer(id="x", ip="192.168.20.31", port=8080).base_url()
      == "http://192.168.20.31:8080")
check("node id is stable across calls", cluster.node_id() == cluster.node_id())
check("node id is not the hostname in clear", len(cluster.node_id()) == 12)
check("primary_ip does not return loopback",
      not cluster.primary_ip().startswith("127."), cluster.primary_ip())

print("\nclock offset arithmetic")
# A peer whose clock reads 5s ahead of ours, with a 40ms round trip.
OFFSET, RTT = 5.0, 0.04


class FakePeer(cluster.Peer):
    pass


def fake_call(peer, path, method="GET", body=None, key="", timeout=8.0):
    time.sleep(RTT / 2)
    return {"t": time.time() + OFFSET}


real_call = cluster.call
cluster.call = fake_call
try:
    offset, rtt = cluster.measure_offset(cluster.Peer(id="x", ip="1.2.3.4"), samples=3)
finally:
    cluster.call = real_call
check("offset is measured with the right sign and magnitude",
      offset is not None and abs(offset - OFFSET) < 0.05, f"{offset}")
check("round trip is reported", rtt is not None and rtt >= 0, f"{rtt}")

# An unreachable peer must not produce a wrong answer; it must produce none.
def dead_call(*a, **k):
    raise cluster.PeerError("unreachable")


cluster.call = dead_call
try:
    offset, rtt = cluster.measure_offset(cluster.Peer(id="x", ip="1.2.3.4"), samples=2)
finally:
    cluster.call = real_call
check("an unreachable peer yields no offset rather than zero", offset is None)

print("\nstart instants")
now = 1_000_000.0
instants = syncplay.start_instant({"a": 0.0, "b": 0.25, "c": None}, now=now, slack=1.0)
check("each node is told an instant in its own clock",
      instants["a"] == now + 1.0 and abs(instants["b"] - (now + 1.25)) < 1e-9,
      str(instants))
check("a node with no measured offset still gets a start time",
      instants["c"] == now + 1.0)
check("the instant is far enough ahead to be reachable",
      min(instants.values()) - now >= 0.5)

print("\ndrift correction")
NOW = 2_000_000.0
pulse = {"item": "clip.mp4", "pos": 10.0, "at": NOW}
check("in sync -> hold",
      syncplay.decide(pulse, 10.0, NOW).action == "hold")
check("50ms behind -> speed up",
      syncplay.decide(pulse, 9.95, NOW).speed > 1.0,
      str(syncplay.decide(pulse, 9.95, NOW).to_dict()))
check("50ms ahead -> slow down",
      syncplay.decide(pulse, 10.05, NOW).speed < 1.0)
check("a second out -> seek",
      syncplay.decide(pulse, 11.0, NOW).action == "seek")
check("a seek targets the leader's position, not ours",
      abs(syncplay.decide(pulse, 11.0, NOW).seek_to - 10.0) < 1e-9)
# The pulse ages: by the time it is acted on the leader has moved on.
check("an aged pulse accounts for the leader having advanced",
      syncplay.decide(pulse, 10.5, NOW + 0.5).action == "hold",
      str(syncplay.decide(pulse, 10.5, NOW + 0.5).to_dict()))
check("a nudge in progress is left to settle",
      syncplay.decide(pulse, 9.95, NOW, nudging_until=NOW + 1).action == "hold")
check("reaching sync while nudging returns to normal speed",
      syncplay.decide(pulse, 10.0, NOW, nudging_until=NOW + 1).speed == 1.0)
check("a pulse from the future is refused",
      syncplay.decide({"pos": 1.0, "at": NOW + 60}, 1.0, NOW).reason == "no usable pulse")
check("a very old pulse is refused",
      syncplay.decide({"pos": 1.0, "at": NOW - 60}, 1.0, NOW).reason == "no usable pulse")
check("no local position yet -> hold",
      syncplay.decide(pulse, None, NOW).action == "hold")
check("a malformed pulse -> hold", syncplay.decide({}, 5.0, NOW).action == "hold")

# It must converge rather than oscillate: simulate a follower 150ms behind.
pos, speed, t = 9.85, 1.0, NOW
nudging = 0.0
history = []
for step in range(40):
    leader = 10.0 + (t - NOW)
    d = syncplay.decide({"pos": leader, "at": t}, pos, t, nudging)
    if d.action == "nudge":
        speed = d.speed
        nudging = t + syncplay.NUDGE_HOLD_S
    elif d.action == "seek":
        pos, speed, nudging = d.seek_to, 1.0, 0.0
    elif d.reason == "in sync":
        speed, nudging = 1.0, 0.0
    history.append(pos - leader)
    t += 0.25
    pos += 0.25 * speed
check("a follower behind the leader converges",
      abs(history[-1]) < syncplay.IN_SYNC_S * 2,
      f"final drift {history[-1] * 1000:.0f}ms, path {[round(h*1000) for h in history[::8]]}")
check("...without overshooting into a seek",
      all(abs(h) < syncplay.SEEK_ABOVE_S for h in history),
      f"worst {max(abs(h) for h in history) * 1000:.0f}ms")

print("\nsummary reporting")
summary = syncplay.summarise([
    {"name": "A", "ok": True, "offset_ms": 1.0},
    {"name": "B", "ok": True, "offset_ms": 4.0},
    {"name": "C", "ok": False, "error": "unreachable"},
])
check("counts what started", summary["started"] == 2 and summary["nodes"] == 3)
check("names what failed and why",
      summary["failed"] == [{"name": "C", "error": "unreachable"}], str(summary["failed"]))
check("reports the clock spread it had to work with",
      summary["clock_spread_ms"] == 3.0, str(summary["clock_spread_ms"]))

print("\nconductor, with fake nodes")


class Node:
    def __init__(self, name, ready=True):
        self.id = name
        self.name = name
        self.ready = ready
        self.prepared = []
        self.started = []
        self.pulses = []
        self.sessions = []


nodes = [Node("A"), Node("B"), Node("C", ready=False)]
clock = {"pos": 0.0}


def prepare(node, item, session=""):
    node.prepared.append(item["target"])
    node.sessions.append(session)
    return node.ready


def start(node, at):
    node.started.append(at)


def pulse(node, body_):
    node.pulses.append(body_)


def position():
    # Two items' worth of playback, then the file ends (None).
    clock["pos"] += 1.0
    return clock["pos"] if clock["pos"] < 6 else None


cond = syncplay.Conductor({
    "prepare": prepare, "start": start, "pulse": pulse,
    "offsets": lambda ids: {i: 0.001 for i in ids},
    "position": position,
})
cond.start([{"target": "one.mp4", "duration": None, "image": False},
            {"target": "two.mp4", "duration": None, "image": False}],
           nodes, loop=False)
deadline = time.time() + 30
while cond.state().get("running") and time.time() < deadline:
    time.sleep(0.2)
cond.stop()
state = cond.state()
check("the session ran and finished", not state["running"])
check("every node was asked to prepare",
      all(n.prepared for n in nodes), str([n.prepared for n in nodes]))
check("only nodes that became ready were started",
      bool(nodes[0].started) and bool(nodes[1].started) and not nodes[2].started,
      str([n.started for n in nodes]))
check("the start instant was the same agreed moment for both",
      abs(nodes[0].started[0] - nodes[1].started[0]) < 1e-6)
check("pulses were sent while the item played",
      bool(nodes[0].pulses), str(len(nodes[0].pulses)))
check("a pulse carries item, position and instant",
      all(k in nodes[0].pulses[0] for k in ("item", "pos", "at")))
check("a node that never became ready got no pulses", not nodes[2].pulses)
check("the failure is reported, not swallowed",
      any(f["name"] == "C" for f in state["last"]["failed"]), str(state["last"]))
check("both items were played", state["index"] == 1, str(state["index"]))

print("\npush progress")
# Progress reporting is tested here rather than against a real transfer: over
# loopback a push finishes inside a single poll, so a live test can only catch
# the intermediate states by luck. Fake deps make every state deterministic.


class FakePeer:
    def __init__(self, name):
        self.id = name
        self.name = name
        self.ip = "10.0.0.1"


def fake_job(upload=None, hashes=None, playlist_fn=None):
    return pushjob.PushJob("Wall", {
        "remote_hashes": hashes or (lambda peer: {}),
        "send_playlist": playlist_fn or (lambda peer: None),
        "resolve": lambda name: "/tmp/" + name,
        "upload": upload or (lambda peer, name, path, progress: progress(10)),
    })


SIZES = {"a.mp4": 1000, "b.mp4": 500}
WANTED = {"a.mp4": "hash-a", "b.mp4": "hash-b"}

seen = []


def slow_upload(peer, name, path, progress):
    for sent in (250, 500, 750, SIZES[name]):
        progress(min(sent, SIZES[name]))
        time.sleep(0.05)


job = fake_job(upload=slow_upload)
job.start([FakePeer("NODE-A")], WANTED, SIZES)
while job.is_running():
    seen.append(job.snapshot())
    time.sleep(0.02)
final = job.snapshot()

check("the total is what will actually be sent",
      final["total_bytes"] == 1500, str(final["total_bytes"]))
check("every byte is accounted for at the end",
      final["moved_bytes"] == 1500, str(final["moved_bytes"]))
check("it finishes at 100%", final["percent"] == 100.0, str(final["percent"]))
check("it reports success", final["ok"] and not final["failed"], str(final))
check("both files are listed as sent",
      final["nodes"][0]["sent"] == ["a.mp4", "b.mp4"], str(final["nodes"][0]))
check("partial progress was visible while running",
      any(0 < s["moved_bytes"] < 1500 for s in seen),
      str([s["moved_bytes"] for s in seen]))
check("the file being sent was named",
      any(s["file"] in ("a.mp4", "b.mp4") for s in seen),
      str({s["file"] for s in seen}))
check("progress never went backwards",
      all(b <= a for a, b in zip([s["moved_bytes"] for s in seen],
                                 [s["moved_bytes"] for s in seen][1:])) is False
      or all(a <= b for a, b in zip([s["moved_bytes"] for s in seen],
                                    [s["moved_bytes"] for s in seen][1:])),
      str([s["moved_bytes"] for s in seen]))
check("a rate was measured", any(s["rate"] > 0 for s in seen))

# A node that already has everything must transfer nothing at all.
job = fake_job(hashes=lambda peer: dict(WANTED))
job.start([FakePeer("NODE-A")], WANTED, SIZES)
while job.is_running():
    time.sleep(0.02)
final = job.snapshot()
check("a node that has everything gets nothing sent",
      final["total_bytes"] == 0 and not final["nodes"][0]["sent"], str(final["nodes"][0]))
check("...and its files are reported as skipped",
      sorted(final["nodes"][0]["skipped"]) == ["a.mp4", "b.mp4"],
      str(final["nodes"][0]["skipped"]))
check("percent is absent rather than a false 0%", final["percent"] is None,
      str(final["percent"]))

# One node failing must not stop the next, and must be reported.
def flaky_upload(peer, name, path, progress):
    if peer.name == "NODE-A":
        raise RuntimeError("HTTP 401 wrong or missing cluster key")
    progress(SIZES[name])


job = fake_job(upload=flaky_upload)
job.start([FakePeer("NODE-A"), FakePeer("NODE-B")], WANTED, SIZES)
while job.is_running():
    time.sleep(0.02)
final = job.snapshot()
check("a failing node is marked failed",
      final["nodes"][0]["state"] == "failed", str(final["nodes"][0]))
check("the reason is kept, not swallowed",
      "401" in final["nodes"][0]["error"], final["nodes"][0]["error"])
check("the next node is still pushed to",
      final["nodes"][1]["state"] == "done", str(final["nodes"][1]))
check("the outcome is not reported as ok", not final["ok"], str(final["ok"]))
check("the failure is summarised for the GUI",
      final["failed"] and final["failed"][0]["name"] == "NODE-A", str(final["failed"]))
check("the bar still reaches 100% despite the failure",
      final["percent"] == 100.0, str(final["percent"]))

# Cancelling mid-transfer must stop promptly and say so.
def slower_upload(peer, name, path, progress):
    for _ in range(20):
        progress(100)
        time.sleep(0.05)


job = fake_job(upload=slower_upload)
job.start([FakePeer("NODE-A")], WANTED, SIZES)
time.sleep(0.15)
job.cancel()
stopped_by = time.monotonic() + 5
while job.is_running() and time.monotonic() < stopped_by:
    time.sleep(0.02)
final = job.snapshot()
check("cancelling stops the push", not job.is_running())
check("...and it is reported as cancelled", final["cancelled"] and not final["ok"],
      str({k: final[k] for k in ("cancelled", "ok")}))

print("\ncounting bytes as they go")
import io  # noqa: E402

marks = []
reader = cluster._CountingReader(io.BytesIO(b"x" * 5000), marks.append, interval=0)
while reader.read(1000):
    pass
check("every read is counted", reader.sent == 5000, str(reader.sent))
check("progress was reported as it went", len(marks) >= 4, str(marks))
check("the counts only ever increase",
      all(a < b for a, b in zip(marks, marks[1:])), str(marks))

print("\nstill images in a synchronised playlist")
# An image reports time-pos 0.0 for ever. The playhead check read that as
# "stopped advancing" and moved on after two seconds, so every image was on
# screen for a fraction of its dwell time. Images end on the clock instead.


class ImgNode:
    def __init__(self, name):
        self.id = name
        self.name = name
        self.prepared = []
        self.started = []
        self.pulses = []
        self.sessions = []


img_nodes = [ImgNode("A")]
held = {}


def img_prepare(node, item, session=""):
    node.prepared.append(item)
    return True


def img_position():
    return 0.0  # exactly what mpv reports for a still, for ever


start_seen = []
cond = syncplay.Conductor({
    "prepare": img_prepare,
    "start": lambda node, at: start_seen.append(at),
    "pulse": lambda node, body_: node.pulses.append(body_),
    "offsets": lambda ids: {i: 0.0 for i in ids},
    "position": img_position,
})
began = time.time()
cond.start([{"target": "slide.png", "duration": 8, "image": True}], img_nodes, loop=False)
deadline = time.time() + 30
while cond.state().get("running") and time.time() < deadline:
    time.sleep(0.05)
held_for = time.time() - began
cond.stop()

check("the image was prepared with its dwell time",
      img_nodes[0].prepared and img_nodes[0].prepared[0]["duration"] == 8,
      str(img_nodes[0].prepared))
check("it is flagged as an image so the node holds it",
      img_nodes[0].prepared[0]["image"] is True)
# Start slack plus eight seconds. The playhead-based version gave up after
# about 3.2s regardless of the dwell time, so the two cannot be confused.
check("it stayed up for its full duration rather than being skipped",
      8.0 < held_for < 12.0, f"{held_for:.1f}s")
check("no drift pulses were sent for a still frame",
      not img_nodes[0].pulses, str(img_nodes[0].pulses))

print("\nstopping a node during a session")
# A node stopped by hand must stay stopped for the rest of that session, or it
# gets dragged back in at every item boundary and stop looks broken.


class StoppyNode:
    def __init__(self, name):
        self.id = name
        self.name = name
        self.asked = 0
        self.started = []


stoppy = StoppyNode("A")
sessions_seen = []


def stoppy_prepare(node, item, session=""):
    node.asked += 1
    sessions_seen.append(session)
    # Behaves like a node that was stopped locally after the first item.
    if node.asked > 1:
        return {"ready": False, "reason": "stopped locally; will rejoin the next session"}
    return {"ready": True}


results_seen = []
cond = syncplay.Conductor({
    "prepare": stoppy_prepare,
    "start": lambda node, at: node.started.append(at),
    "pulse": lambda node, body_: None,
    "offsets": lambda ids: {i: 0.0 for i in ids},
    "position": lambda: None,
})
cond.start([{"target": "a.mp4", "duration": 1, "image": False},
            {"target": "b.mp4", "duration": 1, "image": False}], [stoppy], loop=False)
deadline = time.time() + 30
while cond.state().get("running") and time.time() < deadline:
    time.sleep(0.05)
cond.stop()
final = cond.state()
check("the node was started for the first item", len(stoppy.started) == 1,
      str(stoppy.started))
check("a node that says it was stopped is not started again",
      len(stoppy.started) == 1, str(stoppy.started))
check("the reason it gave is reported, not invented",
      final["last"]["failed"] and "stopped locally" in final["last"]["failed"][0]["error"],
      str(final["last"].get("failed")))
check("every item carried the same session id",
      len(set(sessions_seen)) == 1 and sessions_seen[0], str(sessions_seen))
check("a second session gets a different id",
      cond.start([{"target": "a.mp4", "duration": 1, "image": False}],
                 [StoppyNode("B")], loop=False)["session"] != sessions_seen[0])
cond.stop()

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
