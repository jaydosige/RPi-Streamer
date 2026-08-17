"""HTTP API and web GUI for a pi-streamer node."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (config, diagnose, display, media, ndiconfig, playlists,
               schedule as schedule_mod, sources, system)
from .player import MODE_IDLE, MODE_LOCAL, MODE_NDI, player
from .telemetry import telemetry

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

UPLOAD_CHUNK = 1024 * 1024

# ndisrc's timestamp-mode enum nicks, from gst-plugin-ndi.
TIMESTAMP_MODES = (
    "receive-time",
    "receive-time-vs-timecode",
    "receive-time-vs-timestamp",
    "timecode",
    "timestamp",
)
COLOR_FORMATS = (
    "uyvy-bgra",
    "bgrx-bgra",
    "rgbx-rgba",
    "uyvy-rgba",
    "fastest",
    "best",
    # These require the plugin to have been built against the NDI Advanced
    # SDK. In them ndisrc passes H.264/H.265 through undecoded so hardware can
    # decode it — the single biggest performance lever on a Pi.
    "compressed-v1",
    "compressed-v2",
    "compressed-v3",
    "compressed-v3-with-audio",
    "compressed-v4",
    "compressed-v4-with-audio",
    "compressed-v5",
    "compressed-v5-with-audio",
)
# Formats worth offering for the sink. BGRx is the safe default on vc4;
# "auto" negotiates from an ordered list of cheap formats.
VIDEO_FORMATS = ("auto", "BGRx", "RGBx", "BGRA", "RGB16", "NV12", "UYVY", "I420")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    cfg = config.load()
    # libndi reads its config when it initialises, so this has to happen
    # before the finder starts.
    ndiconfig.apply(cfg.ndi_adapter_ips, cfg.ndi_extra_ips, cfg.ndi_discovery_server)
    # The NDI finder needs to run continuously to see the network; start it
    # before anything else so it has been up for a while by the first poll.
    sources.start()
    player.start()
    telemetry.bind_player(player.stream_stats)
    telemetry.start()
    schedule_mod.scheduler.bind(apply_cue_action)
    if cfg.schedule_enabled:
        schedule_mod.scheduler.start()
    if cfg.autostart and cfg.mode != MODE_IDLE:
        target = cfg.ndi_source if cfg.mode == MODE_NDI else cfg.local_file
        log.info("autostarting mode=%s target=%s", cfg.mode, target)
        player.apply(cfg.mode, target)
    yield
    player.shutdown()
    sources.stop()
    telemetry.stop()
    schedule_mod.scheduler.stop()


def apply_cue_action(action: str, target: str) -> None:
    """Perform what a schedule cue asks for.

    Cue actions map onto the same player modes the GUI uses, so a scheduled
    switch and a manual one are indistinguishable afterwards — which matters
    when someone overrides a cue by hand mid-show.
    """
    if action == "ndi":
        config.update(mode=MODE_NDI, ndi_source=target)
        player.apply(MODE_NDI, target)
    elif action == "playlist":
        if playlists.get(target) is None:
            raise ValueError(f"playlist not found: {target}")
        config.update(mode=MODE_LOCAL, local_playlist=target)
        player.apply(MODE_LOCAL, "")
    elif action == "file":
        if media.resolve(target) is None:
            raise ValueError(f"media file not found: {target}")
        config.update(mode=MODE_LOCAL, local_playlist="", local_file=target)
        player.apply(MODE_LOCAL, target)
    elif action == "folder":
        config.update(mode=MODE_LOCAL, local_playlist="", local_file="")
        player.apply(MODE_LOCAL, "")
    elif action == "standby":
        config.update(mode=MODE_IDLE)
        player.apply(MODE_IDLE)
    else:
        raise ValueError(f"unknown cue action: {action}")


app = FastAPI(title="pi-streamer", version="0.1.0", lifespan=lifespan)


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------


class PlayNdi(BaseModel):
    source: str = Field(min_length=1)


class PlayLocal(BaseModel):
    file: str = ""
    loop: bool = True


class HostnameBody(BaseModel):
    hostname: str = Field(min_length=1, max_length=63)


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    return {
        "player": player.status(),
        "stream": player.stream_stats(),
        "system": system.summary(),
        "config": config.load().to_dict(),
        "discovery": sources.status(),
    }


@app.get("/api/capabilities")
async def get_capabilities() -> Dict[str, Any]:
    """What this particular box can actually do.

    Chiefly: are there hardware video decoders, and was the NDI plugin built
    against the Advanced SDK (which is what makes hardware decode reachable)?
    """
    decoders: List[Dict[str, Any]] = []
    compressed_supported = False
    try:
        import gi  # type: ignore

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore

        if not Gst.is_initialized():
            Gst.init(None)
        from .runner import list_decoders

        decoders = list_decoders()

        # The compressed colour formats only exist in the enum when the plugin
        # was built with --features advanced-sdk, so ask the element itself
        # rather than guessing from a version string.
        factory = Gst.ElementFactory.find("ndisrc")
        if factory is not None:
            element = factory.create(None)
            if element is not None:
                prop = element.find_property("color-format")
                if prop is not None:
                    try:
                        values = prop.enum_class.__enum_values__  # type: ignore[attr-defined]
                        compressed_supported = any(
                            "compressed" in v.value_nick for v in values.values()
                        )
                    except Exception:  # noqa: BLE001
                        compressed_supported = False
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "decoders": [], "hardware_decoders": [],
                "advanced_sdk": False}

    hardware = [d for d in decoders if d["hardware"]]
    return {
        "decoders": decoders,
        "hardware_decoders": hardware,
        "hardware_decode_available": bool(hardware),
        "advanced_sdk": compressed_supported,
        "ndi_config": ndiconfig.current(),
        "color_formats": list(COLOR_FORMATS),
        "video_formats": list(VIDEO_FORMATS),
    }


@app.get("/api/snapshot")
async def get_snapshot() -> FileResponse:
    """The most recent captured frame, for the standby-screen preview."""
    from .player import snapshot_path

    path = snapshot_path()
    if not path.is_file():
        raise HTTPException(404, "no snapshot captured yet")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/diagnose")
async def get_diagnose() -> Dict[str, Any]:
    """Why are frames being lost — the network, or this Pi?"""
    return diagnose.diagnose(player.stream_stats(), system.summary(), player.status())


@app.get("/api/telemetry")
async def get_telemetry(points: int = 0) -> Dict[str, Any]:
    """Rolling history as parallel arrays.

    `points` trims to the most recent N samples — the status page polls a
    dozen for its sparklines and does not need the whole window every two
    seconds over an event network.
    """
    data = telemetry.history()
    if points > 0:
        data["t"] = data["t"][-points:]
        data["series"] = {k: v[-points:] for k, v in data["series"].items()}
    return data


@app.get("/api/logs")
async def get_logs() -> Dict[str, Any]:
    return {"lines": player.logs()}


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    return config.load().to_dict()


@app.post("/api/config")
async def post_config(patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    # Reject junk early so the GUI gets a useful error rather than a silent no-op.
    known = set(config.Config().to_dict().keys())
    unknown = set(patch) - known
    if unknown:
        raise HTTPException(400, f"unknown config keys: {', '.join(sorted(unknown))}")
    if "rotation" in patch and patch["rotation"] not in (0, 90, 180, 270):
        raise HTTPException(400, "rotation must be 0, 90, 180 or 270")
    if "ndi_bandwidth" in patch and patch["ndi_bandwidth"] not in ("highest", "lowest"):
        raise HTTPException(400, "ndi_bandwidth must be 'highest' or 'lowest'")
    if "ndi_timestamp_mode" in patch and patch["ndi_timestamp_mode"] not in TIMESTAMP_MODES:
        raise HTTPException(
            400, f"ndi_timestamp_mode must be one of: {', '.join(TIMESTAMP_MODES)}"
        )

    for key, allowed in (
        ("idle_mode", ("black", "image", "lastframe")),
        ("queue_leaky", ("none", "upstream", "downstream")),
        ("ndi_color_format", tuple(COLOR_FORMATS)),
    ):
        if key in patch and patch[key] not in allowed:
            raise HTTPException(400, f"{key} must be one of: {', '.join(allowed)}")
    if "scale_method" in patch and patch["scale_method"] not in (0, 1, 2, 3):
        raise HTTPException(400, "scale_method must be 0 (nearest), 1, 2 or 3")
    if "video_format" in patch and patch["video_format"] not in VIDEO_FORMATS:
        raise HTTPException(400, f"video_format must be one of: {', '.join(VIDEO_FORMATS)}")
    if patch.get("standby_file") and media.resolve(patch["standby_file"]) is None:
        raise HTTPException(404, f"no such media file: {patch['standby_file']}")

    cfg = config.update(**patch)

    # These live in libndi's own config file, which it reads at init, so the
    # finder has to be torn down and restarted for them to take effect.
    ndi_network_keys = {"ndi_adapter_ips", "ndi_extra_ips", "ndi_discovery_server"}
    if ndi_network_keys & set(patch):
        ndiconfig.apply(cfg.ndi_adapter_ips, cfg.ndi_extra_ips, cfg.ndi_discovery_server)
        sources.stop()
        sources.start()

    if "schedule_enabled" in patch:
        if cfg.schedule_enabled:
            schedule_mod.scheduler.start()
        else:
            schedule_mod.scheduler.stop()

    # Anything that changes the pipeline graph only takes effect on a restart.
    restart_keys = {
        "connector", "video_mode", "rotation",
        "audio_device", "audio_enabled", "volume",
        "ndi_bandwidth", "ndi_latency_ms", "ndi_timestamp_mode",
        "ndi_color_format", "ndi_max_queue",
        "ndi_connect_timeout_ms", "ndi_timeout_ms",
        "sink_sync", "sink_qos", "sink_max_lateness_ms",
        "scale_method", "video_format",
        "queue_leaky", "queue_max_buffers", "convert_threads", "match_source",
        "ndi_url_address", "local_playlist", "fallback_to_standby",
        "snapshot_enabled", "snapshot_interval_s",
        "idle_mode", "standby_file",
        "loop",
    }
    # The standby screen is itself a live pipeline now, so idle restarts too.
    if restart_keys & set(patch):
        player.restart()
    return cfg.to_dict()


# ----------------------------------------------------------------------
# NDI
# ----------------------------------------------------------------------


@app.get("/api/ndi/sources")
async def ndi_sources(refresh: bool = False) -> Dict[str, Any]:
    # The finder runs continuously; this is a snapshot read. On a cold start
    # give it a moment so the first page load is not misleadingly empty.
    found = sources.discover(timeout=4.0 if refresh else 0.0)
    return {
        "sources": [s.to_dict() for s in found],
        "discovery": sources.status(),
    }


@app.get("/api/ndi/diagnostics")
async def ndi_diagnostics() -> Dict[str, Any]:
    """Everything needed to tell 'the plugin is broken' from 'the network is'."""
    env = dict(os.environ)
    checks: Dict[str, Any] = {
        "discovery": sources.status(),
        "gst_plugin_path": env.get("GST_PLUGIN_PATH", ""),
        "ld_library_path": env.get("LD_LIBRARY_PATH", ""),
    }

    plugin_so = Path("/opt/pistreamer/gst-plugins/libgstndi.so")
    checks["plugin_file"] = str(plugin_so) if plugin_so.exists() else "MISSING"

    libndi = sorted(Path("/usr/local/lib").glob("libndi.so*"))
    checks["libndi"] = [str(p) for p in libndi] or "MISSING"

    def _run(cmd: list[str]) -> str:
        if shutil.which(cmd[0]) is None and shutil.which(cmd[-1]) is None:
            hint = ""
            if any("gst-device-monitor" in part for part in cmd):
                hint = " (it ships in gstreamer1.0-plugins-base-apps)"
            return f"not installed: {cmd[0]}{hint}"
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (proc.stdout + proc.stderr).strip()[:4000]
        except (subprocess.SubprocessError, OSError) as exc:
            return f"failed: {exc}"

    inspect = _run(["gst-inspect-1.0", "ndisrc"])
    checks["ndisrc_registered"] = "Factory Details" in inspect
    checks["gst_inspect_ndisrc"] = inspect

    checks["device_monitor"] = _run(
        ["timeout", "8", "gst-device-monitor-1.0", "-f", "Source/Network"]
    )
    return checks


# ----------------------------------------------------------------------
# Playback control
# ----------------------------------------------------------------------


@app.post("/api/play/ndi")
async def play_ndi(body: PlayNdi) -> Dict[str, Any]:
    config.update(mode=MODE_NDI, ndi_source=body.source)
    player.apply(MODE_NDI, body.source)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


@app.post("/api/play/local")
async def play_local(body: PlayLocal) -> Dict[str, Any]:
    if body.file and media.resolve(body.file) is None:
        raise HTTPException(404, f"no such media file: {body.file}")
    config.update(mode=MODE_LOCAL, local_file=body.file, loop=body.loop)
    player.apply(MODE_LOCAL, body.file)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


@app.post("/api/stop")
async def stop() -> Dict[str, Any]:
    config.update(mode=MODE_IDLE)
    player.apply(MODE_IDLE)
    return player.status()


@app.post("/api/restart")
async def restart_player() -> Dict[str, Any]:
    player.restart()
    return player.status()


# ----------------------------------------------------------------------
# Media library
# ----------------------------------------------------------------------


@app.get("/api/media")
async def get_media(probe: bool = False) -> Dict[str, Any]:
    return {"files": [m.to_dict() for m in media.list_media(probe=probe)]}


@app.post("/api/media")
async def upload_media(file: UploadFile) -> Dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "no filename")
    if not media.is_allowed(file.filename):
        raise HTTPException(
            400, f"unsupported file type: {Path(file.filename).suffix or '(none)'}"
        )
    name = media.sanitise_name(file.filename)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MEDIA_DIR / name
    tmp = config.MEDIA_DIR / f".{name}.part"
    try:
        with tmp.open("wb") as fh:
            while chunk := await file.read(UPLOAD_CHUNK):
                fh.write(chunk)
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"write failed: {exc}") from exc
    finally:
        await file.close()
    return {"name": name, "size": dest.stat().st_size}


@app.delete("/api/media/{name}")
async def delete_media(name: str) -> Dict[str, Any]:
    cfg = config.load()
    if cfg.mode == MODE_LOCAL and cfg.local_file == name:
        raise HTTPException(409, "file is currently playing; stop playback first")
    if not media.delete(name):
        raise HTTPException(404, f"no such media file: {name}")
    return {"deleted": name}


# ----------------------------------------------------------------------
# Playlists
# ----------------------------------------------------------------------


class PlaylistBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    items: List[str] = Field(default_factory=list)
    loop: bool = True
    shuffle: bool = False
    image_duration: int = 10


@app.get("/api/playlists")
async def get_playlists() -> Dict[str, Any]:
    return {"playlists": [p.to_dict() for p in playlists.all_playlists()]}


@app.post("/api/playlists")
async def post_playlist(body: PlaylistBody) -> Dict[str, Any]:
    try:
        saved = playlists.save(
            playlists.Playlist(
                name=body.name, items=body.items, loop=body.loop,
                shuffle=body.shuffle, image_duration=body.image_duration,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return saved.to_dict()


@app.delete("/api/playlists/{name}")
async def delete_playlist(name: str) -> Dict[str, Any]:
    cfg = config.load()
    if cfg.local_playlist == name and cfg.mode == MODE_LOCAL:
        raise HTTPException(409, "playlist is currently playing; stop it first")
    if not playlists.delete(name):
        raise HTTPException(404, f"no such playlist: {name}")
    return {"deleted": name}


@app.post("/api/play/playlist")
async def play_playlist(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    name = str(body.get("name", ""))
    if playlists.get(name) is None:
        raise HTTPException(404, f"no such playlist: {name}")
    config.update(mode=MODE_LOCAL, local_playlist=name)
    player.apply(MODE_LOCAL, "")
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


# ----------------------------------------------------------------------
# Schedule
# ----------------------------------------------------------------------


class CueBody(BaseModel):
    id: str = ""
    time: str
    action: str
    target: str = ""
    days: List[int] = Field(default_factory=lambda: list(range(7)))
    enabled: bool = True
    label: str = ""


@app.get("/api/schedule")
async def get_schedule() -> Dict[str, Any]:
    cues = schedule_mod.all_cues()
    return {
        "enabled": config.load().schedule_enabled,
        "cues": [c.to_dict() for c in cues],
        "next": schedule_mod.next_fire(cues),
        "last_fired": schedule_mod.scheduler.last_fired(),
        "day_names": schedule_mod.DAY_NAMES,
        "actions": list(schedule_mod.ACTIONS),
    }


@app.post("/api/schedule")
async def post_cue(body: CueBody) -> Dict[str, Any]:
    cue = schedule_mod.Cue(
        id=body.id or f"cue-{int(time.time() * 1000)}",
        time=body.time, action=body.action, target=body.target,
        days=body.days, enabled=body.enabled, label=body.label,
    )
    try:
        schedule_mod.save(cue)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return cue.to_dict()


@app.delete("/api/schedule/{cue_id}")
async def delete_cue(cue_id: str) -> Dict[str, Any]:
    if not schedule_mod.delete(cue_id):
        raise HTTPException(404, f"no such cue: {cue_id}")
    return {"deleted": cue_id}


@app.post("/api/schedule/run/{cue_id}")
async def run_cue(cue_id: str) -> Dict[str, Any]:
    """Fire a cue now, to check it does what you meant before the show."""
    cue = next((c for c in schedule_mod.all_cues() if c.id == cue_id), None)
    if cue is None:
        raise HTTPException(404, f"no such cue: {cue_id}")
    try:
        apply_cue_action(cue.action, cue.target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc)) from exc
    return {"fired": cue.to_dict(), "player": player.status()}


# ----------------------------------------------------------------------
# Overclocking
# ----------------------------------------------------------------------

OVERCLOCK_HELPER = "/opt/pistreamer/bin/pistreamer-overclock"
OVERCLOCK_PRESETS = ("stock", "mild", "moderate", "maximum")


def _overclock(args: List[str]) -> Dict[str, Any]:
    """Call the privileged helper. It only accepts preset names, never values."""
    if not Path(OVERCLOCK_HELPER).exists():
        return {"available": False, "error": f"{OVERCLOCK_HELPER} not installed"}
    try:
        proc = subprocess.run(
            ["sudo", "-n", OVERCLOCK_HELPER, *args],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"available": False, "error": str(exc)}
    out: Dict[str, Any] = {"available": proc.returncode == 0}
    if proc.returncode != 0:
        out["error"] = (proc.stderr or proc.stdout).strip()
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


@app.get("/api/overclock")
async def get_overclock() -> Dict[str, Any]:
    data = _overclock(["status"])
    data["presets"] = list(OVERCLOCK_PRESETS)
    return data


@app.post("/api/overclock")
async def post_overclock(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    preset = str(body.get("preset", ""))
    if preset not in OVERCLOCK_PRESETS:
        raise HTTPException(400, f"preset must be one of: {', '.join(OVERCLOCK_PRESETS)}")
    data = _overclock([preset])
    if not data.get("available"):
        raise HTTPException(500, data.get("error", "overclock helper failed"))
    data["reboot_required"] = True
    return data


# ----------------------------------------------------------------------
# Display
# ----------------------------------------------------------------------


@app.get("/api/display/connectors")
async def get_connectors() -> Dict[str, Any]:
    return {
        "connectors": [
            {
                "name": c.name,
                "connected": c.connected,
                "modes": [str(m) for m in c.modes],
                "current": c.current,
            }
            for c in display.list_connectors()
        ]
    }


# ----------------------------------------------------------------------
# System
# ----------------------------------------------------------------------


@app.post("/api/system/hostname")
async def post_hostname(body: HostnameBody) -> Dict[str, Any]:
    try:
        system.set_hostname(body.hostname)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not set hostname: {exc}") from exc
    config.update(device_name=body.hostname)
    return {"hostname": system.hostname(), "reboot_required": True}


@app.post("/api/system/reboot")
async def post_reboot() -> JSONResponse:
    system.reboot()
    return JSONResponse({"status": "rebooting"})


@app.post("/api/system/poweroff")
async def post_poweroff() -> JSONResponse:
    system.poweroff()
    return JSONResponse({"status": "shutting down"})


# Serve the rest of the GUI assets. Mounted last so /api routes win.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = config.load()
    uvicorn.run(app, host="0.0.0.0", port=cfg.web_port, log_level="info")  # noqa: S104


if __name__ == "__main__":
    main()
