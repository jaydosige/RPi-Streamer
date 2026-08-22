"""Taking a node's settings off it and putting them on another one.

The scenario is a card dying on a show day: flash a replacement, restore, push
the media, carry on. So the test that matters is a restore onto a node that has
nothing — no media, no playlists — because that is the only state a fresh card
is ever in.

    python3 tests/test_backup.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-backup-"))
os.environ.update(PISTREAMER_CONFIG=str(TMP / "c.json"),
                  PISTREAMER_MEDIA=str(TMP / "media"), PISTREAMER_STATE=str(TMP))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import (backup, config, favourites, playlists,  # noqa: E402
                        schedule, shaders)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"\n         {detail}" if not cond and detail else ""))


def populate():
    media_dir = Path(os.environ["PISTREAMER_MEDIA"])
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "a.mp4").write_bytes(b"x" * 10)
    config.update(device_name="stage-left", cluster_key="show-key-2026",
                  ndi_bandwidth="lowest", stream_cache_s=5, identify=True)
    playlists.save(playlists.Playlist(name="Doors", items=["a.mp4"], loop=True))
    favourites.save(favourites.Favourite(
        name="Kitchen dash", url="http://d.local/w", kind="web"))
    schedule.save(schedule.Cue(id="c1", time="09:00", action="standby",
                               days=[0, 1, 2, 3, 4]))
    shaders.save("plasma", shaders.DEFAULT_SOURCE)


def main() -> int:
    populate()

    print("what a backup contains")
    data = backup.build()
    check("settings", len(data["config"]) > 40, str(len(data["config"])))
    check("playlists", len(data["playlists"]) == 1)
    check("the schedule", len(data["schedule"]) == 1)
    check("favourites", len(data["favourites"]) == 1)
    check("shaders, with their source",
          data["shaders"].get("plasma") == shaders.DEFAULT_SOURCE)
    # Needed to rejoin the group. A node that comes back but cannot be
    # commanded has not come back.
    check("the group key, deliberately",
          data["config"]["cluster_key"] == "show-key-2026")
    # A library is gigabytes and already moves with /api/cluster/push. A backup
    # too big to keep is one nobody keeps.
    check("NOT the media", "media" not in data)
    blob = backup.to_bytes(data)
    check("it is small", len(blob) < 100_000, f"{len(blob)} bytes")
    check("the console password is not in it",
          b"password" not in blob.lower().replace(b"guest_password", b""))

    print("\nrefusing what it should")
    check("a file that is not a backup", backup.check({"nope": 1}) != "")
    check("something that is not an object", backup.check([1, 2, 3]) != "")
    check("a newer format",
          "newer version" in backup.check({"config": {}, "format": 99}))
    check("a good one passes", backup.check(data) == "")

    print("\nrestoring onto a node with nothing on it")
    fresh = Path(tempfile.mkdtemp(prefix="pistreamer-fresh-"))
    # The paths are module constants read from the environment at import, so
    # setting the variables again here would change nothing — every store
    # would keep writing to the first node's files and this would test nothing.
    config.CONFIG_PATH = fresh / "c.json"
    config.MEDIA_DIR = fresh / "media"
    config.STATE_DIR = fresh
    config._cached = None
    config.update(device_name="spare-3")
    check("the fresh node really is fresh",
          not playlists.all_playlists() and not shaders.all_shaders())

    report = backup.restore(json.loads(blob), keep_identity=True)
    check("nothing was skipped", not report["skipped"], str(report["skipped"]))
    # The playlist names a file this node has not got yet — media arrives
    # separately. Rejecting it here would throw the settings away at exactly
    # the moment they were being recovered.
    check("the playlist restored despite the media being absent",
          report["playlists"] == 1 and
          "Doors" in [p.name for p in playlists.all_playlists()])
    check("the cue restored", report["schedule"] == 1)
    check("the favourite restored", report["favourites"] == 1)
    check("the shader restored, byte for byte",
          shaders.get("plasma") == shaders.DEFAULT_SOURCE)

    cfg = config.load()
    check("settings restored", cfg.ndi_bandwidth == "lowest" and cfg.stream_cache_s == 5)
    check("the group key restored", cfg.cluster_key == "show-key-2026")
    # A spare that comes up believing it is the node it replaced is worse than
    # one that needs renaming.
    check("the node kept its OWN name", cfg.device_name == "spare-3",
          cfg.device_name)
    check("...and was not left flagged as the old one", cfg.identify is False)

    print("\ntaking the name too, when asked")
    backup.restore(json.loads(blob), keep_identity=False)
    check("the name comes across", config.load().device_name == "stage-left")

    print("\none bad entry does not lose the rest")
    broken = json.loads(blob)
    broken["playlists"].append({"name": "!!bad!!", "items": []})
    broken["favourites"].append({"name": "x", "url": "file:///etc/passwd",
                                 "kind": "web"})
    report = backup.restore(broken)
    check("the good playlist still arrived", report["playlists"] == 1)
    check("the good favourite still arrived", report["favourites"] == 1)
    check("and the bad ones are reported", len(report["skipped"]) == 2,
          str(report["skipped"]))

    print("\nsettings this version does not have are named, not silently dropped")
    future = json.loads(blob)
    future["config"]["some_setting_from_2027"] = True
    report = backup.restore(future)
    check("it says which", any("2027" in s for s in report["skipped"]),
          str(report["skipped"]))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(fresh, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
