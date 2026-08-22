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
        # Read the modes whatever the status says. A connector forced on from
        # cmdline.txt (video=HDMI-A-1:1920x1080@60e, for a node running with
        # nothing plugged in) lists modes the kernel will happily set, and
        # discarding them because sysfs called it disconnected is what made
        # that workaround appear not to work.
        modes = _parse_modes(entry / "modes")
        out.append(
            Connector(
                name=match.group("name"),
                connected=connected,
                modes=modes,
                current=str(modes[0]) if modes else None,
            )
        )
    return out


def parse_mode(text: str) -> Optional[Mode]:
    """Parse "1920x1080@60" / "1920x1080" into a Mode."""
    m = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*(?:@\s*(\d+))?\s*$", text or "")
    if not m:
        return None
    return Mode(int(m.group(1)), int(m.group(2)), int(m.group(3) or 60))


def target_mode(connector: Optional[Connector], requested: str = "") -> Optional[Mode]:
    """Resolve which mode to actually drive, as one the connector reports.

    kmssink will only set a mode that matches the incoming frame size
    *exactly*: configure_mode_setting() walks the connector's mode list
    looking for hdisplay/vdisplay equal to the video width/height, and fails
    outright if nothing matches. So rather than hoping an NDI sender happens
    to produce a resolution the monitor advertises, we scale the video to a
    mode we know is on the list.

    Falls back to the kernel's preferred mode, which is the first entry in
    the connector's modes file.
    """
    if connector is None or not connector.modes:
        return None
    if requested:
        want = parse_mode(requested)
        if want:
            # Prefer an exact width/height/refresh hit, then ignore refresh.
            for mode in connector.modes:
                if (mode.width, mode.height, mode.refresh) == (
                    want.width,
                    want.height,
                    want.refresh,
                ):
                    return mode
            for mode in connector.modes:
                if (mode.width, mode.height) == (want.width, want.height):
                    return mode
    return connector.modes[0]


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
