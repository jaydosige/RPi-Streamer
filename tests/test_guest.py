"""Guest sharing: the door the room can walk through.

Every test here is written from the point of view of somebody in the audience
rather than the operator, because that is the threat model. The QR is decoded
with a real decoder rather than eyeballed — a QR that renders but does not scan
looks perfect in a screenshot and fails in front of two hundred people.

    python3 tests/test_guest.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-guest-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pistreamer import config, guest  # noqa: E402
from pistreamer.web import app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}{'  — ' + detail if detail and not cond else ''}")


def upload(client, token, name, size=2048, sender=""):
    return client.post(
        f"/s/{token}/upload",
        files={"file": (name, io.BytesIO(b"\0" * size), "application/octet-stream")},
        data={"from": sender},
    )


def decode_qr(svg: str) -> str:
    """Render the SVG and read the code back with OpenCV.

    Returns "" if the tooling is not available, so this file still runs on a
    box without cairosvg/cv2 — it just proves less.
    """
    try:
        # cairosvg first, deliberately: importing cv2 first pulls in its own
        # bundled libraries and cairo then fails to load, which silently turned
        # this check into a skip.
        import cairosvg
        import cv2
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        print(f"  · QR decode unavailable: {exc}")
        return ""
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=600,
                           output_height=600, background_color="white")
    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return text or ""


def main() -> int:
    print(f"\nworkspace: {TMP}\n")
    with TestClient(app) as client:
        print("closed by default")
        r = client.get("/api/guest")
        check("GET /api/guest -> 200", r.status_code == 200, r.text)
        g = r.json()
        check("sharing starts closed", g["open"] is False)
        check("no URL while closed", g["url"] == "")
        check("no token while closed", g["token"] == "")
        r = client.get("/s/anything/status")
        check("a guest status with no session -> 404", r.status_code == 404, r.text)
        r = upload(client, "anything", "a.mp4")
        check("upload with no session -> 404", r.status_code == 404, r.text)

        print("\nopening")
        r = client.post("/api/guest/open", json={"minutes": 30, "note": "Sam & Rowan"})
        check("POST /api/guest/open -> 200", r.status_code == 200, r.text)
        g = r.json()
        token = g["token"]
        check("a token is minted", len(token) >= 8, token)
        check("the URL carries the token", g["url"].endswith("/s/" + token), g["url"])
        check("the note came back", g["note"] == "Sam & Rowan")
        check("it expires", 0 < (g["remaining"] or 0) <= 30 * 60, str(g["remaining"]))
        check("the URL is plain http", g["url"].startswith("http://"), g["url"])

        print("\nthe QR")
        check("a QR was drawn", g["qr"].lstrip().startswith("<svg"), g["qr"][:40])
        check("the QR is inline, not a link", "href" not in g["qr"][:400])
        check("the QR sets its own colours",
              "#000000" in g["qr"] or "#000" in g["qr"], g["qr"][:200])
        # The bug this catches: a fixed width/height and no viewBox means CSS
        # resizes the canvas and leaves the drawing tiny in the corner.
        check("the QR scales with its box", "viewBox" in g["qr"], g["qr"][:120])
        check("the QR has no fixed pixel size",
              " width=" not in g["qr"][:200], g["qr"][:120])
        decoded = decode_qr(g["qr"])
        if decoded:
            check("the QR decodes to the share URL", decoded == g["url"],
                  f"{decoded!r} != {g['url']!r}")
        else:
            print("  · QR decode skipped (cv2/cairosvg not available)")

        print("\na fresh token every time")
        first = token
        token = client.post("/api/guest/open", json={"minutes": 30}).json()["token"]
        check("reopening mints a new token", token != first)
        check("the old token is dead", client.get(f"/s/{first}/status").status_code == 404)

        print("\nwhat a guest can see")
        r = client.get(f"/s/{token}/status")
        check("guest status -> 200", r.status_code == 200, r.text)
        s = r.json()
        check("the guest is not told the token", "token" not in s, str(s.keys()))
        check("the guest is not told about other uploads", "items" not in s)
        check("the guest is told the size cap", s["limits"]["max_mb"] > 0)
        check("the guest is told how many more it will take", s["remaining_items"] > 0)
        check("the guest is told when it closes", (s["closes_in"] or 0) > 0)
        r = client.get(f"/s/{token}")
        check("the guest page is served", r.status_code == 200)
        check("the guest page is not the operator GUI",
              "Guest sharing" not in r.text and "tab-system" not in r.text)
        check("the guest page has no external requests",
              "cdn" not in r.text.lower() and "https://" not in r.text)

        print("\nuploading")
        r = upload(client, token, "IMG_0042.mp4", 4096, sender="Priya")
        check("upload -> 200", r.status_code == 200, r.text)
        stored = r.json()["name"]
        check("the stored name is prefixed", stored.startswith("guest-"), stored)
        check("the original name survives", stored.endswith("IMG_0042.mp4"), stored)
        check("it landed in the library", (config.MEDIA_DIR / stored).exists())
        check("no .part file was left", not list(config.MEDIA_DIR.glob(".*.part")))
        g = client.get("/api/guest").json()
        check("the operator sees it queued", any(i["name"] == stored for i in g["items"]))
        check("the operator sees who sent it",
              g["items"][0]["from"] == "Priya", str(g["items"][0]))
        check("it is not marked shown", g["items"][0]["played"] is False)

        print("\nname collisions do not overwrite")
        second = upload(client, token, "IMG_0042.mp4", 8192).json()["name"]
        check("a second upload of the same name gets its own file", second != stored)
        check("the first file is untouched",
              (config.MEDIA_DIR / stored).stat().st_size == 4096)

        print("\nwhat a guest cannot do")
        r = upload(client, token, "payload.sh", 64)
        check("a script is refused", r.status_code == 400, r.text)
        r = upload(client, token, "../../etc/passwd.mp4", 64)
        check("a traversing name is accepted but flattened", r.status_code == 200, r.text)
        if r.status_code == 200:
            landed = r.json()["name"]
            check("...and it stayed in the media folder",
                  "/" not in landed and ".." not in landed, landed)
            client.delete(f"/api/guest/item/{landed}")
        config.update(guest_max_mb=1)
        r = upload(client, token, "huge.mp4", 3 << 20)
        check("an oversized file is refused", r.status_code == 413, r.text)
        check("the oversized file left nothing behind",
              not list(config.MEDIA_DIR.glob("*huge*")) and
              not list(config.MEDIA_DIR.glob(".*.part")))
        config.update(guest_max_mb=512)

        print("\nthe operator decides, by default")
        check("autoplay is off by default", config.load().guest_autoplay is False)
        r = client.post(f"/s/{token}/play", json={"name": stored})
        check("a guest cannot take the screen -> 403", r.status_code == 403, r.text)
        config.update(guest_autoplay=True)
        r = client.post(f"/s/{token}/play", json={"name": "opening-titles.mp4"})
        check("even then, only this session's own uploads -> 404",
              r.status_code == 404, r.text)
        r = client.post(f"/s/{token}/play", json={"name": stored})
        check("a guest may show their own upload when allowed",
              r.status_code == 200, r.text)
        check("the node switched to it", config.load().local_file == stored)
        g = client.get("/api/guest").json()
        check("it is marked shown",
              [i for i in g["items"] if i["name"] == stored][0]["played"] is True)
        config.update(guest_autoplay=False, mode="idle", local_file="")

        print("\nthe operator's side")
        r = client.post("/api/guest/play", json={"name": stored})
        check("the operator can show a queued item", r.status_code == 200, r.text)
        config.update(mode="idle", local_file="")
        r = client.delete(f"/api/guest/item/{second}")
        check("discard -> 200", r.status_code == 200, r.text)
        check("discard deletes the file too", not (config.MEDIA_DIR / second).exists())
        g = client.get("/api/guest").json()
        check("discard drops it from the queue",
              not any(i["name"] == second for i in g["items"]))
        r = client.delete("/api/guest/item/not-a-thing.mp4")
        check("discarding nothing -> 404", r.status_code == 404, r.text)

        print("\ncaps and closing")
        config.update(guest_max_items=len(client.get("/api/guest").json()["items"]))
        r = upload(client, token, "one-too-many.mp4", 512)
        check("a full session refuses more -> 409", r.status_code == 409, r.text)
        s = client.get(f"/s/{token}/status").json()
        check("the guest page is told it is full", s["remaining_items"] == 0)
        config.update(guest_max_items=20)

        r = client.post("/api/guest/extend", json={"minutes": 90})
        check("extend -> 200", r.status_code == 200, r.text)
        check("extend pushed the deadline out",
              (r.json()["remaining"] or 0) > 60 * 60, str(r.json()["remaining"]))

        r = client.post("/api/guest/close")
        check("close -> 200", r.status_code == 200, r.text)
        check("closed means closed", r.json()["open"] is False)
        check("the URL is gone", r.json()["url"] == "")
        check("the token stops working",
              client.get(f"/s/{token}/status").status_code == 404)
        check("uploads stop", upload(client, token, "late.mp4").status_code == 404)
        check("the page still loads, to explain itself",
              client.get(f"/s/{token}").status_code == 200)

        print("\nexpiry closes the door on its own")
        g = guest.open_session(minutes=1)
        check("a session opens", guest.valid(g.token))
        s = guest.session()
        s.expires = time.time() - 1
        guest._save(s)
        check("an expired session is not valid", not guest.valid(g.token))
        check("...and the API agrees", client.get("/api/guest").json()["open"] is False)
        check("...and the guest routes are shut",
              client.get(f"/s/{g.token}/status").status_code == 404)
        guest.close_session()

        print("\ntoken handling")
        g = guest.open_session(minutes=5)
        check("a near-miss token is rejected", not guest.valid(g.token[:-1] + "x"))
        check("an empty token is rejected", not guest.valid(""))
        check("a token is url-safe", g.token.isalnum(), g.token)
        guest.close_session()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
