#!/usr/bin/env bash
set -euo pipefail

already_built="${1:-}"
declared=$(nix eval --json .#checks --apply 'checks: builtins.filter (system: builtins.getAttr system checks != { }) (builtins.attrNames checks)')
runners=$(jq -c --arg already_built "$already_built" '
  { "x86_64-linux": "ubuntu-latest", "aarch64-linux": "ubuntu-24.04-arm" } as $runner_for
  | map(select(. != $already_built) | $runner_for[.] // error("declared-runners: no GitHub runner builds \(.)"))
' <<<"$declared")
if [ -z "$already_built" ] && [ "$runners" = "[]" ]; then
  echo "declared-runners: the flake declares checks on no system, so nothing would verify it" >&2
  exit 1
fi
printf '%s\n' "$runners"
