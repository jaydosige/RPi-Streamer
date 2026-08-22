"""GLSL shaders, stored as files and drawn by the browser.

A shader is a web page. Web mode already runs Chromium full-screen on the
display, so playing one is web mode pointed back at this node, and the editor
previews with the same page — which is what stops "what I wrote" and "what is
on the wall" being two renderers that agree until they do not.

The rendering half needs playwright and is skipped without it. WebGL in
headless Chromium needs --use-angle=swiftshader; --use-gl=swiftshader loses the
context on this machine, which is worth knowing if this ever starts failing for
no apparent reason.

    python3 tests/test_shaders.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-shaders-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP / "media"), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, shaders  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def main() -> int:
    print("the store")
    check("a name with a slash is refused", not shaders.valid_name("a/b"))
    check("an empty name is refused", not shaders.valid_name(""))
    check("a sensible name is fine", shaders.valid_name("plasma 2"))
    # The name comes from a browser; a name that escapes the directory is a
    # file read, so it is resolved the same way media names are.
    for hostile in ("../../etc/passwd", "..", "a/../../b"):
        check(f"{hostile!r} cannot escape the directory",
              shaders.path_for(hostile) is None)

    print("\nwhat may be stored")
    check("the built-in template is valid",
          shaders.check_source(shaders.DEFAULT_SOURCE) == "")
    check("an empty shader is refused", shaders.check_source("  ") != "")
    check("something with neither entry point is refused",
          "mainImage" in shaders.check_source("float x = 1.0;"))
    check("a shader with its own main() is allowed",
          shaders.check_source("void main(){ gl_FragColor = vec4(1.0); }") == "")
    # The source is embedded in a page; closing the script tag is the one real
    # escape available from inside GLSL, which is otherwise sandboxed by being
    # a fragment shader.
    check("a closing script tag is refused",
          shaders.check_source("void main(){} </script><script>alert(1)") != "")
    check("something enormous is refused",
          shaders.check_source("void main(){}" + "\n// pad" * 100000) != "")

    print("\nsaving and loading")
    shaders.save("plasma", shaders.DEFAULT_SOURCE)
    check("it round-trips", shaders.get("plasma") == shaders.DEFAULT_SOURCE)
    check("it is listed", "plasma" in [s["name"] for s in shaders.all_shaders()])
    check("an unknown shader is None", shaders.get("nope") is None)
    check("a hostile name reads nothing", shaders.get("../../etc/passwd") is None)
    check("deleting works", shaders.delete("plasma"))
    check("deleting again does not", not shaders.delete("plasma"))

    print("\nwrapping")
    check("a mainImage shader gets a main() added",
          shaders.needs_wrapper(shaders.DEFAULT_SOURCE))
    check("a shader with main() is left alone",
          not shaders.needs_wrapper("void main(){ gl_FragColor = vec4(1.0); }"))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n(skipping the render — playwright is not installed)")
        return report()

    print("\nrendering, in a real browser")
    shaders.save("plasma", shaders.DEFAULT_SOURCE)
    config.update(setup_complete=True)
    import uvicorn
    from pistreamer.web import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8786,
                                        log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.5)

    def palette(path):
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB").resize((32, 18))
            return len(set(list(im.getdata()))), im.getpixel((16, 9))

    shots = TMP / "shots"
    shots.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        pg = browser.new_page(viewport={"width": 640, "height": 360})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto("http://127.0.0.1:8786/shader?name=plasma", wait_until="load")
        time.sleep(2)
        check("the shader page renders", pg.is_visible("#gl"))
        check("...with no error shown", not pg.is_visible("#err"),
              pg.inner_text("#err")[:120])
        pg.screenshot(path=str(shots / "a.png"))
        time.sleep(1.3)
        pg.screenshot(path=str(shots / "b.png"))
        count_a, mid_a = palette(shots / "a.png")
        count_b, mid_b = palette(shots / "b.png")
        check(f"it draws something ({count_a} colours)", count_a > 5)
        check("it animates", mid_a != mid_b, f"{mid_a} then {mid_b}")

        # A shader that will not compile must say why, not go quietly black.
        pg.evaluate("""() => window.postMessage({shaderSource:
            'void mainImage(out vec4 c, in vec2 f){ c = nope(f); }'}, '*')""")
        time.sleep(0.8)
        message = pg.inner_text("#err")
        check("a broken shader shows an error", pg.is_visible("#err"))
        check("...naming the offending symbol", "nope" in message, message[:120])
        # Line numbers are against the assembled source, so the reader is told
        # how much was added above their code.
        check("...and how to map the line number back",
              "preamble" in message, message[:200])

        pg.evaluate("""() => window.postMessage({shaderSource:
            'void mainImage(out vec4 c, in vec2 f){ c = vec4(1.0,0.0,0.0,1.0); }'}, '*')""")
        time.sleep(0.8)
        check("fixing it recovers", pg.is_visible("#gl") and not pg.is_visible("#err"))
        pg.screenshot(path=str(shots / "c.png"))
        check("...and draws the new shader", palette(shots / "c.png")[1][0] > 200,
              str(palette(shots / "c.png")[1]))
        check("no JavaScript errors throughout", not errors, str(errors[:2]))
        browser.close()
    srv.should_exit = True
    return report()


def report() -> int:
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
