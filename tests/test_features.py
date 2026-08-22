"""Tests for playlists, the schedule cue list and the standby fallback.

These are the parts with real logic rather than plumbing: playlist validation,
cue matching across days and times, and next-fire calculation. All pure, so no
hardware and no GStreamer needed.

    python3 tests/test_features.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-feat-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import config, playlists, schedule  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}{'  — ' + detail if detail and not cond else ''}")


def main() -> int:
    config.ensure_dirs()
    (config.MEDIA_DIR / "one.mp4").write_bytes(b"\0" * 2048)
    (config.MEDIA_DIR / "two.mp4").write_bytes(b"\0" * 2048)
    (config.MEDIA_DIR / "slide.png").write_bytes(b"\0" * 2048)

    print("\nplaylists")
    pl = playlists.Playlist(name="Doors Open", items=["one.mp4", "slide.png"],
                            loop=True, shuffle=False, image_duration=15)
    playlists.save(pl)
    check("saved and retrievable", playlists.get("Doors Open") is not None)
    check("items preserved in order",
          [i["target"] for i in playlists.get("Doors Open").items]
          == ["one.mp4", "slide.png"],
          str(playlists.get("Doors Open").items))
    check("image duration preserved", playlists.get("Doors Open").image_duration == 15)

    try:
        playlists.save(playlists.Playlist(name="Bad", items=["missing.mp4"]))
        check("missing file rejected", False, "it was accepted")
    except ValueError as exc:
        check("missing file rejected", "missing.mp4" in str(exc))

    try:
        playlists.save(playlists.Playlist(name="in/valid", items=[]))
        check("bad name rejected", False, "it was accepted")
    except ValueError:
        check("bad name rejected", True)

    check("duration clamped to something sane",
          playlists.save(playlists.Playlist(name="Clamp", items=["two.mp4"],
                                            image_duration=99999)).image_duration == 3600)

    resolved = playlists.resolved_files("Doors Open")
    check("resolves to absolute paths", len(resolved) == 2 and resolved[0].startswith("/"),
          str(resolved))
    (config.MEDIA_DIR / "one.mp4").unlink()
    check("a file deleted after saving is skipped at play time",
          len(playlists.resolved_files("Doors Open")) == 1)
    check("delete works", playlists.delete("Doors Open"))
    check("delete twice is False", not playlists.delete("Doors Open"))

    print("\nplaylist segments and NDI items")
    mixed = playlists.Playlist(name="Mixed", items=[
        {"type": "file", "target": "two.mp4", "duration": None},
        {"type": "ndi", "target": "STUDIO-PC (OBS)", "duration": 30},
        {"type": "file", "target": "slide.png", "duration": 5},
    ], loop=True)
    playlists.save(mixed)
    got = playlists.get("Mixed")
    check("mixed playlist saved", got is not None and len(got.items) == 3)
    check("needs the sequencer (mpv cannot play NDI)", got.needs_sequencer())
    check("all-file playlist with no durations uses the smooth path",
          not playlists.Playlist(name="x", items=[
              {"type": "file", "target": "two.mp4", "duration": None}]).needs_sequencer())

    try:
        playlists.save(playlists.Playlist(name="NoDur", items=[
            {"type": "ndi", "target": "CAM 1", "duration": None}]))
        check("NDI item without a duration rejected", False, "it was accepted")
    except ValueError as exc:
        check("NDI item without a duration rejected", "duration" in str(exc))

    try:
        playlists.save(playlists.Playlist(name="NoName", items=[
            {"type": "ndi", "target": "", "duration": 10}]))
        check("NDI item without a name rejected", False, "it was accepted")
    except ValueError:
        check("NDI item without a name rejected", True)

    try:
        playlists.save(playlists.Playlist(name="Empty", items=[]))
        check("empty playlist rejected", False, "it was accepted")
    except ValueError:
        check("empty playlist rejected", True)

    segs = playlists.resolved_segments("Mixed")
    check("segments resolved in order", [s["type"] for s in segs] == ["file", "ndi", "file"],
          str([s["type"] for s in segs]))
    check("file segment carries an absolute path", segs[0]["path"].startswith("/"))
    check("still image gets a duration", segs[2]["duration"] == 5)
    check("image flagged as an image", segs[2]["image"] is True)
    check("video with no duration plays to its end", segs[0]["duration"] is None)

    # Legacy playlists stored plain strings.
    import json as _json
    raw = _json.loads(playlists.store_path().read_text())
    raw["Legacy"] = {"items": ["two.mp4"], "loop": True, "shuffle": False,
                     "image_duration": 10}
    playlists.store_path().write_text(_json.dumps(raw))
    legacy = playlists.get("Legacy")
    check("legacy string items migrate to file segments",
          legacy is not None and legacy.items == [
              {"type": "file", "target": "two.mp4", "duration": None}],
          str(legacy.items if legacy else None))

    print("\nschedule cue matching")
    # Monday 2026-08-17 was a Monday; weekday() == 0
    monday_0900 = datetime(2026, 8, 17, 9, 0)
    tuesday_0900 = datetime(2026, 8, 18, 9, 0)
    monday_0901 = datetime(2026, 8, 17, 9, 1)

    weekdays = schedule.Cue(id="a", time="09:00", action="standby", days=[0, 1, 2, 3, 4])
    check("fires on a matching day and minute", weekdays.matches(monday_0900))
    check("does not fire a minute later", not weekdays.matches(monday_0901))
    weekend = schedule.Cue(id="b", time="09:00", action="standby", days=[5, 6])
    check("does not fire on a non-matching day", not weekend.matches(monday_0900))
    disabled = schedule.Cue(id="c", time="09:00", action="standby",
                            days=list(range(7)), enabled=False)
    check("disabled cue never fires", not disabled.matches(monday_0900))

    for bad, why in (
        (schedule.Cue(id="x", time="9:00", action="standby"), "single-digit hour"),
        (schedule.Cue(id="x", time="25:00", action="standby"), "hour out of range"),
        (schedule.Cue(id="x", time="09:00", action="teleport"), "unknown action"),
        (schedule.Cue(id="x", time="09:00", action="ndi", target=""), "action needs a target"),
        (schedule.Cue(id="x", time="09:00", action="standby", days=[]), "no days"),
        (schedule.Cue(id="x", time="09:00", action="standby", days=[9]), "day out of range"),
    ):
        try:
            schedule.validate(bad)
            check(f"rejects {why}", False, "it was accepted")
        except ValueError:
            check(f"rejects {why}", True)

    print("\nscheduler firing")
    schedule.save(schedule.Cue(id="doors", time="09:00", action="standby",
                               days=list(range(7)), label="Doors"))
    fired = []
    sched = schedule.Scheduler()
    sched.bind(lambda action, target: fired.append((action, target)))
    sched.tick(monday_0900)
    check("cue fires on its minute", fired == [("standby", "")], str(fired))
    sched.tick(monday_0900)
    check("does not double-fire in the same minute", len(fired) == 1, str(fired))
    sched.tick(tuesday_0900)
    check("fires again the next day", len(fired) == 2, str(fired))

    nxt = schedule.next_fire(now=datetime(2026, 8, 17, 8, 30))
    check("next fire is 30 minutes away", nxt and nxt["in_minutes"] == 30, str(nxt))
    nxt = schedule.next_fire(now=datetime(2026, 8, 17, 9, 30))
    check("after today's cue, next is tomorrow", nxt and nxt["in_minutes"] == 1410, str(nxt))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
