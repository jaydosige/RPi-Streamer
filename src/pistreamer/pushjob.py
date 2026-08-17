"""Copying a playlist to the other nodes, with progress worth watching.

The push itself is simple: work out which files each node is missing, send
those, then send the playlist definition. What makes this its own module is the
reporting. A show playlist is gigabytes and an event network is often Wi-Fi, so
a push can take ten minutes — and a request that returns nothing until it is
finished is indistinguishable from one that has hung. Twice now the honest
answer to "is it working?" has been "no idea", which is not a state this should
ever be in.

So the push runs in a thread and keeps a snapshot of exactly where it is: which
node, which file, how many bytes of it, how fast, and what has already been
skipped because the far end already had it. The GUI polls that snapshot.

Only one push runs at a time. Two pushes of overlapping files to the same node
would race over the same temp file at the far end, and there is no sensible
reason to start a second one.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# Rate is measured over a trailing window rather than the whole transfer: an
# average since the start hides the network falling over halfway through, which
# is the thing you most want to see.
RATE_WINDOW_S = 5.0


class PushJob:
    """One push of one playlist to a set of peers."""

    def __init__(self, playlist: str, deps: Dict[str, Callable]) -> None:
        self._lock = threading.Lock()
        self._deps = deps
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._marks: List[tuple] = []
        self._started: Optional[float] = None
        self.state: Dict[str, Any] = {
            "playlist": playlist,
            "running": False,
            "done": False,
            "cancelled": False,
            "started": None,
            "finished": None,
            "node": "",
            "file": "",
            "sent_bytes": 0,
            "file_bytes": 0,
            "total_bytes": 0,
            "done_bytes": 0,
            "rate": 0.0,
            "nodes": [],
            "error": "",
        }

    # -- reporting ------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self.state)
            out["nodes"] = [dict(n) for n in self.state["nodes"]]
        total = out["total_bytes"]
        moved = out["done_bytes"] + out["sent_bytes"]
        out["percent"] = round(100.0 * moved / total, 1) if total else None
        out["moved_bytes"] = moved
        out["failed"] = [{"name": n["name"], "error": n["error"]}
                         for n in out["nodes"] if n["error"]]
        out["ok"] = out["done"] and not out["failed"] and not out["cancelled"]
        if out["rate"] > 0 and total > moved:
            out["eta_s"] = int((total - moved) / out["rate"])
        else:
            out["eta_s"] = None
        return out

    def _note_rate(self, moved_total: int) -> None:
        now = time.monotonic()
        if self._started is None:
            self._started = now
        self._marks.append((now, moved_total))
        while len(self._marks) > 2 and now - self._marks[0][0] > RATE_WINDOW_S:
            self._marks.pop(0)
        span = now - self._marks[0][0]
        if span > 0.5:
            self.state["rate"] = max(0.0, (moved_total - self._marks[0][1]) / span)
        else:
            # The trailing window can be shorter than its own minimum span on a
            # fast link, which left short transfers reporting no rate at all.
            # An average over what has elapsed is still a real measurement.
            elapsed = now - self._started
            if elapsed > 0.1:
                self.state["rate"] = max(0.0, moved_total / elapsed)

    # -- lifecycle ------------------------------------------------------

    def start(self, peers: List[Any], wanted: Dict[str, str],
              sizes: Dict[str, int]) -> None:
        with self._lock:
            self.state.update({
                "running": True,
                "started": time.time(),
                "nodes": [{"name": p.name or p.ip, "id": p.id, "state": "waiting",
                           "sent": [], "skipped": [], "error": ""} for p in peers],
            })
        self._thread = threading.Thread(
            target=self._run, args=(peers, wanted, sizes), name="push", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- the work -------------------------------------------------------

    def _run(self, peers: List[Any], wanted: Dict[str, str],
             sizes: Dict[str, int]) -> None:
        try:
            for index, peer in enumerate(peers):
                if self._cancel.is_set():
                    break
                self._push_to(index, peer, wanted, sizes)
        except Exception as exc:  # noqa: BLE001 - a push must not kill the service
            log.exception("push failed")
            with self._lock:
                self.state["error"] = str(exc)
        finally:
            with self._lock:
                self.state["running"] = False
                self.state["done"] = True
                self.state["cancelled"] = self._cancel.is_set()
                self.state["finished"] = time.time()
                self.state["node"] = ""
                self.state["file"] = ""
                self.state["rate"] = 0.0

    def _push_to(self, index: int, peer: Any, wanted: Dict[str, str],
                 sizes: Dict[str, int]) -> None:
        entry = self.state["nodes"][index]
        with self._lock:
            entry["state"] = "checking"
            self.state["node"] = entry["name"]
            self.state["file"] = ""
        try:
            have = self._deps["remote_hashes"](peer)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                entry["state"] = "failed"
                entry["error"] = str(exc)
            return

        missing = [n for n, digest in wanted.items() if have.get(n) != digest]
        skipped = [n for n in wanted if n not in missing]
        # The total is per node and only counts what will actually be sent, so
        # the bar reflects the transfer rather than the size of the playlist.
        with self._lock:
            entry["skipped"] = skipped
            entry["state"] = "sending" if missing else "playlist"
            self.state["total_bytes"] += sum(sizes.get(n, 0) for n in missing)

        for name in missing:
            if self._cancel.is_set():
                with self._lock:
                    entry["state"] = "cancelled"
                return
            path = self._deps["resolve"](name)
            if path is None:
                continue
            size = sizes.get(name, 0)
            with self._lock:
                self.state["file"] = name
                self.state["file_bytes"] = size
                self.state["sent_bytes"] = 0

            def progress(sent: int, _name: str = name) -> None:
                with self._lock:
                    self.state["sent_bytes"] = sent
                    self._note_rate(self.state["done_bytes"] + sent)

            try:
                self._deps["upload"](peer, name, Path(path), progress)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    entry["state"] = "failed"
                    entry["error"] = str(exc)
                    # Give up on this node, and count everything it was going to
                    # receive as accounted for. Counting only the file that
                    # failed left the overall bar stuck short — at 83% with one
                    # node down — for the rest of the push, which reads as a
                    # second, invisible problem.
                    remaining = missing[missing.index(name):]
                    self.state["done_bytes"] += sum(sizes.get(n, 0) for n in remaining)
                    self.state["sent_bytes"] = 0
                return
            with self._lock:
                entry["sent"].append(name)
                self.state["done_bytes"] += size
                self.state["sent_bytes"] = 0

        with self._lock:
            entry["state"] = "playlist"
            self.state["file"] = "playlist definition"
        try:
            self._deps["send_playlist"](peer)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                entry["state"] = "failed"
                entry["error"] = str(exc)
            return
        with self._lock:
            entry["state"] = "done"
