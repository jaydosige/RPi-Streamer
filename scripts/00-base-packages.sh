#!/usr/bin/env bash
# Base OS packages: GStreamer, mpv, Python, build tools.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

info "Refreshing package lists"
apt-get update -qq

# --- runtime ---------------------------------------------------------------
apt_install \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  `# gst-device-monitor-1.0 lives here, NOT in gstreamer1.0-tools. Without it` \
  `# the CLI discovery fallback and the NDI diagnostics endpoint cannot run.` \
  gstreamer1.0-plugins-base-apps \
  `# gdkpixbufoverlay lives here too — it is what puts the guest QR code on` \
  `# the output. Without it the code is only ever in the operator's browser.` \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad \
  `# textoverlay lives here (in the pango plugin), NOT in gstreamer1.0-plugins-base` \
  `# despite being built from gst-plugins-base. Without it the identify overlay` \
  `# silently never appears: the runner logs one warning and plays on without it.` \
  gstreamer1.0-x \
  gstreamer1.0-alsa \
  gstreamer1.0-libav \
  libgstreamer1.0-0 \
  libgstreamer-plugins-base1.0-0 \
  mpv \
  ffmpeg \
  `# Pillow draws the guest QR panel — the code, and the address underneath it.` \
  `# From apt rather than pip: pip builds Pillow from source on a Pi, which is` \
  `# twenty minutes of compiling for a package the distribution already has.` \
  python3-pil \
  `# A font for it to draw the address with. Pango finds one for the caption` \
  `# via fontconfig; Pillow is handed a file path and needs it to exist.` \
  fonts-dejavu-core \
  `# Uploads arrive in formats a display cannot show. A phone takes HEIC and` \
  `# an office sends a PDF; both are turned into JPEGs on arrival so nothing` \
  `# downstream has to learn a new format. Both packages are small.` \
  libheif-examples \
  poppler-utils \
  alsa-utils \
  avahi-daemon \
  libdrm2 \
  libgbm1

# python3-gi gives us the structured NDI device monitor; without it discovery
# falls back to parsing gst-device-monitor-1.0 output.
apt_install \
  python3 \
  python3-venv \
  python3-pip \
  python3-gi \
  gir1.2-gstreamer-1.0 \
  gir1.2-glib-2.0

# --- build-time (for the Rust NDI plugin) ----------------------------------
apt_install \
  build-essential \
  pkg-config \
  curl \
  ca-certificates \
  git \
  libssl-dev \
  libgstreamer1.0-dev \
  libgstreamer-plugins-base1.0-dev

ok "Base packages ready"

# --- AirPlay receiving -----------------------------------------------------
# uxplay is in Debian from Trixie (and Ubuntu from Noble). On anything older
# it simply is not there, and that is not a reason to fail an install: the node
# works perfectly well without AirPlay, and the GUI already says so with the
# command to fix it. Hence the || rather than set -e taking the whole run down.
# --- Web page source -------------------------------------------------------
# Chromium is ~400 MB installed and only web mode needs it, but a node that
# cannot show a dashboard until somebody SSHes in is worse than a slightly
# bigger image. Optional like uxplay: its absence is not a failed install, and
# the GUI offers to add it later.
# cage is what chromium draws onto. Debian builds chromium with the x11,
# wayland and headless ozone backends and NOT drm, so on a box with no desktop
# it has no way to reach the screen at all — "Invalid ozone platform: drm",
# fatal, on repeat. cage is a kiosk compositor that puts one window full screen
# straight onto KMS, and is about a hundred kilobytes.
if apt_install chromium cage 2>/dev/null \
   || apt_install chromium-browser cage 2>/dev/null; then
  ok "Web page source available (chromium under cage)"
else
  warn "chromium is not in this distribution's archive, so the web page source"
  warn "will be unavailable until it is installed. The GUI has a button for it."
fi

if apt_install uxplay 2>/dev/null; then
  ok "AirPlay receiving available (uxplay $(uxplay -v 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+' | head -1))"
else
  warn "uxplay is not in this distribution's archive, so AirPlay receiving will be unavailable."
  warn "Everything else works. On Raspberry Pi OS Bookworm or older, upgrade to Trixie or build UxPlay from source."
fi

# Advertise the node over mDNS so <hostname>.local resolves without DNS.
if ! systemctl is-enabled --quiet avahi-daemon 2>/dev/null; then
  systemctl enable --now avahi-daemon
  ok "avahi-daemon enabled (<hostname>.local)"
fi

# The default Wi-Fi power saving causes NDI discovery dropouts. Harmless on
# an Ethernet-only node.
if [[ -d /etc/NetworkManager/conf.d ]]; then
  cat > /etc/NetworkManager/conf.d/99-pistreamer-no-powersave.conf <<'EOF'
# NDI discovery is mDNS-based and is unreliable when the radio sleeps.
[connection]
wifi.powersave = 2
EOF
  ok "Wi-Fi power saving disabled"
fi
