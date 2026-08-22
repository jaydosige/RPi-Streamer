"""The support bundle: everything needed to diagnose a node, in one file.

Its whole purpose is to be sent to somebody, so the thing that matters most is
that it can be sent safely. A bundle carrying the group key or a password hash
is one nobody can attach to an email, which makes it useless.

    python3 tests/test_support.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-support-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP / "media"), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, support  # noqa: E402

PASS, FAIL = [], []
SECRET = "hunter2-group-key-do-not-leak"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def main() -> int:
    print("redaction")
    redacted = support.redact_config({
        "cluster_key": SECRET, "device_name": "stage-left",
        "wifi_password": "swordfish", "auth_hash": "abc123",
        "some_token": "t0ken", "web_port": 80,
    })
    for key in ("cluster_key", "wifi_password", "auth_hash", "some_token"):
        check(f"{key} is redacted", support.REDACTED in str(redacted[key]),
              str(redacted[key]))
    check("harmless values survive", redacted["device_name"] == "stage-left")
    check("numbers survive", redacted["web_port"] == 80)
    # "empty" and "wrong" are different faults; the length distinguishes them
    # without giving the value away.
    check("the length is kept, so an empty key is distinguishable",
          "29 chars" in redacted["cluster_key"], redacted["cluster_key"])

    print("\na section that fails is recorded, not raised")
    def explode():
        raise RuntimeError("the disk is on fire")
    bundle = support.collect({"broken": explode, "fine": lambda: {"ok": True}})
    check("the bundle is still produced", isinstance(bundle, dict))
    check("the failure is in it", "the disk is on fire" in str(bundle["broken"]),
          str(bundle["broken"]))
    check("...and the other sections survive", bundle["fine"] == {"ok": True})

    print("\nwhat is in it")
    for section in ("bundle", "host", "journal", "helper_journals", "tools",
                    "versions_cli", "disk"):
        check(f"{section} is present", section in bundle)
    check("it records its own format version", bundle["bundle"]["format"] == 1)
    check("a missing tool is reported, not fatal",
          isinstance(bundle["tools"], dict))

    print("\nserialising")
    raw = support.to_bytes(bundle)
    check("it is valid JSON", isinstance(json.loads(raw), dict))
    # default=str, so one stray unserialisable object cannot lose the whole
    # bundle at the last step after every section caught its own failures.
    odd = support.collect({"path": lambda: Path("/tmp/x")})
    check("an awkward value does not lose the bundle",
          isinstance(json.loads(support.to_bytes(odd)), dict))

    print("\nthe filename says which node and when")
    name = support.filename("stage-left")
    check("it names the node", name.startswith("stage-left-support-"), name)
    check("it ends in .json", name.endswith(".json"), name)
    check("a hostile device name cannot escape",
          "/" not in support.filename("../../etc/passwd"),
          support.filename("../../etc/passwd"))

    try:
        from fastapi.testclient import TestClient
    except ImportError:
        print("\n(skipping the endpoint — fastapi is not installed)")
        return report()

    print("\nthrough the API")
    config.update(cluster_key=SECRET, device_name="stage-left")
    from pistreamer.web import app
    client = TestClient(app)
    r = client.get("/api/support")
    check("it downloads", r.status_code == 200)
    check("...as an attachment",
          "attachment" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", ""))
    body = r.content
    # The point of the whole exercise.
    check("THE GROUP KEY IS NOT IN THE FILE", SECRET.encode() not in body)
    parsed = json.loads(body)
    check("the config is there but redacted",
          support.REDACTED in parsed["config"]["cluster_key"])
    check("the player log is included", "player_log" in parsed)
    check("the journal is included", "journal" in parsed)
    check("capabilities are included", "capabilities" in parsed)

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
