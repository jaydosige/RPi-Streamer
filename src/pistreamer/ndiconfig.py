"""Write the NDI SDK's own configuration file.

A dual-homed node — Wi-Fi for management, Ethernet for media — is the normal
shape of an event install, and it is also where NDI discovery gets confusing.
libndi will happily use every interface it can see, which means it may
advertise or connect over the wrong one. The SDK exposes three settings for
exactly this, and they live in its JSON config rather than in any GStreamer
property:

  adapters.allowed   restrict NDI to specific NICs, by the NIC's own IP
  networks.ips       IPs to probe for senders directly, no mDNS needed
  networks.discovery an NDI Discovery Server, for when mDNS cannot work at all

The file is read by libndi when it initialises, so changing it means
restarting both the finder and the pipeline — the caller handles that.

Location: libndi looks in $HOME/.ndi/. The service's home is set to the state
directory precisely so this is writable by a non-root service user.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def config_dir() -> Path:
    return Path(os.environ.get("HOME", "/var/lib/pistreamer")) / ".ndi"


def config_path() -> Path:
    return config_dir() / "ndi-config.v1.json"


def _split(value: str) -> List[str]:
    return [part.strip() for part in (value or "").replace(";", ",").split(",") if part.strip()]


def build(adapter_ips: str = "", extra_ips: str = "", discovery_server: str = "") -> Dict[str, Any]:
    """Assemble the config, omitting anything not asked for.

    Only the keys actually being used are written. An over-specified adapters
    list is dangerous — the SDK documentation warns that naming NICs which do
    not exist can stop NDI working entirely — so an empty setting must produce
    no key at all rather than an empty list.
    """
    ndi: Dict[str, Any] = {}

    adapters = _split(adapter_ips)
    if adapters:
        ndi["adapters"] = {"allowed": adapters}

    networks: Dict[str, str] = {}
    ips = _split(extra_ips)
    if ips:
        # The SDK expects a comma-separated string here, not a list, and
        # tolerates the trailing comma its own examples show.
        networks["ips"] = ",".join(ips) + ","
    servers = _split(discovery_server)
    if servers:
        networks["discovery"] = ",".join(servers)
    if networks:
        ndi["networks"] = networks

    return {"ndi": ndi} if ndi else {}


def apply(adapter_ips: str = "", extra_ips: str = "", discovery_server: str = "") -> bool:
    """Write or remove the config file. Returns True if it changed on disk."""
    payload = build(adapter_ips, extra_ips, discovery_server)
    path = config_path()

    existing = None
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = None

    if not payload:
        if path.exists():
            try:
                path.unlink()
                log.info("removed %s (no NDI network overrides set)", path)
                return True
            except OSError as exc:
                log.warning("could not remove %s: %s", path, exc)
        return False

    if existing == payload:
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ndi-config-")
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        log.info("wrote %s: %s", path, json.dumps(payload))
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return False


def current() -> Dict[str, Any]:
    """What is on disk right now, for display in the GUI."""
    path = config_path()
    out: Dict[str, Any] = {"path": str(path), "exists": path.exists(), "config": None}
    if path.exists():
        try:
            out["config"] = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            out["config"] = {"error": str(exc)}
    return out
