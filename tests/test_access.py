"""Tests for the operator login, favourites, streams and the setup wizard.

The login tests are the point of this file. Getting auth wrong on an appliance
has two failure modes and both are bad: leaving the console open when it was
meant to be shut, and locking out the person who owns the box. So the
exemptions are asserted explicitly rather than assumed — above all that guest
sharing keeps working, which is the whole reason the QR code exists, and that
node-to-node traffic keeps working, which is what stops a group falling apart
the moment the login is switched on.

Needs fastapi. Where it is missing the file says so and exits 0 rather than
pretending to have run.

    python3 tests/test_access.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-access-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("fastapi is not installed here — skipping the access tests")
    raise SystemExit(0)

from pistreamer import auth, cluster, config, favourites  # noqa: E402
from pistreamer.web import app  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def main() -> int:
    client = TestClient(app)

    print("a node with no login is not locked")
    check("the API is open", client.get("/api/config").status_code == 200)
    check("auth status is readable", client.get("/api/auth/status").status_code == 200)

    print("\nsetting a password does not lock anything on its own")
    auth.set_password("jayden", "a decent passphrase")
    check("still open until auth_enabled is set",
          client.get("/api/config").status_code == 200)

    print("\nswitching the login on")
    config.update(auth_enabled=True, setup_complete=True)
    check("the API refuses", client.get("/api/config").status_code == 401)
    check("the sign-in check still answers",
          client.get("/api/auth/status").status_code == 200)

    print("\nguest sharing is never behind the login")
    # The whole feature is that a stranger with the QR code can use it. If any
    # of these start returning 401 the feature is broken, not tightened.
    check("the guest page opens", client.get("/s/tok").status_code in (200, 404))
    check("guest status opens", client.get("/s/tok/status").status_code in (200, 404))
    check("guest upload is reachable",
          client.post("/s/tok/upload").status_code != 401)
    check("the GUI shell is served so it can show a login",
          client.get("/").status_code in (200, 404))

    print("\nsigning in")
    check("a wrong password is refused",
          client.post("/api/auth/login",
                      json={"username": "jayden", "password": "no"}).status_code == 401)
    check("a wrong username is refused too",
          client.post("/api/auth/login",
                      json={"username": "x", "password": "a decent passphrase"}
                      ).status_code == 401)
    r = client.post("/api/auth/login",
                    json={"username": "jayden", "password": "a decent passphrase"})
    check("the right one is accepted", r.status_code == 200, r.text[:120])
    cookie = r.headers.get("set-cookie", "").lower()
    check("the cookie is HttpOnly", "httponly" in cookie, cookie)
    check("the cookie is SameSite=lax", "samesite=lax" in cookie, cookie)
    # secure=True would mean the cookie is never sent over plain HTTP, which is
    # all this ever serves. It would lock every browser out.
    check("the cookie is not Secure, since this is plain-HTTP LAN",
          "secure" not in cookie.replace("samesite", ""), cookie)
    check("the API opens up", client.get("/api/config").status_code == 200)

    print("\na peer is not a browser")
    peer = TestClient(app)
    check("no key is refused", peer.get("/api/config").status_code == 401)
    key = config.load().cluster_key
    check("the group key is accepted",
          peer.get("/api/config", headers={cluster.AUTH_HEADER: key}).status_code == 200)
    check("a wrong group key is not",
          peer.get("/api/config",
                   headers={cluster.AUTH_HEADER: "nope"}).status_code == 401)

    print("\nsessions end when they should")
    other = TestClient(app)
    other.post("/api/auth/login",
               json={"username": "jayden", "password": "a decent passphrase"})
    check("a second browser can sign in", other.get("/api/config").status_code == 200)
    r = other.post("/api/auth/password",
                   json={"current": "a decent passphrase", "password": "another one here"})
    check("the password can be changed", r.status_code == 200, r.text[:120])
    check("...which signs everyone out", client.get("/api/config").status_code == 401)
    check("the old password stops working",
          client.post("/api/auth/login",
                      json={"username": "jayden",
                            "password": "a decent passphrase"}).status_code == 401)
    check("a wrong current password is refused",
          other.post("/api/auth/password",
                     json={"current": "wrong", "password": "yet another one"}
                     ).status_code in (401, 403))

    print("\nthe way back in")
    auth.disable()
    check("removing auth.json opens the node",
          TestClient(app).get("/api/config").status_code == 200)
    config.update(auth_enabled=False)

    print("\nfavourites")
    fresh = TestClient(app)
    check("the list starts empty",
          fresh.get("/api/favourites").json()["favourites"] == [])
    check("a web page saves",
          fresh.post("/api/favourites",
                     json={"name": "Kitchen dash", "url": "http://d.local:3000/w",
                           "kind": "web"}).status_code == 200)
    check("a stream saves",
          fresh.post("/api/favourites",
                     json={"name": "Stage feed", "url": "udp://238.0.0.1:1234",
                           "kind": "stream"}).status_code == 200)
    for url, kind, why in [
        ("file:///etc/passwd", "web", "a file:// URL"),
        ("javascript:alert(1)", "web", "a javascript: URL"),
        ("udp://1.2.3.4:5", "web", "a stream address saved as a page"),
        ("d.local", "web", "an address with no scheme"),
        ("http://", "web", "an address with no host"),
    ]:
        r = fresh.post("/api/favourites", json={"name": "x", "url": url, "kind": kind})
        check(f"{why} is refused", r.status_code in (400, 422), r.text[:90])
    check("a name with a slash in it is refused",
          fresh.post("/api/favourites",
                     json={"name": "a/b", "url": "http://a.b",
                           "kind": "web"}).status_code in (400, 422))

    favourites.mark_used("http://d.local:3000/w")
    listed = fresh.get("/api/favourites").json()["favourites"]
    check("use is counted",
          next(f for f in listed if f["name"] == "Kitchen dash")["uses"] == 1)
    check("the most recently used sorts first", listed[0]["name"] == "Kitchen dash")
    fresh.post("/api/favourites",
               json={"name": "Kitchen dash", "url": "http://d.local:3000/new",
                     "kind": "web"})
    edited = next(f for f in fresh.get("/api/favourites").json()["favourites"]
                  if f["name"] == "Kitchen dash")
    check("editing keeps the history", edited["uses"] == 1, str(edited))
    check("editing changes the address", edited["url"].endswith("/new"))
    check("deleting works", fresh.delete("/api/favourites/Stage feed").status_code == 200)
    check("deleting something absent is a 404",
          fresh.delete("/api/favourites/ghost").status_code == 404)
    check("playing something absent is a 404",
          fresh.post("/api/favourites/ghost/play").status_code == 404)

    print("\nstream addresses")
    for url in ("file:///etc/passwd", "--vo=drm", "javascript:x", "http://"):
        r = fresh.post("/api/play/stream", json={"url": url})
        check(f"{url[:22]!r} is refused", r.status_code == 400, f"{r.status_code}")
    # A good address gets past validation; it can still fail later for want of
    # a display, which is not this test's business.
    r = fresh.post("/api/play/stream", json={"url": "udp://238.0.0.1:1234"})
    check("a real stream address gets past validation", r.status_code != 400,
          f"{r.status_code} {r.text[:80]}")

    print("\nsetup")
    st = fresh.get("/api/setup").json()
    check("setup reports how to reach the node", "connect" in st and "urls" in st["connect"])
    check("...and what it knows about the network", "network" in st)
    r = fresh.post("/api/setup", json={"enable_auth": True})
    check("switching the login on with no password is refused",
          r.status_code == 400, r.text[:110])

    print("\nregressions found in review")
    # The username box in the wizard is pre-filled, so keying the credential
    # off it made the ordinary path — name the node, skip the lock — fail with
    # "a password is required".
    plain = TestClient(app)
    config.update(setup_complete=False, auth_enabled=False)
    r = plain.post("/api/setup", json={"hostname": "stage-left",
                                       "username": "admin", "password": "",
                                       "enable_auth": False})
    check("setup finishes with a name and no password", r.status_code == 200,
          r.text[:120])
    check("switching the login on still needs a password",
          plain.post("/api/setup",
                     json={"enable_auth": True}).status_code == 400)

    # These are reachable directly as well as through the play endpoints, and
    # an unchecked one only fails at the next boot.
    for key, value in (("web_url", "file:///etc/passwd"),
                       ("web_url", "javascript:alert(1)"),
                       ("stream_url", "--vo=drm"),
                       ("stream_url", "file:///etc/shadow")):
        check(f"config refuses {key}={value[:22]}",
              plain.post("/api/config", json={key: value}).status_code == 400)
    check("config accepts a real web address",
          plain.post("/api/config",
                     json={"web_url": "http://dash.local"}).status_code == 200)
    check("config accepts a real stream address",
          plain.post("/api/config",
                     json={"stream_url": "udp://238.0.0.1:1234"}).status_code == 200)
    check("a nonsense session length is refused",
          plain.post("/api/config",
                     json={"auth_session_hours": 0}).status_code == 400)
    check("a nonsense stream buffer is refused",
          plain.post("/api/config",
                     json={"stream_cache_s": 99}).status_code == 400)

    print("\nthe preview is behind the login")
    # It shows whatever is on the screen, so it is operator information — it
    # must not be reachable by the room the way guest sharing is.
    auth.set_password("jayden", "a decent passphrase")
    config.update(auth_enabled=True, setup_complete=True)
    locked = TestClient(app)
    check("a stranger cannot see the preview",
          locked.get("/api/preview").status_code == 401)
    check("...nor its state", locked.get("/api/preview/state").status_code == 401)
    check("...nor stop someone else's",
          locked.post("/api/preview/stop").status_code == 401)
    check("guest sharing is still open beside it",
          locked.get("/s/tok/status").status_code != 401)
    auth.disable()
    config.update(auth_enabled=False)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
