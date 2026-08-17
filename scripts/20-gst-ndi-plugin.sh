#!/usr/bin/env bash
#
# Build and install the GStreamer NDI plugin (teltek/gst-plugin-ndi, Rust).
#
# This provides ndisrc / ndisrcdemux and the NDI device provider used for
# source discovery. Expect 10-25 minutes on a Pi 4 for the first build.
#
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

GST_NDI_REPO="${GST_NDI_REPO:-https://github.com/teltek/gst-plugin-ndi.git}"
GST_NDI_REF="${GST_NDI_REF:-master}"
BUILD_DIR="/usr/local/src/gst-plugin-ndi"
PLUGIN_SO="${PISTREAMER_GST_PLUGIN_DIR}/libgstndi.so"

export GST_PLUGIN_PATH="${PISTREAMER_GST_PLUGIN_DIR}"

if [[ -f "${PLUGIN_SO}" ]] && gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  ok "ndisrc already available"
  exit 0
fi

# --- toolchain -------------------------------------------------------------
if command -v cargo >/dev/null 2>&1; then
  info "Using existing cargo: $(cargo --version)"
else
  if apt-cache show cargo >/dev/null 2>&1; then
    apt_install cargo rustc
  fi
fi

if ! command -v cargo >/dev/null 2>&1; then
  info "Installing the Rust toolchain via rustup (this takes a few minutes)"
  export RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
  curl -fsSL --proto '=https' --tlsv1.2 https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --profile minimal
  export PATH="/usr/local/cargo/bin:${PATH}"
fi
command -v cargo >/dev/null 2>&1 || die "cargo unavailable; cannot build the NDI plugin"

# --- source ----------------------------------------------------------------
if [[ -d "${BUILD_DIR}/.git" ]]; then
  info "Updating existing checkout"
  git -C "${BUILD_DIR}" fetch --depth 1 origin "${GST_NDI_REF}"
  git -C "${BUILD_DIR}" checkout -q FETCH_HEAD
else
  install -d "$(dirname "${BUILD_DIR}")"
  rm -rf "${BUILD_DIR}"
  info "Cloning ${GST_NDI_REPO} (${GST_NDI_REF})"
  git clone --depth 1 --branch "${GST_NDI_REF}" "${GST_NDI_REPO}" "${BUILD_DIR}"
fi

# --- build -----------------------------------------------------------------
banner "Compiling gst-plugin-ndi (be patient — 10-25 min on a Pi 4)"
export LIBRARY_PATH="${NDI_LIB_DIR}:${LIBRARY_PATH:-}"
export LD_LIBRARY_PATH="${NDI_LIB_DIR}:${LD_LIBRARY_PATH:-}"
export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
# A Pi 4 has 4 cores but limited RAM; -j4 with LTO can OOM on a 2GB board.
JOBS="$(nproc)"
if [[ "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" -lt 3000 ]]; then
  JOBS=2
  info "Low memory detected — limiting to ${JOBS} build jobs"
fi

# The compressed (hardware-decode) colour formats are gated behind the
# advanced-sdk cargo feature, which needs the Advanced SDK headers present.
CARGO_FEATURE_ARGS=()
if [[ -f /usr/local/include/Processing.NDI.compressed.v5.h ]]; then
  CARGO_FEATURE_ARGS=(--features advanced-sdk)
  ok "Advanced SDK headers found — building with hardware decode support"
else
  info "Standard SDK only; building without compressed receive support."
  info "For hardware decode see the note in scripts/10-ndi-sdk.sh."
fi

( cd "${BUILD_DIR}" && cargo build --release --jobs "${JOBS}" "${CARGO_FEATURE_ARGS[@]}" )

BUILT="$(find "${BUILD_DIR}/target/release" -maxdepth 1 -name 'libgstndi.so' | head -n1)"
[[ -n "${BUILT}" ]] || die "build finished but libgstndi.so was not produced"

install -d "${PISTREAMER_GST_PLUGIN_DIR}"
install -m 0644 "${BUILT}" "${PLUGIN_SO}"
ok "Installed ${PLUGIN_SO}"

# --- verify ----------------------------------------------------------------
if GST_PLUGIN_PATH="${PISTREAMER_GST_PLUGIN_DIR}" gst-inspect-1.0 ndisrc >/dev/null 2>&1; then
  ok "ndisrc registered with GStreamer"
else
  warn "gst-inspect-1.0 could not load ndisrc."
  warn "Check the linkage:  ldd ${PLUGIN_SO} | grep -i ndi"
  warn "and the registry:   GST_DEBUG=GST_PLUGIN_LOADING:5 gst-inspect-1.0 ndisrc"
  die "NDI plugin installed but not loadable"
fi
