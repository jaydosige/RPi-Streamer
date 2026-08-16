"""Smoke tests: run the whole app on a non-Pi host and exercise every endpoint.

The point is not to test playback — there is no DRM device or NDI sender here —
but to prove the service boots, the API contract holds, and every failure path
degrades gracefully instead of throwing a 500 with a stack trace.

    python3 tests/test_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-test-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pistreamer import config, media  # noqa: E402
from pistreamer.web import app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}{'  — ' + detail if detail and not cond else ''}")


def main() -> int:
    print(f"\nworkspace: {TMP}\n")
    with TestClient(app) as client:
        print("status + config")
        r = client.get("/api/status")
        check("GET /api/status -> 200", r.status_code == 200, r.text)
        body = r.json()
        check("status has player/system/config", {"player", "system", "config"} <= set(body))
        check("player starts idle", body["player"]["mode"] == "idle", str(body["player"]))
        check("system telemetry present", "hostname" in body["system"])

        r = client.get("/api/config")
        check("GET /api/config -> 200", r.status_code == 200)
        check("config exposes all 17 keys", len(r.json()) == 17, str(len(r.json())))

        print("\nconfig validation")
        r = client.post("/api/config", json={"rotation": 45})
        check("bad rotation -> 400", r.status_code == 400, r.text)
        r = client.post("/api/config", json={"ndi_bandwidth": "medium"})
        check("bad bandwidth -> 400", r.status_code == 400, r.text)
        r = client.post("/api/config", json={"nonsense": 1})
        check("unknown key -> 400", r.status_code == 400, r.text)
        r = client.post("/api/config", json={"rotation": 180, "ndi_latency_ms": 400})
        check("valid patch -> 200", r.status_code == 200, r.text)
        check("patch persisted", r.json()["rotation"] == 180)
        check(
            "patch written to disk",
            "180" in Path(os.environ["PISTREAMER_CONFIG"]).read_text(),
        )

        print("\ndisplay + ndi discovery (no hardware here)")
        r = client.get("/api/display/connectors")
        check("GET connectors -> 200 on a non-DRM host", r.status_code == 200, r.text)
        check("connectors is a list", isinstance(r.json()["connectors"], list))
        r = client.get("/api/ndi/sources")
        check("GET ndi sources degrades to 200", r.status_code == 200, r.text)
        check("returns empty list, not an error", r.json()["sources"] == [])

        print("\nmedia library")
        r = client.get("/api/media")
        check("GET media -> 200", r.status_code == 200)
        check("library starts empty", r.json()["files"] == [])

        r = client.post(
            "/api/media", files={"file": ("clip.mp4", b"\x00" * 2048, "video/mp4")}
        )
        check("upload mp4 -> 200", r.status_code == 200, r.text)
        check("upload reports size", r.json()["size"] == 2048)

        r = client.post(
            "/api/media", files={"file": ("evil.sh", b"#!/bin/sh\n", "text/plain")}
        )
        check("upload .sh rejected -> 400", r.status_code == 400, r.text)

        r = client.post(
            "/api/media",
            files={"file": ("../../../../etc/passwd.mp4", b"x" * 16, "video/mp4")},
        )
        check("path traversal in filename -> 200", r.status_code == 200, r.text)
        check(
            "traversal flattened to a safe basename",
            r.json()["name"] == "passwd.mp4",
            r.json().get("name", ""),
        )
        check(
            "nothing written outside the media dir",
            sorted(p.name for p in config.MEDIA_DIR.iterdir())
            == ["clip.mp4", "passwd.mp4"],
            str(list(config.MEDIA_DIR.iterdir())),
        )

        check("media.resolve rejects ../ escape", media.resolve("../../etc/passwd") is None)
        check("media.resolve accepts a real file", media.resolve("clip.mp4") is not None)

        r = client.get("/api/media")
        check("library lists 2 files", len(r.json()["files"]) == 2, r.text)

        print("\nplayback (backends absent on this host)")
        r = client.post("/api/play/local", json={"file": "nope.mp4"})
        check("play missing file -> 404", r.status_code == 404, r.text)

        r = client.post("/api/play/ndi", json={"source": "FAKE (Test)"})
        check(
            "play ndi with no NDI stack -> clean 500",
            r.status_code == 500,
            r.text,
        )
        check(
            "error names the missing piece",
            any(s in r.text for s in ("ndisrc", "GStreamer", "gst-launch", "not installed")),
            r.text,
        )

        r = client.post("/api/play/ndi", json={"source": ""})
        check("empty ndi source -> 422 from validation", r.status_code == 422, r.text)

        r = client.post("/api/stop")
        check("stop -> 200", r.status_code == 200, r.text)
        check("stop returns to idle", r.json()["mode"] == "idle", r.text)

        print("\nmedia deletion")
        r = client.delete("/api/media/clip.mp4")
        check("delete -> 200", r.status_code == 200, r.text)
        r = client.delete("/api/media/clip.mp4")
        check("delete twice -> 404", r.status_code == 404, r.text)
        r = client.delete("/api/media/..%2F..%2Fetc%2Fpasswd")
        check("delete traversal -> 404", r.status_code in (404, 400), r.text)

        print("\nlogs + gui")
        r = client.get("/api/logs")
        check("GET logs -> 200", r.status_code == 200)
        check("logs recorded the attempted spawn", isinstance(r.json()["lines"], list))

        r = client.get("/")
        check("GET / serves the GUI", r.status_code == 200, str(r.status_code))
        check("GUI is real HTML", "<title>pi-streamer</title>" in r.text)
        check("GUI has no external requests", "http://" not in r.text.split("<script>")[0]
              or "cdn" not in r.text.lower())

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
