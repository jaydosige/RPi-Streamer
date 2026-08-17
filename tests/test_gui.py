"""GUI behaviour that only a real browser can prove.

Everything here is about timing and events rather than the API, so none of it
can be tested from Python alone:

  1. typing into a settings field survives the two-second status poll — the poll
     used to overwrite it, which reads as the page fighting you
  2. uploading shows a real progress bar with byte counts and a rate
  3. a node-to-node push reports progress while it runs, not just at the end
  4. controls that are not saved settings keep following the node rather than
     freezing the first time they are touched

Needs Playwright and Chromium. Skips cleanly without them.

    python3 tests/test_gui.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("skipping: playwright is not installed")
    raise SystemExit(0)

SRC = Path(__file__).resolve().parents[1] / "src"
CHROMIUM = os.environ.get("PISTREAMER_CHROMIUM", "/opt/pw-browsers/chromium")
TMP = Path(tempfile.mkdtemp(prefix="ui-check-"))
KEY = "test-key"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  \033[32m✓\033[0m " if cond else "  \033[31m✗\033[0m ") + name
          + (f" — {detail}" if detail and not cond else ""))


def start_node(name, port):
    state = TMP / name
    (state / "media").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(SRC), "PISTREAMER_NODE_ID": name,
        "PISTREAMER_STATE": str(state),
        "PISTREAMER_CONFIG": str(state / "config.json"),
        "PISTREAMER_MEDIA": str(state / "media"),
    })
    (state / "config.json").write_text(json.dumps({
        "device_name": name, "web_port": port, "cluster_enabled": True,
        "cluster_group": "default", "cluster_key": KEY,
        "cluster_extra_ips": "127.0.0.1", "autostart": False, "mode": "idle",
    }))
    code = ("import uvicorn\nfrom pistreamer.web import app\n"
            f"uvicorn.run(app, host='0.0.0.0', port={port}, log_level='error')\n")
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


async def main():
    procs = [start_node("STAGE-LEFT", 8131), start_node("STAGE-RIGHT", 8132)]
    upload_src = TMP / "big-upload.mp4"
    upload_src.write_bytes(os.urandom(18 * 1024 * 1024))
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:8131/api/cluster", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        time.sleep(7)  # let the beacons find each other

        async with async_playwright() as pw:
            b = await pw.chromium.launch(executable_path=CHROMIUM)
            pg = await b.new_page(viewport={"width": 1100, "height": 1400})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto("http://127.0.0.1:8131/", wait_until="networkidle")

            print("\ntyping is not overwritten by the poll")
            await pg.click("text=Nodes")
            await pg.wait_for_timeout(1500)
            await pg.click("#cfgClusterKey")
            await pg.fill("#cfgClusterKey", "")
            # Type slowly, the way a person does, across several poll cycles.
            await pg.type("#cfgClusterKey", "my-new-show-key", delay=120)
            typed = await pg.input_value("#cfgClusterKey")
            check("the field accepted the text", typed == "my-new-show-key", typed)
            await pg.wait_for_timeout(7000)  # >3 polls
            after = await pg.input_value("#cfgClusterKey")
            check("it survives three status polls while focused",
                  after == "my-new-show-key", after)

            # Click away, without saving: the classic case the focus-only guard
            # missed entirely.
            await pg.click("#cfgClusterGroup")
            await pg.wait_for_timeout(5000)
            after = await pg.input_value("#cfgClusterKey")
            check("it survives after clicking into another field",
                  after == "my-new-show-key", after)
            check("the edited field is marked",
                  "edited" in (await pg.get_attribute("#cfgClusterKey", "class") or ""),
                  await pg.get_attribute("#cfgClusterKey", "class"))

            # Now save, and confirm the field goes back to following the node.
            await pg.click("#btnSaveCluster")
            await pg.wait_for_timeout(3000)
            check("saving clears the edited marker",
                  "edited" not in (await pg.get_attribute("#cfgClusterKey", "class") or ""),
                  await pg.get_attribute("#cfgClusterKey", "class"))
            saved = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:8131/api/status", timeout=5).read())["config"]
            check("the typed value is what got saved",
                  saved["cluster_key"] == "my-new-show-key", saved["cluster_key"])
            # Changing the key on one node only genuinely splits the group —
            # that is what the key is for — so bring the other node with it
            # before testing anything that needs the two to talk.
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8132/api/config", method="POST",
                data=json.dumps({"cluster_key": "my-new-show-key"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=5)
            time.sleep(6)

            # A field nobody is editing must still track the node.
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8131/api/config", method="POST",
                data=json.dumps({"cluster_group": "changed-elsewhere"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=5)
            await pg.wait_for_timeout(4000)
            check("an untouched field still follows the node",
                  await pg.input_value("#cfgClusterGroup") == "changed-elsewhere",
                  await pg.input_value("#cfgClusterGroup"))

            print("\nupload progress")
            await pg.click("text=Local media")
            await pg.wait_for_timeout(800)
            await pg.set_input_files("#fileInput", str(upload_src))
            seen_bar, seen_pct, seen_rate = False, False, False
            deadline = time.time() + 60
            while time.time() < deadline:
                html = await pg.eval_on_selector("#uploadProgress", "e => e.innerHTML")
                if "bar" in html:
                    seen_bar = True
                pct = await pg.eval_on_selector_all(
                    "#uploadProgress .pct", "e => e.map(x => x.textContent)")
                rate = await pg.eval_on_selector_all(
                    "#uploadProgress .rate", "e => e.map(x => x.textContent)")
                if any(p not in ("0%", "") for p in pct):
                    seen_pct = True
                if any("/s" in r for r in rate):
                    seen_rate = True
                if any(p == "done" for p in pct):
                    break
                await pg.wait_for_timeout(100)
            check("a progress bar appeared", seen_bar)
            check("it showed a moving percentage", seen_pct)
            check("it showed a transfer rate", seen_rate)
            final = await pg.eval_on_selector("#uploadProgress", "e => e.innerText")
            check("it finished as done", "done" in final, final.replace("\n", " | "))
            listed = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:8131/api/media", timeout=5).read())["files"]
            check("the file is in the library",
                  any(f["name"] == "big-upload.mp4" for f in listed), json.dumps(listed))
            await pg.screenshot(path=str(TMP / "upload.png"))

            print("\npush progress")
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8131/api/playlists", method="POST",
                data=json.dumps({"name": "Wall", "items": [
                    {"type": "file", "target": "big-upload.mp4"}]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            await pg.click("text=Nodes")
            await pg.wait_for_timeout(1200)
            await pg.select_option("#syncPlaylist", "Wall")
            await pg.click("#btnPushPlaylist")
            seen_push, seen_push_pct, shot = False, False, False
            deadline = time.time() + 90
            while time.time() < deadline:
                text = await pg.eval_on_selector("#pushProgress", "e => e.innerText")
                if "STAGE-RIGHT" in text:
                    seen_push = True
                if "%" in text and "0%" not in text.split("\n")[0]:
                    seen_push_pct = True
                    if not shot:
                        await pg.screenshot(path=str(TMP / "push.png"))
                        shot = True
                if "finished" in text:
                    break
                await pg.wait_for_timeout(120)
            check("the push showed the target node", seen_push)
            check("the push showed a percentage", seen_push_pct)
            final = await pg.eval_on_selector("#pushProgress", "e => e.innerText")
            check("the push reported finishing", "finished" in final,
                  final.replace("\n", " | ")[:200])
            remote = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:8132/api/media", timeout=10).read())["files"]
            check("the other node received the file",
                  any(f["name"] == "big-upload.mp4" for f in remote), json.dumps(remote))
            await pg.screenshot(path=str(TMP / "push-done.png"))

            print("\ncontrols that must not freeze")
            # The playlist selector is not a saved setting: after using it once
            # it must still pick up playlists created since.
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8131/api/playlists", method="POST",
                data=json.dumps({"name": "Later show", "items": [
                    {"type": "file", "target": "big-upload.mp4"}]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            await pg.wait_for_timeout(4000)
            opts = await pg.eval_on_selector_all(
                "#syncPlaylist option", "e => e.map(o => o.value)")
            check("the playlist selector still refreshes after being used",
                  "Later show" in opts, str(opts))

            # Identify applies immediately, so it must keep reflecting reality.
            await pg.click("#cfgIdentify")
            await pg.wait_for_timeout(3000)
            check("identify shows as on after toggling",
                  await pg.is_checked("#cfgIdentify"))
            urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8131/api/cluster/identify", method="POST",
                data=json.dumps({"on": False, "propagate": False}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            await pg.wait_for_timeout(4000)
            check("identify follows a change made elsewhere",
                  not await pg.is_checked("#cfgIdentify"))

            check("no page errors throughout", not errors, str(errors))
            await b.close()
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, 15)
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.kill()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


asyncio.run(main())
