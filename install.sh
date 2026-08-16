#!/usr/bin/env bash
#
# pi-streamer installer.
#
# Turns a fresh Raspberry Pi OS Lite (64-bit, Trixie or Bookworm) install into
# an NDI/media playback node. Idempotent: safe to re-run after a git pull.
#
#   sudo ./install.sh
#
# Environment knobs:
#   NDI_SDK_TARBALL=/path/to/Install_NDI_SDK_v6_Linux.tar.gz
#       Use a locally-downloaded SDK instead of fetching it.
#   SKIP_BOOT_TUNING=1
#       Leave /boot/firmware/config.txt and cmdline.txt alone.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_DIR

# shellcheck source=scripts/common.sh
source "${REPO_DIR}/scripts/common.sh"

require_root

banner "pi-streamer installer"
info "Repository: ${REPO_DIR}"
info "Host:       $(uname -m) / $(source /etc/os-release && echo "${PRETTY_NAME}")"

check_platform

run_step "Base packages"        "${REPO_DIR}/scripts/00-base-packages.sh"
run_step "NDI SDK"              "${REPO_DIR}/scripts/10-ndi-sdk.sh"
run_step "GStreamer NDI plugin" "${REPO_DIR}/scripts/20-gst-ndi-plugin.sh"
run_step "Application"          "${REPO_DIR}/scripts/30-app.sh"

if [[ "${SKIP_BOOT_TUNING:-0}" != "1" ]]; then
  run_step "Boot tuning"        "${REPO_DIR}/scripts/40-tune-boot.sh"
else
  warn "Skipping boot tuning (SKIP_BOOT_TUNING=1)"
fi

banner "Done"
IP="$(ip -o -4 addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -n1)"
cat <<EOF

  pi-streamer is installed and enabled.

  Web GUI:   http://${IP:-<this-pi>}/
             http://$(hostname).local/

  Service:   systemctl status pistreamer
  Logs:      journalctl -u pistreamer -f
  Config:    /etc/pistreamer/config.json
  Media:     /var/lib/pistreamer/media

  A reboot is recommended so the boot tuning and group memberships take effect.

EOF
