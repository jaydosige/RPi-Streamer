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
