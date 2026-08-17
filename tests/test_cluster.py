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

from pistreamer import cluster, syncplay  # noqa: E402

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


nodes = [Node("A"), Node("B"), Node("C", ready=False)]
clock = {"pos": 0.0}


def prepare(node, item):
    node.prepared.append(item)
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
cond.start(["one.mp4", "two.mp4"], nodes, loop=False)
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

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
