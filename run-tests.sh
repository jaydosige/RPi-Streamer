#!/usr/bin/env bash
#
# Run the test suite.
#
#   ./run-tests.sh              # everything
#   ./run-tests.sh cluster doc  # only files whose name matches
#   ./run-tests.sh -q           # one line per file
#
# Exits non-zero if anything failed, which is the whole point: the suite was
# 24 files run by hand, so "did that break something" was answered by whoever
# remembered to ask. A regression reached main that way.
#
# Tests that need something this machine has not got — GStreamer, mpv, a real
# /proc, a writable /etc — skip themselves and count as passes. A test that
# cannot run is not a failure, but it is not evidence either, so skips are
# listed at the end rather than hidden.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  for candidate in ./venv/bin/python /opt/pistreamer/venv/bin/python python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then PYTHON="${candidate}"; break; fi
  done
fi

QUIET=0
PATTERNS=()
for arg in "$@"; do
  case "${arg}" in
    -q|--quiet) QUIET=1 ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) PATTERNS+=("${arg}") ;;
  esac
done

matches() {
  [[ ${#PATTERNS[@]} -eq 0 ]] && return 0
  local name="$1"
  for pattern in "${PATTERNS[@]}"; do
    [[ "${name}" == *"${pattern}"* ]] && return 0
  done
  return 1
}

red=$'\e[31m'; green=$'\e[32m'; yellow=$'\e[33m'; dim=$'\e[2m'; off=$'\e[0m'
[[ -t 1 ]] || { red=""; green=""; yellow=""; dim=""; off=""; }

failed=(); skipped=(); passed=0; started=$(date +%s)

for file in tests/test_*.py; do
  name="$(basename "${file}" .py)"
  matches "${name}" || continue

  output="$("${PYTHON}" "${file}" 2>&1)"
  code=$?
  summary="$(printf '%s\n' "${output}" | grep -oE '^[0-9]+ passed, [0-9]+ failed' | tail -1)"
  skip="$(printf '%s\n' "${output}" | grep -iE '^(skipping|SKIP)' | head -1)"

  if [[ ${code} -ne 0 ]]; then
    failed+=("${name}")
    printf '%s  FAIL%s  %-24s %s\n' "${red}" "${off}" "${name}" "${summary:-exit ${code}}"
    # The output of a failure is the reason to run this at all.
    printf '%s\n' "${output}" | tail -25 | sed 's/^/        /'
  elif [[ -n "${skip}" ]]; then
    skipped+=("${name}: ${skip}")
    [[ ${QUIET} -eq 1 ]] || printf '%s  skip%s  %-24s %s%s%s\n' \
      "${yellow}" "${off}" "${name}" "${dim}" "${skip}" "${off}"
  else
    passed=$((passed + 1))
    [[ ${QUIET} -eq 1 ]] || printf '%s  ok  %s  %-24s %s%s%s\n' \
      "${green}" "${off}" "${name}" "${dim}" "${summary}" "${off}"
  fi
done

elapsed=$(( $(date +%s) - started ))
printf '\n%d passed, %d failed, %d skipped in %ds\n' \
  "${passed}" "${#failed[@]}" "${#skipped[@]}" "${elapsed}"

if [[ ${#skipped[@]} -gt 0 && ${QUIET} -eq 0 ]]; then
  printf '\n%sskipped — not evidence, just absent:%s\n' "${dim}" "${off}"
  printf '  %s\n' "${skipped[@]}"
fi

if [[ ${#failed[@]} -gt 0 ]]; then
  printf '\n%sfailed:%s\n' "${red}" "${off}"
  printf '  %s\n' "${failed[@]}"
  exit 1
fi
exit 0
