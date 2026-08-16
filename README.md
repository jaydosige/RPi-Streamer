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

The Pi 4's video block and its CPU pull in different directions, and NDI has two
very different wire formats:

| Stream | Decode path | Realistic on a Pi 4B |
| --- | --- | --- |
| NDI HX (H.264) | Hardware, V4L2 M2M | 1080p60 comfortably |
| NDI HX3 / HEVC | Hardware HEVC block | 1080p60 comfortably |
| Full-bandwidth NDI (SpeedHQ) | **CPU only** | 1080p30 marginal; 1080p60 unlikely |

There is no hardware SpeedHQ decoder, so full-bandwidth NDI is decoded on four
Cortex-A72 cores and that is the wall you will hit first. Two ways round it:
have the sender emit NDI HX, or select the **proxy stream** in the GUI's NDI
settings (`bandwidth=lowest`), which receives the sender's low-resolution
preview instead.

**These numbers are estimates and need confirming on your hardware.** The GUI's
health panel shows CPU, temperature and the Pi's own under-voltage/throttle
flags — watch all three during a soak test.

Also worth knowing:

- Use a genuine 3A USB-C supply. Under-voltage throttling looks exactly like a
  decode problem and the GUI will tell you which one it is.
- Use wired Ethernet. NDI discovery is mDNS and full-bandwidth NDI is ~130 Mbps
  at 1080p60; Wi-Fi is not a serious option for either.
- NDI discovery does not cross subnets or VLANs without an NDI Discovery Server.
  If the sender is on another VLAN, use the GUI's manual connect field.

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
src/pistreamer/
  config.py                 atomic JSON config store
  display.py                DRM connector + mode discovery from sysfs
  sources.py                NDI discovery (GstDeviceMonitor, CLI fallback)
  media.py                  local media library, path-traversal safe
  player.py                 the state machine and supervisor
  system.py                 telemetry and power actions
  web.py                    FastAPI app
  static/index.html         the GUI, single file, no build step
systemd/pistreamer.service
```

## Status

v0.1 — code complete, **not yet run on hardware**. Everything from
`install.sh` onward is unverified against a real Pi 4B and a real NDI sender.

## Licence and trademarks

NDI® is a registered trademark of Vizrt NDI AB. The NDI SDK is licensed
separately by Vizrt and is not redistributed here.
