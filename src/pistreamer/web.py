"""HTTP API and web GUI for a pi-streamer node."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel, Field

from . import (airplay, cluster, config, diagnose, display, guest, media,
               ndiconfig, playlists, pushjob, schedule as schedule_mod, sources,
               syncplay, system, updates)
from .player import (MODE_AIRPLAY, MODE_IDLE, MODE_LOCAL, MODE_NDI,
                     identify_text, player)
from .telemetry import telemetry

# The conductor is given callables rather than importing the player and the
# cluster itself: that keeps the synchronisation logic testable with fake nodes
# and no display, which is the only way this feature could be developed at all
# without a rack of Pis on the desk.
conductor = syncplay.Conductor({
    "prepare": lambda peer, item, session: _conductor_prepare(peer, item, session),
    "start": lambda peer, at: _conductor_start(peer, at),
    "pulse": lambda peer, body: _conductor_pulse(peer, body),
    "offsets": lambda ids: _conductor_offsets(ids),
    "position": lambda: player.sync_position(),
})


# The most recent push, kept so its progress can be polled after the request
# that started it has returned. One at a time: two pushes of the same file to
# the same node would race over the same temp file at the far end.
push_job = None


def _is_self(peer: cluster.Peer) -> bool:
    return peer.id == cluster.node_id()


def _conductor_prepare(peer: cluster.Peer, item: dict, session: str) -> dict:
    payload = {"file": item["target"], "duration": item.get("duration"),
               "image": bool(item.get("image")), "session": session}
    if _is_self(peer):
        return player.prepare(payload["file"], duration=payload["duration"],
                              image=payload["image"], session=session)
    return cluster.call(peer, "/api/cluster/prepare", method="POST", body=payload,
                        key=config.load().cluster_key, timeout=20)


def _conductor_start(peer: cluster.Peer, at: float) -> None:
    if _is_self(peer):
        player.start_at(at)
        return
    cluster.call(peer, "/api/cluster/start", method="POST", body={"at": at},
                 key=config.load().cluster_key, timeout=10)


def _conductor_pulse(peer: cluster.Peer, body: dict) -> None:
    if _is_self(peer):
        return  # the conductor IS the reference; correcting it against itself
                # would be a feedback loop with nothing outside it
    cluster.call(peer, "/api/cluster/pulse", method="POST", body=body,
                 key=config.load().cluster_key, timeout=5)


def _conductor_offsets(ids):
    """Measure each follower's clock offset, ourselves being zero by definition."""
    cfg = config.load()
    out = {}
    for node in ids:
        if node == cluster.node_id():
            out[node] = 0.0
            continue
        peer = cluster.registry.get(node)
        if peer is None:
            out[node] = None
            continue
        offset, rtt = cluster.measure_offset(peer, key=cfg.cluster_key)
        peer.clock_offset, peer.rtt_ms = offset, rtt
        out[node] = offset
    return out

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
    if cfg.cluster_enabled:
        # status_fn is injected so cluster.py never imports the player: the
        # beacon has to work on a node whose display is broken.
        cluster.beacon(lambda: {**player.status(), "version": app.version}).start()
    # Identify survives a reboot deliberately: if you flagged a node because you
    # were looking for it, finding it should not depend on the node's uptime.
    if cfg.identify:
        player.set_identify(True, identify_text(cfg, cluster.primary_ip()))
    telemetry.bind_player(player.stream_stats)
    telemetry.start()
    _start_update_checks(cfg)
    schedule_mod.scheduler.bind(apply_cue_action)
    if cfg.schedule_enabled:
        schedule_mod.scheduler.start()
    if cfg.autostart and cfg.mode != MODE_IDLE:
        target = "" if cfg.mode == MODE_AIRPLAY else (
            cfg.ndi_source if cfg.mode == MODE_NDI else cfg.local_file)
        log.info("autostarting mode=%s target=%s", cfg.mode, target)
        player.apply(cfg.mode, target)
    yield
    if _update_check_stop is not None:
        _update_check_stop.set()
    conductor.stop()
    cluster.beacon().stop()
    player.shutdown()
    sources.stop()
    telemetry.stop()
    schedule_mod.scheduler.stop()


_update_check_stop = None


def _start_update_checks(cfg) -> None:
    """Ask the remote whether there is a new version, occasionally.

    Without this the badge only appears if somebody thinks to press Check,
    which rather defeats the point. It is a request into the same root job the
    button uses — nothing here talks to the network directly.
    """
    global _update_check_stop
    hours = max(0, int(getattr(cfg, "update_check_hours", 0) or 0))
    if not hours or not updates.helper_installed():
        return
    import threading

    stop = threading.Event()
    _update_check_stop = stop

    def loop() -> None:
        # A first check shortly after boot, not immediately: the network may
        # not be up yet, and nothing here is urgent.
        if stop.wait(120):
            return
        while not stop.is_set():
            try:
                if not updates.busy():
                    updates.request("check")
            except Exception as exc:  # noqa: BLE001 - a failed check is not news
                log.debug("scheduled update check skipped: %s", exc)
            if stop.wait(hours * 3600):
                return

    threading.Thread(target=loop, name="update-check", daemon=True).start()


def apply_cue_action(action: str, target: str) -> None:
    """Perform what a schedule cue asks for.

    Cue actions map onto the same player modes the GUI uses, so a scheduled
    switch and a manual one are indistinguishable afterwards — which matters
    when someone overrides a cue by hand mid-show.
    """
    take_local_control(f"cue action: {action}")
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
    elif action == "airplay":
        ok, reason = airplay.available()
        if not ok:
            raise ValueError(reason)
        config.update(mode=MODE_AIRPLAY)
        player.apply(MODE_AIRPLAY)
    elif action in ("standby", "stop"):
        # "stop" is the group vocabulary for the same thing the GUI's Stop
        # button does. It used to fall through to the error below, so a group
        # stop failed on every node at once — the one moment you least want it.
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
        "update": {"behind": updates.status().get("behind", 0),
                   "busy": updates.busy()},
        "config": config.load().to_dict(),
        "discovery": sources.status(),
        "airplay": airplay.summary(),
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
                "advanced_sdk": False, "airplay": airplay.capabilities()}

    hardware = [d for d in decoders if d["hardware"]]
    return {
        "decoders": decoders,
        "hardware_decoders": hardware,
        "hardware_decode_available": bool(hardware),
        "advanced_sdk": compressed_supported,
        "ndi_config": ndiconfig.current(),
        "color_formats": list(COLOR_FORMATS),
        "video_formats": list(VIDEO_FORMATS),
        "airplay": airplay.capabilities(),
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


def take_local_control(reason: str) -> None:
    """End any synchronised session this node is conducting.

    Without this, stopping playback stops the *player* but leaves the conductor
    thread running, so at the next item boundary it prepares and starts the
    whole group again — including this node. From the operator's side, stop
    simply does not work: the screen goes black and then comes back a few
    seconds later, which is worse than not stopping at all.

    Anything that changes what this node plays counts as taking control: the
    GUI, a schedule cue, or a group command.
    """
    if conductor.state().get("running"):
        log.info("ending the synchronised session (%s)", reason)
        conductor.stop()


@app.post("/api/play/ndi")
async def play_ndi(body: PlayNdi) -> Dict[str, Any]:
    take_local_control("NDI source selected")
    config.update(mode=MODE_NDI, ndi_source=body.source)
    player.apply(MODE_NDI, body.source)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


@app.post("/api/play/local")
async def play_local(body: PlayLocal) -> Dict[str, Any]:
    take_local_control("local playback started")
    if body.file and media.resolve(body.file) is None:
        raise HTTPException(404, f"no such media file: {body.file}")
    config.update(mode=MODE_LOCAL, local_file=body.file, loop=body.loop)
    player.apply(MODE_LOCAL, body.file)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


@app.post("/api/play/airplay")
async def play_airplay() -> Dict[str, Any]:
    """Start receiving AirPlay.

    Nothing appears on the screen until somebody actually mirrors to the node —
    the receiver only takes the display when a session starts. That is a
    deliberate consequence of the one-owner rule, not an oversight, and the GUI
    says so rather than leaving an operator staring at black wondering.
    """
    ok, reason = airplay.available()
    if not ok:
        raise HTTPException(409, reason)
    take_local_control("AirPlay receiving started")
    config.update(mode=MODE_AIRPLAY)
    player.apply(MODE_AIRPLAY)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return {**status, "airplay": airplay.summary()}


@app.get("/api/airplay")
async def get_airplay() -> Dict[str, Any]:
    cfg = config.load()
    caps = airplay.capabilities()
    return {
        **caps,
        "on": cfg.mode == MODE_AIRPLAY,
        "name": airplay.receiver_name(cfg),
        "session": airplay.summary(),
        "ports": airplay.ports(cfg),
        "command": airplay.build_command(cfg, video_sink="kmssink"),
    }


@app.post("/api/stop")
async def stop() -> Dict[str, Any]:
    take_local_control("stopped from the GUI")
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
async def get_media(probe: bool = False, hashes: bool = False) -> Dict[str, Any]:
    files = [m.to_dict() for m in media.list_media(probe=probe)]
    if hashes:
        # Only on request: hashing a folder of 4 GB videos takes real time, and
        # the GUI polls this endpoint. The cluster push is the one caller that
        # needs it, to work out which files a node is actually missing.
        for entry in files:
            path = media.resolve(entry["name"])
            entry["sha256"] = cluster.sha256_file(path) if path else None
    return {"files": files}


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


@app.put("/api/media/raw/{name}")
async def upload_media_raw(name: str, request: Request) -> Dict[str, Any]:
    """Take a media file as a raw request body.

    This exists for node-to-node pushes. A multipart POST has to be assembled
    around the file, which for a multi-gigabyte video means either buffering it
    or hand-rolling a streaming encoder in the sender; a raw body lets the
    sender hand a file object straight to http.client. Browsers keep using the
    multipart endpoint above.
    """
    require_cluster_key(request)
    if not media.is_allowed(name):
        raise HTTPException(400, f"unsupported file type: {Path(name).suffix or '(none)'}")
    safe = media.sanitise_name(name)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MEDIA_DIR / safe
    tmp = config.MEDIA_DIR / f".{safe}.part"
    written = 0
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                written += len(chunk)
        # Rename only once the whole body has arrived, so an interrupted push
        # cannot leave a truncated file that plays for three seconds and stops.
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"write failed: {exc}") from exc
    return {"name": safe, "size": written}


@app.delete("/api/media/{name}")
async def delete_media(name: str) -> Dict[str, Any]:
    cfg = config.load()
    if cfg.mode == MODE_LOCAL and cfg.local_file == name:
        raise HTTPException(409, "file is currently playing; stop playback first")
    if not media.delete(name):
        raise HTTPException(404, f"no such media file: {name}")
    return {"deleted": name}


# ----------------------------------------------------------------------
# Guest sharing
#
# Two audiences, two sets of routes. Everything under /api/guest is the
# operator's; everything under /s/{token} is the room's. They are kept apart on
# purpose — the guest routes never consult the operator session, never accept a
# cluster key, and can only do the three things a guest needs.
# ----------------------------------------------------------------------


class GuestOpenBody(BaseModel):
    minutes: Optional[int] = None
    note: Optional[str] = None


class GuestItemBody(BaseModel):
    name: str = Field(min_length=1)


def _guest_summary() -> Dict[str, Any]:
    cfg = config.load()
    return guest.summary(ip=cluster.primary_ip(), port=cfg.web_port)


@app.get("/api/guest")
async def get_guest() -> Dict[str, Any]:
    return _guest_summary()


@app.post("/api/guest/open")
async def open_guest(body: GuestOpenBody) -> Dict[str, Any]:
    cfg = config.load()
    minutes = cfg.guest_minutes if body.minutes is None else body.minutes
    note = cfg.guest_note if body.note is None else body.note
    guest.open_session(minutes=minutes, note=note)
    return _guest_summary()


@app.post("/api/guest/close")
async def close_guest() -> Dict[str, Any]:
    guest.close_session()
    return _guest_summary()


@app.post("/api/guest/extend")
async def extend_guest(body: GuestOpenBody) -> Dict[str, Any]:
    guest.extend(body.minutes or config.load().guest_minutes)
    return _guest_summary()


@app.post("/api/guest/play")
async def play_guest_item(body: GuestItemBody) -> Dict[str, Any]:
    """Operator puts a queued guest upload on the screen."""
    if media.resolve(body.name) is None:
        raise HTTPException(404, f"no such media file: {body.name}")
    take_local_control("playing a guest upload")
    config.update(mode=MODE_LOCAL, local_file=body.name)
    player.apply(MODE_LOCAL, body.name)
    guest.mark_played(body.name)
    status = player.status()
    if status["last_error"]:
        raise HTTPException(500, status["last_error"])
    return status


@app.delete("/api/guest/item/{name}")
async def delete_guest_item(name: str) -> Dict[str, Any]:
    """Drop a guest upload: off the queue and off the disk.

    An operator rejecting something from the room means it should be gone, not
    hidden. Forgetting it from the queue but leaving the file in the library is
    the failure mode that puts it on the screen an hour later.
    """
    cfg = config.load()
    if cfg.mode == MODE_LOCAL and cfg.local_file == name:
        raise HTTPException(409, "that is on the screen now; stop playback first")
    known = guest.forget(name)
    deleted = media.delete(name)
    if not known and not deleted:
        raise HTTPException(404, f"no such guest item: {name}")
    return {"deleted": name}


# --- the room's half -------------------------------------------------


def _guest_gate(token: str) -> None:
    """A wrong or expired token is a 404, not a 403.

    404 leaks nothing: someone probing /s/ cannot tell an expired session from
    a session that never existed from a node that has the feature switched off.
    """
    if not guest.valid(token):
        raise HTTPException(404, "sharing is closed")


@app.get("/s/{token}", include_in_schema=False)
async def guest_page(token: str) -> FileResponse:
    # The page itself is served even when the token is stale, because it knows
    # how to say "sharing has finished" far more kindly than a 404 page does.
    return FileResponse(STATIC_DIR / "guest.html")


@app.get("/s/{token}/status")
async def guest_status(token: str) -> Dict[str, Any]:
    _guest_gate(token)
    return guest.public_status()


@app.post("/s/{token}/upload")
async def guest_upload(token: str, request: Request) -> Dict[str, Any]:
    _guest_gate(token)
    cfg = config.load()
    refusal = guest.can_accept(cfg)
    if refusal:
        raise HTTPException(409, refusal)

    limit = guest.limits(cfg)["max_mb"] * 1024 * 1024
    # A cheap early refusal before the body is read at all. It is only a hint —
    # the header is whatever the client felt like sending — but it saves
    # spooling a 4 GB phone video onto the SD card just to reject it.
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > limit + (1 << 20):
        raise HTTPException(
            413, f"that file is bigger than the {limit // (1024 * 1024)} MB limit")

    form = await request.form()
    upload = form.get("file")
    # Starlette's UploadFile, not FastAPI's: a multipart part parsed out of a
    # raw Request is the starlette class, and FastAPI's subclass fails the
    # isinstance check, which turned every guest upload into "no file".
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        raise HTTPException(400, "no file was attached")
    if not media.is_allowed(upload.filename):
        raise HTTPException(
            400, f"that kind of file cannot be shown here"
                 f" ({Path(upload.filename).suffix or 'no extension'})")

    name = guest.guest_filename(upload.filename)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.MEDIA_DIR / name
    tmp = config.MEDIA_DIR / f".{name}.part"
    written = 0
    try:
        with tmp.open("wb") as fh:
            while chunk := await upload.read(UPLOAD_CHUNK):
                written += len(chunk)
                # Checked while writing, not from Content-Length: the header is
                # whatever the client felt like sending.
                if written > limit:
                    raise HTTPException(
                        413, f"that file is bigger than the {limit // (1024 * 1024)} MB limit")
                fh.write(chunk)
        tmp.replace(dest)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"the node could not save it: {exc}") from exc
    finally:
        await upload.close()

    sender = str(form.get("from") or "")
    guest.record(name, written, sender=sender)
    log.info("guest upload: %s (%d bytes)%s", name, written,
             f" from {sender}" if sender else "")
    return {"name": name, "size": written}


@app.post("/s/{token}/play")
async def guest_play(token: str, body: GuestItemBody) -> Dict[str, Any]:
    """A guest showing their own upload. Only if the operator allowed it."""
    _guest_gate(token)
    cfg = config.load()
    if not cfg.guest_autoplay:
        raise HTTPException(403, "the operator decides what goes on the screen")
    # Only files this session actually received: without this check the
    # endpoint is "play anything in the library by name" for the whole room.
    s = guest.session()
    if not any(i["name"] == body.name for i in s.items):
        raise HTTPException(404, "that is not one of this session's uploads")
    if media.resolve(body.name) is None:
        raise HTTPException(404, "that file is no longer on the node")
    take_local_control("a guest put their upload on the screen")
    config.update(mode=MODE_LOCAL, local_file=body.name)
    player.apply(MODE_LOCAL, body.name)
    guest.mark_played(body.name)
    return {"playing": body.name}


# ----------------------------------------------------------------------
# Playlists
# ----------------------------------------------------------------------


class PlaylistItemBody(BaseModel):
    """One playlist segment: a media file or an NDI source."""

    type: Literal["file", "ndi"] = "file"
    target: str = Field(min_length=1, max_length=300)
    duration: Optional[int] = Field(default=None, ge=1, le=86400)


class PlaylistBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    # Segments, or bare filenames from a playlist written by an older version.
    # This model went out of step with playlists.py once already: items became
    # segments there while this still said List[str], so every save from the
    # new editor came back as a 422 telling the user their item "should be a
    # valid string". Accept both shapes and let playlists.normalise_items
    # coerce them.
    items: List[Union[PlaylistItemBody, str]] = Field(default_factory=list)
    loop: bool = True
    shuffle: bool = False
    image_duration: int = Field(default=10, ge=1, le=3600)


@app.get("/api/playlists")
async def get_playlists() -> Dict[str, Any]:
    return {"playlists": [p.to_dict() for p in playlists.all_playlists()]}


@app.post("/api/playlists")
async def post_playlist(body: PlaylistBody) -> Dict[str, Any]:
    items: List[Any] = [
        i if isinstance(i, str) else i.model_dump() for i in body.items
    ]
    try:
        saved = playlists.save(
            playlists.Playlist(
                name=body.name, items=items, loop=body.loop,
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

OVERCLOCK_REQUEST = "overclock.request"
OVERCLOCK_RESULT = "overclock.result"


@app.get("/api/overclock")
async def get_overclock() -> Dict[str, Any]:
    """Current preset, read straight from config.txt — no privilege needed."""
    data = system.overclock_status()
    result_path = config.STATE_DIR / OVERCLOCK_RESULT
    if result_path.exists():
        try:
            data["last_result"] = json.loads(result_path.read_text())
        except (OSError, ValueError):
            pass
    unit = Path("/etc/systemd/system/pistreamer-overclock.path")
    data["writable"] = unit.exists()
    if not data["writable"]:
        data["error"] = (
            "the overclock helper unit is not installed — re-run install.sh. "
            "(It is path-activated rather than sudo-based: the service sets "
            "NoNewPrivileges, which blocks sudo entirely.)"
        )
    return data


@app.post("/api/overclock")
async def post_overclock(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Request a preset by dropping a file a root path-unit is watching.

    The service cannot escalate — NoNewPrivileges=yes — so it writes a request
    it already owns and a root oneshot applies it. We then wait briefly for the
    result file so the GUI can report success or failure rather than shrugging.
    """
    preset = str(body.get("preset", ""))
    if preset not in system.OVERCLOCK_PRESETS:
        raise HTTPException(
            400, f"preset must be one of: {', '.join(system.OVERCLOCK_PRESETS)}"
        )

    result_path = config.STATE_DIR / OVERCLOCK_RESULT
    request_path = config.STATE_DIR / OVERCLOCK_REQUEST
    before = result_path.stat().st_mtime if result_path.exists() else 0
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        request_path.write_text(preset + "\n")
    except OSError as exc:
        raise HTTPException(500, f"could not write the request: {exc}") from exc

    deadline = time.time() + 12
    while time.time() < deadline:
        await asyncio.sleep(0.4)
        if result_path.exists() and result_path.stat().st_mtime > before:
            try:
                result = json.loads(result_path.read_text())
            except (OSError, ValueError):
                continue
            if not result.get("ok"):
                raise HTTPException(500, result.get("message") or "overclock failed")
            return {**system.overclock_status(), "last_result": result,
                    "reboot_required": True}

    raise HTTPException(
        504,
        "the overclock helper did not respond. Check "
        "'systemctl status pistreamer-overclock.path' — it may not be enabled.",
    )


# ----------------------------------------------------------------------
# Audio devices
# ----------------------------------------------------------------------


@app.get("/api/audio/devices")
async def get_audio_devices() -> Dict[str, Any]:
    """What we can actually play to. Guessing this is why audio 'does not work'."""
    return system.audio_devices()


@app.post("/api/audio/test")
async def post_audio_test(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """Play a tone to a device so the choice can be confirmed, not guessed."""
    device = str(body.get("device", ""))
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: system.test_tone(device, int(body.get("seconds", 2)))
    )
    if not result.get("ok"):
        raise HTTPException(500, result.get("error") or "test tone failed")
    return result


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


# ----------------------------------------------------------------------
# Cluster
# ----------------------------------------------------------------------
#
# Only these endpoints are authenticated. The rest of the API is deliberately
# open, because the GUI is a browser on the same LAN with no login — but a
# neighbouring node being able to reboot this one, or overwrite its media, is a
# different proposition, so anything that arrives from another node has to carry
# the group key.


def require_cluster_key(request: Request) -> None:
    cfg = config.load()
    if not cfg.cluster_enabled:
        raise HTTPException(403, "clustering is disabled on this node")
    supplied = request.headers.get(cluster.AUTH_HEADER, "")
    # compare_digest, not ==: string comparison short-circuits on the first
    # wrong character, which leaks the key one character at a time to anything
    # that can measure the response.
    if not hmac.compare_digest(supplied, cfg.cluster_key):
        raise HTTPException(401, "wrong or missing cluster key")


def _own_peer() -> cluster.Peer:
    """This node, as a Peer, so the conductor drives it through the same path.

    Two code paths — one for "me" and one for "them" — is how a leader ends up
    behaving subtly differently from its followers. There is one path.
    """
    cfg = config.load()
    return cluster.Peer(
        id=cluster.node_id(), name=cfg.device_name or system.hostname(),
        ip="127.0.0.1", port=cfg.web_port,
    )


def _peers_by_id(ids: List[str]) -> List[cluster.Peer]:
    """Resolve ids to peers, including ourselves, preserving the caller's order."""
    own = _own_peer()
    known = {p.id: p for p in cluster.registry.all()}
    known[own.id] = own
    return [known[i] for i in ids if i in known]


@app.get("/api/cluster/time")
async def get_cluster_time(request: Request) -> Dict[str, Any]:
    """This node's wall clock, for offset measurement.

    Answered as early and cheaply as possible: everything this handler does
    before reading the clock is added to the measured offset.
    """
    require_cluster_key(request)
    return {"t": time.time(), "id": cluster.node_id()}


@app.get("/api/cluster")
async def get_cluster() -> Dict[str, Any]:
    cfg = config.load()
    own = _own_peer()
    return {
        "enabled": cfg.cluster_enabled,
        "group": cfg.cluster_group,
        "self": {"id": own.id, "name": own.name, "ip": cluster.primary_ip(),
                 "port": cfg.web_port, "identify": cfg.identify},
        "beacon": cluster.beacon().stats(),
        "peers": [p.to_dict() for p in cluster.registry.all()],
        "sync": conductor.state(),
    }


class IdentifyBody(BaseModel):
    on: bool = True
    # Empty means "work it out yourself", which is what a node does for itself.
    # The leader does not dictate the caption: a node knows its own name and
    # address better than anyone else does.
    nodes: List[str] = Field(default_factory=list)
    propagate: bool = True


@app.post("/api/cluster/identify")
async def post_identify(body: IdentifyBody, request: Request) -> Dict[str, Any]:
    # An inbound identify from another node carries the key; one from our own
    # GUI does not, and does not need to — so the key is checked only when it
    # was offered, rather than locking the browser out of its own node.
    if request.headers.get(cluster.AUTH_HEADER) is not None:
        require_cluster_key(request)
    config.update(identify=body.on)
    cfg = config.load()
    applied = player.set_identify(
        body.on, identify_text(cfg, cluster.primary_ip())
    )
    out: Dict[str, Any] = {"identify": body.on, "applied": applied, "peers": []}
    if body.propagate:
        targets = (_peers_by_id(body.nodes) if body.nodes
                   else cluster.registry.all())
        for peer in targets:
            if peer.id == cluster.node_id():
                continue
            try:
                cluster.call(peer, "/api/cluster/identify", method="POST",
                             body={"on": body.on, "propagate": False},
                             key=cfg.cluster_key)
                out["peers"].append({"name": peer.name, "ok": True})
            except cluster.PeerError as exc:
                out["peers"].append({"name": peer.name, "ok": False, "error": str(exc)})
    return out


class CommandBody(BaseModel):
    # Same vocabulary the schedule cues use, so "do this everywhere" and "do
    # this here" cannot drift apart.
    action: Literal["ndi", "playlist", "file", "airplay", "standby", "stop", "reboot"]
    target: str = ""
    nodes: List[str] = Field(default_factory=list)


@app.post("/api/cluster/command")
async def post_cluster_command(body: CommandBody, request: Request) -> Dict[str, Any]:
    """Tell every node in the group (or a subset) to do one thing."""
    cfg = config.load()
    targets = _peers_by_id(body.nodes) if body.nodes else (
        [_own_peer()] + cluster.registry.all())
    results = []
    for peer in targets:
        name = peer.name or peer.ip
        try:
            if peer.id == cluster.node_id():
                if body.action == "reboot":
                    system.reboot()
                else:
                    apply_cue_action(body.action, body.target)
            else:
                path = ("/api/system/reboot" if body.action == "reboot"
                        else "/api/cluster/local")
                payload = (None if body.action == "reboot"
                           else {"action": body.action, "target": body.target})
                cluster.call(peer, path, method="POST", body=payload,
                             key=cfg.cluster_key)
            results.append({"name": name, "ok": True})
        except Exception as exc:  # noqa: BLE001 - one node must not stop the rest
            results.append({"name": name, "ok": False, "error": str(exc)})
    return {"action": body.action, "results": results}


class LocalActionBody(BaseModel):
    action: str
    target: str = ""


@app.post("/api/cluster/local")
async def post_cluster_local(body: LocalActionBody, request: Request) -> Dict[str, Any]:
    """Apply one action to this node only. The receiving end of /command."""
    require_cluster_key(request)
    try:
        apply_cue_action(body.action, body.target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"applied": body.action, "target": body.target}


class PrepareBody(BaseModel):
    file: str
    # A still image has no natural end, so the conductor tells each node how
    # long it is for and holds it until the boundary.
    duration: Optional[int] = None
    image: bool = False
    # Which synchronised session this belongs to, so a node stopped by hand can
    # refuse the rest of that one and still join the next.
    session: str = ""


@app.post("/api/cluster/prepare")
async def post_prepare(body: PrepareBody, request: Request) -> Dict[str, Any]:
    require_cluster_key(request)
    try:
        return player.prepare(body.file, duration=body.duration,
                              image=body.image, session=body.session)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


class StartAtBody(BaseModel):
    # An instant on THIS node's clock. The leader has already converted it.
    at: float


@app.post("/api/cluster/start")
async def post_start_at(body: StartAtBody, request: Request) -> Dict[str, Any]:
    require_cluster_key(request)
    try:
        return player.start_at(body.at)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


class PulseBody(BaseModel):
    item: str = ""
    pos: float
    at: float


@app.post("/api/cluster/pulse")
async def post_pulse(body: PulseBody, request: Request) -> Dict[str, Any]:
    require_cluster_key(request)
    if not config.load().cluster_drift_correct:
        return {"action": "hold", "reason": "drift correction disabled"}
    return player.apply_pulse(body.model_dump())


class SyncPushBody(BaseModel):
    playlist: str
    nodes: List[str] = Field(default_factory=list)


@app.post("/api/cluster/push")
async def post_cluster_push(body: SyncPushBody, wait: bool = False) -> Dict[str, Any]:
    """Copy a playlist and every file it needs to the other nodes.

    Only what is missing is sent, decided by hash rather than by name and size:
    two different cuts of a video exported the same afternoon are the same size
    surprisingly often, and sending nothing because of that would put the wrong
    content on a screen.

    Returns immediately with a job to poll, because a playlist is gigabytes and
    a request that returns nothing for ten minutes cannot be told apart from one
    that has hung. `?wait=1` blocks until it finishes, for scripting.
    """
    global push_job
    cfg = config.load()
    playlist = playlists.get(body.playlist)
    if playlist is None:
        raise HTTPException(404, f"playlist not found: {body.playlist}")
    if push_job is not None and push_job.is_running():
        raise HTTPException(409, "a push is already running")

    wanted: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    for segment in playlists.resolved_segments(body.playlist):
        if segment["type"] != "file":
            continue
        path = Path(segment["path"])
        wanted[segment["target"]] = cluster.sha256_file(path)
        sizes[segment["target"]] = path.stat().st_size

    targets = [p for p in (_peers_by_id(body.nodes) if body.nodes
                           else cluster.registry.all())
               if p.id != cluster.node_id()]
    if not targets:
        raise HTTPException(400, "no other nodes to send to")

    def remote_hashes(peer: cluster.Peer) -> Dict[str, Any]:
        remote = cluster.call(peer, "/api/media?hashes=1", key=cfg.cluster_key,
                              timeout=600)
        return {f["name"]: f.get("sha256") for f in remote.get("files", [])}

    def send_playlist(peer: cluster.Peer) -> None:
        cluster.call(peer, "/api/playlists", method="POST",
                     body={"name": playlist.name, "items": playlist.items,
                           "loop": playlist.loop, "shuffle": playlist.shuffle,
                           "image_duration": playlist.image_duration},
                     key=cfg.cluster_key)

    push_job = pushjob.PushJob(body.playlist, {
        "remote_hashes": remote_hashes,
        "send_playlist": send_playlist,
        "resolve": media.resolve,
        "upload": lambda peer, name, path, progress: cluster.upload(
            peer, name, path, key=cfg.cluster_key, on_progress=progress),
    })
    push_job.start(targets, wanted, sizes)

    if wait:
        while push_job.is_running():
            await asyncio.sleep(0.2)
    return push_job.snapshot()


@app.get("/api/cluster/push")
async def get_cluster_push() -> Dict[str, Any]:
    """Where the running (or last) push got to. Polled by the GUI."""
    if push_job is None:
        return {"running": False, "done": False, "nodes": []}
    return push_job.snapshot()


@app.post("/api/cluster/push/cancel")
async def post_cluster_push_cancel() -> Dict[str, Any]:
    if push_job is None:
        raise HTTPException(404, "no push has been started")
    push_job.cancel()
    return {"cancelling": True}


class SyncPlayBody(BaseModel):
    playlist: str
    nodes: List[str] = Field(default_factory=list)
    loop: bool = True


@app.post("/api/cluster/sync/play")
async def post_sync_play(body: SyncPlayBody) -> Dict[str, Any]:
    # Whole segments, not just filenames: a still image needs its dwell time
    # carried through, because it has no playhead to end it.
    items = [{"target": seg["target"], "duration": seg["duration"],
              "image": bool(seg["image"])}
             for seg in playlists.resolved_segments(body.playlist)
             if seg["type"] == "file"]
    if not items:
        raise HTTPException(400, "synchronised playback needs a playlist of files")
    targets = ([_own_peer()] + cluster.registry.all() if not body.nodes
               else _peers_by_id(body.nodes))
    try:
        return conductor.start(items, targets, loop=body.loop)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/cluster/sync/stop")
async def post_sync_stop() -> Dict[str, Any]:
    conductor.stop()
    return conductor.state()


# ----------------------------------------------------------------------
# Updates
# ----------------------------------------------------------------------


class UpdateBody(BaseModel):
    # Updating restarts playback, so a node that is on air says no unless the
    # operator has said they mean it.
    force: bool = False
    ref: str = ""


@app.get("/api/update")
async def get_update() -> Dict[str, Any]:
    return updates.summary()


@app.post("/api/update/check")
async def post_update_check(request: Request) -> Dict[str, Any]:
    if request.headers.get(cluster.AUTH_HEADER) is not None:
        require_cluster_key(request)
    if not updates.helper_installed():
        raise HTTPException(
            409, "the update helper is not installed on this node — run install.sh "
                 "once from the terminal to add it, and it will not be needed again")
    try:
        updates.request("check")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"checking": True}


@app.post("/api/update/apply")
async def post_update_apply(body: UpdateBody, request: Request) -> Dict[str, Any]:
    if request.headers.get(cluster.AUTH_HEADER) is not None:
        require_cluster_key(request)
    summary = updates.summary()
    if not summary["updatable"]:
        raise HTTPException(
            409, "this node was installed from an archive rather than a git clone, "
                 "so it cannot update itself")
    if not updates.helper_installed():
        raise HTTPException(409, "the update helper is not installed on this node")
    status = player.status()
    if status.get("running") and not body.force:
        raise HTTPException(
            409, f"this node is playing {status.get('target') or 'something'} — "
                 "updating restarts playback")
    # Whatever was on screen is coming down either way; stop cleanly rather
    # than have the service killed mid-frame by its own restart.
    take_local_control("updating")
    player.apply(MODE_IDLE)
    try:
        updates.request("apply", ref=body.ref)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"updating": True}


@app.post("/api/update/rollback")
async def post_update_rollback(request: Request) -> Dict[str, Any]:
    if request.headers.get(cluster.AUTH_HEADER) is not None:
        require_cluster_key(request)
    if not (updates.status().get("previous") or {}).get("sha"):
        raise HTTPException(404, "no previous version recorded to roll back to")
    take_local_control("rolling back")
    player.apply(MODE_IDLE)
    try:
        updates.request("rollback")
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"rolling_back": True}


class ClusterUpdateBody(BaseModel):
    nodes: List[str] = Field(default_factory=list)
    action: Literal["check", "apply"] = "check"
    force: bool = False


@app.post("/api/cluster/update")
async def post_cluster_update(body: ClusterUpdateBody) -> Dict[str, Any]:
    """Check or update every node in the group.

    This node goes last on purpose: applying an update restarts it, which takes
    the GUI you are watching with it. Doing the others first means you can see
    them finish before your own page drops.
    """
    cfg = config.load()
    peers = [p for p in (_peers_by_id(body.nodes) if body.nodes
                         else cluster.registry.all())
             if p.id != cluster.node_id()]
    path = "/api/update/check" if body.action == "check" else "/api/update/apply"
    payload = None if body.action == "check" else {"force": body.force}
    results = []
    for peer in peers:
        try:
            cluster.call(peer, path, method="POST", body=payload,
                         key=cfg.cluster_key, timeout=30)
            results.append({"name": peer.name or peer.ip, "ok": True})
        except Exception as exc:  # noqa: BLE001 - one node must not stop the rest
            results.append({"name": peer.name or peer.ip, "ok": False, "error": str(exc)})

    own = {"name": cfg.device_name or system.hostname(), "ok": True, "self": True}
    try:
        if body.action == "check":
            updates.request("check")
        else:
            status = player.status()
            if status.get("running") and not body.force:
                raise RuntimeError("this node is playing; use force to update anyway")
            take_local_control("updating")
            player.apply(MODE_IDLE)
            updates.request("apply")
    except Exception as exc:  # noqa: BLE001
        own = {**own, "ok": False, "error": str(exc)}
    results.append(own)
    return {"action": body.action, "results": results}


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
