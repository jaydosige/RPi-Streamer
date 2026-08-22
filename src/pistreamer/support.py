"""Everything needed to diagnose a node, in one file you can send someone.

The alternative is a conversation: what does the log say, what does the status
say, which ffmpeg, is the plugin registered, what did the pipeline actually
spawn. Every answer takes a round trip and the interesting one is usually the
question nobody thought to ask. This gathers the lot in one request.

Nothing here is new information — it is the same data the GUI already shows,
plus the systemd journal, which is the one thing the GUI has never had a way to
reach. What is new is that it comes out as a file.

Two rules about what goes in it:

  * **Secrets do not.** The group key, the password hash, session tokens and
    Wi-Fi passphrases are all redacted, because this file is going to be
    attached to an email or dropped into a chat window. A bundle that cannot
    safely be sent is a bundle nobody sends.
  * **Failures are recorded, not raised.** Half a bundle from a node that is
    misbehaving is worth far more than an exception, and the parts that fail
    are usually the parts that matter — so each section catches its own
    problems and reports them in place.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

log = logging.getLogger(__name__)

# Config keys whose values never leave the node. Matched as substrings so a
# key added later with an obvious name is covered without anyone remembering
# to come back here.
SECRET_HINTS = ("key", "password", "passphrase", "secret", "token", "hash")
REDACTED = "[redacted]"

JOURNAL_LINES = 400


def _redact(data: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, value in data.items():
        if any(hint in name.lower() for hint in SECRET_HINTS) and value:
            # Length is kept because "the group key is empty" and "the group
            # key is wrong" are different faults and this distinguishes them.
            out[name] = f"{REDACTED} ({len(str(value))} chars)"
        else:
            out[name] = value
    return out


def _section(name: str, gather: Callable[[], Any]) -> Any:
    try:
        return gather()
    except Exception as exc:  # noqa: BLE001 - a broken section is itself a finding
        log.debug("support section %s failed: %s", name, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _run(args: List[str], timeout: int = 20) -> str:
    if not shutil.which(args[0]):
        return f"not installed: {args[0]}"
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (proc.stdout + proc.stderr).strip()[:20000]
    except (subprocess.SubprocessError, OSError) as exc:
        return f"failed: {exc}"


def journal(lines: int = JOURNAL_LINES) -> str:
    """The service's own log, which is where a crash actually lands.

    The GUI's log view is the player's ring buffer — it shows what the pipeline
    said, and nothing about the service that spawned it. A node that restarts
    in a loop leaves its evidence here and nowhere the GUI can see.
    """
    return _run(["journalctl", "-u", "pistreamer", "-n", str(lines),
                 "--no-pager", "--output", "short-iso"], timeout=30)


def helper_journals(lines: int = 60) -> Dict[str, str]:
    """The root helpers, which fail silently by design — they have no GUI."""
    return {
        unit: _run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager",
                    "--output", "short-iso"], timeout=20)
        for unit in ("pistreamer-netcfg", "pistreamer-update",
                     "pistreamer-overclock")
    }


def collect(parts: Dict[str, Callable[[], Any]]) -> Dict[str, Any]:
    """Build the bundle.

    The callers' own accessors are injected rather than imported so this module
    stays free of the player, the cluster and the rest — it is a reporter, and
    a reporter that can crash the thing it reports on is worse than no reporter.
    """
    bundle: Dict[str, Any] = {
        "bundle": {
            "generated": time.time(),
            "generated_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "format": 1,
            "note": "Secrets are redacted. Safe to attach to a bug report.",
        },
        "host": _section("host", lambda: {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "uid": os.getuid(),
            "cwd": os.getcwd(),
            "env": {k: v for k, v in os.environ.items()
                    if k.startswith(("PISTREAMER_", "GST_", "LD_LIBRARY", "HOME"))},
        }),
    }
    for name, gather in parts.items():
        bundle[name] = _section(name, gather)
    bundle["journal"] = _section("journal", journal)
    bundle["helper_journals"] = _section("helper_journals", helper_journals)
    bundle["tools"] = _section("tools", lambda: {
        tool: shutil.which(tool) or None
        for tool in ("mpv", "ffmpeg", "ffprobe", "gst-launch-1.0", "uxplay",
                     "chromium", "chromium-browser", "pdftoppm", "heif-convert",
                     "nmcli", "hostnamectl", "journalctl")
    })
    bundle["versions_cli"] = _section("versions_cli", lambda: {
        "mpv": _run(["mpv", "--version"])[:400],
        "ffmpeg": _run(["ffmpeg", "-version"])[:400],
        "kernel": _run(["uname", "-a"]),
    })
    bundle["disk"] = _section("disk", lambda: _run(["df", "-h"]))
    return bundle


def redact_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return _redact(cfg)


def filename(device_name: str = "") -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in (device_name or "pistreamer")
                   if ch.isalnum() or ch in "-_") or "pistreamer"
    return f"{safe}-support-{stamp}.json"


def to_bytes(bundle: Dict[str, Any]) -> bytes:
    # default=str so a stray Path or datetime cannot lose the whole bundle at
    # the last step, which would defeat the point of every section catching its
    # own failures.
    return (json.dumps(bundle, indent=2, sort_keys=True, default=str) + "\n").encode()
