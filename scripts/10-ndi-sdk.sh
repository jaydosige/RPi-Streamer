#!/usr/bin/env bash
#
# Install the NDI SDK runtime library for aarch64.
#
# The NDI SDK is licensed software from Vizrt and cannot be redistributed, so
# this script fetches the official installer and runs it. If the automatic
# download fails (the URL moves between SDK releases), download the Linux SDK
# yourself from https://ndi.video/for-developers/ndi-sdk/ and re-run with:
#
#   sudo NDI_SDK_TARBALL=/home/pi/Install_NDI_SDK_v6_Linux.tar.gz ./install.sh
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

NDI_VERSION="${NDI_VERSION:-6}"
NDI_URL="${NDI_URL:-https://downloads.ndi.tv/SDK/NDI_SDK_Linux/Install_NDI_SDK_v${NDI_VERSION}_Linux.tar.gz}"

if ls "${NDI_LIB_DIR}"/libndi.so.* >/dev/null 2>&1; then
  ok "NDI runtime already present: $(ls "${NDI_LIB_DIR}"/libndi.so.* | head -n1)"
  exit 0
fi

WORK="$(mktemp -d)"
# shellcheck disable=SC2064  # expand WORK now, not at trap time
trap "rm -rf '${WORK}'" EXIT

TARBALL=""
if [[ -n "${NDI_SDK_TARBALL:-}" ]]; then
  [[ -f "${NDI_SDK_TARBALL}" ]] || die "NDI_SDK_TARBALL does not exist: ${NDI_SDK_TARBALL}"
  TARBALL="${NDI_SDK_TARBALL}"
  info "Using local SDK: ${TARBALL}"
else
  info "Downloading NDI SDK v${NDI_VERSION} for Linux"
  info "  ${NDI_URL}"
  if curl -fsSL --retry 3 --connect-timeout 20 -o "${WORK}/ndi.tar.gz" "${NDI_URL}"; then
    TARBALL="${WORK}/ndi.tar.gz"
  else
    warn "Automatic download failed."
    warn "Download the Linux SDK from https://ndi.video/for-developers/ndi-sdk/"
    warn "and re-run:  sudo NDI_SDK_TARBALL=/path/to/Install_NDI_SDK_v6_Linux.tar.gz ./install.sh"
    die "NDI SDK not available"
  fi
fi

info "Extracting"
tar -xzf "${TARBALL}" -C "${WORK}"

INSTALLER="$(find "${WORK}" -maxdepth 2 -name 'Install_NDI_SDK*.sh' | head -n1)"
[[ -n "${INSTALLER}" ]] || die "no Install_NDI_SDK*.sh found inside the tarball"

banner "NDI SDK licence"
cat <<'EOF'
  The NDI SDK is distributed by Vizrt under its own licence agreement.
  The installer below will present that agreement. By continuing you accept it.
  NDI is a registered trademark of Vizrt NDI AB.
EOF

info "Running the SDK installer"
chmod +x "${INSTALLER}"
# The installer pages the licence then prompts for acceptance. Feed it a
# stream of 'y' and let `yes` die on SIGPIPE when it stops reading.
INSTALLER_DIR="$(dirname "${INSTALLER}")"
INSTALLER_NAME="$(basename "${INSTALLER}")"
( cd "${INSTALLER_DIR}" && yes | env PAGER=cat "./${INSTALLER_NAME}" >/dev/null ) || true

SDK_ROOT="$(find "${WORK}" -maxdepth 2 -type d -name 'NDI SDK for Linux' | head -n1)"
[[ -n "${SDK_ROOT}" ]] || die "installer did not produce an SDK directory (licence declined?)"
info "SDK unpacked at: ${SDK_ROOT}"

# The SDK ships one lib dir per target triple; the Pi 4 64-bit one is
# aarch64-rpi4-linux-gnueabi (naming is a Vizrt quirk — it is aarch64, not eabi).
LIB_SRC="$(find "${SDK_ROOT}/lib" -maxdepth 1 -type d -name 'aarch64*' | head -n1)"
if [[ -z "${LIB_SRC}" ]]; then
  warn "Available lib directories:"
  ls -1 "${SDK_ROOT}/lib" >&2 || true
  die "no aarch64 library directory in the SDK"
fi
info "Library source: ${LIB_SRC}"

install -d "${NDI_LIB_DIR}"
cp -av "${LIB_SRC}"/libndi.so* "${NDI_LIB_DIR}/" >/dev/null

# Headers, needed to build gst-plugin-ndi.
install -d /usr/local/include
cp -a "${SDK_ROOT}/include/"* /usr/local/include/ 2>/dev/null || true

echo "${NDI_LIB_DIR}" > /etc/ld.so.conf.d/ndi.conf
ldconfig

if ldconfig -p | grep -q libndi; then
  ok "NDI runtime installed: $(ldconfig -p | grep libndi | head -n1 | awk '{print $NF}')"
else
  die "libndi did not register with ldconfig"
fi
