#!/usr/bin/env bash
#
# Boot / firmware tuning for an appliance-style node.
#
# Everything here is optional and reversible: originals are backed up to
# *.pistreamer.bak the first time this runs.
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

# Trixie/Bookworm put the firmware files here; older images used /boot.
BOOT_DIR="/boot/firmware"
[[ -d "${BOOT_DIR}" ]] || BOOT_DIR="/boot"
CONFIG_TXT="${BOOT_DIR}/config.txt"
CMDLINE_TXT="${BOOT_DIR}/cmdline.txt"

[[ -f "${CONFIG_TXT}" ]] || { warn "No ${CONFIG_TXT}; skipping boot tuning"; exit 0; }

backup_once() {
  local f="$1"
  [[ -f "${f}.pistreamer.bak" ]] || cp -a "${f}" "${f}.pistreamer.bak"
}

# --- config.txt ------------------------------------------------------------
backup_once "${CONFIG_TXT}"

MARKER="# --- pi-streamer ---"

# Migration: earlier versions of this script set hdmi_force_hotplug=1, which
# costs you EDID and pins the output at 640x480 when no EDID is readable.
if grep -qE '^[[:space:]]*hdmi_force_hotplug=1' "${CONFIG_TXT}"; then
  sed -i 's/^[[:space:]]*hdmi_force_hotplug=1/#hdmi_force_hotplug=1  # disabled by pi-streamer: costs EDID, forces 640x480/' \
    "${CONFIG_TXT}"
  warn "commented out hdmi_force_hotplug=1 (it can force the output to 640x480)"
  warn "reboot required before the full mode list comes back"
fi

if grep -qF "${MARKER}" "${CONFIG_TXT}"; then
  info "config.txt already tuned"
else
  info "Appending pi-streamer settings to config.txt"
  cat >> "${CONFIG_TXT}" <<'EOF'

# --- pi-streamer ---
# Full KMS driver: required for kmssink and mpv's DRM output.
dtoverlay=vc4-kms-v3d
max_framebuffers=2

# NOTE: hdmi_force_hotplug=1 is deliberately NOT set here. It forces the
# connector to report "connected" even with no EDID, and the fallback mode
# in that case is 640x480 — which silently caps the whole node at VGA and
# makes kmssink fail to match any sensible mode. Under the KMS driver the
# correct way to pin an output that may boot with the display off is a
# kernel cmdline entry in cmdline.txt instead, e.g.
#   video=HDMI-A-1:1920x1080@60D
# (the trailing D forces the connector enabled). Add that only if you need it.

# No rainbow splash, no boot delay — this is an appliance.
disable_splash=1
boot_delay=0

# Disable the on-board activity/power LEDs (uncomment for dark installs).
#dtparam=act_led_trigger=none
#dtparam=act_led_activelow=off
EOF
  ok "config.txt updated"
fi

# --- cmdline.txt -----------------------------------------------------------
if [[ -f "${CMDLINE_TXT}" ]]; then
  backup_once "${CMDLINE_TXT}"
  CMDLINE="$(tr -d '\n' < "${CMDLINE_TXT}")"
  ADDED=""
  # Move the kernel console off tty1. Otherwise boot messages and the login
  # prompt sit on the HDMI output underneath the video, and the getty and the
  # player both want the same screen.
  if grep -q "console=tty1" <<<"${CMDLINE}"; then
    CMDLINE="${CMDLINE//console=tty1/console=tty3}"
    ADDED="${ADDED} console=tty3"
  fi
  # consoleblank=0 stops the console blanking after 10 minutes and taking the
  # HDMI output with it. logo.nologo + cursor off keep the screen clean before
  # the player takes over.
  for opt in "consoleblank=0" "logo.nologo" "vt.global_cursor_default=0"; do
    key="${opt%%=*}"
    if ! grep -qE "(^| )${key}(=|\$| )" <<<"${CMDLINE}"; then
      CMDLINE="${CMDLINE} ${opt}"
      ADDED="${ADDED} ${opt}"
    fi
  done
  if [[ -n "${ADDED}" ]]; then
    printf '%s\n' "${CMDLINE}" > "${CMDLINE_TXT}"
    ok "cmdline.txt updated:${ADDED}"
  else
    info "cmdline.txt already tuned"
  fi
fi

# --- give the player sole ownership of the screen --------------------------
# The login prompt on tty1 shares the HDMI output with the video and shows
# through as a terminal behind the picture. This is an appliance; log in over
# SSH instead. Reverse with:
#   sudo systemctl unmask getty@tty1 && sudo systemctl enable --now getty@tty1
if systemctl list-unit-files 'getty@.service' >/dev/null 2>&1; then
  systemctl stop getty@tty1.service >/dev/null 2>&1 || true
  systemctl mask getty@tty1.service >/dev/null 2>&1 || true
  ok "getty on tty1 masked (SSH in instead; see this script to reverse)"
fi

# --- trim the image --------------------------------------------------------
# Nothing here is fatal if the unit does not exist.
for unit in triggerhappy.service man-db.timer apt-daily.timer apt-daily-upgrade.timer; do
  if systemctl list-unit-files "${unit}" >/dev/null 2>&1 \
     && systemctl is-enabled --quiet "${unit}" 2>/dev/null; then
    systemctl disable --now "${unit}" >/dev/null 2>&1 || true
    info "disabled ${unit}"
  fi
done

# Reduce SD card wear: journald to RAM with a small cap. The node's real logs
# are the pipeline log in the GUI; the journal only needs to survive a session.
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/99-pistreamer.conf <<'EOF'
# Keep the journal in RAM to reduce SD card write wear on an appliance node.
[Journal]
Storage=volatile
RuntimeMaxUse=32M
EOF
systemctl restart systemd-journald || true
ok "journald set to volatile storage"

warn "Boot changes take effect after a reboot."
