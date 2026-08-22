"""Operator login for the web GUI.

The threat model is narrow and worth stating, because it decides everything
below. This is an appliance on an event LAN. The people who can reach it are
crew, guests on the same Wi-Fi, and whoever else is on that network. It is
served over plain HTTP — there is no certificate authority for `pistreamer.local`
and there is not going to be one — so this cannot protect against somebody who
can watch the wire. What it protects against is the far more common thing: a
guest, or a curious phone, finding an open control panel that can reboot the rig
or put something on the screen mid-show.

Three deliberate choices follow from that:

  * **Guest sharing is never behind the login.** The whole point of the QR code
    is that a stranger scans it and it works. Putting a password in front of
    `/s/{token}` would break the feature it is meant to sit beside. The token
    *is* the guest credential and it already expires on its own.

  * **Node-to-node traffic authenticates with the cluster key, not a session.**
    A follower has no browser and cannot log in. If a request carries a valid
    cluster key it is a peer, and it is let through — otherwise switching on
    auth would silently break every group command.

  * **The way back in is physical.** Deleting `auth.json` from the node turns
    the login off and restarts the setup wizard. Anyone who can do that already
    has the SD card. This is the escape hatch for a forgotten password, and it
    is documented rather than hidden.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

log = logging.getLogger(__name__)

ALGO = "pbkdf2_sha256"
# OWASP's current figure for PBKDF2-HMAC-SHA256. About 230ms on a Pi 4, which
# is unnoticeable on a login and expensive enough to matter to somebody working
# through a word list.
ITERATIONS = 600_000
SALT_BYTES = 16
SESSION_BYTES = 32
COOKIE_NAME = "pistreamer_session"

# Login throttling. Not a lockout — locking the operator out of their own rig
# ten minutes before doors is a worse failure than a slow brute force — but
# enough delay that guessing over the network is hopeless.
_MAX_STRIKES = 5
_LOCKOUT_S = 60.0

_lock = threading.Lock()
_strikes: Dict[str, list] = {}


# ----------------------------------------------------------------------
# Credential store
# ----------------------------------------------------------------------


def auth_path() -> Path:
    return config.STATE_DIR / "auth.json"


def sessions_path() -> Path:
    return config.STATE_DIR / "sessions.json"


def _write_private(path: Path, payload: str) -> None:
    """Write 0600, atomically. Both files here are credentials."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def hash_password(password: str, salt: Optional[bytes] = None,
                  iterations: int = ITERATIONS) -> dict:
    salt = salt or secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 iterations)
    return {"algo": ALGO, "salt": salt.hex(), "hash": digest.hex(),
            "iterations": iterations}


def _load_auth() -> Optional[dict]:
    path = auth_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and data.get("hash") else None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("auth.json unreadable (%s); treating the node as unconfigured", exc)
        return None


def configured() -> bool:
    """Has a login ever been set up on this node?"""
    return _load_auth() is not None


def username() -> str:
    return str((_load_auth() or {}).get("user", ""))


def set_password(user: str, password: str) -> None:
    """Create or replace the operator credential."""
    user = (user or "").strip()
    if not user:
        raise ValueError("a username is required")
    if len(user) > 64:
        raise ValueError("that username is too long")
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    record = {"user": user, "created": time.time(), **hash_password(password)}
    _write_private(auth_path(), json.dumps(record, indent=2) + "\n")
    log.info("operator credential set for %r", user)


def password_problem(password: str) -> str:
    """Why this password will not do, or "" if it is fine.

    Deliberately mild. A rig that gets a sticky note on the back because the
    rules demanded a symbol is less secure than one with a memorable passphrase,
    and this is a LAN appliance, not a bank.
    """
    if not password:
        return "a password is required"
    if len(password) < 8:
        return "the password must be at least 8 characters"
    if len(password) > 200:
        return "that password is too long"
    if password.lower() in ("password", "12345678", "pistreamer", "raspberry"):
        return "that password is too easy to guess"
    return ""


def verify(user: str, password: str) -> bool:
    record = _load_auth()
    if record is None:
        return False
    if not hmac.compare_digest(str(record.get("user", "")), user or ""):
        # Still do the hash, so a wrong username and a wrong password take the
        # same time. Otherwise the response time enumerates valid usernames.
        hash_password(password or "", bytes.fromhex(record["salt"]),
                      int(record.get("iterations", ITERATIONS)))
        return False
    try:
        candidate = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"),
            bytes.fromhex(record["salt"]),
            int(record.get("iterations", ITERATIONS)))
    except (ValueError, KeyError):
        return False
    return hmac.compare_digest(candidate.hex(), str(record.get("hash", "")))


def disable() -> None:
    """Remove the credential, which switches the login off entirely."""
    auth_path().unlink(missing_ok=True)
    clear_sessions()
    log.warning("operator login removed — the GUI is open to the network again")


# ----------------------------------------------------------------------
# Throttling
# ----------------------------------------------------------------------


def throttled_for(who: str) -> float:
    """Seconds this client must wait before another attempt, 0 if none."""
    with _lock:
        hits = [t for t in _strikes.get(who, []) if time.time() - t < _LOCKOUT_S]
        _strikes[who] = hits
        if len(hits) < _MAX_STRIKES:
            return 0.0
        return max(0.0, _LOCKOUT_S - (time.time() - hits[0]))


def note_failure(who: str) -> None:
    with _lock:
        _strikes.setdefault(who, []).append(time.time())
        del _strikes[who][:-_MAX_STRIKES]


def note_success(who: str) -> None:
    with _lock:
        _strikes.pop(who, None)


# ----------------------------------------------------------------------
# Sessions
#
# Persisted, on purpose. Applying an update restarts the service, and an
# operator who is logged out every time the node updates itself — potentially
# mid-show, from a tab they cannot re-authenticate on quickly — will turn the
# login off and never turn it back on.
# ----------------------------------------------------------------------


# Memory is the source of truth; the file is how it survives a restart. Every
# request checks a session, and the GUI polls every two seconds — reading and
# parsing a file on each of those would be a steady trickle of SD-card I/O for
# the entire length of a show.
_sessions: Optional[Dict[str, dict]] = None


def _load_sessions() -> Dict[str, dict]:
    global _sessions
    if _sessions is not None:
        return _sessions
    path = sessions_path()
    if not path.exists():
        _sessions = {}
        return _sessions
    try:
        data = json.loads(path.read_text())
        _sessions = data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        _sessions = {}
    return _sessions


def _save_sessions(data: Dict[str, dict]) -> None:
    global _sessions
    _sessions = data
    try:
        _write_private(sessions_path(), json.dumps(data) + "\n")
    except OSError as exc:  # noqa: BLE001 - a lost session is not fatal
        log.debug("could not persist sessions: %s", exc)


def _max_age_s() -> float:
    hours = getattr(config.load(), "auth_session_hours", 720) or 720
    return max(1.0, float(hours)) * 3600.0


def start_session(user: str) -> str:
    token = secrets.token_urlsafe(SESSION_BYTES)
    with _lock:
        data = _load_sessions()
        now = time.time()
        data = {t: s for t, s in data.items()
                if now - float(s.get("seen", 0) or 0) < _max_age_s()}
        data[token] = {"user": user, "created": now, "seen": now}
        _save_sessions(data)
    return token


def valid_session(token: str) -> bool:
    """Is this token live? Refreshes its idle clock as a side effect."""
    if not token:
        return False
    with _lock:
        data = _load_sessions()
        entry = data.get(token)
        if entry is None:
            return False
        now = time.time()
        if now - float(entry.get("seen", 0) or 0) >= _max_age_s():
            del data[token]
            _save_sessions(data)
            return False
        # Only rewrite when the clock has moved enough to matter. The GUI polls
        # every two seconds and this would otherwise be a disk write per poll,
        # for the whole life of the show, on an SD card.
        if now - float(entry.get("seen", 0) or 0) > 300:
            entry["seen"] = now
            _save_sessions(data)
        return True


def end_session(token: str) -> None:
    with _lock:
        data = _load_sessions()
        if data.pop(token, None) is not None:
            _save_sessions(data)


def clear_sessions() -> None:
    with _lock:
        _save_sessions({})


def summary() -> Dict[str, Any]:
    cfg = config.load()
    return {
        "configured": configured(),
        "enabled": bool(getattr(cfg, "auth_enabled", False)) and configured(),
        "user": username(),
        "sessions": len(_load_sessions()),
    }
