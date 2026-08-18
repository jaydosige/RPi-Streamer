"""Letting a guest put something on the screen, without giving them the node.

Somebody at an event has a video on their phone and wants it on the wall. The
alternatives are all bad: hand them the operator GUI, take their phone and use a
cable, or email it to yourself. This gives them a QR code that opens one page
with one button.

Everything here is shaped by the fact that the audience is the threat model. The
guest page is not the operator GUI with a few things hidden — it is a separate
page with its own routes, and those routes can do exactly three things: say what
is allowed, accept a file, and (optionally) ask for it to be shown.

  * **Off by default, and it turns itself off.** A session lasts an hour unless
    told otherwise. Nobody remembers to close the door at the end of a job, so
    the door closes itself.
  * **The QR is the credential.** Each session mints a new token; the old QR on
    somebody's camera roll is worthless the moment the session ends.
  * **A guest cannot take the screen** unless the operator says they may. The
    default is that uploads queue up and the operator decides.
  * **Caps on size, count and type**, because the upload endpoint is reachable
    by everyone in the room.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, media

log = logging.getLogger(__name__)

# A token that is short enough to survive being a QR code at arm's length, and
# long enough that guessing it is not worth anyone's evening.
TOKEN_BYTES = 8
# Guest files get a prefix so an upload called IMG_0001.mp4 cannot quietly
# replace an operator's file of the same name, and so they are recognisable in
# the library later.
PREFIX = "guest-"
_ID_RE = re.compile(r"^guest-[0-9a-f]{6}-")


def manifest_path() -> Path:
    return config.STATE_DIR / "guest.json"


@dataclass
class Session:
    """One period during which the door is open."""

    token: str = ""
    started: float = 0.0
    expires: float = 0.0
    note: str = ""
    items: List[Dict[str, Any]] = field(default_factory=list)

    def open(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return bool(self.token) and (self.expires == 0 or now < self.expires)

    def remaining(self, now: Optional[float] = None) -> Optional[int]:
        if not self.token or self.expires == 0:
            return None
        now = time.time() if now is None else now
        return max(0, int(self.expires - now))


def _load() -> Session:
    try:
        raw = json.loads(manifest_path().read_text())
    except (OSError, ValueError):
        return Session()
    return Session(
        token=str(raw.get("token", "")),
        started=float(raw.get("started", 0) or 0),
        expires=float(raw.get("expires", 0) or 0),
        note=str(raw.get("note", "")),
        items=list(raw.get("items", []) or []),
    )


def _save(session: Session) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "token": session.token, "started": session.started,
        "expires": session.expires, "note": session.note, "items": session.items,
    }, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".guest-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def session() -> Session:
    return _load()


def open_session(minutes: int = 60, note: str = "") -> Session:
    """Start a sharing session. Always a fresh token.

    Reusing a token would mean the QR from last month's job still works, which
    is precisely the thing that makes a code on a wall dangerous.
    """
    now = time.time()
    minutes = max(0, min(24 * 60, int(minutes)))
    s = Session(
        token=secrets.token_urlsafe(TOKEN_BYTES).rstrip("=").replace("_", "").replace("-", ""),
        started=now,
        expires=now + minutes * 60 if minutes else 0.0,
        note=note.strip()[:120],
        items=[],
    )
    _save(s)
    log.info("guest sharing opened for %s minutes", minutes or "unlimited")
    return s


def close_session() -> None:
    s = _load()
    s.token = ""
    s.expires = 0.0
    _save(s)
    log.info("guest sharing closed")


def valid(token: str) -> bool:
    """Is this the token of an open session?

    Compared in constant time, and an expired session is as good as no session.
    """
    import hmac

    s = _load()
    if not s.open():
        return False
    return hmac.compare_digest(token or "", s.token)


def extend(minutes: int) -> Session:
    s = _load()
    if s.token:
        s.expires = time.time() + max(1, int(minutes)) * 60
        _save(s)
    return s


def guest_filename(original: str) -> str:
    """A safe, unique, recognisable name for a guest upload."""
    base = media.sanitise_name(original)
    return f"{PREFIX}{secrets.token_hex(3)}-{base}"[:180]


def is_guest_file(name: str) -> bool:
    return bool(_ID_RE.match(name or ""))


def record(name: str, size: int, sender: str = "") -> Dict[str, Any]:
    s = _load()
    item = {
        "name": name,
        "size": int(size),
        "from": (sender or "").strip()[:40],
        "at": time.time(),
        "played": False,
    }
    s.items.append(item)
    _save(s)
    return item


def mark_played(name: str) -> None:
    s = _load()
    for item in s.items:
        if item["name"] == name:
            item["played"] = True
    _save(s)


def forget(name: str) -> bool:
    s = _load()
    before = len(s.items)
    s.items = [i for i in s.items if i["name"] != name]
    _save(s)
    return len(s.items) != before


def limits(cfg: Optional[config.Config] = None) -> Dict[str, Any]:
    cfg = cfg or config.load()
    return {
        "max_mb": max(1, int(cfg.guest_max_mb)),
        "max_items": max(1, int(cfg.guest_max_items)),
        "autoplay": bool(cfg.guest_autoplay),
        "types": sorted(media.ALLOWED_EXTS),
    }


def can_accept(cfg: Optional[config.Config] = None) -> Optional[str]:
    """Why an upload would be refused, or None if it would be accepted."""
    cfg = cfg or config.load()
    s = _load()
    if not s.open():
        return "sharing is closed"
    if len(s.items) >= limits(cfg)["max_items"]:
        return f"this session has reached its limit of {limits(cfg)['max_items']} items"
    return None


def share_url(ip: str, port: int, token: str = "") -> str:
    s = _load()
    token = token or s.token
    if not token:
        return ""
    host = f"{ip}:{port}" if port and port != 80 else ip
    return f"http://{host}/s/{token}"


def qr_svg(url: str, scale: int = 1) -> str:
    """The share URL as an inline SVG QR code.

    Rendered on the node, not by a web service: this runs on show networks with
    no route to the internet, and a QR code that needs Google to draw it is
    worse than useless there.
    """
    if not url:
        return ""
    try:
        import segno
    except ImportError:
        log.warning("segno is not installed, so no QR code can be drawn")
        return ""
    try:
        code = segno.make(url, error="m")
        import io

        # segno writes bytes even for SVG, so this has to be a BytesIO — a
        # StringIO raises and the QR silently never appears.
        buf = io.BytesIO()
        # dark/light are set explicitly so the code survives the dark GUI: a QR
        # needs its quiet zone and its contrast, and inheriting page colours
        # breaks scanning.
        # omitsize gives a viewBox instead of pixel width/height. Without it
        # the SVG carries a 29x29 canvas, CSS stretches the canvas but not the
        # drawing, and the QR renders as a postage stamp in the corner of a
        # large white square — which still decodes in a unit test and is
        # unscannable in a room.
        code.save(buf, kind="svg", scale=scale, border=2, omitsize=True,
                  dark="#000000", light="#ffffff", xmldecl=False, svgns=True)
        return buf.getvalue().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - a missing QR must not break the page
        log.warning("could not render the QR code: %s", exc)
        return ""


def summary(ip: str = "", port: int = 80) -> Dict[str, Any]:
    """Everything the operator's GUI needs about guest sharing."""
    cfg = config.load()
    s = _load()
    url = share_url(ip, port) if s.open() else ""
    return {
        "open": s.open(),
        "url": url,
        "qr": qr_svg(url, scale=1) if url else "",
        "token": s.token if s.open() else "",
        "note": s.note,
        "started": s.started or None,
        "expires": s.expires or None,
        "remaining": s.remaining(),
        "items": list(reversed(s.items)),
        "limits": limits(cfg),
        "default_minutes": int(cfg.guest_minutes),
    }


def public_status(cfg: Optional[config.Config] = None) -> Dict[str, Any]:
    """What the *guest* page is allowed to know.

    Deliberately not `summary()` minus a few keys. The guest is handed a
    separate dict built from scratch, so a field added to the operator view
    later cannot leak to the room by accident. No token, no other guests'
    names, no node identity.
    """
    cfg = cfg or config.load()
    s = _load()
    lim = limits(cfg)
    return {
        "open": s.open(),
        "note": s.note,
        "closes_in": s.remaining(),
        "remaining_items": max(0, lim["max_items"] - len(s.items)),
        "limits": {"max_mb": lim["max_mb"], "max_items": lim["max_items"],
                   "autoplay": lim["autoplay"], "types": lim["types"]},
    }


# ----------------------------------------------------------------------
# The on-screen panel
#
# The QR in the operator's browser is no use to the room: the people who need
# to scan it are looking at the screen. So when sharing is open the same code
# goes over the output, in the same way the identify caption does — as an
# overlay both playback backends can carry, rather than as a mode that would
# stop whatever is playing.
#
# One rendered panel serves both. GStreamer takes the PNG through
# gdkpixbufoverlay; mpv takes the same pixels as pre-multiplied BGRA through
# its `overlay-add` command. Rendering once and converting is what keeps them
# identical — two renderers would drift, and nobody would notice until a job.
# ----------------------------------------------------------------------

# The panel as a fraction of screen height. Big enough to scan from the middle
# of a room, small enough to sit in a corner of somebody's content.
PANEL_HEIGHT_FRACTION = 0.30
# Room for the address underneath the code.
_QR_FRACTION = 0.62
_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def overlay_png_path() -> Path:
    return config.STATE_DIR / "guest-overlay.png"


def overlay_bgra_path() -> Path:
    return config.STATE_DIR / "guest-overlay.bgra"


def overlay_meta_path() -> Path:
    return config.STATE_DIR / "guest-overlay.json"


def overlay_available() -> Tuple[bool, str]:
    """Can a panel be drawn at all?"""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False, ("the on-screen QR needs Pillow. Install it with: "
                       "sudo apt install python3-pil")
    return True, ""


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for path in (_FONTS if bold else _FONTS[::-1]):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    # Better a small ugly caption than no panel: the QR is the part that has
    # to work, and it is drawn by us rather than by the font stack.
    return ImageFont.load_default()


def _qr_matrix(url: str):
    import segno

    code = segno.make(url, error="m")
    return [list(row) for row in code.matrix]


def render_overlay(url: str, screen_h: int = 1080, note: str = ""):
    """Draw the panel and write it in both forms. Returns its metadata."""
    ok, reason = overlay_available()
    if not ok:
        log.warning("%s", reason)
        return None
    from PIL import Image, ImageChops, ImageDraw

    panel = max(160, int(screen_h * PANEL_HEIGHT_FRACTION))
    pad = max(8, panel // 20)
    qr_side = int(panel * _QR_FRACTION)

    matrix = _qr_matrix(url)
    modules = len(matrix)
    # Whole pixels per module, or the code develops a moiré when the rows land
    # on fractional boundaries — which is exactly the kind of "it scans on my
    # phone but not on theirs" problem nobody can debug in a dark room.
    scale = max(1, qr_side // (modules + 4))
    quiet = 2 * scale
    qr_px = modules * scale + 2 * quiet

    text = url.split("://", 1)[-1]
    caption = (note or "Scan to share to this screen").strip()

    body = _font(max(11, int(panel * 0.062)))
    addr = _font(max(12, int(panel * 0.075)), bold=True)

    width = max(qr_px + 2 * pad, 0)
    # Measure the text first: a long address decides the panel width, not the
    # code, and a panel narrower than its own caption looks broken.
    probe = Image.new("RGBA", (10, 10))
    d = ImageDraw.Draw(probe)
    for fnt, s in ((body, caption), (addr, text)):
        # Three pads, not two: an address that reaches the rounded corner reads
        # as a rendering fault rather than as a deliberate panel.
        width = max(width, int(d.textlength(s, font=fnt)) + 3 * pad)

    line_h = int(panel * 0.075)
    height = pad + qr_px + int(pad * 0.6) + line_h + int(line_h * 1.15) + pad

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = max(6, panel // 22)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius,
                           fill=(255, 255, 255, 255))

    # The code, drawn module by module rather than scaled from a smaller
    # bitmap: any resampling at all softens the edges, and a soft QR is a QR
    # that needs three attempts.
    ox = (width - qr_px) // 2 + quiet
    oy = pad + quiet
    for ry, row in enumerate(matrix):
        y0 = oy + ry * scale
        run = None
        for rx, on in enumerate(row + [0]):
            if on and run is None:
                run = rx
            elif not on and run is not None:
                draw.rectangle([ox + run * scale, y0,
                                ox + rx * scale - 1, y0 + scale - 1],
                               fill=(0, 0, 0, 255))
                run = None

    ty = pad + qr_px + int(pad * 0.6)
    draw.text((width / 2, ty), caption, font=body, fill=(90, 96, 110, 255),
              anchor="ma")
    draw.text((width / 2, ty + line_h), text, font=addr, fill=(12, 14, 20, 255),
              anchor="ma")

    png = overlay_png_path()
    png.parent.mkdir(parents=True, exist_ok=True)
    tmp_png = png.with_suffix(".png.tmp")
    img.save(tmp_png, "PNG")

    # mpv wants pre-multiplied BGRA. Only the rounded corners are translucent,
    # but leaving them unmultiplied puts a white fringe around the panel on a
    # dark picture, which is the one place it will be seen.
    r, g, b, a = img.split()
    flat = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a),
                                ImageChops.multiply(r, a), a))
    tmp_raw = overlay_bgra_path().with_suffix(".bgra.tmp")
    tmp_raw.write_bytes(flat.tobytes())

    meta = {"url": url, "width": img.width, "height": img.height,
            "stride": img.width * 4, "screen_h": int(screen_h),
            "note": caption, "png": str(png), "bgra": str(overlay_bgra_path())}
    tmp_meta = overlay_meta_path().with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps(meta, indent=2) + "\n")

    # Rename last and in this order: the runner polls the PNG and mpv mmaps the
    # raw file, so both must be complete before anything points at them.
    os.replace(tmp_raw, overlay_bgra_path())
    os.replace(tmp_meta, overlay_meta_path())
    os.replace(tmp_png, png)
    log.info("drew the guest QR panel at %dx%d for a %dpx screen",
             img.width, img.height, screen_h)
    return meta


def overlay_meta() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(overlay_meta_path().read_text())
    except (OSError, ValueError):
        return None


def clear_overlay() -> None:
    """Take the panel down. The PNG goes last, for the same reason."""
    for path in (overlay_meta_path(), overlay_bgra_path(), overlay_png_path()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def publish_overlay(ip: str = "", port: int = 0, screen_h: int = 1080) -> Optional[Dict]:
    """Make the panel on disk match the session. Safe to call on every tick.

    Called from the player's supervisor rather than from the API, because the
    session closes itself: if the only thing that drew and cleared the panel
    were the open and close endpoints, an expired session would leave its QR on
    the screen until somebody happened to press something.
    """
    cfg = config.load()
    s = _load()
    if not (s.open() and cfg.guest_overlay):
        if overlay_meta_path().exists() or overlay_png_path().exists():
            clear_overlay()
            log.info("took the guest QR panel down")
        return None
    if not ip:
        from . import cluster

        ip = cluster.primary_ip()
    url = share_url(ip, port or cfg.web_port)
    if not url:
        return None
    current = overlay_meta()
    if (current and current.get("url") == url
            and current.get("screen_h") == int(screen_h)
            and overlay_png_path().exists() and overlay_bgra_path().exists()):
        return current
    return render_overlay(url, screen_h=screen_h, note=cfg.guest_note)


def overlay_status() -> Dict[str, Any]:
    ok, reason = overlay_available()
    meta = overlay_meta()
    return {
        "supported": ok,
        "reason": reason,
        "showing": bool(meta) and overlay_png_path().exists(),
        "size": [meta["width"], meta["height"]] if meta else None,
    }
