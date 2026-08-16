"""DRM/KMS display discovery.

We drive the display directly through KMS with no X server or Wayland
compositor. This module only *reads* the kernel's view of the connectors
(via /sys/class/drm) so the GUI can show what's plugged in and offer the
modes the monitor actually reports. Setting the mode is the player's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DRM_SYSFS = Path("/sys/class/drm")

# On the Pi 4 the VideoCore card is card0 or card1 depending on boot order;
# connector dirs look like "card1-HDMI-A-1".
_CONNECTOR_RE = re.compile(r"^card\d+-(?P<name>.+)$")


@dataclass
class Mode:
    width: int
    height: int
    refresh: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height}@{self.refresh}"


@dataclass
class Connector:
    name: str  # e.g. "HDMI-A-1"
    connected: bool
    modes: List[Mode]
    current: Optional[str] = None


def _parse_modes(path: Path) -> List[Mode]:
    """Read a connector's modes file.

    Lines look like "1920x1080" on older kernels and "1920x1080@60" or
    "1920x1080p60" on newer ones. We normalise all three and dedupe while
    preserving the kernel's preference order (first line = preferred mode).
    """
    modes: List[Mode] = []
    seen: set[tuple[int, int, int]] = set()
    try:
        raw = path.read_text()
    except OSError:
        return modes

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)x(\d+)(?:[p i@]?(\d+))?", line)
        if not m:
            continue
        width, height = int(m.group(1)), int(m.group(2))
        refresh = int(m.group(3)) if m.group(3) else 60
        key = (width, height, refresh)
        if key in seen:
            continue
        seen.add(key)
        modes.append(Mode(width, height, refresh))
    return modes


def list_connectors() -> List[Connector]:
    """Enumerate DRM connectors. Returns [] on non-DRM hosts (e.g. a dev box)."""
    out: List[Connector] = []
    if not DRM_SYSFS.exists():
        return out

    for entry in sorted(DRM_SYSFS.iterdir()):
        match = _CONNECTOR_RE.match(entry.name)
        if not match or not entry.is_dir():
            continue
        status_file = entry / "status"
        if not status_file.exists():
            continue
        try:
            connected = status_file.read_text().strip() == "connected"
        except OSError:
            continue
        modes = _parse_modes(entry / "modes") if connected else []
        out.append(
            Connector(
                name=match.group("name"),
                connected=connected,
                modes=modes,
                current=str(modes[0]) if modes else None,
            )
        )
    return out


def pick_connector(preferred: str = "") -> Optional[Connector]:
    """Resolve the configured connector, falling back to the first connected one."""
    connectors = list_connectors()
    if preferred:
        for c in connectors:
            if c.name == preferred:
                return c
    for c in connectors:
        if c.connected:
            return c
    return connectors[0] if connectors else None


def _card_dir_for(connector_name: str) -> Optional[Path]:
    """Return the /sys/class/drm/cardN directory that owns a given connector."""
    if not DRM_SYSFS.exists():
        return None
    for entry in sorted(DRM_SYSFS.iterdir()):
        match = _CONNECTOR_RE.match(entry.name)
        if match and match.group("name") == connector_name:
            return DRM_SYSFS / entry.name.split("-", 1)[0]  # "card1"
    return None


def drm_card_for(connector_name: str) -> Optional[str]:
    """Return the /dev/dri/cardN device node that owns a given connector.

    Useful for mpv (--drm-device) and for diagnostics. NOT for kmssink — see
    drm_driver_for.
    """
    card = _card_dir_for(connector_name)
    return f"/dev/dri/{card.name}" if card else None


# The driver names kmssink itself probes for, from gstkmssink.c. We use this
# as a whitelist: if what we detect is not on it, drmOpen would reject the
# name anyway, so we stay quiet and let kmssink run its own probe.
KNOWN_DRM_DRIVERS = frozenset(
    {
        "i915", "nouveau", "radeon", "amdgpu", "omapdrm", "exynos", "tilcdc",
        "msm", "sti", "imx-drm", "rockchip", "atmel-hlcdc", "mediatek",
        "meson", "sun4i-drm", "vc4", "stm", "rcar-du", "vkms", "v3d",
    }
)


def drm_driver_for(connector_name: str) -> Optional[str]:
    """Return the DRM driver name for a connector, e.g. "vc4", or None.

    This is what kmssink's `driver-name` wants, and it is NOT the same string
    as the platform driver in sysfs. On a Pi, /sys/.../device/driver resolves
    to `vc4-drm` (the platform driver) while drmOpen only answers to `vc4`
    (the DRM driver). Feeding it the platform name fails with "Could not open
    DRM module vc4-drm".

    Nor is it a device path — kmssink's other selector, `bus-id`, is passed to
    drmOpen(NULL, bus_id) as a bus identifier, so a /dev/dri/cardN path there
    fails too.

    Returning None is a safe answer: the caller omits the property and
    kmssink probes its own list, which includes vc4.
    """
    card = _card_dir_for(connector_name)
    if card is None:
        return None
    try:
        raw = (card / "device" / "driver").resolve().name
    except OSError:
        return None

    # Check the unmodified name first: `imx-drm` and `sun4i-drm` are genuine
    # DRM driver names, so stripping the suffix unconditionally would break
    # those platforms.
    if raw in KNOWN_DRM_DRIVERS:
        return raw
    if raw.endswith("-drm") and raw[:-4] in KNOWN_DRM_DRIVERS:
        return raw[:-4]  # vc4-drm -> vc4
    return None
