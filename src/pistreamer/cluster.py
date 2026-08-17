"""Finding other nodes, and talking to them.

A show is several Pis on one switch. Nobody wants to configure a list of IP
addresses on every unit before doors, so nodes announce themselves and discover
each other; and nobody wants to open six browser tabs, so any node can command
the rest of its group.

Design decisions worth keeping:

  * **UDP broadcast beacons, not mDNS.** NDI discovery on these same event
    networks has already cost us days, and it is mDNS-based. A 300-byte
    broadcast every two seconds on a fixed port is something you can see with
    tcpdump and reason about, and it needs no daemon. Unicast targets can be
    added for switches that drop broadcast — the same escape hatch NDI needs.
  * **Signed with a group key.** The beacon carries an HMAC over its payload
    and inbound commands must present the same key. This is not protection
    against a determined attacker on the wire; it stops a second show on the
    same VLAN, or a curious laptop, from stopping playback mid-set.
  * **No election, no leader daemon.** Whichever node's GUI you have open acts
    as leader for that operation. There is no cluster state to get out of sync,
    nothing to fail over, and a node that is switched off is simply absent.
  * **stdlib HTTP only.** This box may have no route to the internet and the
    installer should not need to fetch a client library, so peer calls use
    urllib. Large media pushes stream from a file object rather than being
    buffered, because playlists are gigabytes.

Time: the leader measures each follower's clock offset itself and sends every
instruction in *that follower's own clock*. Followers therefore never need to
know anything about the leader's clock, which removes the classic distributed
bug where two nodes disagree about which of them is wrong.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

log = logging.getLogger(__name__)

# Fixed port for beacons. Chosen well clear of the NDI range (5960+) so a
# packet capture on an event network is never ambiguous about what it is.
BEACON_PORT = 47600
BEACON_INTERVAL = 2.0
# A node is considered gone after this long without a beacon. Six missed
# beacons: long enough to ride out a Wi-Fi hiccup, short enough that the GUI
# does not offer to command a Pi somebody has unplugged.
PEER_TIMEOUT = 12.0
# Beacons older than this are ignored, which limits how long a captured packet
# stays useful for replay.
BEACON_MAX_AGE = 30.0
PROTOCOL = 1

# Header carrying the group key on inbound commands.
AUTH_HEADER = "X-Pistreamer-Key"


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------


def _hardware_serial() -> str:
    """The Pi's board serial, which is unique per unit.

    This is load-bearing and was found the hard way. /etc/machine-id looks like
    the obvious identity, but it is *copied* when an SD card is cloned — and
    cloning a working card is exactly how you deploy the second, third and
    fourth node. Identical ids make every node treat its neighbours' beacons as
    its own echo, so nothing is ever discovered and there is no error to
    explain it. The CPU serial is burned into the board, so it survives cloning.
    """
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("serial"):
                value = line.split(":", 1)[1].strip()
                if value and set(value) != {"0"}:
                    return value
    except OSError:
        pass
    return ""


def _mac_addresses() -> str:
    """Wired-first MAC addresses, as a last-resort discriminator."""
    out = []
    try:
        for iface in sorted(Path("/sys/class/net").iterdir()):
            if iface.name == "lo":
                continue
            try:
                mac = (iface / "address").read_text().strip()
            except OSError:
                continue
            if mac and mac != "00:00:00:00:00:00":
                out.append(mac)
    except OSError:
        pass
    return ",".join(out)


def node_id() -> str:
    """A stable id for this node, independent of hostname and IP.

    Combines every identifier we can find rather than trusting one: the board
    serial (unique per unit, survives SD card cloning), the machine-id (unique
    per OS install) and the MAC addresses. Any one of them being unique is
    enough to keep two nodes apart, which matters because the common deployment
    route — clone a card that already works — makes machine-id identical.

    PISTREAMER_NODE_ID overrides it. That exists so several nodes can be run on
    one host for testing, where every hardware identifier really is the same.
    """
    override = os.environ.get("PISTREAMER_NODE_ID", "").strip()
    if override:
        return hashlib.sha256(override.encode()).hexdigest()[:12]
    parts = [_hardware_serial()]
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            parts.append(Path(path).read_text().strip())
            break
        except OSError:
            continue
    parts.append(_mac_addresses())
    parts.append(socket.gethostname())
    return hashlib.sha256("|".join(p for p in parts if p).encode()).hexdigest()[:12]


def primary_ip() -> str:
    """The address other nodes should use to reach us.

    Uses a UDP socket to a routable address to ask the kernel which source
    address it would pick. Nothing is sent — no traffic, no DNS, works with no
    default route beyond the LAN. Reading the first entry of `hostname -I`
    instead would pick the wrong interface on the dual-homed nodes we run.
    """
    for probe in ("192.168.255.255", "8.8.8.8"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 9))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
            finally:
                s.close()
        except OSError:
            continue
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


# ----------------------------------------------------------------------
# Wire format
# ----------------------------------------------------------------------


def _sign(key: str, payload: bytes) -> str:
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()[:32]


def encode_beacon(body: Dict[str, Any], key: str) -> bytes:
    """Wrap a beacon body with a signature over its exact bytes.

    The signature covers the serialised payload rather than a re-serialisation
    of the parsed dict, so a receiver verifies precisely what was sent — key
    ordering or float formatting cannot change the digest.
    """
    payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    envelope = {"p": payload.decode(), "s": _sign(key, payload)}
    return json.dumps(envelope, separators=(",", ":")).encode()


def decode_beacon(data: bytes, key: str, group: str,
                  now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Parse and authenticate a beacon. Returns None for anything suspect.

    Silent rejection is deliberate: on a busy event network this port will see
    scanners and stale packets, and logging every one would bury the real
    diagnostics.
    """
    now = time.time() if now is None else now
    try:
        envelope = json.loads(data.decode("utf-8", errors="strict"))
        payload = envelope["p"].encode()
        signature = envelope["s"]
    except (ValueError, KeyError, AttributeError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(signature, _sign(key, payload)):
        return None
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("v") != PROTOCOL:
        return None
    if body.get("group") != group:
        return None
    stamp = body.get("t")
    if not isinstance(stamp, (int, float)) or abs(now - stamp) > BEACON_MAX_AGE:
        return None
    if not body.get("id") or not isinstance(body["id"], str):
        return None
    return body


# ----------------------------------------------------------------------
# Peer registry
# ----------------------------------------------------------------------


@dataclass
class Peer:
    id: str
    name: str = ""
    ip: str = ""
    port: int = 80
    # Where the node says it can be reached, and where its packets came from.
    # They differ on a dual-homed node; see Registry.observe.
    seen_ip: str = ""
    mode: str = "idle"
    target: str = ""
    playing: bool = False
    identify: bool = False
    version: str = ""
    # Monotonic clock, so a system clock step cannot make every peer look stale.
    last_seen: float = 0.0
    # Measured by the leader when it needs to schedule something; None until then.
    clock_offset: Optional[float] = None
    rtt_ms: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.monotonic() if now is None else now
        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "mode": self.mode,
            "target": self.target,
            "playing": self.playing,
            "identify": self.identify,
            "version": self.version,
            "age": round(max(0.0, now - self.last_seen), 1),
            "clock_offset_ms": (round(self.clock_offset * 1000, 1)
                                if self.clock_offset is not None else None),
            "rtt_ms": self.rtt_ms,
            **self.extra,
        }

    def base_url(self, ip: str = "") -> str:
        port = "" if self.port == 80 else f":{self.port}"
        return f"http://{ip or self.ip}{port}"

    def addresses(self) -> List[str]:
        """Every address worth trying, best first and without duplicates."""
        out = [a for a in (self.ip, self.seen_ip) if a]
        return list(dict.fromkeys(out))


class Registry:
    """Who is out there, as of the last beacon from each of them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._peers: Dict[str, Peer] = {}

    def observe(self, body: Dict[str, Any], src_ip: str) -> None:
        with self._lock:
            peer = self._peers.get(body["id"]) or Peer(id=body["id"])
            peer.name = str(body.get("name", ""))[:64]
            # Two candidate addresses, and which one is right is not obvious.
            #
            # The claimed address is preferred, because a node knows which of
            # its interfaces it wants to be reached on and we run dual-homed
            # nodes (Wi-Fi for internet, Ethernet for the show). Trusting the
            # packet's source address instead made the recorded address flap
            # between interfaces as broadcast and unicast beacons alternated,
            # so a command landed on whichever one beaconed most recently.
            #
            # The source address is kept as the fallback, for the case the
            # claim cannot be reached — a node with a stale static address, or
            # one whose claim is a network the leader has no route to.
            claimed = str(body.get("ip", "")).strip()
            peer.ip = claimed or src_ip
            peer.seen_ip = src_ip
            try:
                peer.port = int(body.get("port", 80))
            except (TypeError, ValueError):
                peer.port = 80
            peer.mode = str(body.get("mode", "idle"))[:16]
            peer.target = str(body.get("target", ""))[:200]
            peer.playing = bool(body.get("playing"))
            peer.identify = bool(body.get("identify"))
            peer.version = str(body.get("version", ""))[:32]
            peer.last_seen = time.monotonic()
            known = {"v", "id", "name", "ip", "port", "mode", "target",
                     "playing", "identify", "version", "t", "group"}
            peer.extra = {k: v for k, v in body.items() if k not in known}
            self._peers[peer.id] = peer

    def all(self, include_stale: bool = False) -> List[Peer]:
        now = time.monotonic()
        with self._lock:
            peers = list(self._peers.values())
        if not include_stale:
            peers = [p for p in peers if now - p.last_seen <= PEER_TIMEOUT]
        return sorted(peers, key=lambda p: (p.name.lower(), p.id))

    def get(self, node_id_: str) -> Optional[Peer]:
        with self._lock:
            return self._peers.get(node_id_)

    def forget_stale(self) -> None:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, p in self._peers.items()
                        if now - p.last_seen > PEER_TIMEOUT * 5]:
                del self._peers[key]


# ----------------------------------------------------------------------
# Beacon transport
# ----------------------------------------------------------------------


class Beacon:
    """Announces this node and listens for others.

    Two threads and two sockets: a sender that broadcasts, and a listener bound
    to the beacon port. They are separate because the listener has to bind the
    port with SO_REUSEADDR (several nodes may share a host while testing) while
    the sender needs SO_BROADCAST, and combining them made the failure modes
    harder to reason about than the duplication saves.
    """

    def __init__(self, registry: Registry, status_fn) -> None:
        self._registry = registry
        # Callable returning the dict of live fields (mode, target, playing…).
        # Injected rather than importing the player, which would make this
        # module untestable without a display.
        self._status_fn = status_fn
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._id = node_id()
        self._sent = 0
        self._received = 0
        self._rejected = 0
        self._last_error = ""
        # Addresses seen claiming our own node id. Should always be empty.
        self._duplicates: set = set()
        self._cached_ip = ""


    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for target, name in ((self._listen_loop, "beacon-listen"),
                             (self._announce_loop, "beacon-announce")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads = []

    def _own_ip(self) -> str:
        # Cached: this is consulted per received packet and each miss opens a
        # socket, which is wasteful on a box that is busy decoding video.
        if not self._cached_ip:
            self._cached_ip = primary_ip()
        return self._cached_ip

    def stats(self) -> Dict[str, Any]:
        return {
            "running": any(t.is_alive() for t in self._threads),
            "node_id": self._id,
            "port": BEACON_PORT,
            "sent": self._sent,
            "received": self._received,
            "rejected": self._rejected,
            "last_error": self._last_error,
            # Non-empty means two nodes share an identity — see the listener.
            "duplicate_ids_at": sorted(self._duplicates),
        }

    # -- sending --------------------------------------------------------

    def _body(self, cfg: config.Config) -> Dict[str, Any]:
        status = {}
        try:
            status = self._status_fn() or {}
        except Exception as exc:  # noqa: BLE001 - a beacon must not die
            self._last_error = f"status: {exc}"
        return {
            "v": PROTOCOL,
            "id": self._id,
            "name": cfg.device_name or socket.gethostname(),
            "ip": primary_ip(),
            "port": cfg.web_port,
            "group": cfg.cluster_group,
            "mode": status.get("mode", "idle"),
            "target": status.get("target", ""),
            "playing": bool(status.get("running")),
            "identify": bool(cfg.identify),
            "version": status.get("version", ""),
            "t": time.time(),
        }

    def _targets(self, cfg: config.Config) -> List[str]:
        out = ["255.255.255.255"]
        for extra in (cfg.cluster_extra_ips or "").replace(";", ",").split(","):
            extra = extra.strip()
            if extra and extra not in out:
                out.append(extra)
        return out

    def _announce_loop(self) -> None:
        while not self._stop.is_set():
            cfg = config.load()
            if cfg.cluster_enabled:
                try:
                    self._announce_once(cfg)
                except OSError as exc:
                    self._last_error = f"announce: {exc}"
            if self._stop.wait(BEACON_INTERVAL):
                return

    def _announce_once(self, cfg: config.Config) -> None:
        data = encode_beacon(self._body(cfg), cfg.cluster_key)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            for target in self._targets(cfg):
                try:
                    sock.sendto(data, (target, BEACON_PORT))
                    self._sent += 1
                except OSError as exc:
                    # One unreachable unicast target must not stop the rest.
                    self._last_error = f"send {target}: {exc}"
        finally:
            sock.close()

    # -- receiving ------------------------------------------------------

    def _listen_loop(self) -> None:
        sock = None
        while not self._stop.is_set():
            if sock is None:
                sock = self._bind()
                if sock is None:
                    if self._stop.wait(5.0):
                        return
                    continue
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._last_error = f"listen: {exc}"
                sock.close()
                sock = None
                continue
            cfg = config.load()
            if not cfg.cluster_enabled:
                continue
            body = decode_beacon(data, cfg.cluster_key, cfg.cluster_group)
            if body is None:
                self._rejected += 1
                continue
            self._received += 1
            if body["id"] == self._id:
                # Our own broadcast echoed back — unless it came from somewhere
                # else, in which case two nodes really are claiming the same
                # identity and neither will ever see the other. Silence here
                # would present as "discovery does not work" with nothing in
                # any log to explain it, so say so loudly.
                if addr[0] not in ("127.0.0.1", self._own_ip()):
                    self._duplicates.add(addr[0])
                    log.error(
                        "another node at %s is using our node id (%s). Both are "
                        "invisible to each other. This happens when an SD card "
                        "is cloned; set PISTREAMER_NODE_ID on one of them or "
                        "regenerate /etc/machine-id.", addr[0], self._id)
                continue
            self._registry.observe(body, addr[0])
            self._registry.forget_stale()
        if sock is not None:
            sock.close()

    def _bind(self) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Several nodes on one host during testing, and a clean restart
            # without waiting out TIME_WAIT.
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.settimeout(1.0)
            sock.bind(("", BEACON_PORT))
            return sock
        except OSError as exc:
            self._last_error = f"bind {BEACON_PORT}: {exc}"
            return None


# ----------------------------------------------------------------------
# Talking to a peer
# ----------------------------------------------------------------------


class PeerError(RuntimeError):
    pass


def call(peer: Peer, path: str, method: str = "GET",
         body: Optional[Dict[str, Any]] = None, key: str = "",
         timeout: float = 8.0) -> Dict[str, Any]:
    """One JSON request to a peer, trying each address it might answer on.

    A dual-homed node has two plausible addresses and only one of them may be
    reachable from here. Rather than making the operator work out which, try
    them in order and remember the one that answered — so the cost is paid once
    rather than on every call.
    """
    last: Optional[PeerError] = None
    for address in peer.addresses():
        try:
            reply = _call_one(peer, address, path, method, body, key, timeout)
        except PeerError as exc:
            last = exc
            continue
        if address != peer.ip:
            log.info("peer %s answers on %s, not %s; using that from now on",
                     peer.name or peer.id, address, peer.ip)
            peer.ip = address
        return reply
    raise last or PeerError(f"{peer.name or peer.id}: no known address")


def _call_one(peer: Peer, address: str, path: str, method: str,
              body: Optional[Dict[str, Any]], key: str,
              timeout: float) -> Dict[str, Any]:
    url = f"{peer.base_url(address)}{path}"
    data = None
    headers = {AUTH_HEADER: key, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise PeerError(f"{peer.name or peer.ip}: HTTP {exc.code} {detail}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise PeerError(f"{peer.name or peer.ip}: unreachable ({exc})") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw.decode(errors="replace")[:300]}


class _CountingReader:
    """A file wrapper that reports how much has been handed to the socket.

    http.client reads from the object we give it in chunks, so counting reads is
    the only place a byte count is available — and without one, a multi-gigabyte
    push to four nodes is a spinner with no information in it for ten minutes.

    Reads are what has been *handed to the kernel*, not what the far end has
    acknowledged, so the count can run slightly ahead of reality. Over a LAN the
    difference is a socket buffer; it is not worth a second channel to correct.
    """

    def __init__(self, fh, on_progress=None, interval: float = 0.25) -> None:
        self._fh = fh
        self._on_progress = on_progress
        self._sent = 0
        self._interval = interval
        self._last = 0.0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._sent += len(chunk)
            now = time.monotonic()
            # Rate-limited: a 4 GB file is tens of thousands of reads and the
            # callback takes a lock.
            if self._on_progress and (now - self._last) >= self._interval:
                self._last = now
                try:
                    self._on_progress(self._sent)
                except Exception:  # noqa: BLE001 - reporting must never break a transfer
                    pass
        return chunk

    def close(self) -> None:
        self._fh.close()

    @property
    def sent(self) -> int:
        return self._sent


def upload(peer: Peer, name: str, path: Path, key: str = "",
           timeout: float = 3600.0, on_progress=None) -> Dict[str, Any]:
    """Stream one media file to a peer.

    Uses the raw-body endpoint rather than multipart: a multipart body has to be
    assembled around the file, and for a 4 GB video that means either building
    it in memory or hand-rolling a streaming encoder. A raw PUT lets http.client
    read straight from the file object in chunks.
    """
    size = path.stat().st_size
    # Same address question as call(): use whichever address the peer has
    # already been shown to answer on. call() promotes it, and every push is
    # preceded by a call to ask what the peer already has, so by the time we
    # get here peer.ip is the address that worked.
    url = f"{peer.base_url()}/api/media/raw/{urllib.request.quote(name)}"
    with path.open("rb") as raw_fh:
        fh = _CountingReader(raw_fh, on_progress)
        req = urllib.request.Request(
            url, data=fh, method="PUT",
            headers={
                AUTH_HEADER: key,
                "Content-Type": "application/octet-stream",
                # Without an explicit length urllib would use chunked encoding,
                # which Starlette accepts but makes progress unknowable.
                "Content-Length": str(size),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode(errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise PeerError(f"{peer.name or peer.ip}: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise PeerError(f"{peer.name or peer.ip}: upload failed ({exc})") from exc
        finally:
            # One last report so a finished file lands on its true size rather
            # than wherever the rate limiter last happened to fire.
            if on_progress:
                try:
                    on_progress(fh.sent)
                except Exception:  # noqa: BLE001
                    pass
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def measure_offset(peer: Peer, key: str = "", samples: int = 5
                   ) -> Tuple[Optional[float], Optional[float]]:
    """Clock offset of a peer, in seconds, plus the round trip that produced it.

    The usual NTP arithmetic: with t1 as send, t2 as the peer's clock and t3 as
    receive, the offset is t2 - (t1 + t3) / 2. Several samples are taken and the
    one with the *lowest* round trip wins rather than an average — a slow sample
    is slow because something queued, and queueing is what biases the estimate.

    Returned offset is (peer clock - our clock): add it to our time to express
    an instant in the peer's clock.
    """
    best: Optional[Tuple[float, float]] = None
    for _ in range(max(1, samples)):
        t1 = time.time()
        try:
            reply = call(peer, "/api/cluster/time", key=key, timeout=3.0)
        except PeerError:
            continue
        t3 = time.time()
        remote = reply.get("t")
        if not isinstance(remote, (int, float)):
            continue
        rtt = t3 - t1
        offset = remote - (t1 + t3) / 2
        if best is None or rtt < best[1]:
            best = (offset, rtt)
    if best is None:
        return None, None
    return best[0], round(best[1] * 1000, 1)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ----------------------------------------------------------------------
# Module singletons
# ----------------------------------------------------------------------

registry = Registry()
_beacon: Optional[Beacon] = None


def beacon(status_fn=None) -> Beacon:
    global _beacon
    if _beacon is None:
        _beacon = Beacon(registry, status_fn or (lambda: {}))
    return _beacon
