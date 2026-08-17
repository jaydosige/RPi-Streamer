"""Host telemetry and power actions.

Everything here reads /proc and /sys directly rather than pulling in psutil,
to keep the image small and the dependency list short. All readers degrade to
None on a non-Pi host so the GUI still runs on a dev machine.

Rate-based figures (CPU %, network throughput) are differences between
successive calls, so the first call after start returns None for those and the
sampler in telemetry.py is what actually drives them.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
_UPTIME = Path("/proc/uptime")
_MEMINFO = Path("/proc/meminfo")
_STAT = Path("/proc/stat")
_LOADAVG = Path("/proc/loadavg")
_NET_DEV = Path("/proc/net/dev")
_CPUINFO = Path("/proc/cpuinfo")
_MODEL = Path("/proc/device-tree/model")
_SYS_NET = Path("/sys/class/net")
_CPUFREQ = Path("/sys/devices/system/cpu/cpu0/cpufreq")

_lock = threading.Lock()
_prev_cpu: Optional[Dict[str, tuple]] = None
_prev_net: Optional[tuple] = None  # (timestamp, {iface: (rx, tx, ...)})


# ----------------------------------------------------------------------
# CPU
# ----------------------------------------------------------------------


def _read_cpu_lines() -> Dict[str, tuple]:
    out: Dict[str, tuple] = {}
    try:
        for line in _STAT.read_text().splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            key = parts[0]
            vals = [int(x) for x in parts[1:]]
            if len(vals) < 4:
                continue
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            out[key] = (idle, sum(vals))
    except (OSError, ValueError, IndexError):
        pass
    return out


def cpu_usage() -> Dict[str, Optional[float]]:
    """Overall and per-core CPU percentage since the previous call."""
    global _prev_cpu
    current = _read_cpu_lines()
    with _lock:
        prev = _prev_cpu
        _prev_cpu = current
    result: Dict[str, Optional[float]] = {"total": None, "cores": []}
    if not prev or not current:
        return result

    def pct(key: str) -> Optional[float]:
        if key not in prev or key not in current:
            return None
        d_idle = current[key][0] - prev[key][0]
        d_total = current[key][1] - prev[key][1]
        if d_total <= 0:
            return None
        return round(100.0 * (1.0 - d_idle / d_total), 1)

    result["total"] = pct("cpu")
    cores = sorted(k for k in current if re.fullmatch(r"cpu\d+", k))
    result["cores"] = [pct(k) for k in cores]
    return result


def cpu_percent() -> Optional[float]:
    """Backwards-compatible overall CPU percentage."""
    return cpu_usage()["total"]


def cpu_freq() -> Dict[str, Optional[int]]:
    """Current and maximum CPU clock in MHz."""
    out: Dict[str, Optional[int]] = {"current": None, "max": None}
    for key, name in (("current", "scaling_cur_freq"), ("max", "scaling_max_freq")):
        try:
            out[key] = int((_CPUFREQ / name).read_text().strip()) // 1000
        except (OSError, ValueError):
            pass
    return out


def load_average() -> List[float]:
    try:
        return [float(x) for x in _LOADAVG.read_text().split()[:3]]
    except (OSError, ValueError, IndexError):
        return []


def cpu_temp() -> Optional[float]:
    try:
        return int(_THERMAL.read_text().strip()) / 1000.0
    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------
# Memory, disk, uptime
# ----------------------------------------------------------------------


def uptime() -> Optional[float]:
    try:
        return float(_UPTIME.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def memory() -> Dict[str, Optional[int]]:
    wanted = {
        "MemTotal:": "total",
        "MemAvailable:": "available",
        "MemFree:": "free",
        "SwapTotal:": "swap_total",
        "SwapFree:": "swap_free",
    }
    out: Dict[str, Optional[int]] = {v: None for v in wanted.values()}
    try:
        for line in _MEMINFO.read_text().splitlines():
            key = line.split()[0]
            if key in wanted:
                out[wanted[key]] = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def disk() -> Dict[str, Optional[int]]:
    try:
        usage = shutil.disk_usage("/")
        return {"total": usage.total, "free": usage.free, "used": usage.used}
    except OSError:
        return {"total": None, "free": None, "used": None}


# ----------------------------------------------------------------------
# Power / thermal
# ----------------------------------------------------------------------

_THROTTLE_BITS = [
    (0x1, "under-voltage now", True),
    (0x2, "ARM frequency capped now", True),
    (0x4, "currently throttled", True),
    (0x8, "soft temperature limit now", True),
    (0x10000, "under-voltage has occurred", False),
    (0x20000, "ARM frequency capping has occurred", False),
    (0x40000, "throttling has occurred", False),
    (0x80000, "soft temperature limit has occurred", False),
]


def throttled() -> Dict[str, object]:
    """Decode vcgencmd get_throttled into current vs historical conditions.

    Under-voltage is the single most common cause of a Pi behaving oddly under
    load, and it looks exactly like a decode problem unless you check here.
    """
    out: Dict[str, object] = {"raw": None, "now": [], "since_boot": [], "ok": None}
    if not shutil.which("vcgencmd"):
        return out
    try:
        proc = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        )
        m = re.search(r"throttled=0x([0-9a-fA-F]+)", proc.stdout)
        if not m:
            return out
        bits = int(m.group(1), 16)
        out["raw"] = f"0x{bits:x}"
        out["now"] = [label for mask, label, is_now in _THROTTLE_BITS if is_now and bits & mask]
        out["since_boot"] = [
            label for mask, label, is_now in _THROTTLE_BITS if not is_now and bits & mask
        ]
        out["ok"] = bits == 0
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return out


def core_voltage() -> Optional[float]:
    if not shutil.which("vcgencmd"):
        return None
    try:
        proc = subprocess.run(
            ["vcgencmd", "measure_volts", "core"], capture_output=True, text=True, timeout=5
        )
        m = re.search(r"volt=([\d.]+)V", proc.stdout)
        return float(m.group(1)) if m else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


# ----------------------------------------------------------------------
# Network
# ----------------------------------------------------------------------


def _read_net_counters() -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    try:
        lines = _NET_DEV.read_text().splitlines()[2:]
    except OSError:
        return out
    for line in lines:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        f = rest.split()
        if len(f) < 16:
            continue
        out[name] = {
            "rx_bytes": int(f[0]),
            "rx_packets": int(f[1]),
            "rx_errs": int(f[2]),
            "rx_drop": int(f[3]),
            "tx_bytes": int(f[8]),
            "tx_packets": int(f[9]),
            "tx_errs": int(f[10]),
            "tx_drop": int(f[11]),
        }
    return out


def _link_info(iface: str) -> Dict[str, object]:
    base = _SYS_NET / iface
    info: Dict[str, object] = {"speed_mbps": None, "duplex": None, "operstate": None, "mac": None}
    for key, filename in (
        ("duplex", "duplex"),
        ("operstate", "operstate"),
        ("mac", "address"),
    ):
        try:
            value = (base / filename).read_text().strip()
            # Virtual and down interfaces report "unknown" here; that is not
            # information, so do not show it as though it were.
            info[key] = value if value and value != "unknown" else None
        except OSError:
            pass
    try:
        # Reading speed on a down interface raises EINVAL, and virtual
        # interfaces report -1. Both mean "no negotiated link speed".
        speed = int((base / "speed").read_text().strip())
        info["speed_mbps"] = speed if speed > 0 else None
    except (OSError, ValueError):
        pass
    return info


_WIRELESS = Path("/proc/net/wireless")


def wifi() -> Dict[str, object]:
    """Wireless link quality, if the node is on Wi-Fi.

    Worth having in its own right: a marginal Wi-Fi link and an overloaded CPU
    produce the same symptom — dropped frames — and this is how you tell them
    apart without unplugging anything. Signal below about -70 dBm, or a link
    rate well under the stream's bitrate, is the network's fault.
    """
    out: Dict[str, object] = {
        "present": False, "interface": None, "ssid": None, "signal_dbm": None,
        "link_quality": None, "noise_dbm": None, "tx_bitrate_mbps": None,
        "rx_bitrate_mbps": None, "frequency_ghz": None, "retries": None,
        "missed_beacons": None, "power_save": None,
    }

    iface = None
    try:
        for line in _WIRELESS.read_text().splitlines()[2:]:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            iface = name.strip()
            f = rest.split()
            # Columns: status link level noise nwid crypt frag retry misc beacon
            if len(f) >= 4:
                out["link_quality"] = float(f[1].rstrip("."))
                out["signal_dbm"] = float(f[2].rstrip("."))
                out["noise_dbm"] = float(f[3].rstrip("."))
            if len(f) >= 9:
                out["missed_beacons"] = int(float(f[8].rstrip(".")))
            break
    except (OSError, ValueError, IndexError):
        pass

    if iface is None:
        return out
    out["present"] = True
    out["interface"] = iface

    if shutil.which("iw"):
        try:
            proc = subprocess.run(
                ["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=5
            )
            text = proc.stdout
            m = re.search(r"SSID:\s*(.+)", text)
            if m:
                out["ssid"] = m.group(1).strip()
            m = re.search(r"signal:\s*(-?\d+)", text)
            if m:
                out["signal_dbm"] = float(m.group(1))
            m = re.search(r"freq:\s*(\d+)", text)
            if m:
                out["frequency_ghz"] = round(int(m.group(1)) / 1000.0, 3)
            m = re.search(r"tx bitrate:\s*([\d.]+)", text)
            if m:
                out["tx_bitrate_mbps"] = float(m.group(1))
            m = re.search(r"rx bitrate:\s*([\d.]+)", text)
            if m:
                out["rx_bitrate_mbps"] = float(m.group(1))
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

        try:
            proc = subprocess.run(
                ["iw", "dev", iface, "station", "dump"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"tx retries:\s*(\d+)", proc.stdout)
            if m:
                out["retries"] = int(m.group(1))
        except (subprocess.SubprocessError, OSError, ValueError):
            pass

    if shutil.which("iw"):
        try:
            proc = subprocess.run(
                ["iw", "dev", iface, "get", "power_save"],
                capture_output=True, text=True, timeout=5,
            )
            if "on" in proc.stdout.lower():
                out["power_save"] = True
            elif "off" in proc.stdout.lower():
                out["power_save"] = False
        except (subprocess.SubprocessError, OSError):
            pass
    return out


def _addresses() -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
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
        if len(parts) < 4 or parts[1] == "lo":
            continue
        out.setdefault(parts[1], []).append(parts[3])
    return out


def network() -> List[Dict[str, object]]:
    """Per-interface addresses, link state and throughput since the last call.

    Throughput matters here specifically: full-bandwidth NDI at 1080p60 is
    roughly 130 Mbps, so seeing the actual rate against the link speed tells
    you immediately whether you are near the ceiling.
    """
    global _prev_net
    now = time.monotonic()
    counters = _read_net_counters()
    with _lock:
        prev = _prev_net
        _prev_net = (now, counters)

    addresses = _addresses()
    elapsed = (now - prev[0]) if prev else 0.0

    out: List[Dict[str, object]] = []
    for iface, c in sorted(counters.items()):
        entry: Dict[str, object] = {
            "interface": iface,
            "addresses": addresses.get(iface, []),
            "rx_mbps": None,
            "tx_mbps": None,
            "rx_bytes": c["rx_bytes"],
            "tx_bytes": c["tx_bytes"],
            "rx_errs": c["rx_errs"] + c["rx_drop"],
            "tx_errs": c["tx_errs"] + c["tx_drop"],
        }
        entry.update(_link_info(iface))
        if prev and elapsed > 0 and iface in prev[1]:
            p = prev[1][iface]
            entry["rx_mbps"] = round(
                (c["rx_bytes"] - p["rx_bytes"]) * 8 / elapsed / 1_000_000, 2
            )
            entry["tx_mbps"] = round(
                (c["tx_bytes"] - p["tx_bytes"]) * 8 / elapsed / 1_000_000, 2
            )
        out.append(entry)
    return out


# ----------------------------------------------------------------------
# Identity and versions
# ----------------------------------------------------------------------


def hostname() -> str:
    return socket.gethostname()


def _first_line(cmd: List[str]) -> Optional[str]:
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        text = (proc.stdout or proc.stderr).strip()
        return text.splitlines()[0] if text else None
    except (subprocess.SubprocessError, OSError, IndexError):
        return None


_versions_cache: Optional[Dict[str, Optional[str]]] = None


def versions() -> Dict[str, Optional[str]]:
    """Component versions. Cached — these do not change while we run."""
    global _versions_cache
    if _versions_cache is not None:
        return _versions_cache

    ndi = None
    try:
        libs = sorted(Path("/usr/local/lib").glob("libndi.so.*"))
        if libs:
            ndi = libs[-1].name.replace("libndi.so.", "")
    except OSError:
        pass

    os_name = None
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass

    model = None
    try:
        model = _MODEL.read_text().replace("\x00", "").strip()
    except OSError:
        pass

    serial, revision = None, None
    try:
        for line in _CPUINFO.read_text().splitlines():
            if line.startswith("Serial"):
                serial = line.split(":", 1)[1].strip()
            elif line.startswith("Revision"):
                revision = line.split(":", 1)[1].strip()
    except (OSError, IndexError):
        pass

    from . import __version__

    _versions_cache = {
        "pistreamer": __version__,
        "model": model,
        "serial": serial,
        "revision": revision,
        "os": os_name,
        "kernel": os.uname().release,
        "gstreamer": _first_line(["gst-launch-1.0", "--version"]),
        "mpv": _first_line(["mpv", "--version"]),
        "ndi_sdk": ndi,
        "firmware": _first_line(["vcgencmd", "version"]),
    }
    return _versions_cache


def set_hostname(name: str) -> None:
    """Persist a new hostname. Requires root or CAP_SYS_ADMIN via hostnamectl."""
    clean = re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-").lower()[:63]
    if not clean:
        raise ValueError("invalid hostname")
    if shutil.which("hostnamectl"):
        subprocess.run(["hostnamectl", "set-hostname", clean], check=True, timeout=10)
    else:
        Path("/etc/hostname").write_text(clean + "\n")
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


# ----------------------------------------------------------------------
# Aggregate
# ----------------------------------------------------------------------


def summary() -> dict:
    cpu = cpu_usage()
    return {
        "hostname": hostname(),
        "uptime": uptime(),
        "cpu_percent": cpu["total"],
        "cpu_cores": cpu["cores"],
        "cpu_freq": cpu_freq(),
        "load": load_average(),
        "cpu_temp": cpu_temp(),
        "throttled": throttled(),
        "core_voltage": core_voltage(),
        "memory": memory(),
        "disk": disk(),
        "network": network(),
        "wifi": wifi(),
        "versions": versions(),
        "time": time.time(),
    }


# ----------------------------------------------------------------------
# Overclocking and audio devices
# ----------------------------------------------------------------------

_OC_BEGIN = "# --- pi-streamer overclock (managed, do not edit inside) ---"
_OC_END = "# --- end pi-streamer overclock ---"
OVERCLOCK_PRESETS = ("stock", "mild", "moderate", "maximum")


def boot_config_path() -> Path:
    for candidate in (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")):
        if candidate.exists():
            return candidate
    return Path("/boot/firmware/config.txt")


def overclock_status() -> Dict[str, object]:
    """Read the applied preset straight from config.txt.

    Reading needs no privilege — config.txt is world-readable — so only the
    *write* path goes through the root helper. That keeps the common case
    (showing the current state) free of any escalation machinery.
    """
    path = boot_config_path()
    out: Dict[str, object] = {
        "preset": "stock",
        "settings": {},
        "config": str(path),
        "max_mhz": None,
        "available": path.exists(),
        "presets": list(OVERCLOCK_PRESETS),
    }
    freq = cpu_freq()
    out["max_mhz"] = freq.get("max")
    if not path.exists():
        out["error"] = f"{path} not found — is this a Raspberry Pi?"
        return out
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        out["error"] = str(exc)
        return out

    inside = False
    settings: Dict[str, str] = {}
    preset = "stock"
    for line in lines:
        if line.strip() == _OC_BEGIN:
            inside = True
            preset = "custom"
            continue
        if line.strip() == _OC_END:
            inside = False
            continue
        if not inside:
            continue
        if line.startswith("# preset:"):
            preset = line.split(":", 1)[1].strip() or "custom"
        elif "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()
    out["preset"] = preset
    out["settings"] = settings
    return out


def audio_devices() -> Dict[str, object]:
    """Enumerate what we can actually play audio to.

    Guessing an ALSA device name is the single most common reason "audio does
    not work" on a Pi: the HDMI outputs are separate cards (vc4hdmi0/1) and
    "default" often is not one of them. So list the real names, with the
    descriptions ALSA gives, and let the GUI offer them.
    """
    out: Dict[str, object] = {"alsa": [], "cards": [], "mpv": [], "error": ""}

    try:
        cards = Path("/proc/asound/cards").read_text()
        for line in cards.splitlines():
            line = line.strip()
            m = re.match(r"^(\d+)\s+\[([^\]]+)\]:\s*(.*)$", line)
            if m:
                out["cards"].append(
                    {"index": int(m.group(1)), "id": m.group(2).strip(), "name": m.group(3).strip()}
                )
    except OSError:
        pass

    if shutil.which("aplay"):
        try:
            proc = subprocess.run(["aplay", "-L"], capture_output=True, text=True, timeout=10)
            name, desc = None, []
            for raw in proc.stdout.splitlines():
                if not raw.strip():
                    continue
                if not raw.startswith((" ", "\t")):
                    if name:
                        out["alsa"].append({"device": name, "description": " ".join(desc).strip()})
                    name, desc = raw.strip(), []
                else:
                    desc.append(raw.strip())
            if name:
                out["alsa"].append({"device": name, "description": " ".join(desc).strip()})
        except (subprocess.SubprocessError, OSError) as exc:
            out["error"] = f"aplay -L failed: {exc}"
    else:
        out["error"] = "aplay not installed (alsa-utils)"

    # mpv keeps its own device list and its own naming, so local playback
    # needs those names rather than the raw ALSA ones.
    if shutil.which("mpv"):
        try:
            proc = subprocess.run(
                ["mpv", "--audio-device=help"], capture_output=True, text=True, timeout=10
            )
            for raw in proc.stdout.splitlines():
                m = re.match(r"^\s*'([^']+)'\s*\((.*)\)\s*$", raw)
                if m:
                    out["mpv"].append({"device": m.group(1), "description": m.group(2)})
        except (subprocess.SubprocessError, OSError):
            pass

    # Prefer HDMI: on a Pi that is nearly always what is wanted, and it is
    # never the ALSA default.
    hdmi = [d for d in out["alsa"] if "vc4hdmi" in d["device"].lower()]
    out["suggested"] = hdmi[0]["device"] if hdmi else ""
    return out


def test_tone(device: str = "", seconds: int = 2) -> Dict[str, object]:
    """Play a short tone so a device choice can be confirmed, not guessed."""
    if not shutil.which("speaker-test"):
        return {"ok": False, "error": "speaker-test not installed (alsa-utils)"}
    cmd = ["speaker-test", "-t", "sine", "-f", "440", "-l", "1", "-p", str(max(1, seconds) * 1000)]
    if device:
        cmd += ["-D", device]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=max(5, seconds + 8))
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "device": device or "default",
            "output": (proc.stdout + proc.stderr).strip()[-1200:],
            "error": "" if ok else "speaker-test reported a failure",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "device": device or "default", "error": "timed out"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {"ok": False, "device": device or "default", "error": str(exc)}
