#!/usr/bin/env bash
# Install the pistreamer service user, virtualenv, application and systemd unit.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# --- service user ----------------------------------------------------------
if ! id -u "${PISTREAMER_USER}" >/dev/null 2>&1; then
  info "Creating service user ${PISTREAMER_USER}"
  useradd --system --home-dir "${PISTREAMER_HOME}" --shell /usr/sbin/nologin "${PISTREAMER_USER}"
fi
# video/render: DRM master + V4L2 M2M hardware decode. audio: ALSA.
for grp in video render audio tty input; do
  getent group "${grp}" >/dev/null 2>&1 && usermod -aG "${grp}" "${PISTREAMER_USER}"
done
ok "Service user ready"

# --- directories -----------------------------------------------------------
install -d -o root -g root -m 0755 "${PISTREAMER_HOME}"
install -d -o "${PISTREAMER_USER}" -g "${PISTREAMER_USER}" -m 0755 "${PISTREAMER_CONFIG_DIR}"
install -d -o "${PISTREAMER_USER}" -g "${PISTREAMER_USER}" -m 0755 "${PISTREAMER_STATE_DIR}"
install -d -o "${PISTREAMER_USER}" -g "${PISTREAMER_USER}" -m 0755 "${PISTREAMER_STATE_DIR}/media"
# libndi reads $HOME/.ndi/ndi-config.v1.json; the unit sets HOME here so the
# service can write it without being root.
install -d -o "${PISTREAMER_USER}" -g "${PISTREAMER_USER}" -m 0755 "${PISTREAMER_STATE_DIR}/.ndi"
install -d -o root -g root -m 0755 "${PISTREAMER_GST_PLUGIN_DIR}"

# --- python venv -----------------------------------------------------------
VENV="${PISTREAMER_HOME}/venv"
if [[ ! -x "${VENV}/bin/python" ]]; then
  info "Creating virtualenv (with system site-packages, for python3-gi)"
  python3 -m venv --system-site-packages "${VENV}"
fi
info "Installing application and dependencies"
"${VENV}/bin/pip" install --quiet --upgrade pip wheel
"${VENV}/bin/pip" install --quiet "${REPO_DIR}"
ok "Application installed into ${VENV}"

# --- default config --------------------------------------------------------
if [[ ! -f "${PISTREAMER_CONFIG_DIR}/config.json" ]]; then
  install -o "${PISTREAMER_USER}" -g "${PISTREAMER_USER}" -m 0644 \
    "${REPO_DIR}/config/pistreamer.default.json" \
    "${PISTREAMER_CONFIG_DIR}/config.json"
  ok "Wrote default config"
else
  info "Keeping existing ${PISTREAMER_CONFIG_DIR}/config.json"
fi

# --- polkit: let the service reboot/shut down without being root -----------
install -d /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-pistreamer.rules <<EOF
// Allow the pistreamer service user to reboot and power off the node from the
// web GUI. Deliberately narrow: no other systemd or logind verbs.
polkit.addRule(function(action, subject) {
  if (subject.user == "${PISTREAMER_USER}" &&
      (action.id == "org.freedesktop.login1.power-off" ||
       action.id == "org.freedesktop.login1.reboot" ||
       action.id == "org.freedesktop.login1.power-off-multiple-sessions" ||
       action.id == "org.freedesktop.login1.reboot-multiple-sessions")) {
    return polkit.Result.YES;
  }
});
EOF
# hostnamectl also goes through polkit.
cat > /etc/polkit-1/rules.d/51-pistreamer-hostname.rules <<EOF
polkit.addRule(function(action, subject) {
  if (subject.user == "${PISTREAMER_USER}" &&
      action.id == "org.freedesktop.hostname1.set-static-hostname") {
    return polkit.Result.YES;
  }
});
EOF
ok "polkit rules installed"

# --- host tuning helper ----------------------------------------------------
install -d "${PISTREAMER_HOME}/bin"
install -m 0755 "${REPO_DIR}/scripts/pistreamer-tuning" "${PISTREAMER_HOME}/bin/pistreamer-tuning"
install -m 0755 "${REPO_DIR}/scripts/pistreamer-overclock" "${PISTREAMER_HOME}/bin/pistreamer-overclock"

# Overclocking edits config.txt on the boot partition, which needs root. The
# service does not run as root, so grant exactly one command and nothing else.
# The helper takes only preset NAMES, never frequencies or voltages, because
# the caller is an unauthenticated web GUI on the event network.
cat > /etc/sudoers.d/pistreamer-overclock <<EOF
${PISTREAMER_USER} ALL=(root) NOPASSWD: ${PISTREAMER_HOME}/bin/pistreamer-overclock
EOF
chmod 0440 /etc/sudoers.d/pistreamer-overclock
if visudo -c -f /etc/sudoers.d/pistreamer-overclock >/dev/null 2>&1; then
  ok "overclock helper installed (sudoers rule limited to that one command)"
else
  rm -f /etc/sudoers.d/pistreamer-overclock
  warn "sudoers rule failed validation and was removed; overclocking will be unavailable"
fi
if [[ ! -f "${PISTREAMER_CONFIG_DIR}/tuning.conf" ]]; then
  cat > "${PISTREAMER_CONFIG_DIR}/tuning.conf" <<'EOF'
# Host tuning applied at boot by pistreamer-tuning.service.
# Change a value and reboot, or: sudo systemctl restart pistreamer-tuning

# CPU frequency policy. "performance" pins the clock, which removes the
# ramp-up lag that shows as frame timing wobble. "none" leaves it alone.
CPU_GOVERNOR=performance

# Kernel socket receive buffer, in MB. Full-bandwidth NDI is a fat stream.
SOCKET_BUFFER_MB=16

# Wi-Fi radio sleeping between beacons costs you a burst of frames every few
# seconds. 1 disables power saving, 0 leaves it as the driver set it.
DISABLE_WIFI_POWERSAVE=1

# --- overclocking -----------------------------------------------------------
# NOT applied from here: overclocking is set in /boot/firmware/config.txt and
# needs a reboot, and doing it without adequate cooling shortens the life of
# the board. It is worth roughly 20-30% more decode headroom on a Pi 4, which
# can be the difference for full-bandwidth NDI.
#
# If you want it, add to /boot/firmware/config.txt and reboot:
#     arm_freq=2000
#     over_voltage=6
#     gpu_freq=750
# Then watch the Diagnostics tab: if "throttling has occurred" appears, or the
# temperature passes 80 C, back it off. A heatsink is the minimum; a fan is
# better. Check for under-voltage first — an inadequate supply plus an
# overclock is how boards become unreliable.
EOF
  ok "Wrote default tuning.conf"
fi

# --- systemd ---------------------------------------------------------------
install -m 0644 "${REPO_DIR}/systemd/pistreamer.service" /etc/systemd/system/pistreamer.service
install -m 0644 "${REPO_DIR}/systemd/pistreamer-tuning.service" \
  /etc/systemd/system/pistreamer-tuning.service
systemctl daemon-reload
systemctl enable pistreamer-tuning >/dev/null
systemctl restart pistreamer-tuning || warn "host tuning reported a problem (not fatal)"
systemctl enable pistreamer >/dev/null
systemctl restart pistreamer
ok "Services enabled and started"

sleep 2
if systemctl is-active --quiet pistreamer; then
  ok "pistreamer is running"
else
  warn "pistreamer failed to start. Recent log:"
  journalctl -u pistreamer -n 30 --no-pager >&2 || true
  die "service did not start"
fi
