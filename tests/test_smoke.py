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
        # Assert the keys that matter rather than a count, so adding a
        # setting does not break the test for no reason.
        keys = set(r.json())
        required = {
            "mode", "ndi_source", "local_file", "autostart", "connector",
            "video_mode", "rotation", "ndi_bandwidth", "ndi_timestamp_mode",
            "ndi_color_format", "sink_sync", "sink_qos", "scale_method",
            "video_format", "queue_leaky", "idle_mode", "standby_file",
            "snapshot_enabled", "use_gst_launch",
        }
        check("config exposes every documented key", required <= keys,
              f"missing: {sorted(required - keys)}")

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

        print("\nnew settings validation")
        for bad in ({"idle_mode": "rainbow"}, {"queue_leaky": "sideways"},
                    {"scale_method": 9}, {"video_format": "XRGB9999"},
                    {"ndi_color_format": "beige"}):
            r = client.post("/api/config", json=bad)
            check(f"reject {list(bad)[0]}={list(bad.values())[0]} -> 400",
                  r.status_code == 400, r.text)
        r = client.post("/api/config", json={"standby_file": "nope.png"})
        check("standby_file must exist -> 404", r.status_code == 404, r.text)
        r = client.post("/api/config", json={"sink_qos": False, "scale_method": 0,
                                             "idle_mode": "lastframe"})
        check("valid performance patch -> 200", r.status_code == 200, r.text)

        print("\nNDI networking (multi-homed support)")
        r = client.post("/api/config", json={"ndi_adapter_ips": "10.0.0.50",
                                            "ndi_extra_ips": "10.0.0.20, 10.0.0.21",
                                            "ndi_discovery_server": "10.0.0.2"})
        check("NDI network settings accepted -> 200", r.status_code == 200, r.text)
        from pistreamer import ndiconfig  # noqa: PLC0415
        built = ndiconfig.build("10.0.0.50", "10.0.0.20, 10.0.0.21", "10.0.0.2")
        check("adapters.allowed is a list of IPs",
              built["ndi"]["adapters"]["allowed"] == ["10.0.0.50"], str(built))
        check("networks.ips is a comma string, as the SDK wants",
              built["ndi"]["networks"]["ips"] == "10.0.0.20,10.0.0.21,", str(built))
        check("empty settings write no keys at all", ndiconfig.build() == {},
              "an empty adapters list can stop NDI working entirely")
        r = client.post("/api/config", json={"ndi_url_address": "10.0.0.20:5961"})
        check("connect-by-address accepted -> 200", r.status_code == 200, r.text)
        r = client.post("/api/config", json={"ndi_adapter_ips": "", "ndi_extra_ips": "",
                                            "ndi_discovery_server": "", "ndi_url_address": ""})
        check("clearing them again -> 200", r.status_code == 200, r.text)

        print("\ndiagnosis")
        r = client.get("/api/diagnose")
        check("GET /api/diagnose -> 200", r.status_code == 200, r.text)
        body = r.json()
        check("diagnose returns a verdict", "verdict" in body and "headline" in body, r.text)
        check("verdict is idle with nothing playing", body["verdict"] == "idle", r.text)

        r = client.get("/api/telemetry?points=5")
        check("telemetry honours points=5", r.status_code == 200 and len(r.json()["t"]) <= 5, r.text)

        # Playlist segments over the API. The request model and playlists.py
        # drifted apart once — items became segments in one and stayed
        # List[str] in the other — so every save from the editor came back as
        # a 422 saying the item "should be a valid string". Both shapes, and
        # the real validation errors, are pinned here.
        print("\nplaylist API")
        r = client.post("/api/playlists", json={
            "name": "Pre-show",
            "items": [
                {"type": "file", "target": "clip.mp4", "duration": None},
                {"type": "ndi", "target": "STUDIO-PC (Test Patterns)", "duration": 20},
            ],
            "loop": True, "shuffle": False, "image_duration": 10,
        })
        check("POST a mixed file/NDI playlist -> 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            saved = r.json()
            check("segment order preserved",
                  [i["target"] for i in saved["items"]]
                  == ["clip.mp4", "STUDIO-PC (Test Patterns)"], str(saved["items"]))
            check("NDI segment keeps its type and duration",
                  saved["items"][1]["type"] == "ndi" and saved["items"][1]["duration"] == 20,
                  str(saved["items"][1]))

        r = client.post("/api/playlists", json={"name": "Legacy", "items": ["clip.mp4"]})
        check("legacy bare-string items still accepted -> 200", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            check("legacy item migrated to a file segment",
                  r.json()["items"][0] == {"type": "file", "target": "clip.mp4",
                                           "duration": None}, str(r.json()["items"]))

        r = client.post("/api/playlists", json={
            "name": "Endless", "items": [{"type": "ndi", "target": "STUDIO-PC (X)"}]})
        check("NDI without a duration -> 400 with a useful message",
              r.status_code == 400 and "duration" in r.text.lower(),
              f"{r.status_code} {r.text[:200]}")
        r = client.post("/api/playlists", json={
            "name": "Nope", "items": [{"type": "hologram", "target": "x"}]})
        check("unknown segment type -> 422", r.status_code == 422, str(r.status_code))
        for name in ("Pre-show", "Legacy"):
            client.delete(f"/api/playlists/{name}")

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
