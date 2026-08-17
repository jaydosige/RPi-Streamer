#!/usr/bin/env bash
#
# Take a Raspberry Pi from a freshly-flashed card to a working, named,
# group-joined pi-streamer node in one command.
#
# install.sh turns a Pi into a node. This does the things around it that are
# easy to forget and awkward to notice you have forgotten: fetching the code in
# the first place, giving the unit a unique name and identity, and joining it to
# the right group with the right key. Getting any of those wrong on unit three
# of six is how a show day goes sideways.
#
#   sudo ./scripts/bootstrap.sh --name STAGE-LEFT --group wall --key 's3cret'
#
# Every option is optional; with none of them it installs and leaves the
# identity and group settings alone. Idempotent — safe to re-run, and re-running
# is how you update a node.
#
# Options:
#   --name NAME         hostname and display name for this node (e.g. STAGE-LEFT)
#   --group NAME        cluster group; nodes only see others in the same group
#   --key SECRET        shared group key; must match on every node
#   --repo URL          where to clone from (default: the Live Wire repo)
#   --branch NAME       branch to use (default: main)
#   --dir PATH          where the working copy lives (default: ~/RPi-Streamer)
#   --from-archive FILE install from a tarball instead of git (no network needed)
#   --fresh-identity    regenerate machine-id: use this on a CLONED SD card
#   --skip-install      set up identity and group only, do not run install.sh
#   --dry-run           print what would happen and change nothing
#
set -euo pipefail

REPO_URL="https://github.com/jaydosige/RPi-Streamer.git"
BRANCH="main"
NODE_NAME=""
GROUP=""
KEY=""
ARCHIVE=""
TARGET_DIR=""
FRESH_IDENTITY=0
SKIP_INSTALL=0
DRY_RUN=0

# --- output helpers ---------------------------------------------------------
# Deliberately self-contained: this script has to work before the repo exists,
# so it cannot source scripts/common.sh.
if [[ -t 1 ]]; then
  B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; Z=$'\033[0m'
else
  B=""; D=""; G=""; Y=""; R=""; Z=""
fi
usage() {
  # Written out rather than sed-ed out of the comment header: a line range
  # silently drifts every time the header changes, and the first version of
  # this printed a line of code as if it were documentation.
  cat <<'EOF'
Take a Raspberry Pi from a freshly-flashed card to a working, named,
group-joined pi-streamer node in one command. Idempotent: re-running is also
how you update a node.

  sudo ./scripts/bootstrap.sh --name STAGE-LEFT --group wall --key 's3cret'

  --name NAME         hostname and display name for this node (e.g. STAGE-LEFT)
  --group NAME        cluster group; nodes only see others in the same group
  --key SECRET        shared group key; must match on every node
  --repo URL          where to clone from (default: the Live Wire repo)
  --branch NAME       branch to use (default: main)
  --dir PATH          where the working copy lives (default: ~/RPi-Streamer)
  --from-archive FILE install from a tarball instead of git (no network needed)
  --fresh-identity    regenerate machine-id: use this on a CLONED SD card
  --skip-install      set up identity and group only, do not run install.sh
  --dry-run           print what would happen and change nothing

Environment knobs are passed through to install.sh, so NDI_SDK_TARBALL and
SKIP_BOOT_TUNING still work.
EOF
}

say()  { printf '\n%s==> %s%s\n' "${B}" "$*" "${Z}"; }
info() { printf '%s  - %s%s\n' "${D}" "$*" "${Z}"; }
ok()   { printf '%s  ✓ %s%s\n' "${G}" "$*" "${Z}"; }
warn() { printf '%s  ! %s%s\n' "${Y}" "$*" "${Z}" >&2; }
die()  { printf '%s  ✗ %s%s\n' "${R}" "$*" "${Z}" >&2; exit 1; }
run()  {
  if [[ "${DRY_RUN}" == "1" ]]; then
    printf '%s  would run: %s%s\n' "${D}" "$*" "${Z}"
  else
    "$@"
  fi
}

# A bootstrap that dies halfway leaves a node that is named but not installed,
# or installed but not in the group — and with set -e the exit is silent. Say
# where it stopped and what state the node is in, because the alternative is
# discovering it during a show.
STAGE="starting"
on_error() {
  local code=$?
  printf '\n%s  ✗ bootstrap failed during: %s (exit %d)%s\n' "${R}" "${STAGE}" "${code}" "${Z}" >&2
  printf '%s    The node may be partly configured. Fix the cause and re-run this\n' "${R}" >&2
  printf '%s    script — it is idempotent, so re-running is safe.%s\n' "${R}" "${Z}" >&2
  exit "${code}"
}
trap on_error ERR

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)           NODE_NAME="${2:?--name needs a value}"; shift 2 ;;
    --group)          GROUP="${2:?--group needs a value}"; shift 2 ;;
    --key)            KEY="${2:?--key needs a value}"; shift 2 ;;
    --repo)           REPO_URL="${2:?--repo needs a value}"; shift 2 ;;
    --branch)         BRANCH="${2:?--branch needs a value}"; shift 2 ;;
    --dir)            TARGET_DIR="${2:?--dir needs a value}"; shift 2 ;;
    --from-archive)   ARCHIVE="${2:?--from-archive needs a value}"; shift 2 ;;
    --fresh-identity) FRESH_IDENTITY=1; shift ;;
    --skip-install)   SKIP_INSTALL=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown option: $1 (try --help)" ;;
  esac
done

[[ "${EUID}" -eq 0 || "${DRY_RUN}" == "1" ]] \
  || die "Run this as root: sudo $0 $*"

# Work out who the real user is. Everything under their home has to end up
# owned by them, not root, or the next git pull fails with permission errors —
# which is a confusing way to find out you ran an installer as root.
REAL_USER="${SUDO_USER:-${USER:-root}}"
REAL_HOME="$(getent passwd "${REAL_USER}" 2>/dev/null | cut -d: -f6)"
REAL_HOME="${REAL_HOME:-/root}"
TARGET_DIR="${TARGET_DIR:-${REAL_HOME}/RPi-Streamer}"

# A hostname is not a free-text field: it goes into DNS and mDNS, so it must be
# letters, digits and hyphens only. Rejecting a bad one here is much kinder than
# a node that installs fine and is then unreachable by name.
if [[ -n "${NODE_NAME}" ]]; then
  [[ "${NODE_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,62}$ ]] \
    || die "invalid name '${NODE_NAME}': use letters, digits and hyphens (no spaces or underscores)"
fi

say "pi-streamer bootstrap"
info "Node name:  ${NODE_NAME:-(unchanged: $(hostname))}"
info "Group:      ${GROUP:-(unchanged)}"
info "Key:        $(if [[ -n "${KEY}" ]]; then echo "set (${#KEY} characters)"; else echo "(unchanged)"; fi)"
info "Source:     ${ARCHIVE:-${REPO_URL} (${BRANCH})}"
info "Directory:  ${TARGET_DIR}"
info "Running as: ${REAL_USER}"
[[ "${DRY_RUN}" == "1" ]] && warn "Dry run: nothing will be changed."

if [[ -z "${KEY}" && ! -f /etc/pistreamer/config.json ]]; then
  warn "No --key given, so this node will use the default group key. That is"
  warn "not a secret: anything on the network can command the group. Set one"
  warn "with --key, or in the GUI under Nodes, before using this at a job."
fi

# --- 1. identity, before anything else -------------------------------------
# A cloned card carries the original's machine-id and hostname. Node identity
# now prefers the board serial, so discovery survives it, but two nodes called
# the same thing still collide on <hostname>.local and are impossible to tell
# apart in a list.
if [[ "${FRESH_IDENTITY}" == "1" ]]; then
  STAGE="regenerating machine-id"
  say "Fresh identity"
  info "Regenerating machine-id (this card was cloned from another node)"
  run rm -f /etc/machine-id /var/lib/dbus/machine-id
  if command -v systemd-machine-id-setup >/dev/null 2>&1; then
    run systemd-machine-id-setup
  fi
  # dbus historically kept its own copy; a symlink keeps them in step.
  run ln -sf /etc/machine-id /var/lib/dbus/machine-id
  ok "machine-id regenerated"
fi

if [[ -n "${NODE_NAME}" ]]; then
  STAGE="setting the hostname"
  say "Naming this node"
  CURRENT="$(hostname)"
  if [[ "${CURRENT}" == "${NODE_NAME}" ]]; then
    ok "already named ${NODE_NAME}"
  else
    # hostnamectl is preferred because it updates the running kernel hostname
    # as well as the file, but it needs a working systemd bus and it is not
    # worth abandoning the whole bootstrap over. Writing the file always works;
    # it just needs a reboot, which this script recommends anyway.
    if [[ "${DRY_RUN}" == "1" ]]; then
      info "would set the hostname to ${NODE_NAME}"
    elif command -v hostnamectl >/dev/null 2>&1 \
         && hostnamectl set-hostname "${NODE_NAME}" 2>/dev/null; then
      info "hostname set via hostnamectl"
    else
      printf '%s\n' "${NODE_NAME}" > /etc/hostname
      hostname "${NODE_NAME}" 2>/dev/null || true
      warn "hostnamectl was unavailable; wrote /etc/hostname instead (takes effect on reboot)"
    fi
    # /etc/hosts has to agree, or sudo prints "unable to resolve host" on every
    # single command from here on and everything feels broken.
    if [[ "${DRY_RUN}" != "1" ]]; then
      if grep -qE "^127\.0\.1\.1" /etc/hosts; then
        sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${NODE_NAME}/" /etc/hosts
      else
        printf '127.0.1.1\t%s\n' "${NODE_NAME}" >> /etc/hosts
      fi
    else
      info "would point 127.0.1.1 at ${NODE_NAME} in /etc/hosts"
    fi
    ok "hostname set to ${NODE_NAME} (was ${CURRENT})"
  fi
fi

# --- 2. git, and the code ---------------------------------------------------
STAGE="fetching the code"
say "Fetching the code"
if [[ -n "${ARCHIVE}" ]]; then
  [[ -f "${ARCHIVE}" ]] || die "no such archive: ${ARCHIVE}"
  info "Installing from ${ARCHIVE}"
  run mkdir -p "${TARGET_DIR}"
  # --strip-components=1 drops the wrapper directory inside the tarball so the
  # contents land in TARGET_DIR itself rather than a level down.
  run tar -xzf "${ARCHIVE}" -C "${TARGET_DIR}" --strip-components=1
  run chown -R "${REAL_USER}:${REAL_USER}" "${TARGET_DIR}"
  ok "unpacked into ${TARGET_DIR}"
else
  if ! command -v git >/dev/null 2>&1; then
    info "Installing git"
    run apt-get update -qq
    run apt-get install -y -qq git ca-certificates
    ok "git installed"
  else
    ok "git already present"
  fi

  if [[ -d "${TARGET_DIR}/.git" ]]; then
    info "Updating the existing working copy"
    # Run git as the owning user: doing it as root leaves root-owned objects
    # behind and the user's next pull fails. Also don't assume the remote is
    # called origin — this repo's remote is named differently on some machines.
    REMOTE="$(sudo -u "${REAL_USER}" git -C "${TARGET_DIR}" remote | head -n1)"
    REMOTE="${REMOTE:-origin}"
    info "Remote is '${REMOTE}'"
    # Local edits from a previous hand-fix would block the pull; park them
    # rather than discarding anything.
    run sudo -u "${REAL_USER}" git -C "${TARGET_DIR}" stash --include-untracked
    run sudo -u "${REAL_USER}" git -C "${TARGET_DIR}" fetch "${REMOTE}" "${BRANCH}"
    run sudo -u "${REAL_USER}" git -C "${TARGET_DIR}" checkout "${BRANCH}"
    run sudo -u "${REAL_USER}" git -C "${TARGET_DIR}" reset --hard "${REMOTE}/${BRANCH}"
    ok "updated to the latest ${BRANCH}"
  else
    info "Cloning ${REPO_URL}"
    if [[ "${DRY_RUN}" != "1" ]]; then
      sudo -u "${REAL_USER}" git clone --branch "${BRANCH}" "${REPO_URL}" "${TARGET_DIR}" || die \
        "clone failed. If the repository is private, either use a token in the URL
     (--repo https://USER:TOKEN@github.com/jaydosige/RPi-Streamer.git) or copy
     a release tarball over with scp and use --from-archive instead."
    else
      info "would clone as ${REAL_USER}"
    fi
    ok "cloned into ${TARGET_DIR}"
  fi
fi

# The executable bit has been lost in transit more than once, and the symptom —
# "sudo: ./install.sh: command not found" — sends you looking in the wrong place
# entirely. Fix it here rather than trusting the transport.
run chmod +x "${TARGET_DIR}/install.sh" "${TARGET_DIR}"/scripts/*.sh \
  "${TARGET_DIR}/scripts/pistreamer-overclock" "${TARGET_DIR}/scripts/pistreamer-tuning"

# --- 3. install -------------------------------------------------------------
if [[ "${SKIP_INSTALL}" == "1" ]]; then
  warn "Skipping install.sh (--skip-install)"
else
  STAGE="running install.sh"
  say "Running the installer"
  info "First run on a new Pi takes 20-40 minutes: most of it is compiling the"
  info "Rust NDI plugin. Re-runs are quick."
  # Pass the environment through so NDI_SDK_TARBALL and friends still work.
  run bash -c "cd '${TARGET_DIR}' && ./install.sh"
fi

# --- 4. group settings ------------------------------------------------------
# Written after install.sh, because that is what creates the config file. Edited
# with python rather than sed so the JSON cannot be corrupted by a stray
# character in a key, and so unknown keys are preserved.
if [[ -n "${NODE_NAME}" || -n "${GROUP}" || -n "${KEY}" ]]; then
  STAGE="writing group settings"
  say "Group settings"
  CONFIG="/etc/pistreamer/config.json"
  if [[ "${DRY_RUN}" == "1" ]]; then
    info "would set device_name/cluster_group/cluster_key in ${CONFIG}"
  elif [[ ! -f "${CONFIG}" ]]; then
    warn "${CONFIG} does not exist yet, so there is nothing to configure."
    warn "Run without --skip-install, or set these in the GUI under Nodes."
  else
    NODE_NAME="${NODE_NAME}" GROUP="${GROUP}" KEY="${KEY}" CONFIG="${CONFIG}" \
    python3 - <<'PY'
import json
import os

path = os.environ["CONFIG"]
with open(path) as fh:
    cfg = json.load(fh)

changes = []
for key, value in (("device_name", os.environ["NODE_NAME"]),
                   ("cluster_group", os.environ["GROUP"]),
                   ("cluster_key", os.environ["KEY"])):
    if value:
        cfg[key] = value
        changes.append(key)
cfg["cluster_enabled"] = True

tmp = path + ".bootstrap.tmp"
with open(tmp, "w") as fh:
    json.dump(cfg, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
print("  updated: " + ", ".join(changes))
PY
    # The service reads config at startup, so it has to be told.
    run systemctl restart pistreamer || warn "could not restart pistreamer"
    ok "group settings applied"
  fi
fi

# --- 5. what to do next -----------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
  say "Dry run complete — nothing was changed"
  exit 0
fi

STAGE="printing the summary"
# `|| true` is load-bearing: if `ip` is missing the command substitution exits
# 127, set -e trips, and the error trap declares the bootstrap failed *after*
# everything actually succeeded. Nothing in a summary should be able to do that.
IP="$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"
say "Bootstrap complete"
cat <<EOF

  Node:      $(hostname)
  Web GUI:   http://${IP:-<this-pi>}/
             http://$(hostname).local/

  Check it:  systemctl status pistreamer
             journalctl -u pistreamer -f
             curl -s localhost/api/cluster | python3 -m json.tool | head -20

  Reboot now: the boot tuning, the group memberships and the hidden console
  only take effect after one.

      sudo reboot

EOF
