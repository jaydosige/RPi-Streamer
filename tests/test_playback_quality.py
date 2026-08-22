"""Tests for judging how a file will play, and for re-encoding one that won't.

The assessment half is pure and always runs: it is a decision about codec,
resolution, colour depth and frame rate, and getting it wrong is what puts a
stuttering file on a screen having told the operator it was fine.

The encoding half needs ffmpeg. Where it is present the test does a real round
trip — build an awkward file, convert it, and check the verdict actually
changed — because every interesting failure here (a muxer that will not
initialise, a filter that squashes the aspect, an odd dimension H.264 cannot
represent) only appears when ffmpeg is really run.

    python3 tests/test_playback_quality.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-quality-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import media, transcode  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"\n         {detail}" if not cond and detail else ""))


def info(**kw):
    base = dict(codec="h264", profile="High", width=1920, height=1080,
                fps=30.0, pix_fmt="yuv420p", bit_rate=8_000_000, audio_codec="aac")
    base.update(kw)
    return media.VideoInfo(**base)


def main() -> int:
    print("what will play well")
    check("a 1080p30 H.264 file is good", media.assess(info())["verdict"] == "good")
    check("...and is not offered a pointless re-encode",
          media.assess(info())["transcode"] is False)
    check("720p is good too — smaller is not worse",
          media.assess(info(width=1280, height=720))["verdict"] == "good")
    check("1080p50 is still within the hardware's reach",
          media.assess(info(fps=50))["verdict"] == "good")

    print("\nwhat will not")
    tenbit = media.assess(info(pix_fmt="yuv420p10le", profile="High 10"))
    check("10-bit H.264 falls back to software", tenbit["verdict"] == "poor")
    check("...and says why in terms of the colour depth",
          any("8-bit" in r for r in tenbit["reasons"]), str(tenbit["reasons"]))
    check("...and offers to fix it", tenbit["transcode"] is True)

    uhd = media.assess(info(width=3840, height=2160))
    check("4K H.264 is past the hardware limit", uhd["verdict"] == "poor")
    check("...and names the limit it passed",
          any("1080p hardware limit" in r for r in uhd["reasons"]), str(uhd["reasons"]))

    check("VP9 has no hardware path at all",
          media.assess(info(codec="vp9"))["verdict"] == "poor")
    check("AV1 likewise", media.assess(info(codec="av1"))["verdict"] == "poor")
    check("120fps is past the frame rate limit",
          media.assess(info(fps=120))["verdict"] == "poor")

    print("\nHEVC depends on the node, not just the file")
    hevc = info(codec="hevc")
    check("HEVC is good where the decoder is registered",
          media.assess(hevc, hw_codecs={"h264", "hevc"})["verdict"] == "good")
    off = media.assess(hevc, hw_codecs={"h264"})
    check("...and poor where it is not", off["verdict"] == "poor")
    check("...pointing at the overlay rather than blaming the file",
          any("rpivid" in r for r in off["reasons"]), str(off["reasons"]))

    print("\nhonest about what it does not know")
    check("a file that could not be probed is 'unknown', not 'poor'",
          media.assess(None)["verdict"] == "unknown")
    check("...and is not offered a re-encode on a guess",
          media.assess(None)["transcode"] is False)
    # None means "could not ask", which must not condemn the whole library.
    check("an unknown decoder set falls back to the board's own abilities",
          media.assess(info(), hw_codecs=None)["verdict"] == "good")

    print("\n4K HEVC decodes but still costs work")
    fair = media.assess(info(codec="hevc", width=3840, height=2160),
                        hw_codecs={"hevc"})
    check("it is 'fair' — hardware, but scaled", fair["verdict"] == "fair")
    check("...and worth converting anyway", fair["transcode"] is True)
    check("...and still counts as hardware decode", fair["hardware"] is True)

    print("\nthe ffmpeg command")
    cmd = transcode.build_command(Path("/m/a.mkv"), Path("/m/.b.mp4.part"),
                                  "libx264", source_fps=24.0)
    check("the container is named explicitly, not guessed from '.part'",
          "-f" in cmd and cmd[cmd.index("-f") + 1] == "mp4", " ".join(cmd))
    check("a 24fps source is not forced up to the cap", "-r" not in cmd, " ".join(cmd))
    check("a 120fps source is capped",
          "-r" in transcode.build_command(Path("/a"), Path("/b"), "libx264",
                                          source_fps=120.0))
    check("scaling only ever goes down", "min(1920,iw)" in " ".join(cmd), " ".join(cmd))
    check("dimensions stay even, as H.264 4:2:0 requires",
          "force_divisible_by=2" in " ".join(cmd))
    check("audio comes along, and is optional so a silent file still converts",
          "0:a:0?" in cmd)
    check("the hardware encoder gets a bitrate, having no CRF mode",
          "-b:v" in transcode.build_command(Path("/a"), Path("/b"), "h264_v4l2m2m"))
    check("the converted file is marked so it cannot be confused with the source",
          transcode.target_name("clip.mkv") == "clip-pi.mp4")

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("\n(skipping the round trip — ffmpeg is not installed here)")
    else:
        print("\na real conversion")
        media_dir = Path(os.environ["PISTREAMER_MEDIA"])
        media_dir.mkdir(parents=True, exist_ok=True)
        src = media_dir / "awkward.mp4"
        # 10-bit and oversized: two independent reasons it will not play well,
        # and an odd width so the even-dimension rounding is actually exercised.
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=size=2561x1440:rate=24:duration=2",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p10le",
             "-profile:v", "high10", "-c:a", "aac", str(src)],
            check=True, capture_output=True, timeout=180)

        duration, before = media.probe_file(src)
        verdict = media.assess(before)
        check("the awkward file probes as 10-bit",
              before is not None and before.pix_fmt == "yuv420p10le",
              str(before.to_dict() if before else None))
        check("...and is judged poor", verdict["verdict"] == "poor", str(verdict))

        encoder = transcode.pick_encoder()
        check("an encoder is available", encoder is not None)
        dest = media_dir / transcode.target_name(src.name)
        job = transcode.TranscodeJob(src.name, {})
        job.start(src, dest, encoder, duration, source_fps=before.fps)
        deadline = time.time() + 180
        while job.is_running() and time.time() < deadline:
            time.sleep(0.2)
        snap = job.snapshot()
        check("the conversion succeeds", snap["ok"] is True,
              f"error={snap['error']} tail={snap['tail'][-2:]}")
        check("a finished job reports 100%, not the last progress line",
              snap["percent"] == 100.0, str(snap["percent"]))
        check("no partial file is left behind",
              not list(media_dir.glob(".*.part")),
              str([p.name for p in media_dir.glob('.*')]))

        if snap["ok"]:
            after_duration, after = media.probe_file(dest)
            check("the result is 8-bit H.264", after.codec == "h264"
                  and after.pix_fmt == "yuv420p", str(after.to_dict()))
            check("...within 1080p", after.width <= 1920 and after.height <= 1080,
                  f"{after.width}x{after.height}")
            check("...with even dimensions",
                  after.width % 2 == 0 and after.height % 2 == 0,
                  f"{after.width}x{after.height}")
            check("...keeping the original aspect ratio",
                  abs((after.width / after.height) - (before.width / before.height)) < 0.02,
                  f"{before.width}x{before.height} -> {after.width}x{after.height}")
            check("...keeping the audio", after.audio_codec == "aac", after.audio_codec)
            check("...keeping the frame rate rather than inflating it",
                  abs(after.fps - before.fps) < 0.5, f"{before.fps} -> {after.fps}")
            check("...and the running time", abs(after_duration - duration) < 0.5,
                  f"{duration} -> {after_duration}")
            check("and now it is judged good",
                  media.assess(after)["verdict"] == "good",
                  str(media.assess(after)))
            check("...so it is not offered a second conversion",
                  media.assess(after)["transcode"] is False)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    shutil.rmtree(TMP, ignore_errors=True)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
