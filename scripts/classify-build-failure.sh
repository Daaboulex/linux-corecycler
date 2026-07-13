#!/usr/bin/env bash
set -uo pipefail

# Canonical classify-build-failure — Nix Packaging Standard.
#
# Source of truth: github.com/Daaboulex/nix-packaging-standard, synced into
# each packaging repo's scripts/ by sync.sh — DO NOT edit per-repo copies.
#
# Reads a nix / nix-fast-build log and emits a NAMED failure class plus the
# complete failure inventory, so a red run is a one-glance diagnosis instead
# of a raw log dig, and repeated fix-one-discover-next loops are visible as
# a single enumerated report (builds run with --keep-going).
#
#   classify-build-failure.sh <logfile>
#
# Outputs (GITHUB_OUTPUT or stdout):
#   class=<transient-infra|upstream-rerelease-hash-mismatch|nixpkgs-package-drop|
#          missing-python-dep|requirements-coverage|unclassified>
#   failed_attrs=<nix-fast-build failed attribute list, if present>
#   failed_drvs=<up to 12 failing derivations, deduplicated>
#
# Precedence: the LAST matching class wins — ordered least- to most-
# actionable, so the report names the most specific known cause.

LOG="${1:?usage: classify-build-failure.sh <logfile>}"
OUTPUT_FILE="${GITHUB_OUTPUT:-/dev/stdout}"
out() { echo "$1=$2" >>"$OUTPUT_FILE"; }

[ -r "$LOG" ] || {
  out "class" "unclassified"
  out "failed_attrs" ""
  out "failed_drvs" ""
  exit 0
}

failed_attrs=$(grep -oE 'Failed attributes: .*' "$LOG" | tail -1 | sed 's/Failed attributes: //')
failed_drvs=$(grep -oE "Cannot build '/nix/store/[^']+\.drv'" "$LOG" |
  sed "s|Cannot build '||; s|'\$||" | sort -u | head -12 | tr '\n' ' ')

class=unclassified
grep -qiE "couldn.t resolve host|temporary failure in name resolution|connection reset by peer|status code: (403|429)|operation timed out|service unavailable" "$LOG" && class=transient-infra
grep -q 'hash mismatch in fixed-output' "$LOG" && class=upstream-rerelease-hash-mismatch
grep -qE 'not supported for interpreter python|is marked as broken' "$LOG" && class=nixpkgs-package-drop
grep -q 'ModuleNotFoundError' "$LOG" && class=missing-python-dep
grep -q 'requirements not present in the env' "$LOG" && class=requirements-coverage

out "class" "$class"
out "failed_attrs" "${failed_attrs:-}"
out "failed_drvs" "${failed_drvs% }"
exit 0
