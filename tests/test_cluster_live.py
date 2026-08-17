"""Two real nodes on one host: discovery, authentication and a file push.

The unit tests cover the protocol and the arithmetic. This covers the thing
they cannot: that two actual processes running the actual app find each other
over a real UDP socket, refuse each other when the group key differs, and can
copy media between themselves over HTTP.

It runs two app instances on loopback with different ports and state
directories. Broadcast to 255.255.255.255 is delivered on loopback in most
environments but not all, so each node also unicasts to the other via
cluster_extra_ips — which is the same escape hatch a managed switch that drops
broadcast needs, so it is worth exercising anyway.

    python3 tests/test_cluster_live.py

Skips cleanly if the beacon port cannot be bound (already in use, or a sandbox
with no UDP).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TMP = Path(tempfile.mkdtemp(prefix="pistreamer-live-"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def port_free() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 47600))
        s.close()
        return True
    except OSError:
        return False


if not port_free():
    print("skipping: UDP port 47600 is not bindable here")
    sys.exit(0)


def api(port: int, path: str, method="GET", body=None, key=None, timeout=30):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if key is not None:
        headers["X-Pistreamer-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def start_node(name: str, port: int, key: str, group: str, peer_ip: str) -> subprocess.Popen:
    state = TMP / name
    (state / "media").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(SRC),
        # Every hardware identifier is identical when both nodes run on one
        # host, so without this they would compute the same node id and each
        # would dismiss the other's beacon as its own echo.
        "PISTREAMER_NODE_ID": name,
        "PISTREAMER_STATE": str(state),
        "PISTREAMER_CONFIG": str(state / "config.json"),
        "PISTREAMER_MEDIA": str(state / "media"),
    })
    (state / "config.json").write_text(json.dumps({
        "device_name": name, "web_port": port, "cluster_enabled": True,
        "cluster_group": group, "cluster_key": key,
        "cluster_extra_ips": peer_ip, "autostart": False, "mode": "idle",
    }))
    code = (
        "import uvicorn\n"
        "from pistreamer.web import app\n"
        f"uvicorn.run(app, host='0.0.0.0', port={port}, log_level='error')\n"
    )
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)


KEY = "test-key"
procs = []
try:
    print("two nodes, same group")
    procs.append(start_node("NODE-A", 8111, KEY, "default", "127.0.0.1"))
    procs.append(start_node("NODE-B", 8112, KEY, "default", "127.0.0.1"))

    up = False
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            api(8111, "/api/cluster", timeout=3)
            api(8112, "/api/cluster", timeout=3)
            up = True
            break
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.4)
    check("both nodes answered", up)
    if not up:
        for p in procs:
            print((p.stderr.read() or b"").decode()[-2000:])
        raise SystemExit(1)

    # Discovery: allow several beacon intervals.
    seen_a = seen_b = []
    deadline = time.time() + 20
    while time.time() < deadline:
        seen_a = api(8111, "/api/cluster")["peers"]
        seen_b = api(8112, "/api/cluster")["peers"]
        if seen_a and seen_b:
            break
        time.sleep(0.5)
    check("A discovered B", any(p["name"] == "NODE-B" for p in seen_a),
          json.dumps(seen_a))
    check("B discovered A", any(p["name"] == "NODE-A" for p in seen_b),
          json.dumps(seen_b))
    check("a discovered peer reports its address and mode",
          bool(seen_a) and seen_a[0]["ip"] and "mode" in seen_a[0], json.dumps(seen_a))
    check("a node does not list itself as a peer",
          all(p["name"] != "NODE-A" for p in seen_a), json.dumps(seen_a))
    check("the beacon reports it is running",
          api(8111, "/api/cluster")["beacon"]["running"])
    check("beacons are being sent and received",
          api(8111, "/api/cluster")["beacon"]["sent"] > 0
          and api(8111, "/api/cluster")["beacon"]["received"] > 0)

    print("\nauthentication")
    try:
        api(8112, "/api/cluster/time", key="wrong-key", timeout=5)
        check("the wrong key is refused", False, "it was accepted")
    except urllib.error.HTTPError as exc:
        check("the wrong key is refused", exc.code == 401, f"HTTP {exc.code}")
    try:
        api(8112, "/api/cluster/time", timeout=5)
        check("a missing key is refused", False, "it was accepted")
    except urllib.error.HTTPError as exc:
        check("a missing key is refused", exc.code == 401, f"HTTP {exc.code}")
    reply = api(8112, "/api/cluster/time", key=KEY, timeout=5)
    check("the right key is accepted and returns a clock", "t" in reply, str(reply))

    print("\nclock offset between two real processes")
    # Both nodes share this host's clock, so the measured offset must be tiny.
    # This is the sanity check that catches a sign error or a units mistake:
    # if it were seconds instead of milliseconds, every start would be wrong.
    sys.path.insert(0, str(SRC))
    from pistreamer import cluster as cl  # noqa: E402

    peer = cl.Peer(id="b", name="NODE-B", ip="127.0.0.1", port=8112)
    offset, rtt = cl.measure_offset(peer, key=KEY)
    check("offset between processes on one host is near zero",
          offset is not None and abs(offset) < 0.05, f"{offset}")
    check("round trip is plausible on loopback",
          rtt is not None and rtt < 200, f"{rtt}ms")

    print("\npushing media from A to B")
    clip = TMP / "NODE-A" / "media" / "sync-clip.mp4"
    clip.write_bytes(os.urandom(512 * 1024))
    digest = cl.sha256_file(clip)
    api(8111, "/api/playlists", method="POST",
        body={"name": "Wall", "items": [{"type": "file", "target": "sync-clip.mp4"}]})
    result = api(8111, "/api/cluster/push", method="POST",
                 body={"playlist": "Wall"}, timeout=120)
    pushed = result["results"][0] if result["results"] else {}
    check("the push reported success", pushed.get("ok"), json.dumps(result))
    check("the file was listed as sent", "sync-clip.mp4" in pushed.get("sent", []),
          json.dumps(pushed))
    remote = api(8112, "/api/media?hashes=1", timeout=60)["files"]
    match = [f for f in remote if f["name"] == "sync-clip.mp4"]
    check("B now has the file", bool(match), json.dumps(remote))
    check("byte-for-byte identical, verified by hash",
          bool(match) and match[0]["sha256"] == digest,
          f"{match[0]['sha256'] if match else None} != {digest}")
    check("the playlist came across too",
          any(p["name"] == "Wall" for p in api(8112, "/api/playlists")["playlists"]))

    # A second push must send nothing: the point of hashing is not re-sending
    # gigabytes before every show.
    again = api(8111, "/api/cluster/push", method="POST",
                body={"playlist": "Wall"}, timeout=120)
    second = again["results"][0]
    check("a repeat push skips what is already there",
          second.get("skipped") == ["sync-clip.mp4"] and not second.get("sent"),
          json.dumps(second))

    print("\ncommanding the group")
    out = api(8111, "/api/cluster/command", method="POST",
              body={"action": "standby"}, timeout=30)
    check("every node accepted the command",
          all(r["ok"] for r in out["results"]) and len(out["results"]) == 2,
          json.dumps(out))

    print("\nidentify across the group")
    out = api(8111, "/api/cluster/identify", method="POST", body={"on": True}, timeout=30)
    check("identify was applied locally", out["identify"] is True, json.dumps(out))
    check("identify was propagated to the peer",
          out["peers"] and out["peers"][0]["ok"], json.dumps(out["peers"]))
    caption_a = (TMP / "NODE-A" / "overlay.txt").read_text()
    caption_b = (TMP / "NODE-B" / "overlay.txt").read_text()
    check("A's caption names A", caption_a.startswith("NODE-A"), repr(caption_a))
    check("B's caption names B, not A", caption_b.startswith("NODE-B"), repr(caption_b))
    check("the caption carries an address", len(caption_b.splitlines()) == 2,
          repr(caption_b))
    api(8111, "/api/cluster/identify", method="POST", body={"on": False}, timeout=30)
    check("identify off clears both captions",
          not (TMP / "NODE-A" / "overlay.txt").read_text().strip()
          and not (TMP / "NODE-B" / "overlay.txt").read_text().strip())

    print("\na node in a different group")
    procs.append(start_node("NODE-X", 8113, KEY, "other-show", "127.0.0.1"))
    time.sleep(8)
    names = [p["name"] for p in api(8111, "/api/cluster")["peers"]]
    check("a node in another group is not discovered", "NODE-X" not in names, str(names))
    check("...and the beacons it sent were counted as rejected",
          api(8111, "/api/cluster")["beacon"]["rejected"] > 0)

finally:
    for proc in procs:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
