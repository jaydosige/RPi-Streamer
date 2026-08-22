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

# Slack between "everyone is ready" and the agreed start instant. It has to
# cover the slowest node's scheduling jitter and the one-way trip of the start
# command; 750ms is generous on a wired LAN and still feels immediate.
START_SLACK_S = 0.75


@dataclass(frozen=True)
class Strength:
    """How hard a follower works to stay locked to the leader.

    There is no single right answer here, which is why it is a setting rather
    than a tuned constant. Two panels of a video wall side by side make a 100ms
    error obvious, so that job wants correction hard enough to be worth a
    visible seek. A room of speakers playing the same music wants the opposite:
    a 5% speed change is audible and a seek is far worse than the drift it
    fixes. The same node does both jobs on different days.

    Fields:
      in_sync_s      below this, stop correcting — chasing noise never settles
      seek_above_s   above this, only a seek gets us back
      max_nudge      largest speed change allowed, as a fraction of 1.0
      hold_s         how long a nudge is left to take effect before re-deciding
      close_over_s   time a nudge aims to take to close the whole gap
      pulse_interval_s how often the leader publishes its playhead
      give_up_after_s  nudging for this long without reaching sync escalates to
                       a seek; 0 never escalates
    """

    name: str
    label: str
    in_sync_s: float
    seek_above_s: float
    max_nudge: float
    hold_s: float
    close_over_s: float
    pulse_interval_s: float
    give_up_after_s: float
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "note": self.note,
            "in_sync_ms": round(self.in_sync_s * 1000),
            "seek_above_ms": round(self.seek_above_s * 1000),
            "max_nudge_percent": round(self.max_nudge * 100, 1),
            "pulse_interval_s": self.pulse_interval_s,
        }


STRENGTHS: Dict[str, Strength] = {
    "gentle": Strength(
        "gentle", "Gentle", in_sync_s=0.040, seek_above_s=0.60, max_nudge=0.010,
        hold_s=2.0, close_over_s=8.0, pulse_interval_s=3.0, give_up_after_s=0.0,
        note="Never audibly changes speed and almost never seeks. For music and "
             "speech, where a correction is worse than the drift.",
    ),
    "normal": Strength(
        "normal", "Normal", in_sync_s=0.020, seek_above_s=0.25, max_nudge=0.020,
        hold_s=1.0, close_over_s=4.0, pulse_interval_s=2.0, give_up_after_s=30.0,
        note="Corrections stay invisible on picture and inaudible on speech. "
             "The right default for most content.",
    ),
    "firm": Strength(
        "firm", "Firm", in_sync_s=0.015, seek_above_s=0.12, max_nudge=0.050,
        hold_s=0.75, close_over_s=2.0, pulse_interval_s=1.0, give_up_after_s=15.0,
        note="Pulls harder and seeks sooner. Occasional audible speed change on "
             "music. For a video wall where panels are seen side by side.",
    ),
    "lock": Strength(
        "lock", "Lock", in_sync_s=0.010, seek_above_s=0.06, max_nudge=0.100,
        hold_s=0.5, close_over_s=1.0, pulse_interval_s=0.5, give_up_after_s=8.0,
        note="Tightest hold available: frame-accurate, at the cost of visible "
             "seeks and audible speed changes. For silent picture-critical walls.",
    ),
}
DEFAULT_STRENGTH = "normal"


def profile(name: Any = None) -> Strength:
    """Resolve a strength by name, falling back to the default.

    Unknown names fall back rather than raising: a config file carried over
    from a newer version should not stop a node playing.
    """
    if isinstance(name, Strength):
        return name
    return STRENGTHS.get(str(name or DEFAULT_STRENGTH), STRENGTHS[DEFAULT_STRENGTH])


# The default profile's numbers, kept as module constants because they are the
# shape of the problem rather than one profile's opinion of it.
IN_SYNC_S = STRENGTHS[DEFAULT_STRENGTH].in_sync_s
SEEK_ABOVE_S = STRENGTHS[DEFAULT_STRENGTH].seek_above_s
NUDGE = STRENGTHS[DEFAULT_STRENGTH].max_nudge
NUDGE_HOLD_S = STRENGTHS[DEFAULT_STRENGTH].hold_s


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
           nudging_until: float = 0.0, strength: Any = None,
           correcting_since: float = 0.0) -> Correction:
    """Compare our playhead with the leader's and say what to do about it.

    `correcting_since` is when this node last went out of sync and has been
    trying to get back ever since — 0 if it is not currently correcting. It
    exists because a nudge can be *exactly* cancelled out: a node whose decode
    runs half a percent slow, fed a half-percent speed-up, sits at a constant
    drift forever, correcting the whole time and never arriving. Speed alone
    cannot fix a rate difference, only an offset. So once a profile has given
    the nudge long enough, it stops asking politely and seeks.
    """
    s = profile(strength)
    target = expected_position(pulse, now)
    if target is None:
        return Correction("hold", 0.0, reason="no usable pulse")
    if not isinstance(own_position, (int, float)):
        return Correction("hold", 0.0, reason="no local position yet")

    drift = own_position - target
    out = abs(drift)
    way = "ahead" if drift > 0 else "behind"

    if out > s.seek_above_s:
        return Correction("seek", drift, speed=1.0, seek_to=target,
                          reason=f"{drift * 1000:.0f}ms out — seeking")
    if out <= s.in_sync_s:
        # Returning to 1.0 explicitly matters: a node that reaches sync while
        # nudging would otherwise sail straight past it.
        return Correction("hold", drift, speed=1.0, reason="in sync")
    if (s.give_up_after_s and correcting_since
            and now - correcting_since > s.give_up_after_s):
        return Correction(
            "seek", drift, speed=1.0, seek_to=target,
            reason=f"{drift * 1000:.0f}ms {way} after "
                   f"{now - correcting_since:.0f}s of nudging — seeking",
        )
    if now < nudging_until:
        return Correction("hold", drift, reason="nudge in progress")
    # Proportional rather than a flat step: aim to close the whole gap over the
    # profile's window, and cap it at what the profile will tolerate. A flat
    # nudge has to be sized for the worst case, which makes it overshoot the
    # common one and oscillate; scaling it means a large error is corrected
    # hard and a small one is barely touched.
    rate = min(out / s.close_over_s, s.max_nudge)
    speed = 1.0 - rate if drift > 0 else 1.0 + rate
    return Correction("nudge", drift, speed=round(speed, 4),
                      reason=f"{drift * 1000:.0f}ms {way} — {rate * 100:.1f}% for "
                             f"{s.hold_s:g}s")


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
        # How hard followers will be asked to hold. The leader only needs it for
        # the pulse rate — the corrections themselves are each follower's own
        # decision, made against its own config, because a node may legitimately
        # be set gentler than the group (the one with the speakers on it).
        self._strength = profile(DEFAULT_STRENGTH)

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
              loop: bool = True, strength: Any = None) -> Dict[str, Any]:
        import threading
        self._ensure()
        if not items:
            raise ValueError("nothing to play")
        self._strength = profile(strength)
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
                "strength": self._strength.to_dict(),
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
            if self._stop.wait(self._strength.pulse_interval_s):
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
