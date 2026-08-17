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
from pydantic import BaseModel, Field

from . import (cluster, config, diagnose, display, media, ndiconfig, playlists,
               pushjob, schedule as schedule_mod, sources, syncplay, system)
from .player import MODE_IDLE, MODE_LOCAL, MODE_NDI, identify_text, player
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
    schedule_mod.scheduler.bind(apply_cue_action)
    if cfg.schedule_enabled:
        schedule_mod.scheduler.start()
    if cfg.autostart and cfg.mode != MODE_IDLE:
        target = cfg.ndi_source if cfg.mode == MODE_NDI else cfg.local_file
        log.info("autostarting mode=%s target=%s", cfg.mode, target)
        player.apply(cfg.mode, target)
    yield
    conductor.stop()
    cluster.beacon().stop()
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
    action: Literal["ndi", "playlist", "file", "standby", "stop", "reboot"]
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
