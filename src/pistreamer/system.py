"""Host telemetry and power actions.

Everything here reads /proc and /sys directly rather than pulling in psutil,
to keep the image small and the dependency list short. All readers degrade to
None on a non-Pi host so the GUI still runs on a dev machine.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
_UPTIME = Path("/proc/uptime")
_MEMINFO = Path("/proc/meminfo")
_STAT = Path("/proc/stat")

_prev_cpu: Optional[tuple[int, int]] = None


def cpu_temp() -> Optional[float]:
    try:
        return int(_THERMAL.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def uptime() -> Optional[float]:
    try:
        return float(_UPTIME.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpu_percent() -> Optional[float]:
    """Sample-to-sample CPU usage. First call after start returns None."""
    global _prev_cpu
    try:
        line = _STAT.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = [int(x) for x in line.split()[1:]]
    if len(parts) < 4:
        return None
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    prev = _prev_cpu
    _prev_cpu = (idle, total)
    if prev is None:
        return None
    d_idle = idle - prev[0]
    d_total = total - prev[1]
    if d_total <= 0:
        return None
    return round(100.0 * (1.0 - d_idle / d_total), 1)


def memory() -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {"total": None, "available": None}
    try:
        for line in _MEMINFO.read_text().splitlines():
            if line.startswith("MemTotal:"):
                out["total"] = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                out["available"] = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def disk() -> Dict[str, Optional[int]]:
    try:
        usage = shutil.disk_usage("/")
        return {"total": usage.total, "free": usage.free}
    except OSError:
        return {"total": None, "free": None}


def throttled() -> Optional[str]:
    """Pi-specific: undervoltage / thermal throttle flags from vcgencmd."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        proc = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        )
        m = re.search(r"throttled=0x([0-9a-fA-F]+)", proc.stdout)
        if not m:
            return None
        bits = int(m.group(1), 16)
        if bits == 0:
            return "ok"
        flags = []
        if bits & 0x1:
            flags.append("under-voltage now")
        if bits & 0x4:
            flags.append("frequency capped now")
        if bits & 0x8:
            flags.append("throttled now")
        if bits & 0x10000:
            flags.append("under-voltage occurred")
        if bits & 0x40000:
            flags.append("throttling occurred")
        return ", ".join(flags) if flags else "ok"
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def network() -> List[Dict[str, str]]:
    """Interfaces with an IPv4 address, from `ip -o -4 addr`."""
    out: List[Dict[str, str]] = []
    if not shutil.which("ip"):
        return out
    try:
        proc = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        return out
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface, addr = parts[1], parts[3]
        if iface == "lo":
            continue
        out.append({"interface": iface, "address": addr})
    return out


def hostname() -> str:
    return socket.gethostname()


def set_hostname(name: str) -> None:
    """Persist a new hostname. Requires root or CAP_SYS_ADMIN via hostnamectl."""
    clean = re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-").lower()[:63]
    if not clean:
        raise ValueError("invalid hostname")
    if shutil.which("hostnamectl"):
        subprocess.run(["hostnamectl", "set-hostname", clean], check=True, timeout=10)
    else:
        Path("/etc/hostname").write_text(clean + "\n")
    # Keep /etc/hosts consistent so sudo does not hang on name resolution.
    hosts = Path("/etc/hosts")
    if hosts.exists():
        try:
            lines = hosts.read_text().splitlines()
            new = [ln for ln in lines if not ln.startswith("127.0.1.1")]
            new.append(f"127.0.1.1\t{clean}")
            hosts.write_text("\n".join(new) + "\n")
        except OSError:
            pass


def reboot() -> None:
    subprocess.Popen(["systemctl", "reboot"], start_new_session=True)


def poweroff() -> None:
    subprocess.Popen(["systemctl", "poweroff"], start_new_session=True)


def summary() -> dict:
    mem = memory()
    dsk = disk()
    return {
        "hostname": hostname(),
        "uptime": uptime(),
        "cpu_percent": cpu_percent(),
        "cpu_temp": cpu_temp(),
        "throttled": throttled(),
        "memory": mem,
        "disk": dsk,
        "network": network(),
        "time": time.time(),
    }
