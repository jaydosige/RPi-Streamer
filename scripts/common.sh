#!/usr/bin/env bash
# Shared helpers for the pi-streamer install scripts.
# Sourced, never executed directly.

set -euo pipefail

if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_BLUE=$'\033[34m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_BLUE=""; C_YELLOW=""; C_RED=""; C_GREEN=""
fi

banner() { printf '\n%s==> %s%s\n' "${C_BOLD}${C_BLUE}" "$*" "${C_RESET}"; }
info()   { printf '%s  - %s%s\n' "${C_DIM}" "$*" "${C_RESET}"; }
ok()     { printf '%s  ✓ %s%s\n' "${C_GREEN}" "$*" "${C_RESET}"; }
warn()   { printf '%s  ! %s%s\n' "${C_YELLOW}" "$*" "${C_RESET}" >&2; }
die()    { printf '%s  ✗ %s%s\n' "${C_RED}" "$*" "${C_RESET}" >&2; exit 1; }

require_root() {
  [[ "${EUID}" -eq 0 ]] || die "Run this as root: sudo ./install.sh"
}

run_step() {
  local title="$1" script="$2"
  banner "${title}"
  [[ -f "${script}" ]] || die "missing script: ${script}"
  bash "${script}"
}

check_platform() {
  local arch
  arch="$(uname -m)"
  if [[ "${arch}" != "aarch64" ]]; then
    warn "Architecture is ${arch}, not aarch64."
    warn "pi-streamer targets 64-bit Raspberry Pi OS. Continuing, but the NDI"
    warn "libraries and hardware decode paths will almost certainly not work."
  fi
  if [[ -r /proc/device-tree/model ]]; then
    local model
    model="$(tr -d '\0' < /proc/device-tree/model)"
    info "Board: ${model}"
    case "${model}" in
      *"Raspberry Pi 4"*|*"Raspberry Pi 5"*|*"Compute Module 4"*) ;;
      *) warn "Untested board. Pi 4B is the reference platform." ;;
    esac
  fi
}

# Install apt packages, skipping any already present, in one transaction.
apt_install() {
  local -a missing=()
  local pkg
  for pkg in "$@"; do
    if ! dpkg-query -W -f='${Status}' "${pkg}" 2>/dev/null | grep -q "ok installed"; then
      missing+=("${pkg}")
    fi
  done
  if [[ ${#missing[@]} -eq 0 ]]; then
    ok "already installed: $*"
    return 0
  fi
  info "installing: ${missing[*]}"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

PISTREAMER_USER="pistreamer"
PISTREAMER_HOME="/opt/pistreamer"
PISTREAMER_CONFIG_DIR="/etc/pistreamer"
PISTREAMER_STATE_DIR="/var/lib/pistreamer"
PISTREAMER_GST_PLUGIN_DIR="${PISTREAMER_HOME}/gst-plugins"
NDI_LIB_DIR="/usr/local/lib"
