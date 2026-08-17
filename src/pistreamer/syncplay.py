"""Getting several nodes to show the same frame at the same moment.

Three mechanisms, because one is not enough:

  1. **Aligned start.** Every node loads the file and holds on its first frame
     *paused*, reports ready, and is then told a wall-clock instant at which to
     un-pause — expressed in its own clock, so it needs no knowledge of anyone
     else's. Spawning a process is tens to hundreds of milliseconds and varies
     per node; un-pausing an already-decoding player is close to a frame. That
     difference is the whole reason for the prepare/start split.

  2. **A pulse at each boundary.** When the leader starts a new item it says so,
     and followers align to that item rather than trying to infer it. This is
     what the user asked for and it is the cheap, robust part: even if drift
     correction is off, every item begins together.

  3. **Drift correction while an item plays.** Two Pis decoding the same file
     from their own SD cards will not stay locked: clocks differ by parts per
     million and decode is not deterministic. The leader publishes its position
     periodically; a follower that is out by a little changes speed slightly
     until it catches up, and one that is out by a lot seeks. Nudging speed is
     the difference between a sync that is invisible and one that is a visible
     jump every few seconds.

Correction thresholds are deliberately conservative. This drives a video wall at
an event: a seek is visible, so it is reserved for genuinely lost sync, and the
speed nudge is small enough to be inaudible on speech.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Below this we are as good as locked; leave it alone. A frame at 50fps is 20ms,
# and chasing smaller errors than that just means never settling.
IN_SYNC_S = 0.020
# Above this a seek is the only way back; below it, ride the speed nudge.
# A quarter second is roughly where a viewer stops reading it as "soft" and
# starts seeing two different pictures.
SEEK_ABOVE_S = 0.25
# Playback speed used while catching up. 2% is inaudible on speech and music
# and closes a 100ms error in five seconds.
NUDGE = 0.02
# How long to hold a nudge before re-evaluating. Long enough for the correction
# to actually take effect, short enough to not overshoot.
NUDGE_HOLD_S = 1.0
# Slack between "everyone is ready" and the agreed start instant. It has to
# cover the slowest node's scheduling jitter and the one-way trip of the start
# command; 750ms is generous on a wired LAN and still feels immediate.
START_SLACK_S = 0.75


@dataclass
class Correction:
    """What a follower should do about its current drift."""

    action: str  # "hold" | "nudge" | "seek"
    drift: float  # seconds; positive means this node is AHEAD of the leader
    speed: float = 1.0
    seek_to: Optional[float] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "drift_ms": round(self.drift * 1000, 1),
            "speed": self.speed,
            "seek_to": self.seek_to,
            "reason": self.reason,
        }


def expected_position(pulse: Dict[str, Any], now: float) -> Optional[float]:
    """Where the leader's playhead is *now*, from a pulse that has aged.

    A pulse says "at instant `at` I was at position `pos`" — both already
    converted into this node's clock by the leader. Latency between the pulse
    being sent and being acted on is therefore accounted for rather than
    silently added to the drift.
    """
    at = pulse.get("at")
    pos = pulse.get("pos")
    if not isinstance(at, (int, float)) or not isinstance(pos, (int, float)):
        return None
    elapsed = now - at
    # A pulse from the future, or a very old one, means clocks or the network
    # have misbehaved; refusing to act on it is better than a wild seek.
    if elapsed < -1.0 or elapsed > 10.0:
        return None
    return pos + max(0.0, elapsed)


def decide(pulse: Dict[str, Any], own_position: Optional[float], now: float,
           nudging_until: float = 0.0) -> Correction:
    """Compare our playhead with the leader's and say what to do about it."""
    target = expected_position(pulse, now)
    if target is None:
        return Correction("hold", 0.0, reason="no usable pulse")
    if not isinstance(own_position, (int, float)):
        return Correction("hold", 0.0, reason="no local position yet")

    drift = own_position - target

    if abs(drift) > SEEK_ABOVE_S:
        return Correction("seek", drift, speed=1.0, seek_to=target,
                          reason=f"{drift * 1000:.0f}ms out — seeking")
    if abs(drift) <= IN_SYNC_S:
        # Returning to 1.0 explicitly matters: a node that reaches sync while
        # nudging would otherwise sail straight past it.
        return Correction("hold", drift, speed=1.0, reason="in sync")
    if now < nudging_until:
        return Correction("hold", drift, reason="nudge in progress")
    # Ahead of the leader -> play slower to let them catch up, and vice versa.
    speed = 1.0 - NUDGE if drift > 0 else 1.0 + NUDGE
    return Correction("nudge", drift, speed=round(speed, 4),
                      reason=f"{drift * 1000:.0f}ms {'ahead' if drift > 0 else 'behind'}")


def start_instant(offsets: Dict[str, Optional[float]], now: Optional[float] = None,
                  slack: float = START_SLACK_S) -> Dict[str, float]:
    """Pick a start instant and express it in every node's own clock.

    Returns node id -> instant in that node's clock. A node whose offset could
    not be measured gets the unadjusted instant: being a few milliseconds out is
    much better than being excluded from the show.
    """
    now = time.time() if now is None else now
    at = now + slack
    return {node: at + (offset or 0.0) for node, offset in offsets.items()}


class Conductor:
    """Runs a synchronised playlist across a group of nodes.

    One node acts as conductor for the duration of an operation — whichever one
    the operator has open in a browser. It holds no cluster state that could get
    out of step: if it goes away, followers simply finish the item they are on
    and the operator picks another node. That is a deliberate trade of
    automatic failover (which nobody at an event trusts anyway) for a system
    with no split-brain to debug at 5pm on a show day.

    The loop per item is: prepare everyone -> agree an instant -> release -> pulse
    while it plays -> advance when the conductor's own copy ends. The
    conductor's own player is driven through exactly the same calls as a
    follower's, so there is one code path to get right rather than two.
    """

    def __init__(self, deps: Dict[str, Any]) -> None:
        # Injected so this is testable without a network or a display:
        #   peers()          -> list of peer-like objects with .id/.name
        #   prepare(t, file) -> bool ready
        #   start(t, at)     -> None
        #   pulse(t, body)   -> None
        #   offsets(targets) -> {id: offset seconds or None}
        #   position()       -> conductor's own playhead, or None
        #   duration()       -> conductor's own item duration, or None
        self._d = deps
        self._thread: Optional[Any] = None
        self._stop = None
        self._state: Dict[str, Any] = {"running": False}
        self._lock = None

    # The threading objects are created lazily so the pure-logic parts of this
    # module stay importable in contexts that never run a session.
    def _ensure(self) -> None:
        import threading
        if self._stop is None:
            self._stop = threading.Event()
        if self._lock is None:
            self._lock = threading.Lock()

    def state(self) -> Dict[str, Any]:
        self._ensure()
        with self._lock:
            return dict(self._state)

    def start(self, items: List[Dict[str, Any]], targets: List[Any],
              loop: bool = True) -> Dict[str, Any]:
        import threading
        self._ensure()
        if not items:
            raise ValueError("nothing to play")
        if self._thread is not None and self._thread.is_alive():
            self.stop()
        self._stop.clear()
        # A session id, so a node that has been stopped by hand can refuse the
        # rest of *this* session and still join the next one. Without it, the
        # only choices are a node that never rejoins and a node that cannot be
        # stopped.
        session = f"{int(time.time() * 1000):x}"
        with self._lock:
            self._state = {
                "running": True, "session": session,
                "files": [i.get("target", "") for i in items],
                "index": 0, "loop": loop,
                "nodes": [getattr(t, "name", "?") for t in targets],
                "started_at": None, "last": {},
            }
        self._thread = threading.Thread(
            target=self._run, args=(items, targets, loop, session),
            name="conductor", daemon=True,
        )
        self._thread.start()
        return self.state()

    def stop(self) -> None:
        self._ensure()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._thread = None
        with self._lock:
            self._state["running"] = False

    def _run(self, items: List[Dict[str, Any]], targets: List[Any], loop: bool,
             session: str) -> None:
        index = 0
        while not self._stop.is_set():
            item = items[index]
            try:
                outcome = self._play_item(item, targets, session)
            except Exception as exc:  # noqa: BLE001 - a show must not stop on one item
                log.exception("synchronised item %r failed", item)
                outcome = {"error": str(exc)}
            with self._lock:
                self._state["index"] = index
                self._state["last"] = outcome
            if self._stop.is_set():
                break
            index += 1
            if index >= len(items):
                if not loop:
                    break
                index = 0
        with self._lock:
            self._state["running"] = False

    def _play_item(self, item: Dict[str, Any], targets: List[Any],
                   session: str) -> Dict[str, Any]:
        prepare = self._d["prepare"]
        results: List[Dict[str, Any]] = []
        ready: List[Any] = []
        for target in targets:
            name = getattr(target, "name", "?")
            try:
                outcome = prepare(target, item, session)
                if outcome is True or (isinstance(outcome, dict) and outcome.get("ready")):
                    ready.append(target)
                    results.append({"name": name, "ok": True})
                else:
                    # A node that has been stopped by hand says so, and is left
                    # alone for the rest of this session rather than being
                    # dragged back into playing every time the item changes.
                    reason = (outcome.get("reason") if isinstance(outcome, dict) else "")
                    results.append({"name": name, "ok": False,
                                    "error": reason or "did not become ready"})
            except Exception as exc:  # noqa: BLE001
                results.append({"name": name, "ok": False, "error": str(exc)})

        offsets = self._d["offsets"]([getattr(t, "id", "") for t in ready])
        for result in results:
            for target in ready:
                if getattr(target, "name", "?") == result["name"]:
                    offset = offsets.get(getattr(target, "id", ""))
                    result["offset_ms"] = (round(offset * 1000, 1)
                                           if offset is not None else None)

        # One base instant, kept, because a fixed-length item (a still image
        # above all) ends on the clock rather than on a playhead.
        base = time.time()
        instants = start_instant({getattr(t, "id", ""): offsets.get(getattr(t, "id", ""))
                                  for t in ready}, now=base)
        for target in ready:
            try:
                self._d["start"](target, instants[getattr(target, "id", "")])
            except Exception as exc:  # noqa: BLE001
                for result in results:
                    if result["name"] == getattr(target, "name", "?"):
                        result["ok"] = False
                        result["error"] = str(exc)

        with self._lock:
            self._state["started_at"] = time.time()
            self._state["item"] = item.get("target", "")
        self._pulse_until_end(item, ready, offsets, base + START_SLACK_S)
        return summarise(results)

    def _pulse_until_end(self, item: Dict[str, Any], targets: List[Any],
                         offsets: Dict[str, Optional[float]],
                         started_at: float) -> None:
        """Hold the item until it is over, publishing our playhead as we go.

        How an item ends depends on what it is:

        * A **still image has no playhead**. mpv reports time-pos 0.0 for it
          forever, which the playhead check below read as "stopped advancing"
          and so skipped every image after about two seconds — the item was on
          screen for a fraction of its dwell time. Images therefore end on the
          clock, at the agreed start instant plus their duration.
        * A **video with an explicit duration** ends on the clock too, for the
          same reason its cut is explicit.
        * Anything else ends when the conductor's own copy runs out. Using its
          end for the whole group, rather than each node's own, is what stops
          them wandering apart item by item.
        """
        position_fn = self._d["position"]
        pulse_fn = self._d["pulse"]
        duration = item.get("duration")
        is_image = bool(item.get("image"))
        if is_image and not duration:
            # A playlist should not contain an image with no dwell time, but a
            # hand-edited file might; ten seconds beats holding it forever.
            duration = 10
        deadline = started_at + duration if duration else None

        last_position = -1.0
        stalled_since: Optional[float] = None
        # Give the item a moment to actually start before deciding it has ended.
        if self._stop.wait(START_SLACK_S + 0.5):
            return
        while not self._stop.is_set():
            now = time.time()
            if deadline is not None and now >= deadline:
                return
            position = position_fn()
            if not is_image:
                # Only meaningful where there is a playhead to read.
                if position is None:
                    # No playhead: either the file ended or the player went
                    # away. Either way this item is over.
                    return
                if position <= last_position + 0.001:
                    stalled_since = stalled_since or now
                    if now - stalled_since > 2.0 and deadline is None:
                        return  # paused at the end, or wedged; move on
                else:
                    stalled_since = None
                last_position = position
            if is_image:
                # Nothing to correct on a still frame: it was put up on the
                # agreed instant and comes down on the agreed instant.
                if self._stop.wait(min(0.25, max(0.0, deadline - time.time()))):
                    return
                continue
            for target in targets:
                offset = offsets.get(getattr(target, "id", "")) or 0.0
                try:
                    pulse_fn(target, {
                        "item": item.get("target", ""),
                        "pos": position,
                        # Converted into the follower's clock here, for the same
                        # reason start times are: one place does the arithmetic.
                        "at": now + offset,
                    })
                except Exception:  # noqa: BLE001 - a missed pulse is not fatal
                    pass
            if self._stop.wait(2.0):
                return


def summarise(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold per-node outcomes into something a GUI can show in one line."""
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    spread = [r["offset_ms"] for r in results
              if isinstance(r.get("offset_ms"), (int, float))]
    return {
        "nodes": len(results),
        "started": len(ok),
        "failed": [{"name": r.get("name", "?"), "error": r.get("error", "")}
                   for r in failed],
        # The honest measure of how tight the start was: the worst clock
        # disagreement we had to work with.
        "clock_spread_ms": (round(max(spread) - min(spread), 1)
                            if len(spread) > 1 else 0.0),
        "results": results,
    }
