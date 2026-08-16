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

# --- systemd ---------------------------------------------------------------
install -m 0644 "${REPO_DIR}/systemd/pistreamer.service" /etc/systemd/system/pistreamer.service
systemctl daemon-reload
systemctl enable pistreamer >/dev/null
systemctl restart pistreamer
ok "Service enabled and started"

sleep 2
if systemctl is-active --quiet pistreamer; then
  ok "pistreamer is running"
else
  warn "pistreamer failed to start. Recent log:"
  journalctl -u pistreamer -n 30 --no-pager >&2 || true
  die "service did not start"
fi
