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
- Receives AirPlay: an iPhone, iPad or Mac mirrors onto the node from the
  ordinary picker, with an optional pairing code
- Plays or loops local video files, uploadable through the browser
- Plays a **live stream** by address — HLS and DASH over http(s), or
  `udp://`, `rtp://`, `rtsp://`, `srt://` from an encoder on the LAN
- Shows a **web page** full screen — a dashboard, a scoreboard, a running order
- Shows **documents**: a PDF stays one entry in the library and is paged
  through on screen, with arrow keys or a page number. Plain text too
- Accepts **HEIC** from a phone, converting it on arrival
- Runs a **GLSL shader** you write in the GUI, with a live preview beside the
  code — Shadertoy conventions, so anything from there pastes straight in
- **Favourites**: any page or stream saved under the name you actually call it
- Takes a photo or video from anyone in the room, from a QR code the operator
  can switch on and off
- **Previews the output in the browser**, so you can see what is on the screen
  without being in front of it
- Drives HDMI through DRM/KMS — no X, no Wayland, no desktop
- Restores the last source on boot, and reconnects on its own when a sender
  drops and comes back
- Optional **login** for the console. Guest sharing stays open either way —
  the QR code is the guest's credential
- **Backs its settings up** to one small file, and restores them onto a fresh
  card
- Produces a **support bundle**: everything needed to diagnose the node,
  secrets redacted, in one file you can send to somebody
- Web GUI at `http://<hostname>.local/` — one page: a dashboard that is always
  on screen (what is playing, a preview of it, and whether the node is
  healthy), over tiles that expand into **Now playing**, **Sources**,
  **Media**, **Nodes**, **Output** and **System**. Built for a phone in a dark
  room as much as a laptop

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
git clone <this-repo> ~/RPi-Streamer
cd ~/RPi-Streamer

sudo -v                 # authenticate first, on its own line
sudo ./install.sh
sudo reboot
```

The first run takes 20–40 minutes, almost all of it compiling the Rust NDI
plugin. After the reboot the GUI is at `http://<hostname>.local/`.

`sudo -v` on its own line matters if you paste a block of commands: `sudo`'s
password prompt reads from stdin, so the *next pasted line* gets eaten as the
password and both commands fail. Authenticate first and nothing that follows can
be swallowed.

### Updating

After the first install, updates happen **from the GUI**: System → Software →
Check for updates. The node fetches from the git remote it was installed from,
shows what the update contains, and applies it — stopping playback, reinstalling
and restarting the service. Nodes → Update the group does the same across every
node, itself last so you can watch the others finish before your own page drops.

It uses the clone's own credentials, so a private repository needs no token
stored in the app. If a check reports an authentication failure, store the
credentials once on the node:

```bash
git -C ~/RPi-Streamer config credential.helper store
git -C ~/RPi-Streamer pull        # enter them once
```

Every update records the commit it came from, so **Roll back** returns the node
to the previous version if something is wrong. A node installed from a tarball
has no remote to update from and says so.

The mechanics are the same path-activated root job as overclocking, for the same
reason — the service cannot escalate — plus one more: the service's unit sets
`ProtectHome=yes`, so it cannot even see the working copy in a login user's
home. It writes a request; a root job does the git work and writes back a status
file, which survives the service restart the update itself causes.

### One command per node: `bootstrap.sh`

`install.sh` turns a Pi into a node. `scripts/bootstrap.sh` does everything
around it — installs git, fetches the code, gives the unit a unique name and
identity, joins it to a group with the right key, and runs the installer:

```bash
sudo ./scripts/bootstrap.sh --name STAGE-LEFT --group wall --key 'your-secret'
```

It is idempotent, so **re-running it is also how you update a node**. It works
out the git remote's name rather than assuming `origin`, stashes any local edits
instead of refusing to pull, fixes the executable bits itself, and preserves
every other setting in `config.json` when it writes the group settings.

Useful flags:

| Flag | For |
| --- | --- |
| `--from-archive FILE` | Install from a tarball — no git, no network needed |
| `--fresh-identity` | **Use this on a cloned SD card.** Regenerates machine-id |
| `--dry-run` | Print every step and change nothing |
| `--skip-install` | Set name and group only, skip the long install |

Deploying by cloning a card that already works is the sensible route for units
three onwards. Do it with `--name` and `--fresh-identity`, so the new unit gets
its own hostname and its own machine-id rather than the original's.

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

## AirPlay

An iPhone, iPad or Mac mirrors onto the node from the ordinary AirPlay picker.
Sources tab → **AirPlay** → *Start receiving*. The node appears under its own
name, so on a multi-node rig you pick STAGE-LEFT or STAGE-RIGHT from the phone.

Receiving is a **playback mode**, not a background service, and that is the
whole design: an AirPlay session takes the display, and this project has one
rule about the display — one process owns it at a time. Starting AirPlay stops
whatever was playing, exactly as switching to NDI does. A schedule cue or a
group command can switch to it like any other mode.

**The screen is black until somebody connects.** The receiver only takes the
display when a session actually starts, so between devices there is nothing to
show. That is deliberate: the alternative is two processes contending for DRM
master, and losing that race means the mirror fails in front of a room rather
than merely starting late. If you want a holding slide up until the moment
someone mirrors, leave the node on standby or a playlist and switch to AirPlay
when they are ready — one click, or a cue.

Notes from getting it working, all of which will bite again:

* **The pairing code has nowhere to go.** With *Ask for a pairing code* on,
  uxplay prints a four-digit code — to a terminal a headless node does not
  have. The GUI is that terminal: the code appears on the card, and changes
  each time the receiver restarts.
* **Avahi must be running.** Without it uxplay prints one error and exits,
  which a supervisor reads as a crash worth retrying all evening. It is checked
  before starting instead, and the GUI says what to run.
* **Hardware decode is checked, not assumed.** `v4l2h264dec` is the Pi's GPU
  h264 decoder and is on by default, but uxplay *aborts* on an unknown element
  in about 40 ms. If it is not there — the codec module has not loaded, or this
  is not a Pi — the receiver quietly falls back to software rather than
  crash-looping behind `exited with code -5`.
* **The colour workaround is on by default.** Apple sends a full-range variant
  the Pi's V4L2 decoder refuses outright, and the symptom is the receiver
  sitting there perfectly healthy until somebody connects and then
  `Internal data stream error` at the first frame. `-bt709` puts the right
  colorimetry on the h264 caps ahead of the decoder. It only sets metadata, so
  it costs nothing where it is not needed.
* **And if it still fails, it degrades itself.** If the GPU decoder dies on a
  live stream anyway, the receiver restarts once with software decoding and
  says so on the card, rather than leaving somebody in front of a room pressing
  Start again. A Pi 4 manages about 720p that way.
* **Ports.** Left alone they are dynamic and advertised over mDNS. Pin them
  with `airplay_port` if there is a firewall between the phones and the node:
  with `-p n` the AirPlay service ends up on **n+2** (measured, not guessed),
  and mDNS needs UDP 5353 either way.

### Miracast: not built, and why

Miracast (Android and Windows "cast to a wireless display") does not fit this
box. The Linux sink implementation is MiracleCast, and it requires shutting
down NetworkManager and taking exclusive control of wpa_supplicant on the
radio — on this node that is the radio carrying the web GUI, so turning
Miracast on disconnects the operator from the thing they would use to turn it
off. It is also not packaged in Debian, and needs Wi-Fi Direct P2P running
alongside the station connection, which the Pi's onboard Broadcom radio
supports only in a limited combination.

With a second, dedicated Wi-Fi adapter it becomes arguable — MiracleCast could
own that one while NetworkManager keeps the built-in. That is untested here and
would need the hardware in hand before it went near a job.

For Windows and Android laptops, **NDI is the better answer and already
works**: NDI Screen Capture is a free download for Windows, sends over the
wired network, and appears in the Sources tab like any other sender.

## Guest sharing

Somebody at the job has a video on their phone and wants it on the screen. Media
tab → **Guest sharing** → *Open sharing*. The node draws a QR code and shows the
address underneath it; a guest scans it or types the address and gets a one-page
site with one button. What they send lands in the queue under the card, and the
operator presses **Show**.

**The code goes on the output too.** The people who need to scan it are looking
at the screen, not at the operator's browser — so while sharing is open the QR
and the address sit in the bottom-right corner of whatever is playing, the same
way the identify caption sits in the top-left. It survives a change of content,
comes down by itself when the session expires, and can be switched off from the
card if the picture needs to stay clean.

The QR is generated on the node, not by a web service, because a show network
usually has no route to the internet. The on-screen panel needs `python3-pil`,
which the installer adds; without it guest sharing still works and the code just
stays in the browser.

It is built on the assumption that the audience is the threat model:

* **Off by default, and it closes itself.** A session runs for an hour unless
  you say otherwise. Nobody remembers to shut the door at the end of a job.
* **The QR is the credential.** Every time you open sharing a new token is
  minted, so last month's photo of the code is worthless.
* **Guests cannot take the screen.** Uploads queue and you decide. Tick *Guests
  may put their own upload on the screen* if you would rather they didn't have
  to find you — they can then only show files they themselves sent this session.
* **Caps on size, count and type** (`guest_max_mb`, `guest_max_items`), because
  the upload page is reachable by everybody in the room.
* **Discard deletes.** Rejecting something from the queue removes the file from
  the node, not just from the list.

Guest files are stored as `guest-<id>-<their filename>`, so an `IMG_0001.mp4`
from the floor can never overwrite one of yours and you can tell at a glance in
the library where a file came from.

**Screen sharing is not offered, deliberately.** A browser can only share a
screen through `getDisplayMedia`, and that API does not exist on any browser on
iOS, is unreliable on Android, and needs HTTPS — which means a certificate, on a
box whose address changes with the DHCP lease. Building it would produce a
button that fails for most of the room. If you need a phone or laptop screen on
the wall, the right answer is AirPlay/Miracast receiving, which belongs as
another playback mode next to NDI and local rather than as a web page.

## First boot, and locking the console

The GUI opens on a four-step wizard the first time: how to reach the node, what
to call it, its network, and an optional password. "Show on the screen" puts
the node's name and address on the display it is plugged into, which is how you
tell which of six boxes you are talking to.

The login is **off unless you switch it on**. Setting a password does not lock
anything; `auth_enabled` does. That way updating a node mid-job never starts
demanding a password nobody has to hand.

Two things are deliberately outside it:

- **Guest sharing is never behind the login.** The QR code is the guest's
  credential and the whole point is that a stranger can use it.
- **Nodes authenticate to each other with the group key**, not a session, so a
  group keeps working when the console is locked.

Forgotten the password? Delete `/var/lib/pistreamer/auth.json` over SSH and the
login turns itself off. Anyone who can do that already has the SD card.

### Wi-Fi and the setup hotspot

Hostname and Wi-Fi go through a root helper (`pistreamer-netcfg`), because the
service runs with `NoNewPrivileges` and a read-only `/etc` and cannot do either
itself. The GUI can scan, join a network, or start an access point for a node
that is not on a network at all.

**The hotspot takes the wireless interface.** Starting one on a node you are
reaching over Wi-Fi will drop you — join `pistreamer-setup` and open
`http://10.42.0.1` to carry on. It puts the previous network back on its own
after 30 minutes, and a failed join restores the hotspot rather than leaving
the node unreachable. First time you use it, do so on a node you can also reach
over ethernet or with a keyboard.

## Touchscreens, keyboards and mice

A web page **is** interactive, and already was. Chromium is driven straight
onto DRM by its ozone backend, which reads `/dev/input` itself, and the service
runs with `SupplementaryGroups=... input` so it can. A keyboard, mouse or
touchscreen plugged into the node reaches the page with nothing else to set up.

Sources → Web page has a switch for it. On adds `--touch-events=enabled`, which
matters for a panel that enumerates a moment after Chromium starts and would
otherwise be missed for the life of the session — a screen that looks right and
ignores every tap. Off takes away pinch and scrollbars so a passer-by cannot
scroll the page by accident.

Off is **not a lock**. Nothing in Chromium ignores evdev on command; a keyboard
still works. A screen nobody can touch is one with nothing plugged into it.

The other sources are not interactive and cannot easily be made so: mpv is
started with `--no-terminal` and its DRM output takes no evdev input, and NDI
and AirPlay are one-way by definition.

Two things worth checking on the node itself, because neither can be checked
from anywhere else:

```bash
# Does this chromium have the DRM backend at all? Web mode needs it — there is
# no X or Wayland under it. A build without it fails whatever the input does.
chromium --ozone-platform=drm --headless --dump-dom about:blank >/dev/null && echo ok

# Are the input devices actually visible to the service?
sudo -u pistreamer ls -l /dev/input/
```

The GUI reports the second of these beside the browser name.

## Backing a node up

**System → Settings backup.** One small file with playlists, the schedule,
favourites, shaders and every setting. Media is not in it — a library is
gigabytes and already moves between nodes with *Send files to the group*.

Restoring onto a freshly flashed card brings everything back before the media
has arrived; playlists naming files the node has not got yet are kept, not
rejected. By default the node keeps its own name and login setting, so a spare
does not come up believing it is the machine it replaced.

The file contains the group key, because a node that cannot rejoin its group
has not been restored. It does **not** contain the console password. Treat it
as a credential.

## When something is wrong: the support bundle

**System → Support bundle → Download.** One file with the player log, the
systemd journal — which is where a service that restarts in a loop leaves its
evidence, and which nothing else in the GUI can reach — versions, capabilities,
telemetry, media, cluster state and the settings.

The group key, password hash and Wi-Fi passphrase are redacted, so it is safe
to attach to a message. Sections that fail are recorded in place rather than
losing the bundle: half of one from a misbehaving node is worth more than an
exception.

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

**Terminal text visible on the display.** Two separate causes. On a fresh boot
it is kernel and systemd messages printed before playback starts: the cmdline
gets `quiet loglevel=3 systemd.show_status=false` plus `console=tty3`, and the
getty on tty1 is masked, so nothing writes to the visible console. When
switching streams it is the old console contents being repainted: only one
process can hold the display, so when a player exits it releases DRM master and
the kernel redraws the framebuffer console underneath. `pistreamer-console`
clears that console once at boot, which makes the redraw black rather than text.
A brief black frame between items is therefore expected and cannot be removed
without a second compositor holding the output.

To get the console back for local debugging:

```bash
sudo systemctl unmask getty@tty1 && sudo systemctl enable --now getty@tty1
sudo sed -i 's/ quiet//; s/ loglevel=3//; s/ systemd.show_status=false//' /boot/firmware/cmdline.txt
sudo reboot
```

The originals are at `/boot/firmware/cmdline.txt.pistreamer.bak`. If text still
gets through, the last resort is adding `fbcon=map:2` to `cmdline.txt`, which
detaches the console from the framebuffer entirely. Untested on this hardware,
and it costs you the local display for debugging — SSH becomes the only way in.

**Overclock control unavailable, mentioning sudo.** The installed units are
older than the app. The service cannot escalate at all by design; overclocking
is applied by a root path-activated unit. Run `install.sh` again to install
`pistreamer-overclock.path`, then check `systemctl status
pistreamer-overclock.path` shows it active.

**Nothing plays, and the log repeats "failed to set pipeline to PLAYING".**
Nothing is plugged into HDMI, so the connector advertises no modes and there is
none for the sink to set. Either connect a display, or run headless by adding
`video=HDMI-A-1:1920x1080@60e` to the end of the single line in
`/boot/firmware/cmdline.txt` and rebooting. The node says this outright rather
than looping silently.

**A web page or shader says chromium is not installed.** Sources → Web page has
a button for it, or `sudo apt install chromium`. Shaders need it too — they are
drawn by the browser.

**A conversion fails with `frame= 0 ... Conversion failed!`.** The Pi's V4L2
H.264 encoder is present in ffmpeg but frequently unusable on current
Raspberry Pi OS. The node now tests an encoder before choosing it and falls
back to software, so this should not recur; if it does, the support bundle has
the real ffmpeg error in it.

## Several nodes together

Nodes announce themselves on UDP 47600 every two seconds and appear in each
other's **Nodes** tab. There is nothing to configure beyond a group name and a
shared key, which must match on every unit — two shows on one network stay apart
by using different group names, and the key stops a laptop on the guest VLAN
from stopping playback. Change it from the default.

From any node you can put the whole group into standby, reboot it, send a
playlist to it, or play that playlist in step. There is no leader daemon and no
cluster state: whichever node you have open acts as conductor for that
operation, so there is no split-brain to debug on a show day.

**Identify** puts each node's name and address on its own screen, over whatever
is playing and on nodes that are playing nothing. It is how you work out which
box is which without unplugging HDMI cables. It persists across a reboot on
purpose.

### How synchronised playback works

Per item: every node loads the file and holds its first frame *paused*, reports
ready, and is then told a wall-clock instant to un-pause — expressed in its own
clock, because the conductor measures each node's clock offset and does the
arithmetic itself. Spawning a player takes tens to hundreds of milliseconds and
varies per node and per file; un-pausing one that has already decoded costs
about a frame, which is the whole reason for the two-step start.

While an item plays, the conductor publishes its playhead every two seconds. A
node that is out by under a quarter second changes speed by 2% until it catches
up, which is invisible and inaudible; one that is further out seeks, which is
not invisible and is therefore reserved for genuinely lost sync. Turn drift
correction off and you still get a synchronised start for every item.

Only files are synchronised. An NDI source is already live with its own timing,
so putting one in a synchronised playlist is not meaningful.

**Still images** end on the clock rather than on a playhead, because they do not
have one — mpv reports a position of zero for an image for as long as it is up.
Each node holds the image open and the conductor takes it down at the agreed
instant, so a wall of screens changes slide together.

**Stopping** a node — from its GUI, a cue, or a group command — ends the
synchronised session it was conducting, and a node stopped by hand sits out the
rest of that session rather than being pulled back in at the next item. It
rejoins when the group is started again. Without that, stop looks broken: the
screen goes black and comes back a few seconds later.

**Node identity and cloned SD cards.** A node's identity is derived from the Pi's
board serial first, then machine-id, then MAC. This matters because cloning a
working card — the obvious way to deploy the second and third node — copies
`/etc/machine-id`. Nodes sharing an identity treat each other's beacons as their
own echo and discover nothing, with no other symptom, so the Nodes tab warns
loudly if it sees one.

## Testing

None of this needs a Pi. Run the lot:

```bash
./run-tests.sh              # everything; exits non-zero if anything failed
./run-tests.sh cluster doc  # only files whose name matches
./run-tests.sh -q           # one line per file
```

Tests that need something the machine has not got — GStreamer, mpv, a real
`/proc`, a writable `/etc` — skip themselves and are listed at the end. A test
that cannot run is not a failure, but it is not evidence either.

Roughly what each covers:

| file | what it is for |
| --- | --- |
| `test_smoke` | API contract, config, uploads, degradation |
| `test_features` | playlist segments, validation, schedule cues |
| `test_cluster` | beacons, group auth, sync maths, push progress |
| `test_cluster_live` | two real nodes: discovery, auth, file push |
| `test_access` | the login, and what it must never lock out |
| `test_backup` | settings off one node and onto a blank one |
| `test_support` | the diagnostic bundle, and that it leaks no secrets |
| `test_preview` | capture follows demand, and stops when nobody looks |
| `test_documents` | a PDF stays one entry and pages at playback |
| `test_ingest` | HEIC becomes a JPEG at the door |
| `test_shaders` | GLSL renders, and a broken one says why |
| `test_playback_quality` | what will play well, and re-encoding what will not |
| `test_status_render` | the status panel survives every backend's stats |
| `test_gapless` | playlist items reuse the running mpv |
| `test_nodisplay` | a node with no cable says so |
| `test_netjoin` | joining Wi-Fi from the hotspot, and getting back |
| `test_guest` | what the room can and cannot do, and the QR decodes |
| `test_airplay` | AirPlay, against a real uxplay process |
| `test_teardown` | nothing outlives its segment (real processes) |
| `test_diagnose` | network-vs-Pi verdicts |
| `test_overlay`, `test_mpvoverlay` | the caption and QR actually render |
| `test_gui` | a real browser: the poll must not overwrite what you type |
| `test_update` | updates, against real git repositories |

```bash
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
  pistreamer-netcfg         root helper: hostname, Wi-Fi, hotspot, packages
  pistreamer-tuning         boot-time governor and scheduler tuning
run-tests.sh                the suite; non-zero if anything failed
src/pistreamer/
  config.py                 atomic JSON config store, and the atomic
                            write/read every other store uses
  display.py                DRM connector + mode discovery from sysfs
  sources.py                NDI discovery (GstDeviceMonitor, CLI fallback)
  ndiconfig.py              ndi-config.v1.json: adapters, IPs, discovery server
  media.py                  local media library, path-traversal safe, and
                            whether a file will decode in hardware
  ingest.py                 HEIC becomes a JPEG on arrival
  documents.py              a PDF stays one entry; pages rendered at playback
  transcode.py              re-encoding what will not play well
  favourites.py             saved pages and streams
  shaders.py                GLSL stored as files, drawn by the browser
  preview.py                output frames, captured only while somebody looks
  playlists.py              named playlists of file/NDI segments
  schedule.py               time-of-day cues
  player.py                 the state machine, supervisor and sequencer
  runner.py                 the instrumented GStreamer pipeline
  mpvipc.py                 mpv JSON IPC client for local playback stats
  diagnose.py               network-vs-Pi verdict with evidence
  telemetry.py              rolling 10-minute sampler, memory only
  system.py                 telemetry, audio devices, overclock, power actions
  airplay.py                AirPlay receiving: uxplay's command line, and
                            reading a session out of its output
  guest.py                  guest sharing sessions, tokens, QR
  auth.py                   the operator login: hashing, sessions, throttling
  network.py                asking the root helper for Wi-Fi and hostname
  backup.py                 settings off one node and onto another
  support.py                the diagnostic bundle, with secrets redacted
  cluster.py                signed UDP beacons, peer registry, node-to-node calls
  syncplay.py               synchronised playback: aligned starts, drift
                            correction, and how hard to correct
  pushjob.py                copying a playlist to the other nodes, with progress
  updates.py                the app half of GUI-driven updates
  web.py                    FastAPI app
  static/index.html         the GUI, single file, no build step
  static/guest.html         the guest upload page, separate on purpose
  static/shader.html        the WebGL runner — the screen and the editor
                            preview use this same page
systemd/
  pistreamer.service            the app (NoNewPrivileges=yes)
  pistreamer-overclock.path     watches for an overclock request
  pistreamer-overclock.service  root oneshot that applies a preset
  pistreamer-netcfg.path        watches for a network/hostname request
  pistreamer-netcfg.service     root oneshot that applies it
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
