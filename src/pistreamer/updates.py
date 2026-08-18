"""Updating the node from the GUI.

The work happens in a root job (see scripts/pistreamer-update); this module is
the app's half — it reads what that job publishes and asks it for things.

The split is forced by the service's own unit. It sets NoNewPrivileges=yes, so
it cannot escalate; and ProtectHome=yes, so it cannot even see the git working
copy in a login user's home. What is left is: write a request file, read a
status file. That is the whole interface, and it has one useful property — the
answer survives the service being restarted by the very update that produced it,
because it is a file rather than an HTTP response.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("PISTREAMER_CONFIG_DIR", "/etc/pistreamer"))
# Actions the root job understands. Anything else is refused here rather than
# written to a file for a root process to puzzle over.
ACTIONS = ("check", "apply", "rollback")


def build_path() -> Path:
    """What install.sh recorded about the code that is running."""
    return CONFIG_DIR / "build.json"


def status_path() -> Path:
    return config.STATE_DIR / "update.status"


def request_path() -> Path:
    return config.STATE_DIR / "update.request"


def build() -> Dict[str, Any]:
    try:
        return json.loads(build_path().read_text())
    except (OSError, ValueError):
        return {}


def status() -> Dict[str, Any]:
    try:
        return json.loads(status_path().read_text())
    except (OSError, ValueError):
        return {}


def available() -> bool:
    return bool((status().get("behind") or 0) > 0)


def busy() -> bool:
    """Is an update running right now?

    A stale "updating" would lock the button for ever if the root job died, so
    a phase that has not been touched for a while is not treated as busy.
    """
    st = status()
    if st.get("phase") not in ("checking", "updating", "installing", "restarting"):
        return False
    touched = st.get("updated_at") or st.get("started") or 0
    return (time.time() - touched) < 3600


def request(action: str, ref: str = "") -> Dict[str, Any]:
    """Ask the root job to do something. Returns immediately.

    Deliberately fire-and-forget: an update reinstalls the application and
    restarts this service, so there is no response to wait for. The GUI follows
    the status file instead.
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown update action: {action}")
    if busy():
        raise RuntimeError("an update is already running")
    payload = {"action": action, "ref": ref, "at": time.time()}
    path = request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written atomically, because a path unit watches this file and would
    # otherwise be able to read half of it.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".update-req-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log.info("requested update action %r", action)
    return payload


def helper_installed() -> bool:
    """Is the root job actually armed?

    A node installed before this feature existed has the app but not the unit,
    and the button would appear to work and then do nothing at all.
    """
    return Path("/etc/systemd/system/pistreamer-update.path").exists()


def summary() -> Dict[str, Any]:
    """Everything the GUI needs about updates, in one call."""
    st = status()
    info = build()
    ref = st.get("current") or {}
    return {
        # What is running: the status file is fresher (it is rewritten by an
        # update), but build.json is there from the first install.
        "current": ref or {k: info.get(k) for k in ("sha", "short", "subject", "date")},
        "source": info.get("source", "git"),
        "repo": st.get("repo") or info.get("repo", ""),
        "branch": st.get("branch", ""),
        "behind": st.get("behind", 0),
        "available": st.get("available"),
        "commits": st.get("commits", []),
        "checked_at": st.get("checked_at"),
        "phase": st.get("phase", "idle"),
        "action": st.get("action", ""),
        "ok": st.get("ok"),
        "message": st.get("message", ""),
        "log": st.get("log", []),
        "previous": st.get("previous"),
        "busy": busy(),
        "helper": helper_installed(),
        "updatable": info.get("source", "git") != "archive",
    }
