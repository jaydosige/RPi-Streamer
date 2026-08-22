"""The status panel must survive every backend's stats, not just the runner's.

mpv and the GStreamer runner report different subsets of one vocabulary: mpv
has no frame counters, no arrival rate and no queue depth at all. renderStream
guarded with `!== null`, which undefined sails straight past, so it threw on
.toFixed for anything mpv-backed — local files and streams both. poll() caught
that and reported it as the node being unreachable, which sent the search for
the cause to the network.

Needs playwright. Skips cleanly without it.

    python3 tests/test_status_render.py
"""
import os, sys, tempfile, threading, time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("skipping: playwright is not installed")
    raise SystemExit(0)

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-render-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config  # noqa: E402
config.update(setup_complete=True)
from pistreamer.player import player  # noqa: E402
import uvicorn  # noqa: E402
from pistreamer.web import app  # noqa: E402

# Exactly what mpvipc.to_stats produces: no rendered, no frames_total, no
# arrival_fps, no queue_overruns, no since_last_frame.
MPV_STATS = {"backend": "mpv", "fps": 50.0, "width": 1920, "height": 1080,
             "format": "yuv420p", "decoder": "drm-copy", "hardware_decode": True,
             "dropped": 0, "position": 12.0, "duration": 300.0}
# And what the runner produces, which has all of them.
GST_STATS = {"fps": 50.0, "arrival_fps": 50.1, "width": 1920, "height": 1080,
             "rendered": 1234, "frames_total": 1240, "dropped": 6,
             "render_mbps": 120, "queue_overruns": 0, "since_last_frame": 0.02,
             "time_to_first_frame": 1.2, "qos_events": 0, "format": "NV12"}

CASES = [
    ("stream", "udp://238.0.0.1:1234", MPV_STATS, "playing a stream"),
    ("local", "clip.mp4", MPV_STATS, "playing local"),
    ("ndi", "STUDIO (OBS)", GST_STATS, "receiving NDI"),
]

srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8795,
                                    log_level="error"))
threading.Thread(target=srv.run, daemon=True).start()
time.sleep(1.5)

fails = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for mode, target, stats, expect_pill in CASES:
        player.status = lambda m=mode, t=target: {
            "mode": m, "target": t, "running": True, "fallback": False,
            "last_error": "", "restarts": 0, "uptime": 60.0, "pid": 1,
            "strays_cleaned": 0, "sync": {}}
        player.stream_stats = lambda s=stats: {**s, "t": time.time()}
        pg = browser.new_page(viewport={"width": 1280, "height": 900})
        pg.goto("http://127.0.0.1:8795/", wait_until="networkidle")
        time.sleep(2.5)
        thrown = pg.evaluate("""() => {
            try { renderStatus(); return null; } catch (e) { return String(e); }
        }""")
        pill = pg.inner_text("#statusText")
        label = pg.inner_text("#nowTarget")
        pg.close()

        def check(name, cond, detail=""):
            print(("  ok   " if cond else "  FAIL ") + f"{mode}: {name}" +
                  (f"   [{detail}]" if not cond and detail else ""))
            if not cond:
                fails.append(f"{mode}: {name}")

        check("renderStatus does not throw", thrown is None, str(thrown))
        # The specific lie: a drawing bug reported as a network failure.
        check("not reported as unreachable", pill != "unreachable", pill)
        check(f"status reads {expect_pill!r}", pill == expect_pill, pill)
        check("the hero names what is playing", target in label or label == target,
              label)
    browser.close()

srv.should_exit = True
print("ok" if not fails else f"{len(fails)} failed")
sys.exit(1 if fails else 0)
