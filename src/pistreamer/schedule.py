"""Time-based cue list.

Deliberately trigger-based rather than window-based: each entry says "at this
time on these days, switch to this", like a cue sheet. Windows ("play X
between 09:00 and 17:00") sound tidier but behave worse — you have to define
precedence when they overlap, decide what happens in the gaps, and re-evaluate
continuously. A cue fires once and the node stays where it was put, which is
both easier to reason about and easier to override by hand mid-show.

Times are local, because that is how a running order is written. That means a
DST change can skip or repeat a cue; for an event node that is the right
trade, and the alternative surprises people far more often.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

TICK_SECONDS = 20.0
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

ACTIONS = ("ndi", "playlist", "file", "folder", "web", "stream", "favourite",
           "airplay", "standby")


@dataclass
class Cue:
    id: str
    time: str  # "HH:MM", local
    action: str  # one of ACTIONS
    target: str = ""  # NDI name, playlist name, or filename
    days: List[int] = field(default_factory=lambda: list(range(7)))  # 0 = Monday
    enabled: bool = True
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, when: datetime) -> bool:
        if not self.enabled:
            return False
        if when.weekday() not in self.days:
            return False
        return when.strftime("%H:%M") == self.time


def validate(cue: Cue) -> None:
    if not _TIME_RE.match(cue.time or ""):
        raise ValueError("time must be HH:MM in 24-hour form")
    if cue.action not in ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(ACTIONS)}")
    if cue.action in ("ndi", "playlist", "file") and not cue.target:
        raise ValueError(f"action '{cue.action}' needs a target")
    if not cue.days or any(d not in range(7) for d in cue.days):
        raise ValueError("days must be a non-empty list of 0-6, Monday first")


def store_path() -> Path:
    return config.STATE_DIR / "schedule.json"


def _load_raw() -> List[dict]:
    return config.read_json(store_path(), [])


def _save_raw(data: List[dict]) -> None:
    config.write_json(store_path(), data, sort_keys=False)


def all_cues() -> List[Cue]:
    out = []
    for raw in _load_raw():
        try:
            out.append(
                Cue(
                    id=str(raw["id"]),
                    time=str(raw.get("time", "")),
                    action=str(raw.get("action", "standby")),
                    target=str(raw.get("target", "")),
                    days=[int(d) for d in raw.get("days", range(7))],
                    enabled=bool(raw.get("enabled", True)),
                    label=str(raw.get("label", "")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: (c.time, c.id))
    return out


def save(cue: Cue) -> Cue:
    validate(cue)
    data = [c for c in _load_raw() if str(c.get("id")) != cue.id]
    data.append(cue.to_dict())
    _save_raw(data)
    return cue


def delete(cue_id: str) -> bool:
    data = _load_raw()
    remaining = [c for c in data if str(c.get("id")) != cue_id]
    if len(remaining) == len(data):
        return False
    _save_raw(remaining)
    return True


def next_fire(cues: Optional[List[Cue]] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """The next cue due, so the GUI can say what happens next."""
    cues = all_cues() if cues is None else cues
    now = now or datetime.now()
    best = None
    for offset in range(8):  # today plus the next seven days
        day = (now.weekday() + offset) % 7
        for cue in cues:
            if not cue.enabled or day not in cue.days:
                continue
            hh, mm = (int(x) for x in cue.time.split(":"))
            minutes = offset * 1440 + hh * 60 + mm - (now.hour * 60 + now.minute)
            if minutes <= 0 and offset == 0:
                continue  # already gone today
            if best is None or minutes < best[0]:
                best = (minutes, cue)
        if best is not None and offset >= 1:
            break
    if best is None:
        return None
    minutes, cue = best
    return {"in_minutes": minutes, "cue": cue.to_dict()}


class Scheduler:
    """Fires cues as their minute arrives.

    A cue is fired at most once per minute-of-day, tracked by the exact
    date-and-minute string, so a tick landing twice in the same minute cannot
    double-fire and a missed tick cannot fire the cue late.
    """

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fired: set[str] = set()
        self._apply = None
        self._last: Optional[Dict[str, Any]] = None

    def bind(self, apply_callable) -> None:
        """apply_callable(action, target) performs the switch."""
        self._apply = apply_callable

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def last_fired(self) -> Optional[Dict[str, Any]]:
        return dict(self._last) if self._last else None

    def _run(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a scheduler must not die
                log.exception("scheduler tick failed")

    def tick(self, now: Optional[datetime] = None) -> List[Cue]:
        """Fire any cues due now. Returns what fired, for tests."""
        now = now or datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        fired: List[Cue] = []
        for cue in all_cues():
            key = f"{stamp}:{cue.id}"
            if key in self._fired or not cue.matches(now):
                continue
            self._fired.add(key)
            fired.append(cue)
            log.info("cue %s (%s) firing: %s %s", cue.id, cue.label, cue.action, cue.target)
            if self._apply is not None:
                try:
                    self._apply(cue.action, cue.target)
                    self._last = {"at": time.time(), "cue": cue.to_dict(), "error": ""}
                except Exception as exc:  # noqa: BLE001
                    log.error("cue %s failed: %s", cue.id, exc)
                    self._last = {"at": time.time(), "cue": cue.to_dict(), "error": str(exc)}
        # Keep the fired set from growing without bound over a long run.
        if len(self._fired) > 512:
            self._fired = {k for k in self._fired if k.startswith(stamp[:10])}
        return fired


scheduler = Scheduler()
