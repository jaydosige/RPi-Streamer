"""AirPlay receiving, driven against a real uxplay process.

There is no iPhone in CI, so this cannot prove that mirroring works. What it
can prove — and what every bug found while building this feature actually was —
is that the *integration* holds: that uxplay starts under the constraints our
service runs with, that its output reaches the GUI while it is still useful,
that it dies when told to, and that the messages the operator depends on are
parsed out of the wording the installed binary really uses.

    python3 tests/test_airplay.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="pistreamer-airplay-"))
os.environ["PISTREAMER_CONFIG"] = str(TMP / "config.json")
os.environ["PISTREAMER_MEDIA"] = str(TMP / "media")
os.environ["PISTREAMER_STATE"] = str(TMP)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pistreamer import airplay, config  # noqa: E402
from pistreamer.player import MODE_AIRPLAY, MODE_IDLE, Player  # noqa: E402

PASS, FAIL = [], []
HAVE_UXPLAY = shutil.which("uxplay") is not None
HAVE_DNSSD = airplay.dns_sd_available()[0]


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if cond else "\033[31m✗\033[0m"
    print(f"  {mark} {name}{'  — ' + detail if detail and not cond else ''}")


def binary_strings() -> str:
    """The literal messages the installed uxplay can print.

    The parser is tested against these rather than against strings copied out
    of the documentation, so if a future uxplay rewords something the test
    fails here instead of silently in front of a room.
    """
    if not HAVE_UXPLAY:
        return ""
    try:
        out = subprocess.run(["strings", shutil.which("uxplay")],
                             capture_output=True, text=True, timeout=30)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def wait_for(predicate, timeout=12.0, step=0.25):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return False


def uxplay_pids():
    """Real uxplay processes, by executable name.

    Deliberately not `pgrep -f uxplay`: the receiver is launched through
    `stdbuf`, so a full-command-line match also catches the wrapper in the
    instant before it execs — and, for that matter, this test file. Matching
    the executable name is the question actually being asked.
    """
    out = subprocess.run(["pgrep", "-x", "ux" "play"], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def main() -> int:  # noqa: C901 - a test script, read top to bottom
    print(f"\nworkspace: {TMP}")
    print(f"uxplay: {shutil.which('uxplay') or 'not installed'} "
          f"{airplay.version()}   DNS-SD: {HAVE_DNSSD}\n")

    print("availability is honest")
    ok, reason = airplay.available()
    check("available() agrees with the binary being present",
          ok == (HAVE_UXPLAY and HAVE_DNSSD), f"{ok} vs {HAVE_UXPLAY}/{HAVE_DNSSD}")
    if not ok:
        check("...and says what to do about it",
              "apt install" in reason or "systemctl" in reason, reason)
    caps = airplay.capabilities()
    check("capabilities() reports a version when installed",
          bool(caps["version"]) == HAVE_UXPLAY, str(caps))

    print("\nthe command line")
    cfg = config.update(device_name="STAGE-LEFT", audio_enabled=True,
                        audio_device="hw:CARD=vc4hdmi0,DEV=0", rotation=0,
                        airplay_name="", airplay_pin=False, airplay_port=0,
                        airplay_hw_decode=True, airplay_fps=30,
                        airplay_timeout_s=15, airplay_hold_last_frame=True)
    cmd = airplay.build_command(cfg, video_sink="kmssink force-modesetting=true",
                                width=1920, height=1080, refresh=60)
    joined = " ".join(cmd)
    check("stdbuf wraps it", cmd[:4] == ["stdbuf", "-oL", "-eL"] + [cmd[3]], joined[:80])
    check("...because uxplay block-buffers a pipe", "-oL" in cmd and "-eL" in cmd)
    check("the receiver is named after the node", "-n" in cmd
          and cmd[cmd.index("-n") + 1] == "STAGE-LEFT", joined)
    check("no @hostname suffix", "-nh" in cmd)
    # The trap this exists to avoid: ProtectHome=yes means $HOME is not there,
    # and uxplay's default keypair location is $HOME/.uxplay.pem.
    check("the keypair path is explicit", "-key" in cmd)
    check("...and lives in the state directory",
          str(config.STATE_DIR) in cmd[cmd.index("-key") + 1],
          cmd[cmd.index("-key") + 1])
    check("the pin register path is explicit too", "-reg" in cmd)
    check("nothing is written to $HOME", "~" not in joined and ".uxplay" not in joined)
    check("the display size is pinned to a real mode",
          "-s" in cmd and cmd[cmd.index("-s") + 1] == "1920x1080@60", joined)
    check("audio goes to the configured ALSA device",
          "alsasink device=hw:CARD=vc4hdmi0,DEV=0" in cmd, joined)
    if airplay.element_available("v4l2h264dec"):
        check("hardware decode is asked for with uxplay's own option",
              "-v4l2" in cmd, joined)
        # Verified against a real Pi 4B: without this the decoder rejects
        # Apple's stream and the pipeline dies the moment somebody connects.
        check("...with the colour workaround the Pi needs", "-bt709" in cmd, joined)
    else:
        check("software decoding when the GPU decoder is absent",
              "-avdec" in cmd and "-v4l2" not in cmd, joined)
    check("the frame rate is capped", "-fps" in cmd)
    check("the last frame is held by default", "-nc" in cmd)
    check("no PIN unless asked", "-pin" not in cmd)

    cfg = config.update(audio_enabled=False, rotation=90, airplay_pin=True,
                        airplay_hw_decode=False, airplay_port=7100)
    cmd = airplay.build_command(cfg, video_sink="kmssink")
    check("audio can be switched off", cmd[cmd.index("-as") + 1] == "0", " ".join(cmd))
    check("90 degrees maps to uxplay's own vocabulary",
          "-r" in cmd and cmd[cmd.index("-r") + 1] == "R", " ".join(cmd))
    check("software decode is explicit, not a default",
          "-avdec" in cmd and "-v4l2" not in cmd, " ".join(cmd))
    check("the PIN is requested with a bare -pin", "-pin" in cmd)
    check("...and with no fixed code, which uxplay 1.68 rejects",
          not any(re.fullmatch(r"-pin\d+", c) for c in cmd), " ".join(cmd))
    check("ports can be pinned", cmd[cmd.index("-p") + 1] == "7100")
    cmd180 = airplay.build_command(config.update(rotation=180), video_sink="k")
    check("180 degrees is a flip, not a rotate",
          "-f" in cmd180 and cmd180[cmd180.index("-f") + 1] == "I", " ".join(cmd180))
    config.update(rotation=0, airplay_pin=False, airplay_port=0,
                  airplay_hw_decode=True, audio_enabled=True)

    print("\nelements are checked before they are asked for")
    # uxplay aborts on an unknown element in about 40ms, so asking for the Pi's
    # GPU decoder on a box that does not have it is a crash loop, not a
    # fallback. The check has to happen before the command line is built.
    cfg = config.update(airplay_hw_decode=True)
    cmd = airplay.build_command(cfg, video_sink="fakesink")
    have_v4l2 = airplay.element_available("v4l2h264dec")
    check("hardware decode is only requested when the element exists",
          ("v4l2h264dec" in cmd) == have_v4l2, f"have={have_v4l2} cmd={' '.join(cmd)}")
    check("a missing element is remembered, not re-probed",
          airplay.element_available("v4l2h264dec") == have_v4l2)
    check("a nonsense element is never available",
          not airplay.element_available("definitelynotanelement"))

    print("\n-reset counts in threes, not seconds")
    check("15 seconds is 5 units", airplay.reset_units(15) == 5)
    check("3 seconds is 1 unit", airplay.reset_units(3) == 1)
    check("1 second still asks for something", airplay.reset_units(1) == 1)
    check("0 means never", airplay.reset_units(0) == 0)

    print("\nthe advertised ports")
    ports = airplay.ports(config.update(airplay_port=7100))
    check("the AirPlay service is advertised on n+2, as measured",
          ports["airplay"] == 7102, str(ports))
    check("mDNS is listed too, because somebody always asks", ports["mdns"] == 5353)
    check("dynamic ports say so", airplay.ports(config.update(airplay_port=0))["fixed"]
          is False)

    print("\nreading uxplay's output")
    airplay.reset()
    airplay.observe("Initialized server socket(s)")
    check("it knows when the receiver is listening", airplay.session().listening)
    airplay.observe('*** CLIENT MUST NOW ENTER PIN = "4821" AS AIRPLAY PASSWORD')
    check("the pairing PIN is captured", airplay.session().pin == "4821",
          airplay.session().pin)
    airplay.observe("connection request from Priya's iPhone (iPhone14,5) "
                    "with deviceID = aa:bb:cc:dd:ee:ff")
    s = airplay.session()
    check("the device name is read", s.client == "Priya's iPhone", s.client)
    check("the model is read", s.model == "iPhone14,5", s.model)
    check("the device id is read", s.device_id == "aa:bb:cc:dd:ee:ff", s.device_id)
    check("it is not mirroring yet", not s.mirroring)
    airplay.observe("Mirroring initialized successfully")
    check("mirroring is noticed", airplay.session().mirroring)
    check("...and timed", (airplay.session().since or 0) > 0)
    airplay.observe("Connection closed for socket 12")
    check("a disconnect clears the session", not airplay.session().mirroring)
    check("...and forgets the device", airplay.session().client == "")

    airplay.reset()
    airplay.observe("*** ERROR: No DNS-SD Server found "
                    "(DNSServiceRegister call returned kDNSServiceErr_Unknown)")
    check("a missing Avahi is explained, not just logged",
          "avahi" in airplay.session().last_error.lower(),
          airplay.session().last_error)
    airplay.reset()
    airplay.observe("*** ERROR: DNSServiceRegister call returned "
                    "kDNSServiceErr_NameConflict")
    check("a name clash on the network is explained",
          "name" in airplay.session().last_error.lower(),
          airplay.session().last_error)
    airplay.reset()
    airplay.observe("Client Authentication Failure (client proof not validated)")
    check("a wrong PIN is explained", "pin" in airplay.session().last_error.lower(),
          airplay.session().last_error)
    print("\nthe decoder failing on a live stream")
    # Reported from a real Pi 4B: the receiver is healthy, a phone connects,
    # and the pipeline collapses at the first frame because the Pi's V4L2
    # decoder will not take Apple's full-range colour. uxplay prints its own
    # advice about it, which is what makes it recognisable.
    airplay.reset()
    check("hardware decoding is on to begin with", not airplay.software_forced())
    airplay.observe("Begin streaming to GStreamer video pipeline")
    airplay.observe("GStreamer error: video_source Internal data stream error.")
    check("the failure is recognised", airplay.software_forced())
    check("it asks to be restarted", airplay.restart_wanted())
    check("...only once", not airplay.restart_wanted())
    check("the operator is told in words, not in an exit code",
          "software decoding" in airplay.session().last_error,
          airplay.session().last_error)
    cmd_sw = airplay.build_command(config.load(), video_sink="fakesink")
    check("the next receiver decodes in software",
          "-avdec" in cmd_sw and "-v4l2" not in cmd_sw, " ".join(cmd_sw))
    airplay.observe("*** was unable to construct a working video pipeline.")
    check("a second failure is not another restart loop",
          not airplay.restart_wanted())
    check("...and says so plainly",
          "even in software" in airplay.session().last_error,
          airplay.session().last_error)
    airplay.reset(keep_degrade=True)
    check("a restart keeps the decision", airplay.software_forced())
    airplay.reset()
    check("changing mode forgets it", not airplay.software_forced())

    airplay.reset()
    check("a line it does not know is harmless",
          airplay.observe("some future message") is None)
    check("...and an empty line too", airplay.observe("") is None)

    blob = binary_strings()
    if blob:
        print("\nthe parser matches the binary that is installed")
        for label, needle in (
            ("connection request", "connection request from %s"),
            ("mirroring started", "Mirroring initialized successfully"),
            ("the PIN prompt", "AS AIRPLAY PASSWORD"),
            ("a closed connection", "Connection closed for socket"),
        ):
            check(f"uxplay still says: {label}", needle in blob, needle)
    else:
        print("\n  · binary string check skipped (no uxplay/strings)")

    if not (HAVE_UXPLAY and HAVE_DNSSD):
        print("\n  · live process tests skipped "
              f"(uxplay={HAVE_UXPLAY}, dns-sd={HAVE_DNSSD})")
    else:
        print("\na real uxplay, started by the player")
        for pid in uxplay_pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        time.sleep(0.5)
        # fakesink, not the DRM sink: this host has no display, and the point
        # of these tests is the integration around uxplay, not the pixels.
        config.update(device_name="TESTNODE-AP", audio_enabled=False,
                      airplay_hw_decode=False, connector="", video_mode="",
                      airplay_video_sink="fakesink")
        player = Player()
        player.start()
        try:
            player.apply(MODE_AIRPLAY)
            check("the player reports it running",
                  wait_for(lambda: player.status()["running"]),
                  player.status()["last_error"])
            check("a real uxplay process exists", wait_for(lambda: bool(uxplay_pids())))

            # The buffering fix, measured: without stdbuf, uxplay's banner sits
            # in a 4 KB buffer and the GUI log stays empty for minutes.
            got = wait_for(lambda: any("UxPlay" in l for l in player.logs()), timeout=4)
            check("its output reaches the log within four seconds", got,
                  "\n".join(player.logs()[-5:]))
            check("the session says it is listening",
                  wait_for(lambda: airplay.session().listening, timeout=8),
                  str(airplay.summary()))
            check("no error was recorded",
                  not airplay.session().last_error, airplay.session().last_error)

            # It runs with no HOME at all, which is the ProtectHome case.
            check("the keypair was written where we told it to",
                  wait_for(lambda: airplay.key_path().exists(), timeout=8),
                  str(airplay.key_path()))

            if shutil.which("avahi-browse"):
                out = subprocess.run(
                    ["timeout", "6", "avahi-browse", "-rtp", "_airplay._tcp"],
                    capture_output=True, text=True)
                check("the node is advertised on the network by name",
                      "TESTNODE-AP" in out.stdout, out.stdout[:300])

            print("\nit gives the display back")
            player.apply(MODE_IDLE)
            check("the receiver is gone",
                  wait_for(lambda: not uxplay_pids(), timeout=10),
                  str(uxplay_pids()))
            check("the session was forgotten", not airplay.session().listening)

            print("\nswitching in and out leaves nothing behind")
            player.apply(MODE_AIRPLAY)
            wait_for(lambda: bool(uxplay_pids()))
            before = player.status()["strays_cleaned"]
            player.apply(MODE_AIRPLAY)   # restart while running
            # Settled, not instantaneous: the old receiver is signalled and then
            # waited for, so a moment with two pids is teardown in progress. A
            # moment that lasts is the bug.
            check("restarting settles on exactly one receiver",
                  wait_for(lambda: len(uxplay_pids()) == 1, timeout=8),
                  str(uxplay_pids()))
            time.sleep(1.0)
            # The invariant that actually matters. A receiver that escapes
            # teardown is the AirPlay shape of "two soundtracks at once": it
            # keeps the display and the audio device, and the next one fights
            # it. Counting processes at one instant can miss it; this cannot.
            check("nothing escaped teardown",
                  player.status()["strays_cleaned"] == before,
                  f"{before} -> {player.status()['strays_cleaned']}")
            player.apply(MODE_IDLE)
            check("and stopping leaves none",
                  wait_for(lambda: not uxplay_pids(), timeout=10), str(uxplay_pids()))

            print("\na stray receiver is swept up")
            stray = subprocess.Popen(
                ["uxplay", "-n", "STRAY", "-nh", "-vs", "0", "-as", "0",
                 "-key", str(TMP / "stray.pem")],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            time.sleep(1.5)
            check("the stray is running", stray.poll() is None)
            player.apply(MODE_AIRPLAY)
            time.sleep(2.0)
            check("starting AirPlay killed the unsupervised one",
                  stray.poll() is not None or stray.pid not in uxplay_pids(),
                  f"stray={stray.pid} alive={uxplay_pids()}")
            player.apply(MODE_IDLE)
            wait_for(lambda: not uxplay_pids(), timeout=10)
        finally:
            player.shutdown()
            for pid in uxplay_pids():
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass

        if not Path("/dev/dri").exists():
            print("\na receiver that starts but never listens")
            # With no DRM device at all, uxplay wedges while building its video
            # pipeline: alive, banner printed, never advertising. Every liveness
            # check says fine and the operator gets a green light and a black
            # screen — so the supervisor has to notice on its own.
            config.update(airplay_video_sink="kmssink")
            player = Player()
            player.start()
            try:
                player.apply(MODE_AIRPLAY)
                check("it is alive but silent", wait_for(
                    lambda: player.status()["running"], timeout=5))
                check("uxplay's own reason is picked up straight away",
                      wait_for(lambda: "video output could not be opened"
                               in airplay.session().last_error, timeout=20),
                      airplay.session().last_error)
                check("the supervisor calls it out rather than showing green",
                      wait_for(lambda: "never began advertising"
                               in player.status()["last_error"], timeout=25),
                      player.status()["last_error"])
            finally:
                player.apply(MODE_IDLE)
                player.shutdown()
                for pid in uxplay_pids():
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
            config.update(airplay_video_sink="fakesink")

        print("\na receiver that will not stay up")
        # The real case: hardware decoding on for a box with no GPU decoder.
        # uxplay aborts in 40ms with `no element "v4l2h264dec"`, and left alone
        # the supervisor restarts it forever behind an error that says only
        # `player exited with code -5`.
        config.update(airplay_video_sink="fakesink")
        player = Player()
        player.start()
        try:
            player._airplay_fast_fails = 0
            player.apply(MODE_AIRPLAY)
            time.sleep(0.5)
            # Force the failure the way a missing decoder would.
            player._terminate()
            player._build_command_orig = player._build_command
            player._build_command = lambda cfg, mode, target: (
                airplay.build_command(cfg, video_sink="nosuchsink")
                if mode == MODE_AIRPLAY else player._build_command_orig(cfg, mode, target))
            player.apply(MODE_AIRPLAY)
            check("it gives up instead of restarting forever",
                  wait_for(lambda: "would not stay up"
                           in player.status()["last_error"], timeout=25),
                  player.status()["last_error"])
            check("...and says what uxplay actually complained about",
                  "nosuchsink" in player.status()["last_error"]
                  or "no element" in player.status()["last_error"],
                  player.status()["last_error"])
            settled = player.status()["restarts"]
            time.sleep(4)
            check("it really has stopped trying",
                  player.status()["restarts"] == settled,
                  f"{settled} -> {player.status()['restarts']}")
            check("nothing was left running", not uxplay_pids(), str(uxplay_pids()))
        finally:
            player._build_command = player._build_command_orig
            player.shutdown()

        print("\nwhen Avahi is not there")
        real = airplay.dns_sd_available
        airplay.dns_sd_available = lambda: (False, "Avahi is not running (test)")
        try:
            player = Player()
            player.start()
            player.apply(MODE_AIRPLAY)
            time.sleep(1.0)
            st = player.status()
            check("it refuses to start rather than crash-looping",
                  not st["running"] and "Avahi" in st["last_error"], str(st))
            check("...and it did not spawn anything", not uxplay_pids(),
                  str(uxplay_pids()))
            check("no restart storm", st["restarts"] <= 1, str(st["restarts"]))
            player.shutdown()
        finally:
            airplay.dns_sd_available = real

    print("\nthe API and the vocabulary")
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from pistreamer import schedule as schedule_mod  # noqa: PLC0415
    from pistreamer.web import CommandBody, app  # noqa: PLC0415

    config.update(mode="idle", airplay_video_sink="fakesink", audio_enabled=False)
    with TestClient(app) as client:
        r = client.get("/api/airplay")
        check("GET /api/airplay -> 200", r.status_code == 200, r.text)
        body = r.json()
        check("it reports whether the box can do it at all", "available" in body)
        check("it shows the name a phone will see", bool(body["name"]), str(body))
        check("it shows the command, so a support call can be answered",
              isinstance(body["command"], list) and "-key" in body["command"])
        r = client.get("/api/capabilities")
        check("capabilities mentions AirPlay", "airplay" in r.json(), r.text[:200])
        r = client.get("/api/status")
        check("status carries the session", "airplay" in r.json(), r.text[:200])

        if HAVE_UXPLAY and HAVE_DNSSD:
            r = client.post("/api/play/airplay")
            check("POST /api/play/airplay -> 200", r.status_code == 200, r.text)
            # Arming AirPlay deliberately does NOT take the screen: whatever is
            # playing stays up until a device actually connects.
            check("...and the node is armed rather than switched over",
                  config.load().airplay_enabled is True, config.load().mode)
            check("...leaving the current playback alone",
                  config.load().mode != "airplay", config.load().mode)
            r = client.get("/api/airplay")
            check("...and the API reports it ready", r.json()["ready"] is True, r.text)
            client.post("/api/stop")
            check("stopping leaves idle", config.load().mode == "idle")
            check("stopping disarms AirPlay too",
                  config.load().airplay_enabled is False)
        else:
            r = client.post("/api/play/airplay")
            check("it refuses clearly when it cannot", r.status_code == 409, r.text)

    check("a schedule cue can switch to AirPlay",
          "airplay" in schedule_mod.ACTIONS, str(schedule_mod.ACTIONS))
    check("a group command can too",
          "airplay" in CommandBody.model_fields["action"].annotation.__args__,
          str(CommandBody.model_fields["action"].annotation))
    check("a group stop is a real action now, not an error",
          "stop" in CommandBody.model_fields["action"].annotation.__args__)

    for pid in uxplay_pids():
        try:
            os.kill(pid, 9)
        except OSError:
            pass

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nfailures:")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
