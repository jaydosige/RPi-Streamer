"""Asking the root helper to change the network or the hostname.

None of this can be done by the service itself. `pistreamer.service` sets
`ProtectSystem=full`, so /etc is read-only to it, and `NoNewPrivileges=yes`, so
the polkit route `hostnamectl` would normally take is closed too. The same
constraint that produced the overclock helper produces this one: the service
writes a request file it owns, and a path-activated root oneshot acts on it.

Everything here is therefore asynchronous and best-effort. A request is posted
and the answer arrives in a result file some seconds later — which is not a
limitation worth hiding, because the two interesting operations genuinely do
take the network down and come back. Joining a network cannot report its own
success over the connection it is replacing.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

ACTIONS = ("scan", "join", "hotspot-on", "hotspot-off", "hostname")
HELPER = Path("/opt/pistreamer/bin/pistreamer-netcfg")

# How long a caller should wait for the helper before deciding it is not there.
# Scans take a few seconds; joins take much longer and are never waited on.
WAIT_S = 25.0


def request_path() -> Path:
    return config.STATE_DIR / "network.request"


def result_path() -> Path:
    return config.STATE_DIR / "network.result"


def state_path() -> Path:
    return config.STATE_DIR / "network.state"


def helper_installed() -> bool:
    return HELPER.is_file()


def available() -> tuple[bool, str]:
    if not helper_installed():
        return False, ("the network helper is not installed on this node — "
                       "re-run install.sh once to add it")
    return True, ""


def _read(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def result() -> Dict[str, Any]:
    return _read(result_path())


def state() -> Dict[str, Any]:
    return _read(state_path())


def busy() -> bool:
    """Is a request still waiting to be picked up?"""
    return request_path().exists()


def submit(action: str, **params: Any) -> float:
    """Post a request and return the result file's mtime before it ran.

    The mtime is the only reliable way to tell a fresh answer from the previous
    one: the helper rewrites the same path every time, and a caller that polls
    for "a result" will otherwise read the last one instantly and believe it.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown network action: {action}")
    ok, reason = available()
    if not ok:
        raise RuntimeError(reason)
    if busy():
        raise RuntimeError("a network change is already in progress")

    before = _mtime(result_path())
    payload = {"action": action, "at": time.time(), **params}
    path = request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".network-")
    try:
        # 0600: a join request carries the Wi-Fi passphrase in clear, for as
        # long as it takes the helper to read and delete it.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log.info("network request posted: %s", action)
    return before


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def wait_for(before: float, timeout: float = WAIT_S) -> Optional[Dict[str, Any]]:
    """Block until the helper writes a result newer than `before`.

    Only for the operations that do not disturb the connection the caller is
    using — scanning, and setting the hostname. Waiting on a join or a hotspot
    would mean waiting on the network being taken away.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _mtime(result_path()) > before:
            return result()
        time.sleep(0.3)
    return None


def networks() -> List[Dict[str, Any]]:
    """The most recent scan, whenever it happened."""
    data = result()
    if data.get("action") == "scan" and data.get("ok"):
        found = data.get("networks")
        return found if isinstance(found, list) else []
    return []


def summary() -> Dict[str, Any]:
    """Everything the GUI needs to describe the node's network in one call."""
    ok, reason = available()
    current = state()
    last = result()
    return {
        "available": ok,
        "reason": reason,
        "busy": busy(),
        "hotspot": bool(current.get("hotspot")),
        "wifi": current.get("wifi") or "",
        "device": current.get("device") or "",
        "addresses": current.get("addresses") or [],
        "networks": networks(),
        "last": {k: last.get(k) for k in ("ok", "action", "message", "at")}
                if last else {},
        # Reported so the GUI can say what will happen rather than implying the
        # node can reconfigure a network it has no manager for.
        "nmcli": bool(current.get("nmcli", shutil.which("nmcli") is not None)),
    }
