"""Saved sources: a URL, a friendly name, and which kind of thing it is.

Typing `http://dashboard.local:3000/wallboard?token=...` into a phone browser at
the back of a room, correctly, is not a thing anybody should be asked to do
twice. A favourite is the address plus the name somebody actually calls it —
"Kitchen dashboard", "Stage feed" — so going back to it is one tap.

Web pages and streams live in the same store deliberately. To an operator they
are the same act: pick the thing, put it on the screen. Keeping two lists whose
only difference is the URL scheme would mean two half-empty panels and a
question about which one a given address belongs in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

# Same character set as playlist names, for the same reason: it is shown in a
# GUI, sorted, and used as a dict key, and none of those want punctuation.
_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,60}$")

KINDS = ("web", "stream")

# Schemes a browser will load. Anything else — file://, data://, javascript: —
# is either useless on a signage screen or a way to read the node's own disk.
WEB_SCHEMES = ("http", "https")
# Schemes mpv can pull a live video stream from. HLS and DASH arrive over plain
# HTTP, which is why http(s) appears in both lists and why the kind has to be
# stated rather than guessed from the URL.
STREAM_SCHEMES = ("http", "https", "udp", "rtp", "rtsp", "rtmp", "srt", "tcp")

MAX_FAVOURITES = 100


@dataclass
class Favourite:
    name: str
    url: str
    kind: str = "web"
    added: float = 0.0
    # Bumped whenever it is played, so the GUI can offer the ones actually used
    # rather than the ones added first.
    used: float = 0.0
    uses: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def check_url(url: str, kind: str) -> str:
    """Return a cleaned URL, or raise ValueError explaining what is wrong.

    The message matters more than usual here: this is the one field a user types
    by hand, and "invalid URL" tells them nothing about which part they got
    wrong.
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("an address is required")
    if len(url) > 2000:
        raise ValueError("that address is too long")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    parsed = urllib.parse.urlparse(url)
    allowed = WEB_SCHEMES if kind == "web" else STREAM_SCHEMES
    if not parsed.scheme:
        raise ValueError(
            f"the address needs a scheme — try http://{url}"
            if kind == "web" else
            f"the address needs a scheme, e.g. udp://{url} or https://.../play.m3u8")
    if parsed.scheme.lower() not in allowed:
        raise ValueError(
            f"{parsed.scheme}: cannot be shown as a {kind} source — "
            f"use one of: {', '.join(allowed)}")
    # udp://238.0.0.1:1234 parses with a netloc; udp://@:1234 (listen on any
    # interface) is also legitimate and has one too. Only a completely empty
    # authority is actually unusable.
    if not parsed.netloc:
        raise ValueError("that address has no host or port in it")
    return url


def store_path() -> Path:
    return config.STATE_DIR / "favourites.json"


def _load_raw() -> Dict[str, dict]:
    path = store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("favourites.json unreadable (%s); starting empty", exc)
        return {}


def _save_raw(data: Dict[str, dict]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".favourites-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def all_favourites() -> List[Favourite]:
    out: List[Favourite] = []
    for name, raw in _load_raw().items():
        if not isinstance(raw, dict):
            continue
        out.append(Favourite(
            name=name,
            url=str(raw.get("url", "")),
            kind=str(raw.get("kind", "web")),
            added=float(raw.get("added", 0) or 0),
            used=float(raw.get("used", 0) or 0),
            uses=int(raw.get("uses", 0) or 0),
        ))
    # Most recently used first, then never-used alphabetically. The list is a
    # working set, not an archive.
    out.sort(key=lambda f: (-f.used, f.name.lower()))
    return out


def get(name: str) -> Optional[Favourite]:
    return next((f for f in all_favourites() if f.name == name), None)


def save(fav: Favourite) -> Favourite:
    if not valid_name(fav.name):
        raise ValueError(
            "a name may only contain letters, numbers, spaces, dots, "
            "dashes and underscores, up to 60 characters")
    fav.url = check_url(fav.url, fav.kind)
    data = _load_raw()
    if fav.name not in data and len(data) >= MAX_FAVOURITES:
        raise ValueError(f"that is more than {MAX_FAVOURITES} favourites — "
                         f"delete some first")
    existing = data.get(fav.name) or {}
    data[fav.name] = {
        "url": fav.url,
        "kind": fav.kind,
        # Keep the original add time and usage across an edit: renaming a
        # favourite should not send it to the bottom of the list.
        "added": float(existing.get("added") or fav.added or time.time()),
        "used": float(existing.get("used") or fav.used or 0.0),
        "uses": int(existing.get("uses") or fav.uses or 0),
    }
    _save_raw(data)
    return get(fav.name) or fav


def delete(name: str) -> bool:
    data = _load_raw()
    if name not in data:
        return False
    del data[name]
    _save_raw(data)
    return True


def mark_used(url: str) -> None:
    """Record that a favourite was played, matched by URL.

    By URL rather than by name because playing happens from several places —
    the favourites list, a retyped address, a schedule cue — and all of them
    should count. A URL that is not a favourite is simply not recorded.
    """
    data = _load_raw()
    changed = False
    for name, raw in data.items():
        if isinstance(raw, dict) and raw.get("url") == url:
            raw["used"] = time.time()
            raw["uses"] = int(raw.get("uses", 0) or 0) + 1
            changed = True
    if changed:
        try:
            _save_raw(data)
        except OSError as exc:  # noqa: BLE001 - never fail a play over bookkeeping
            log.debug("could not record favourite use: %s", exc)
