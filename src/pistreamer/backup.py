"""Taking a node's settings off it, and putting them back on another one.

An SD card dies on a Friday. The replacement is flashed in ten minutes and then
somebody spends an hour retyping playlists, favourites, cues, shaders and the
group key from memory, on a show day, under time pressure. That is the failure
this exists for — not disaster recovery in the abstract, but the twenty minutes
between a card failing and the wall needing to be back.

What travels is what a person typed: settings, playlists, the schedule,
favourites and shaders. What does not is anything the node can work out for
itself — its telemetry, its rendered document pages, its snapshots — and, above
all, media files. A library is gigabytes and already has a way to move between
nodes; `/api/cluster/push` is that way, and duplicating it here would produce a
"backup" too big to keep.

The group key is included, because a restored node that cannot rejoin its own
group has not been restored. That makes the file a credential: it is only ever
handed to somebody already logged in, and the GUI says plainly what is in it.
The operator password is NOT included — a restore should not silently carry a
login somebody has since changed.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import config, favourites, playlists, schedule, shaders

log = logging.getLogger(__name__)

FORMAT = 1

# Settings that describe *this* node rather than how it is set up. Restoring
# them onto a different box is how two nodes end up with one name, or a spare
# comes up believing it is the machine it replaced.
IDENTITY_KEYS = {"device_name", "identify", "setup_complete", "auth_enabled"}

# Never leaves in a backup: the operator credential lives in auth.json and is
# deliberately not part of this.
EXCLUDED_KEYS: set = set()


def build() -> Dict[str, Any]:
    cfg = config.load().to_dict()
    return {
        "format": FORMAT,
        "created": time.time(),
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "device_name": cfg.get("device_name", ""),
        "config": {k: v for k, v in cfg.items() if k not in EXCLUDED_KEYS},
        "playlists": [p.to_dict() for p in playlists.all_playlists()],
        "schedule": [c.to_dict() for c in schedule.all_cues()],
        "favourites": [f.to_dict() for f in favourites.all_favourites()],
        "shaders": {s["name"]: shaders.get(s["name"]) or ""
                    for s in shaders.all_shaders()},
    }


def to_bytes(data: Dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, default=str) + "\n").encode()


def filename(device_name: str = "") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in (device_name or "pistreamer")
                   if ch.isalnum() or ch in "-_") or "pistreamer"
    return f"{safe}-settings-{stamp}.json"


def check(data: Any) -> str:
    """Why this cannot be restored, or "" if it can."""
    if not isinstance(data, dict):
        return "that is not a settings file"
    if "config" not in data:
        return "that file has no settings in it"
    version = data.get("format")
    if not isinstance(version, int) or version > FORMAT:
        return (f"that file was written by a newer version "
                f"(format {version}, this node understands {FORMAT})")
    return ""


def restore(data: Dict[str, Any], keep_identity: bool = True) -> Dict[str, Any]:
    """Apply a backup. Returns what was restored and what was refused.

    Every section is applied independently: a playlist referring to a file this
    node has not got should not stop the schedule being restored, and finding
    that out one section at a time is the difference between a usable node and
    an evening of guessing.
    """
    problem = check(data)
    if problem:
        raise ValueError(problem)

    report: Dict[str, Any] = {"config": 0, "playlists": 0, "schedule": 0,
                              "favourites": 0, "shaders": 0, "skipped": []}

    known = set(config.Config().to_dict().keys())
    patch = {k: v for k, v in (data.get("config") or {}).items() if k in known}
    if keep_identity:
        # A spare coming up believing it is the node it replaced is worse than
        # one that needs renaming.
        for key in IDENTITY_KEYS:
            patch.pop(key, None)
    unknown = set((data.get("config") or {})) - known
    if unknown:
        report["skipped"].append(
            f"settings this version does not have: {', '.join(sorted(unknown))}")
    if patch:
        config.update(**patch)
        report["config"] = len(patch)

    for raw in data.get("playlists") or []:
        try:
            # Without require_media=False every playlist is rejected on a
            # freshly flashed node, because the media has not been pushed yet
            # — which is precisely when a restore happens.
            playlists.save(playlists.Playlist(
                name=raw["name"], items=raw.get("items") or [],
                loop=raw.get("loop", True), shuffle=raw.get("shuffle", False),
                image_duration=raw.get("image_duration", 10)),
                require_media=False)
            report["playlists"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad entry is not the lot
            report["skipped"].append(f"playlist {raw.get('name', '?')}: {exc}")

    for raw in data.get("schedule") or []:
        try:
            schedule.save(schedule.Cue(
                id=raw.get("id") or f"cue-{int(time.time() * 1000)}",
                time=raw["time"], action=raw["action"],
                target=raw.get("target", ""),
                days=raw.get("days") or list(range(7)),
                enabled=raw.get("enabled", True), label=raw.get("label", "")))
            report["schedule"] += 1
        except Exception as exc:  # noqa: BLE001
            report["skipped"].append(f"cue {raw.get('label') or raw.get('id', '?')}: {exc}")

    for raw in data.get("favourites") or []:
        try:
            favourites.save(favourites.Favourite(
                name=raw["name"], url=raw["url"], kind=raw.get("kind", "web")))
            report["favourites"] += 1
        except Exception as exc:  # noqa: BLE001
            report["skipped"].append(f"favourite {raw.get('name', '?')}: {exc}")

    for name, source in (data.get("shaders") or {}).items():
        try:
            shaders.save(name, source)
            report["shaders"] += 1
        except Exception as exc:  # noqa: BLE001
            report["skipped"].append(f"shader {name}: {exc}")

    log.info("restored settings: %s", {k: v for k, v in report.items()
                                       if k != "skipped"})
    return report


def summary() -> Dict[str, Any]:
    """What a backup taken now would contain."""
    return {
        "playlists": len(playlists.all_playlists()),
        "schedule": len(schedule.all_cues()),
        "favourites": len(favourites.all_favourites()),
        "shaders": len(shaders.all_shaders()),
        "includes_group_key": True,
        "includes_password": False,
        "includes_media": False,
    }
