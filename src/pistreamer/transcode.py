"""Re-encoding a file into something the Pi can actually play.

A video that does not decode in hardware is not a small problem on this board.
Software H.264 at 1080p is roughly the whole CPU budget of a Pi 4, and VP9 or
AV1 at the same size is not close to realtime at all — the symptom is a file
that plays for two seconds, stutters, and drifts away from the audio. The fix is
always the same: get it into 8-bit 4:2:0 H.264 at or below 1080p, which is what
the SoC's decode block was built for.

So this module does one thing: run ffmpeg with those settings and report where
it has got to. It is a separate module for the same reason `pushjob` is — the
work takes minutes, so the request that starts it cannot be the request that
reports on it, and "no idea whether it is working" is not an acceptable state
for something an operator is waiting on before a show.

Encoding is slow on a Pi. Where the hardware H.264 *encoder* is available it is
used, because it is roughly realtime against x264's small fraction of it; the
quality is worse at the same bitrate, which is the right trade for a file that
otherwise does not play at all. The original is never touched until the new file
is complete and verified.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# ffmpeg reports progress as "time=00:01:23.45"; against the known duration that
# is the only percentage available without parsing its stats machinery.
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")

# Hardware encoders in the order we would rather have them. h264_v4l2m2m is the
# Pi's own block. It is not always present — it depends on the kernel and on
# how ffmpeg was built — so its absence is normal rather than an error.
HW_ENCODERS = ("h264_v4l2m2m",)


def available() -> tuple[bool, str]:
    """Can this node transcode at all?"""
    if not shutil.which("ffmpeg"):
        return False, ("ffmpeg is not installed on this node — "
                       "'sudo apt install ffmpeg' adds it")
    return True, ""


def encoders() -> List[str]:
    """Which H.264 encoders this ffmpeg actually has, best first."""
    if not shutil.which("ffmpeg"):
        return []
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                              capture_output=True, text=True, timeout=20)
    except (subprocess.SubprocessError, OSError):
        return []
    text = proc.stdout + proc.stderr
    out = [name for name in HW_ENCODERS if re.search(rf"\b{name}\b", text)]
    if re.search(r"\blibx264\b", text):
        out.append("libx264")
    return out


def pick_encoder() -> Optional[str]:
    found = encoders()
    return found[0] if found else None


def build_command(src: Path, dest: Path, encoder: str, *,
                  width: int = 1920, height: int = 1080,
                  fps_cap: int = 60, source_fps: float = 0.0) -> List[str]:
    """The ffmpeg argv for one conversion.

    Both limits are caps rather than targets. A 720p source stays 720p instead
    of being upscaled into a bigger file that looks no better, and a 24fps
    source stays 24fps — forcing it to 60 would triple the frame count and the
    file size to show the same pictures. `force_original_aspect_ratio=decrease`
    keeps a 2.35:1 master from being squashed, and the even-number rounding
    after it is required: H.264 4:2:0 cannot represent an odd dimension and
    ffmpeg fails outright on one.
    """
    scale = (f"scale='min({width},iw)':'min({height},ih)'"
             f":force_original_aspect_ratio=decrease:force_divisible_by=2")
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"{scale},format=yuv420p",
        "-c:v", encoder,
    ]
    # Only rate-limit when the source is actually above the cap. An unknown
    # source rate (0) is left alone rather than guessed at.
    if source_fps and source_fps > fps_cap + 1:
        cmd += ["-r", str(fps_cap)]
    if encoder == "libx264":
        # veryfast, not slower: on a Pi the difference between presets is hours,
        # and the file only has to decode well, not be small.
        cmd += ["-preset", "veryfast", "-crf", "21", "-profile:v", "high",
                "-level", "4.1"]
    else:
        # The V4L2 encoder has no CRF mode — it is bitrate-driven only.
        cmd += ["-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M"]
    cmd += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        # Puts the index at the front, so playback can start without reading to
        # the end of the file first.
        "-movflags", "+faststart",
        # Named explicitly because the job encodes to a ".part" temp file, and
        # ffmpeg picks its muxer from the extension — left to guess it fails
        # with "Error initializing the muxer" before writing a frame.
        "-f", "mp4",
        str(dest),
    ]
    return cmd


def target_name(name: str) -> str:
    """What the converted file is called.

    Always .mp4, and always marked, so the library does not end up with two
    files whose only visible difference is which one plays properly.
    """
    return f"{Path(name).stem}-pi.mp4"


class TranscodeJob:
    """One conversion, running in the background with progress worth polling."""

    def __init__(self, name: str, deps: Dict[str, Callable]) -> None:
        self._lock = threading.Lock()
        self._deps = deps
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self.state: Dict[str, Any] = {
            "name": name,
            "output": "",
            "running": False,
            "done": False,
            "cancelled": False,
            "encoder": "",
            "hardware": False,
            "started": None,
            "finished": None,
            "duration": None,
            "position": 0.0,
            "error": "",
            "tail": [],
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(self.state)
            out["tail"] = list(self.state["tail"])
        total = out["duration"]
        if out["done"] and not out["error"] and not out["cancelled"]:
            # ffmpeg's last progress line lands a little short of the true
            # duration, so a finished encode reports 97% unless it is said
            # outright. A bar that stops short reads as a job that stopped.
            out["percent"] = 100.0
        elif total:
            out["percent"] = round(min(100.0, 100.0 * out["position"] / total), 1)
        else:
            out["percent"] = None
        elapsed = ((out["finished"] or time.time()) - out["started"]
                   if out["started"] else 0)
        out["elapsed_s"] = round(elapsed, 1)
        # Encoding rate against realtime is the number that tells an operator
        # whether to wait or go and do something else.
        out["speed"] = (round(out["position"] / elapsed, 2)
                        if elapsed > 1 and out["position"] else None)
        if out["speed"] and total and out["position"] < total:
            out["eta_s"] = int((total - out["position"]) / out["speed"])
        else:
            out["eta_s"] = None
        out["ok"] = out["done"] and not out["error"] and not out["cancelled"]
        return out

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, src: Path, dest: Path, encoder: str,
              duration: Optional[float], source_fps: float = 0.0) -> None:
        with self._lock:
            self.state.update({
                "running": True, "started": time.time(), "duration": duration,
                "encoder": encoder, "hardware": encoder in HW_ENCODERS,
                "output": dest.name,
            })
        self._thread = threading.Thread(
            target=self._run, args=(src, dest, encoder, source_fps),
            name="transcode", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _run(self, src: Path, dest: Path, encoder: str,
             source_fps: float = 0.0) -> None:
        # Encode to a temp name and rename only on success. A cancelled or
        # failed encode that left a short, playable file behind would be worse
        # than one that left nothing: it would go on a screen.
        tmp = dest.with_name(f".{dest.name}.part")
        try:
            cmd = build_command(src, tmp, encoder, source_fps=source_fps)
            log.info("transcoding %s -> %s with %s", src.name, dest.name, encoder)
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, bufsize=1)
            for line in self._proc.stderr or ():
                self._note(line.rstrip())
            code = self._proc.wait()
            if self._cancel.is_set():
                with self._lock:
                    self.state["cancelled"] = True
                return
            if code != 0:
                with self._lock:
                    tail = " / ".join(self.state["tail"][-3:])
                    self.state["error"] = (
                        f"ffmpeg failed (exit {code})" + (f": {tail}" if tail else ""))
                return
            if not tmp.is_file() or tmp.stat().st_size == 0:
                with self._lock:
                    self.state["error"] = "ffmpeg produced no output"
                return
            tmp.replace(dest)
        except (OSError, subprocess.SubprocessError) as exc:
            with self._lock:
                self.state["error"] = str(exc)
        finally:
            tmp.unlink(missing_ok=True)
            with self._lock:
                self.state["running"] = False
                self.state["done"] = True
                self.state["finished"] = time.time()

    def _note(self, line: str) -> None:
        if not line:
            return
        match = _TIME_RE.search(line)
        with self._lock:
            if match:
                hours, minutes, seconds = match.groups()
                self.state["position"] = (
                    int(hours) * 3600 + int(minutes) * 60 + float(seconds))
            else:
                # Progress lines are noise; everything else is the diagnosis
                # when it goes wrong, so only that is kept.
                self.state["tail"].append(line)
                del self.state["tail"][:-40]
