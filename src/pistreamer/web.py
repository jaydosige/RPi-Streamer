"""HTTP API and web GUI for a pi-streamer node."""

from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, display, media, sources, system
from .player import MODE_IDLE, MODE_LOCAL, MODE_NDI, player

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

UPLOAD_CHUNK = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    cfg = config.load()
    player.start()
    if cfg.autostart and cfg.mode != MODE_IDLE:
        target = cfg.ndi_source if cfg.mode == MODE_NDI else cfg.local_file
        log.info("autostarting mode=%s target=%s", cfg.mode, target)
        player.apply(cfg.mode, target)
    yield
    player.shutdown()


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
        "system": system.summary(),
        "config": config.load().to_dict(),
    }


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

    cfg = config.update(**patch)
    # Display/audio/NDI settings only take effect on a fresh pipeline.
    restart_keys = {
        "connector",
        "video_mode",
        "rotation",
        "audio_device",
        "audio_enabled",
        "volume",
        "ndi_bandwidth",
        "ndi_latency_ms",
        "loop",
    }
    if restart_keys & set(patch) and cfg.mode != MODE_IDLE:
        player.restart()
    return cfg.to_dict()


# ----------------------------------------------------------------------
# NDI
# ----------------------------------------------------------------------


@app.get("/api/ndi/sources")
async def ndi_sources(refresh: bool = False) -> Dict[str, Any]:
    found = sources.discover(timeout=2.0, use_cache=not refresh)
    return {"sources": [s.__dict__ for s in found]}


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
