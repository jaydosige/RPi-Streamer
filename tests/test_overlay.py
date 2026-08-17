"""Tests for the identify overlay in the GStreamer runner.

This is a rendering feature, so the only test worth having is one that looks at
pixels. The runner is driven for real — its own video chain, its own file
polling, its own timeouts — over a black idle source in GRAY8, with a pad probe
at the sink counting bytes above a brightness threshold. Black source plus
white text means that count IS the overlay: zero when nothing is drawn, non-zero
when something is, and a different number when the caption changes.

No display, no NDI sender and no Pi needed. Skips rather than fails when
GStreamer or textoverlay is not installed, since that is the same missing
package the runner is written to survive.

    python3 tests/test_overlay.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    import gi  # type: ignore

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # type: ignore
except (ImportError, ValueError) as exc:  # noqa: BLE001
    print(f"SKIP: no usable GStreamer Python bindings ({exc}). "
          f"Install python3-gi and gir1.2-gstreamer-1.0 to run this test.")
    sys.exit(0)

Gst.init(None)

if Gst.ElementFactory.find("textoverlay") is None:
    print("SKIP: textoverlay is not installed, so there is nothing to test "
          "here. Install the gstreamer1.0-x package.")
    sys.exit(0)

from pistreamer import runner as R  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-overlay-"))

# Well above the black background (which converts to 16, not 0, in GRAY8) and
# well below the white text, so the count is unambiguous.
BRIGHT = 200

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


class Counter(R.Runner):
    """The real runner, plus a count of bright pixels per phase of the run."""

    def __init__(self, spec):
        super().__init__(spec)
        self.phase = "start"
        self.counts = {}

    def build(self):
        pipeline = super().build()
        # The sink pad is where the frame is exactly what a display would get.
        self.video_sink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._count
        )
        return pipeline

    def _count(self, _pad, info):
        buf = info.get_buffer()
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                bright = sum(1 for b in mapped.data if b >= BRIGHT)
            finally:
                buf.unmap(mapped)
            got = self.counts.setdefault(self.phase, [])
            got.append(bright)
        return Gst.PadProbeReturn.OK

    def at(self, seconds, fn):
        """Run fn once, this many seconds into the run."""
        GLib.timeout_add(int(seconds * 1000), lambda: (fn(), False)[1])

    def window(self, phase, start, end):
        """Count frames as belonging to phase only between start and end.

        Closed at both ends on purpose. An open-ended phase keeps collecting
        after the next write to the overlay file, so the frames that prove the
        overlay came on get filed under the phase that expects it off.
        """
        self.at(start, lambda: setattr(self, "phase", phase))
        self.at(end, lambda: setattr(self, "phase", "between"))

    def worst(self, phase):
        """The brightest frame seen in a phase — 0 if nothing was rendered."""
        return max(self.counts.get(phase, [0]))


def spec_for(overlay_file, run_for):
    # source_type "idle" is the standby pipeline: solid black from
    # videotestsrc, which makes any bright pixel the overlay's doing. GRAY8 at
    # the sink keeps one byte per pixel, so counting needs no image decoder.
    spec = {
        "source_type": "idle",
        "sink": "fake",
        "audio": False,
        "video_format": "GRAY8",
        "width": 640,
        "height": 360,
        "run_for": run_for,
        "stats_interval": 60.0,  # keep the test's output to the test's output
    }
    if overlay_file is not None:
        spec["overlay_file"] = str(overlay_file)
    return spec


# --- the overlay, driven entirely by the file ------------------------------
# One run, one pipeline, never restarted: the file changes underneath it and the
# rendering has to follow. Phases are sampled a beat after each write so the
# 500 ms poll and the 5 fps idle framerate have both had a turn.
print("identify overlay")

overlay_file = TMP / "identify.txt"  # deliberately not created yet
run = Counter(spec_for(overlay_file, 12.0))
run.window("missing", 0.5, 1.8)
run.at(2.0, lambda: overlay_file.write_text("STAGE-LEFT-01\n10.42.7.13\n"))
run.window("text", 3.0, 4.3)
run.at(4.5, lambda: overlay_file.write_text("FOH-RACK-04\n10.42.7.99\n"))
run.window("changed", 5.5, 6.8)
run.at(7.0, lambda: overlay_file.write_text("   \n  \n"))
run.window("cleared", 8.0, 8.9)
# textoverlay renders its text property as Pango markup, so an unescaped "&"
# or "<" in a node name renders an empty box instead of the caption. Names like
# this are ordinary on an event network, hence the check.
run.at(9.2, lambda: overlay_file.write_text("BAR & GRILL <2>\n10.42.7.7\n"))
run.window("markup", 10.2, 11.8)
code = run.run()

check("the pipeline ran and stopped cleanly", code == 0, f"exit {code}")
check("frames reached the sink", run._frames > 0, f"{run._frames} frames")
check("nothing rendered while the file does not exist",
      run.worst("missing") == 0, f"{run.worst('missing')} bright pixels")
check("text in the file renders",
      run.worst("text") > 100, f"{run.worst('text')} bright pixels")
check("a live edit changes the picture without a restart",
      run.worst("changed") > 100 and run.worst("changed") != run.worst("text"),
      f"{run.worst('text')} then {run.worst('changed')} bright pixels")
check("emptying the file hides the overlay again",
      run.worst("cleared") == 0, f"{run.worst('cleared')} bright pixels")
# Not just "something is drawn": text Pango rejects leaves the previous caption
# on screen, which would pass a mere non-zero check while displaying the wrong
# node's name. The count has to differ from the earlier caption's.
check("a name containing & and < renders that name, not the previous one",
      run.worst("markup") > 100 and run.worst("markup") != run.worst("changed"),
      f"{run.worst('markup')} bright pixels, "
      f"previous caption was {run.worst('changed')}")

# A path that exists but cannot be read as text is the same case as a missing
# one: the overlay stays off and playback carries on. A directory is used for
# this rather than a chmod 000 file, because permissions are not enforced for
# root and these tests are often run as root.
unreadable = TMP / "not-a-file"
unreadable.mkdir()
run2 = Counter(spec_for(unreadable, 2.5))
run2.window("denied", 0.5, 2.3)
code2 = run2.run()
check("an unreadable overlay path does not stop playback",
      code2 == 0 and run2._frames > 0, f"exit {code2}, {run2._frames} frames")
check("an unreadable overlay path renders nothing",
      run2.worst("denied") == 0, f"{run2.worst('denied')} bright pixels")

# --- no overlay_file at all -----------------------------------------------
# The overwhelmingly common case, and the one that must not gain any cost or
# any new way to fail: no textoverlay in the pipeline, plain black picture.
print("\nno overlay requested")
plain = Counter(spec_for(None, 2.5))
plain.window("plain", 0.5, 2.3)
code3 = plain.run()
check("the pipeline still builds and plays", code3 == 0 and plain._frames > 0,
      f"exit {code3}, {plain._frames} frames")
check("no overlay element was added", plain.pipeline.get_by_name("identify") is None)
check("nothing is rendered over the picture", plain.worst("plain") == 0,
      f"{plain.worst('plain')} bright pixels")

# --- where the overlay sits in the chain -----------------------------------
# The NDI source itself cannot be built without the NDI plugin, so what is
# checked here is the thing the NDI path shares with every other path: one
# video chain, with the overlay between the queue and videoconvert. If that
# holds, NDI, local test patterns and standby all get the overlay, and all of
# them get it before the frame is converted and scaled for kmssink.
print("\nposition in the chain")
built = R.Runner({
    "source_type": "test",
    "sink": "fake",
    "audio": False,
    "rotation": 90,
    "overlay_file": str(overlay_file),
}).build()
ov = built.get_by_name("identify")
check("the overlay is in the pipeline", ov is not None)


def chain_from(element):
    """Element names from here to the sink, following the linked src pads."""
    order = [element.get_name()]
    while True:
        pad = element.get_static_pad("src")
        peer = pad.get_peer() if pad is not None else None
        if peer is None:
            return order
        element = peer.get_parent_element()
        order.append(element.get_name())


order = chain_from(built.get_by_name("vqueue"))
print(f"  chain: {' -> '.join(order)}")
if ov is not None:
    check("fed by the leaky queue, not by the source directly",
          order.index("identify") == 1, str(order))
    check("ahead of videoconvert, so the convert for kmssink happens after it",
          order.index("identify") < order.index("vconvert"), str(order))
    check("ahead of videoflip, so the text rotates with the picture",
          order.index("identify") < order.index("vflip"), str(order))
    check("ahead of videoscale and the output caps",
          order.index("identify") < order.index("vscale") < order.index("vcaps"),
          str(order))
    check("the caption starts hidden", ov.get_property("silent") is True)
    check("shaded background is on", ov.get_property("shaded-background") is True)
built.set_state(Gst.State.NULL)

# --- the missing-package case ---------------------------------------------
# An older node that has not been reinstalled has no textoverlay. Simulated by
# making the factory unavailable, because that is the only honest way to test it
# on a host where the package is installed.
print("\ntextoverlay not installed")
real_make_optional = R.make_optional
R.make_optional = lambda factory, name=None: (
    None if factory == "textoverlay" else real_make_optional(factory, name)
)
try:
    degraded = Counter(spec_for(TMP / "identify.txt", 2.5))
    degraded.window("degraded", 0.5, 2.3)
    code4 = degraded.run()
finally:
    R.make_optional = real_make_optional
check("video plays without the overlay plugin",
      code4 == 0 and degraded._frames > 0, f"exit {code4}, {degraded._frames} frames")
check("the overlay is simply absent",
      degraded.pipeline.get_by_name("identify") is None)

print()
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for name in FAIL:
        print(f"  FAILED: {name}")
    sys.exit(1)
