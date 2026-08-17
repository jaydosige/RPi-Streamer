"""Instrumented GStreamer pipeline runner.

Runs as a subprocess so the player can still kill it to recover the display,
but builds the pipeline programmatically instead of shelling out to
gst-launch-1.0. Two reasons:

  1. Telemetry. Pad probes give real frames/sec, real bitrate and the actually
     negotiated caps, and GstBaseSink's stats give rendered/dropped counts.
     None of that is visible from outside a gst-launch process.

  2. Safety. gst-launch joins its argv back into one string and re-parses it,
     so an NDI name containing spaces and parentheses — i.e. every real NDI
     name — has to be quoted inside the value. Setting properties directly
     removes that whole class of bug. Property names and enum nicks are also
     validated against the element before use, so a wrong property fails
     loudly here rather than silently producing a pipeline that never starts.

Protocol: one JSON object per line on stdout, prefixed with "@STATS ".
Everything else (GStreamer warnings, errors) goes to stderr.

Usage:
    python -m pistreamer.runner '<json spec>'
    python -m pistreamer.runner --self-test
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, Optional

import gi  # type: ignore

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # type: ignore  # noqa: E402

# ndisrc's timestamp-mode enum, by value. Set numerically rather than by nick
# so a typo cannot silently fall back to the default.
TIMESTAMP_MODES = {
    "receive-time-vs-timecode": 0,
    "receive-time-vs-timestamp": 1,
    "timecode": 2,
    "timestamp": 3,
    "receive-time": 4,
}

# videoflip method enum values.
FLIP_METHODS = {90: 1, 180: 2, 270: 3}

# queue leaky enum: 0 none, 1 upstream, 2 downstream.
LEAKY = {"none": 0, "upstream": 1, "downstream": 2}
LEAKY_DOWNSTREAM = 2

# ndisrc color-format enum nicks.
COLOR_FORMATS = {
    "bgrx-bgra": 0,
    "uyvy-bgra": 1,
    "rgbx-rgba": 2,
    "uyvy-rgba": 3,
    "fastest": 4,
    "best": 5,
}

STATS_PREFIX = "@STATS "


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class PipelineError(RuntimeError):
    pass


def make(factory: str, name: Optional[str] = None):
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise PipelineError(
            f"element '{factory}' is not available — is the plugin installed "
            f"and on GST_PLUGIN_PATH?"
        )
    return element


def set_prop(element, prop: str, value: Any) -> None:
    """Set a property, failing loudly if the element has no such property.

    This is the guard that would have caught passing bandwidth="highest" to a
    property that is actually an integer.
    """
    if element.find_property(prop) is None:
        raise PipelineError(
            f"{element.get_factory().get_name()} has no property '{prop}'"
        )
    element.set_property(prop, value)


class Runner:
    def __init__(self, spec: Dict[str, Any]) -> None:
        self.spec = spec
        self.pipeline: Optional[Gst.Pipeline] = None
        self.loop = GLib.MainLoop()
        self.video_sink = None

        # Counters updated from the pad probe.
        self._frames = 0
        self._bytes = 0
        self._last_sample = time.monotonic()
        self._last_frames = 0
        self._last_bytes = 0
        self._last_buffer_at: Optional[float] = None
        self._first_frame_at: Optional[float] = None
        self._qos_events = 0
        self._started = time.monotonic()
        self._exit_code = 0
        self._caps: Dict[str, Any] = {}
        self.video_queue = None
        self._arrivals = 0
        self._last_arrivals = 0
        self._arrival_bytes = 0
        self._last_arrival_bytes = 0
        self._queue_overruns = 0

    # -- construction ------------------------------------------------------

    def _build_video_chain(self, pipeline) -> tuple:
        """Returns (first_element, last_element) of the video branch."""
        spec = self.spec
        latency_ns = max(0, int(spec.get("latency_ms", 200))) * 1_000_000

        queue = make("queue", "vqueue")
        set_prop(queue, "max-size-time", latency_ns)
        set_prop(queue, "max-size-bytes", 0)
        set_prop(queue, "max-size-buffers", int(spec.get("queue_max_buffers", 0)))
        set_prop(queue, "leaky", LEAKY.get(spec.get("queue_leaky", "downstream"), LEAKY_DOWNSTREAM))
        # A queue that overruns is the pipeline telling us, in so many words,
        # that the far end cannot keep up with the near end.
        queue.connect("overrun", self._on_queue_overrun)
        self.video_queue = queue

        convert = make("videoconvert", "vconvert")
        threads = int(spec.get("convert_threads", 0) or 0)
        if threads and convert.find_property("n-threads") is not None:
            set_prop(convert, "n-threads", threads)
        elements = [queue, convert]

        rotation = int(spec.get("rotation", 0) or 0)
        if rotation in FLIP_METHODS:
            flip = make("videoflip", "vflip")
            set_prop(flip, "method", FLIP_METHODS[rotation])
            elements.append(flip)

        scale = make("videoscale", "vscale")
        set_prop(scale, "add-borders", True)
        # videoscale is passthrough when input and output sizes already match,
        # so the method only costs anything when scaling is actually needed.
        set_prop(scale, "method", int(spec.get("scale_method", 1)))
        elements.append(scale)

        # kmssink only sets a mode whose size matches the frame exactly, so
        # pin the output to a mode the connector actually advertises.
        capsfilter = make("capsfilter", "vcaps")
        caps_str = f"video/x-raw,format={spec.get('video_format', 'BGRx')}"
        width, height = spec.get("width"), spec.get("height")
        if width and height:
            caps_str += f",width={int(width)},height={int(height)}"
        set_prop(capsfilter, "caps", Gst.Caps.from_string(caps_str))
        elements.append(capsfilter)

        if spec.get("sink") == "fake":
            sink = make("fakesink", "vsink")
        else:
            sink = make("kmssink", "vsink")
            set_prop(sink, "force-modesetting", True)
            if spec.get("connector_id") is not None:
                set_prop(sink, "connector-id", int(spec["connector_id"]))
            if spec.get("driver_name"):
                set_prop(sink, "driver-name", spec["driver_name"])

        set_prop(sink, "sync", bool(spec.get("sink_sync", True)))
        if sink.find_property("qos") is not None:
            set_prop(sink, "qos", bool(spec.get("sink_qos", True)))
        lateness_ms = int(spec.get("sink_max_lateness_ms", -1))
        if sink.find_property("max-lateness") is not None:
            set_prop(sink, "max-lateness", -1 if lateness_ms < 0 else lateness_ms * 1_000_000)
        self.video_sink = sink

        # Optional snapshot branch: keep the most recent frame on disk so the
        # standby screen can hold the last picture when a feed stops, instead
        # of cutting to black. Throttled hard and leaky, so it can never
        # apply back-pressure to the display path.
        snapshot_path = spec.get("snapshot_path")
        tee = None
        if snapshot_path:
            tee = make("tee", "vtee")
            set_prop(tee, "allow-not-linked", True)
            elements.append(tee)

        for element in elements:
            pipeline.add(element)
        for a, b in zip(elements, elements[1:]):
            if not a.link(b):
                raise PipelineError(f"could not link {a.get_name()} -> {b.get_name()}")

        pipeline.add(sink)
        if tee is not None:
            sink_queue = make("queue", "sinkq")
            set_prop(sink_queue, "leaky", 0)
            set_prop(sink_queue, "max-size-buffers", 3)
            pipeline.add(sink_queue)
            if not tee.link(sink_queue) or not sink_queue.link(sink):
                raise PipelineError("could not link tee -> display sink")
            self._build_snapshot_branch(pipeline, tee, snapshot_path,
                                        int(spec.get("snapshot_interval_s", 3)))
        else:
            if not elements[-1].link(sink):
                raise PipelineError("could not link video chain -> sink")

        # Two measurement points, and this is the whole diagnostic trick:
        #   * the queue's sink pad counts frames ARRIVING from the network
        #   * the video sink's pad counts frames REACHING the display
        # If arrival is short of what the sender declares, the problem is
        # upstream — network or sender. If arrival is fine but render is
        # short, the Pi cannot keep up. One number cannot tell those apart;
        # two can.
        queue.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER, self._on_arrival
        )
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_buffer)
        return elements[0], sink

    def _build_snapshot_branch(self, pipeline, tee, path: str, interval: int) -> None:
        """tee -> leaky queue -> rate limit -> JPEG -> one file, overwritten."""
        interval = max(1, interval)
        queue = make("queue", "snapq")
        set_prop(queue, "leaky", LEAKY_DOWNSTREAM)
        set_prop(queue, "max-size-buffers", 1)
        set_prop(queue, "max-size-time", 0)
        set_prop(queue, "max-size-bytes", 0)

        # Rate-limit with a dropping pad probe, NOT a framerate capsfilter.
        # A capsfilter here negotiates upstream through the tee and throttles
        # the display path to the snapshot rate — which is exactly what
        # happened the first time this was written.
        self._snap_interval = interval
        self._last_snap = 0.0

        def gate(_pad, info):
            now = time.monotonic()
            if now - self._last_snap < self._snap_interval:
                return Gst.PadProbeReturn.DROP
            self._last_snap = now
            return Gst.PadProbeReturn.OK

        queue.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, gate)

        convert = make("videoconvert", "snapconvert")
        enc = make("jpegenc", "snapenc")
        set_prop(enc, "quality", 80)

        sink = make("multifilesink", "snapsink")
        # No %d in the location, so every buffer overwrites the same file.
        set_prop(sink, "location", path)
        set_prop(sink, "sync", False)
        set_prop(sink, "async", False)
        if sink.find_property("post-messages") is not None:
            set_prop(sink, "post-messages", False)

        chain = [queue, convert, enc, sink]
        for element in chain:
            pipeline.add(element)
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                raise PipelineError(f"snapshot branch: {a.get_name()} -> {b.get_name()}")
        if not tee.link(queue):
            raise PipelineError("could not link tee -> snapshot branch")

    def _build_audio_chain(self, pipeline):
        spec = self.spec
        latency_ns = max(0, int(spec.get("latency_ms", 200))) * 1_000_000

        queue = make("queue", "aqueue")
        set_prop(queue, "leaky", LEAKY_DOWNSTREAM)
        set_prop(queue, "max-size-time", latency_ns)
        convert = make("audioconvert", "aconvert")
        resample = make("audioresample", "aresample")

        if spec.get("sink") == "fake":
            sink = make("fakesink", "asink")
            set_prop(sink, "sync", False)
        else:
            sink = make("alsasink", "asink")
            set_prop(sink, "sync", False)
            # An audio sink volunteers as the pipeline clock by default, and
            # that clock stops advancing if no audio is actually consumed —
            # which freezes the video sink on its first frame. Never let it.
            set_prop(sink, "provide-clock", False)
            set_prop(sink, "async", False)
            if spec.get("audio_device"):
                set_prop(sink, "device", spec["audio_device"])

        elements = [queue, convert, resample, sink]
        for element in elements:
            pipeline.add(element)
        for a, b in zip(elements, elements[1:]):
            if not a.link(b):
                raise PipelineError(f"could not link {a.get_name()} -> {b.get_name()}")
        return elements[0]

    def build(self) -> Gst.Pipeline:
        spec = self.spec
        pipeline = Gst.Pipeline.new("pistreamer")
        self.pipeline = pipeline

        video_head, _ = self._build_video_chain(pipeline)
        audio_head = self._build_audio_chain(pipeline) if spec.get("audio") else None

        if spec.get("source_type") == "idle":
            # A standby screen exists so the node never shows a Linux console.
            # An image is frozen into a still video stream; with no image we
            # emit black, which is still an active KMS output and therefore
            # still keeps the console off the screen.
            # A standby screen is static, so cap it to a few frames a second.
            # Without this, imagefreeze pushes the same picture as fast as the
            # CPU allows and the leaky queue throws almost all of it away.
            idle_caps = make("capsfilter", "idlecaps")
            set_prop(idle_caps, "caps", Gst.Caps.from_string("video/x-raw,framerate=5/1"))
            pipeline.add(idle_caps)
            if not idle_caps.link(video_head):
                raise PipelineError("could not link idle caps -> video chain")

            image = spec.get("idle_image")
            if image:
                src = make("filesrc", "src")
                set_prop(src, "location", image)
                decode = make("decodebin", "decode")
                freeze = make("imagefreeze", "freeze")
                # Without is-live, imagefreeze pushes the same still as fast
                # as the CPU allows and relies on the sink to absorb it — the
                # leaky queue then throws away tens of thousands of frames a
                # second. is-live paces it to the negotiated framerate.
                if freeze.find_property("is-live") is not None:
                    set_prop(freeze, "is-live", True)
                pipeline.add(src)
                pipeline.add(decode)
                pipeline.add(freeze)
                if not src.link(decode):
                    raise PipelineError("could not link filesrc -> decodebin")
                if not freeze.link(idle_caps):
                    raise PipelineError("could not link imagefreeze -> idle caps")

                def on_decoded(_bin, pad):
                    sink_pad = freeze.get_static_pad("sink")
                    if not sink_pad.is_linked():
                        pad.link(sink_pad)

                decode.connect("pad-added", on_decoded)
            else:
                src = make("videotestsrc", "src")
                set_prop(src, "pattern", 2)  # solid colour
                set_prop(src, "foreground-color", 0xFF000000)  # opaque black
                set_prop(src, "is-live", True)
                pipeline.add(src)
                if not src.link(idle_caps):
                    raise PipelineError("could not link videotestsrc -> idle caps")
            return pipeline

        if spec.get("source_type") == "test":
            src = make("videotestsrc", "src")
            set_prop(src, "is-live", True)
            pipeline.add(src)
            if not src.link(video_head):
                raise PipelineError("could not link videotestsrc -> video chain")
            if audio_head is not None:
                asrc = make("audiotestsrc", "asrc")
                set_prop(asrc, "is-live", True)
                pipeline.add(asrc)
                if not asrc.link(audio_head):
                    raise PipelineError("could not link audiotestsrc -> audio chain")
            return pipeline

        src = make("ndisrc", "src")
        set_prop(src, "ndi-name", spec["source"])
        set_prop(src, "connect-timeout", int(spec.get("connect_timeout_ms", 10000)))
        set_prop(src, "timeout", int(spec.get("timeout_ms", 5000)))
        set_prop(src, "bandwidth", int(spec.get("bandwidth", 100)))
        mode = spec.get("timestamp_mode", "receive-time")
        if mode not in TIMESTAMP_MODES:
            raise PipelineError(f"unknown timestamp mode: {mode}")
        set_prop(src, "timestamp-mode", TIMESTAMP_MODES[mode])
        if spec.get("receiver_name"):
            set_prop(src, "receiver-ndi-name", spec["receiver_name"])
        color = spec.get("color_format", "uyvy-bgra")
        if color not in COLOR_FORMATS:
            raise PipelineError(f"unknown colour format: {color}")
        set_prop(src, "color-format", COLOR_FORMATS[color])
        if spec.get("max_queue"):
            set_prop(src, "max-queue-length", int(spec["max_queue"]))

        demux = make("ndisrcdemux", "demux")
        pipeline.add(src)
        pipeline.add(demux)
        if not src.link(demux):
            raise PipelineError("could not link ndisrc -> ndisrcdemux")

        # ndisrcdemux exposes video and audio as sometimes-pads: they appear
        # only once the stream is up, and the audio pad never appears at all
        # if the sender has no audio.
        def on_pad_added(_demux, pad):
            name = pad.get_name()
            target = video_head if name.startswith("video") else audio_head
            if target is None:
                log(f"no {name} branch configured; ignoring pad")
                return
            sink_pad = target.get_static_pad("sink")
            if sink_pad.is_linked():
                return
            result = pad.link(sink_pad)
            if result != Gst.PadLinkReturn.OK:
                log(f"failed to link demux pad {name}: {result.value_nick}")
            else:
                log(f"linked demux pad {name}")

        demux.connect("pad-added", on_pad_added)
        return pipeline

    # -- instrumentation ---------------------------------------------------

    def _on_queue_overrun(self, _queue):
        self._queue_overruns += 1

    def _on_arrival(self, _pad, info):
        buf = info.get_buffer()
        if buf is not None:
            self._arrivals += 1
            self._arrival_bytes += buf.get_size()
        return Gst.PadProbeReturn.OK

    def _on_buffer(self, _pad, info):
        buf = info.get_buffer()
        if buf is not None:
            self._frames += 1
            self._bytes += buf.get_size()
            now = time.monotonic()
            self._last_buffer_at = now
            if self._first_frame_at is None:
                self._first_frame_at = now
        return Gst.PadProbeReturn.OK

    def _read_caps(self) -> None:
        if self.video_sink is None:
            return
        pad = self.video_sink.get_static_pad("sink")
        caps = pad.get_current_caps()
        if caps is None or caps.get_size() == 0:
            return
        s = caps.get_structure(0)
        ok_w, width = s.get_int("width")
        ok_h, height = s.get_int("height")
        fps = None
        ok_fps, fps_n, fps_d = s.get_fraction("framerate")
        if ok_fps and fps_d:
            fps = round(fps_n / fps_d, 3)
        self._caps = {
            "format": s.get_string("format"),
            "width": width if ok_w else None,
            "height": height if ok_h else None,
            "declared_fps": fps,
        }

    def _sink_stats(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"rendered": None, "dropped": None}
        sink = self.video_sink
        if sink is None or sink.find_property("stats") is None:
            return out
        try:
            stats = sink.get_property("stats")
            if stats is not None:
                ok_r, rendered = stats.get_uint64("rendered")
                ok_d, dropped = stats.get_uint64("dropped")
                out["rendered"] = rendered if ok_r else None
                out["dropped"] = dropped if ok_d else None
        except (TypeError, AttributeError):
            pass
        return out

    def _emit_stats(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last_sample
        if elapsed <= 0:
            return True

        frames = self._frames - self._last_frames
        octets = self._bytes - self._last_bytes
        arrivals = self._arrivals - self._last_arrivals
        arrival_octets = self._arrival_bytes - self._last_arrival_bytes
        self._last_sample = now
        self._last_frames = self._frames
        self._last_bytes = self._bytes
        self._last_arrivals = self._arrivals
        self._last_arrival_bytes = self._arrival_bytes

        self._read_caps()
        stats = self._sink_stats()

        payload = {
            "t": time.time(),
            "uptime": round(now - self._started, 1),
            "fps": round(frames / elapsed, 2),
            # Frames handed in by the NDI receiver, before any of our
            # processing. Compare against declared_fps to judge the network.
            "arrival_fps": round(arrivals / elapsed, 2),
            "arrival_mbps": round((arrival_octets * 8) / elapsed / 1_000_000, 2),
            "arrivals_total": self._arrivals,
            "queue_overruns": self._queue_overruns,
            # Bytes at the sink after conversion, so this is the rate into the
            # display rather than the NDI wire rate. Labelled accordingly.
            "render_mbps": round((octets * 8) / elapsed / 1_000_000, 2),
            "frames_total": self._frames,
            "since_last_frame": (
                round(now - self._last_buffer_at, 2) if self._last_buffer_at else None
            ),
            "time_to_first_frame": (
                round(self._first_frame_at - self._started, 2)
                if self._first_frame_at
                else None
            ),
            "qos_events": self._qos_events,
            "rendered": stats["rendered"],
            "dropped": stats["dropped"],
            **self._caps,
        }
        print(STATS_PREFIX + json.dumps(payload), flush=True)
        return True  # keep the timeout installed

    # -- bus ---------------------------------------------------------------

    def _on_message(self, _bus, message) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log(f"ERROR: {err.message}")
            if debug:
                log(f"debug: {debug}")
            self._exit_code = 1
            self.loop.quit()
        elif t == Gst.MessageType.WARNING:
            err, _ = message.parse_warning()
            log(f"WARNING: {err.message}")
        elif t == Gst.MessageType.EOS:
            log("end of stream")
            self.loop.quit()
        elif t == Gst.MessageType.QOS:
            self._qos_events += 1
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src is self.pipeline:
                old, new, _ = message.parse_state_changed()
                log(f"pipeline {old.value_nick} -> {new.value_nick}")

    # -- run ---------------------------------------------------------------

    def run(self) -> int:
        pipeline = self.build()
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

        interval = float(self.spec.get("stats_interval", 1.0))
        GLib.timeout_add(int(interval * 1000), self._emit_stats)

        for sig in ("SIGTERM", "SIGINT"):
            GLib.unix_signal_add(
                GLib.PRIORITY_HIGH, getattr(__import__("signal"), sig), self._quit
            )

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log("failed to set pipeline to PLAYING")
            pipeline.set_state(Gst.State.NULL)
            return 1

        run_for = self.spec.get("run_for")
        if run_for:
            GLib.timeout_add(int(float(run_for) * 1000), self._quit)

        try:
            self.loop.run()
        finally:
            # Release DRM promptly — the next pipeline cannot start until the
            # display is handed back.
            pipeline.set_state(Gst.State.NULL)
        return self._exit_code

    def _quit(self, *_args) -> bool:
        log("stopping")
        self.loop.quit()
        return False


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    Gst.init(None)

    if argv and argv[0] == "--self-test":
        # Exercises the whole video chain with no NDI sender and no display,
        # which is what makes this runner testable off the Pi.
        spec = {
            "source_type": "test",
            "sink": "fake",
            "audio": True,
            "width": 1280,
            "height": 720,
            "run_for": float(argv[1]) if len(argv) > 1 else 3.0,
            "stats_interval": 1.0,
        }
    elif argv:
        spec = json.loads(argv[0])
    else:
        spec = json.loads(sys.stdin.read())

    try:
        return Runner(spec).run()
    except PipelineError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
