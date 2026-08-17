# pi-streamer

Turns a Raspberry Pi 4B into a headless playback node: it receives an **NDI**
stream over the network or loops **local media**, drives HDMI directly with no
desktop, and is controlled entirely from a **web GUI** on the same network.

Built for Live Wire Event Solutions. Reference platform: Pi 4B (4GB or 8GB),
64-bit Raspberry Pi OS Lite, wired Ethernet.

---

## What it does

- Receives NDI from any sender on the LAN (OBS, vMix, a PTZ camera, NDI Tools)
- Discovers senders automatically and lists them in the GUI
- Plays or loops local video files, uploadable through the browser
- Drives HDMI through DRM/KMS — no X, no Wayland, no desktop
- Restores the last source on boot, and reconnects on its own when a sender
  drops and comes back
- Web GUI at `http://<hostname>.local/` for source selection, media upload,
  display and audio settings, health, logs, reboot

## Architecture

```
                       ┌──────────────────────────────┐
   browser ──HTTP:80──▶│  FastAPI  (pistreamer.web)   │
                       │    REST API + single-page GUI│
                       └───────────────┬──────────────┘
                                       │
                       ┌───────────────▼──────────────┐
                       │  Player  (pistreamer.player) │
                       │  single-owner state machine  │
                       │  + restart supervisor        │
                       └───────┬──────────────┬───────┘
                     NDI mode  │              │  local mode
                               ▼              ▼
                    gst-launch-1.0          mpv
                    ndisrc                  --vo=gpu
                      → ndisrcdemux         --gpu-context=drm
                      → videoconvert        --hwdec=auto-safe
                      → kmssink
                               │              │
                               └──────┬───────┘
                                      ▼
                            /dev/dri/cardN  →  HDMI
```

**The single-owner rule is the load-bearing design decision.** Only one process
can be DRM master at a time, so every mode change kills the current process and
waits for it to exit before starting the next. That is why playback is a
supervised subprocess rather than an in-process pipeline: a wedged decoder can
be killed and the display recovered without restarting the service.

### Why Raspberry Pi OS Lite and not Buildroot/Yocto

For a handful of manually-updated units, Pi OS Lite wins on every axis that
matters here: working V4L2 hardware decode and KMS out of the box, apt for the
GStreamer stack, and no cross-toolchain to maintain. Buildroot would buy a few
seconds of boot time and a read-only rootfs, at the cost of porting the NDI SDK
and the whole GStreamer stack yourself. Revisit only if this grows into a
managed fleet with OTA updates.

## Install

Start from **Raspberry Pi OS Lite (64-bit)**, flashed with Raspberry Pi Imager.
Set the hostname, enable SSH and configure the network in Imager's advanced
options, then:

```bash
sudo apt update && sudo apt install -y git
git clone <this-repo> pi-streamer
cd pi-streamer
sudo ./install.sh
sudo reboot
```

The first run takes 20–40 minutes, almost all of it compiling the Rust NDI
plugin. After the reboot the GUI is at `http://<hostname>.local/`.

### NDI SDK

The NDI SDK is licensed by Vizrt and cannot be redistributed, so `install.sh`
downloads the official installer and accepts its licence on your behalf. If the
download URL has moved, fetch the Linux SDK from
<https://ndi.video/for-developers/ndi-sdk/> and point the installer at it:

```bash
sudo NDI_SDK_TARBALL=/home/pi/Install_NDI_SDK_v6_Linux.tar.gz ./install.sh
```

## Performance — read this before specifying a job

### The one lever that matters most

The Pi 4 has **no hardware SpeedHQ decoder**, so full-bandwidth NDI is decoded
on four Cortex-A72 cores and nothing you configure will change that. NDI HX is
H.264/HEVC, which the Pi *can* decode in hardware — but only if the receiver
hands the compressed frames over instead of decoding them itself.

That requires the **NDI Advanced SDK**. With it, `ndisrc` emits
`video/x-h264` / `video/x-h265` untouched, and the pipeline routes that to
whatever hardware decoder the box has. Without it, every frame is decoded in
software no matter what the sender does.

```bash
# Get the Advanced SDK from https://ndi.video/for-developers/ then:
sudo NDI_ADVANCED_SDK_TARBALL=/path/to/advanced-sdk.tar.gz ./install.sh
```

The installer detects the Advanced SDK headers and builds the plugin with
`--features advanced-sdk` automatically. Then pick a `compressed-*` colour
format (the **Hardware decode** preset does this) and check the Performance
tab: it reports which decoder actually got used and whether it is hardware.
No element name is hardcoded — the decoder is chosen by rank at runtime and
reported honestly.

**This only helps if the sender emits NDI HX.** Full-bandwidth NDI has no
compressed form; the setting will be ignored.

| Stream | Decode path | Realistic on a Pi 4B |
| --- | --- | --- |
| NDI HX, Advanced SDK build | **Hardware** | 1080p60 comfortably |
| NDI HX, standard SDK build | Software (libndi) | 1080p30–60, CPU-heavy |
| Full-bandwidth NDI (SpeedHQ) | Software, no hardware path exists | 1080p30 marginal, 1080p60 unlikely |

### Everything else, roughly in order of payoff

1. **Have the sender emit NDI HX.** Less data on the wire and the only route to
   hardware decode. Bigger than every software tweak combined.
2. **Turn off sink QoS.** The sink drops late frames to stay in time; if the Pi
   is only slightly too slow that mechanism *is* the visible problem. Turning
   off clock sync goes further — every frame is shown as it arrives.
3. **Eliminate the colour conversion.** Set the output format to `auto`, which
   negotiates from an ordered list of cheap formats. UYVY and NV12 are native
   vc4 plane formats, so if the SDK hands back UYVY the conversion disappears.
   (`auto` is a *preference list*, not "anything" — left unconstrained,
   negotiation will cheerfully pick `A444_16LE`, which is the most expensive
   format on offer.)
4. **Eliminate the scale.** Either pin the display mode to the source
   resolution, or tick "send the source's own resolution to the display".
   videoscale is a passthrough when sizes already match.
5. **Nearest-neighbour scaling** if you must scale. Free quality loss you
   cannot see from ten metres.
6. **Drop to the proxy stream** (`bandwidth=lowest`). Cuts network and CPU at
   once. Sometimes the right answer for a confidence monitor.
7. **Lower the output mode.** 720p instead of 1080p is a big saving in
   conversion and scaling.
8. **CPU governor pinned to `performance`.** Applied at boot by
   `pistreamer-tuning.service`. The default `ondemand` ramps up *after* load
   appears, which shows as periodic timing wobble.
9. **Overclock.** Worth roughly 20–30% more headroom on a Pi 4 and documented
   in `/etc/pistreamer/tuning.conf`. Deliberately not applied automatically:
   check for under-voltage first and fit at least a heatsink.
10. **Turn off audio** if you are not using it — one less branch and no ALSA.
11. **Turn off the snapshot** if you do not need the standby hold; it costs a
    JPEG encode every few seconds.
12. **A Pi 5** if full-bandwidth 1080p60 is a hard requirement. Two to three
    times the CPU. Sometimes the honest answer is a bigger board.

The Performance tab has presets — Maximum compatibility, Balanced, Lowest CPU,
Hardware decode — which fill the fields in without saving so you can see what
changes before applying.

### Diagnosing rather than guessing

Frames are measured at two points: as they arrive from the receiver and as they
reach the display. That distinguishes the two causes that look identical from
outside.

- Short **on arrival** → the network or the sender.
- Arrive fine, short **on screen** → this Pi.

The Diagnosis card on Now Playing states which, with its evidence. Wi-Fi
signal, link rate, retries and power-save state are on the Diagnostics tab,
along with ten minutes of history for CPU, temperature, throughput and frame
rate.

Also worth knowing:

- Use a genuine 3A USB-C supply. Under-voltage throttling looks exactly like a
  decode problem; the Diagnostics tab separates them.
- Use wired Ethernet. Full-bandwidth 1080p60 NDI is ~130 Mbps and discovery is
  mDNS; Wi-Fi is not a serious option for either.
- NDI discovery does not cross subnets or VLANs without an NDI Discovery
  Server. Use the GUI's manual connect field for that case.

## Configuration

`/etc/pistreamer/config.json` — written by the GUI, safe to hand-edit over SSH.
The service reloads it on restart.

| Key | Meaning |
| --- | --- |
| `mode` | `idle` / `ndi` / `local` |
| `ndi_source` | NDI name exactly as advertised, e.g. `STUDIO-PC (OBS)` |
| `local_file` | Filename in the media directory, or `""` for the whole folder |
| `autostart` | Restore `mode` on boot |
| `connector` | DRM connector, e.g. `HDMI-A-1`. `""` = first connected |
| `video_mode` | e.g. `1920x1080@60`. `""` = monitor's preferred mode |
| `rotation` | `0` / `90` / `180` / `270` |
| `ndi_bandwidth` | `highest` (full) or `lowest` (proxy stream) |
| `ndi_latency_ms` | Receive buffer. Raise if the picture stutters |

Media lives in `/var/lib/pistreamer/media`.

## Operating

```bash
systemctl status pistreamer
journalctl -u pistreamer -f          # service log
gst-inspect-1.0 ndisrc               # is the NDI plugin loaded?
gst-device-monitor-1.0 Source/Network  # what senders can this Pi see?
```

The GUI's pipeline log shows GStreamer/mpv stderr, which is where decode errors
and NDI connection failures surface first.

## Troubleshooting

**No NDI sources listed.** Check `gst-inspect-1.0 ndisrc` loads. If it does,
the plugin is fine and it is a network problem: same subnet, mDNS not blocked,
sender actually running. Try the manual connect field with the exact name.

**Black screen but the service is running.** Check the pipeline log. The usual
cause is another process holding DRM master — make sure no desktop or getty is
on the same connector (`systemctl stop getty@tty1` to test).

**Stuttering NDI.** Almost always full-bandwidth SpeedHQ decode. See the
performance table. Confirm with the CPU figure in the health panel: pegged at
100% means decode, not network.

**`ndisrc` not found after a reboot.** The plugin lives outside the distro
plugin path; the systemd unit sets `GST_PLUGIN_PATH`. Running `gst-launch-1.0`
by hand needs `GST_PLUGIN_PATH=/opt/pistreamer/gst-plugins`.

**No audio.** HDMI is a separate ALSA card on the Pi (`vc4hdmi0`/`vc4hdmi1`) and
is almost never the default, so this is usually the device rather than the
stream. The Audio card on the Display tab lists the real cards and devices,
points out which one looks like HDMI, and plays a test tone — prove the output
first, then look at the stream. NDI and local media have separate device
settings because ALSA and mpv name devices differently.

**Two things playing at once, or audio from the previous item.** Every player
runs in its own process group and is torn down before the next one starts, with
a sweep for anything left unsupervised — if that sweep finds something, the
Stream tab says so and the log records it. A count above zero there means a
player escaped teardown and is worth reporting. Local playback writes straight
to ALSA (`--ao=alsa`) rather than through a sound server, because a server keeps
its own buffer and would go on playing a stopped item underneath the next one.

**Overclock control unavailable, mentioning sudo.** The installed units are
older than the app. The service cannot escalate at all by design; overclocking
is applied by a root path-activated unit. Run `install.sh` again to install
`pistreamer-overclock.path`, then check `systemctl status
pistreamer-overclock.path` shows it active.

## Testing

None of this needs a Pi:

```bash
python3 tests/test_smoke.py      # API contract, config, uploads, degradation
python3 tests/test_features.py   # playlist segments, validation, schedule cues
python3 tests/test_teardown.py   # nothing outlives its segment (real processes)
python3 tests/test_diagnose.py   # network-vs-Pi verdicts

python -m pistreamer.runner --self-test             # the real video chain
python -m pistreamer.runner --self-test-compressed  # decoder selection
python -m pistreamer.runner --list-decoders         # what this box can decode
```

The self-tests are the important ones: they make the pipeline testable off
hardware, which is the layer where nearly every bug in this project has lived.

## Layout

```
install.sh                  orchestrator
scripts/
  common.sh                 shared bash helpers
  00-base-packages.sh       GStreamer, mpv, Python, build tools
  10-ndi-sdk.sh             NDI SDK runtime + headers
  20-gst-ndi-plugin.sh      builds teltek/gst-plugin-ndi
  30-app.sh                 user, venv, polkit, systemd
  40-tune-boot.sh           config.txt / cmdline.txt, journald, service trim
  pistreamer-overclock      root helper: status | --from-request | <preset>
  pistreamer-tuning         boot-time governor and scheduler tuning
src/pistreamer/
  config.py                 atomic JSON config store
  display.py                DRM connector + mode discovery from sysfs
  sources.py                NDI discovery (GstDeviceMonitor, CLI fallback)
  ndiconfig.py              ndi-config.v1.json: adapters, IPs, discovery server
  media.py                  local media library, path-traversal safe
  playlists.py              named playlists of file/NDI segments
  schedule.py               time-of-day cues
  player.py                 the state machine, supervisor and sequencer
  runner.py                 the instrumented GStreamer pipeline
  mpvipc.py                 mpv JSON IPC client for local playback stats
  diagnose.py               network-vs-Pi verdict with evidence
  telemetry.py              rolling 10-minute sampler, memory only
  system.py                 telemetry, audio devices, overclock, power actions
  web.py                    FastAPI app
  static/index.html         the GUI, single file, no build step
systemd/
  pistreamer.service            the app (NoNewPrivileges=yes)
  pistreamer-overclock.path     watches for an overclock request
  pistreamer-overclock.service  root oneshot that applies a preset
  pistreamer-tuning.service     boot-time tuning
```

## Status

Working on a Pi 4B: NDI receive and display are confirmed against a real
sender. Still unverified on hardware: the Advanced-SDK hardware decode path,
the standby fallback, playlists and the scheduler, the overclock presets. NDI
discovery on a dual-homed node (Wi-Fi with internet, Ethernet without) is a
known open problem — transport works, discovery does not; use the manual
address field there for now.

## Licence and trademarks

NDI® is a registered trademark of Vizrt NDI AB. The NDI SDK is licensed
separately by Vizrt and is not redistributed here.
