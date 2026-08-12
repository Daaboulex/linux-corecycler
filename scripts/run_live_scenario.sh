#!/usr/bin/env bash
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${XDG_RUNTIME_DIR:-/tmp}"
RESULT="${RESULT:-$RUNTIME/cc-result.json}"

OUT="$(cd "$REPO" && NIXPKGS_ALLOW_UNFREE=1 nix build .#full --impure --no-link --print-out-paths 2>/dev/null)"
if [ -z "$OUT" ]; then
  echo '{"verdict":"ERROR","reason":"could not build .#full"}' >"$RESULT"
  exit 1
fi

cleanup() {
  pkill -9 -f "lib/y-cruncher" 2>/dev/null
  pkill -9 -f "mprime -t" 2>/dev/null
  pkill -9 -f "stressapptest -W" 2>/dev/null
  pkill -9 -f "scripts/live_scenarios" 2>/dev/null
  pkill -9 -f "Xvfb :" 2>/dev/null
}

if pgrep -x mprime >/dev/null || pgrep -f "lib/y-cruncher" >/dev/null; then
  echo '{"verdict":"ABORT","reason":"a stress process is already running"}' >"$RESULT"
  exit 0
fi

cleanup
sleep 1
rm -rf $RUNTIME/cc-live
mkdir -p $RUNTIME/cc-live

mp=$(nix-store -qR "$OUT" | grep -m1 -- "-mprime-31")
yc=$(nix-store -qR "$OUT" | grep -m1 -- "-y-cruncher-")
sng=$(nix-store -qR "$OUT" | grep -m1 -- "-stress-ng-")
sat=$(nix-store -qR "$OUT" | grep -m1 -- "-stressapptest-")

export HOME=$RUNTIME/cc-live
export CORECYCLER_MPRIME_BIN="$mp/bin/mprime"
export CORECYCLER_Y_CRUNCHER_BIN="$yc/bin/y-cruncher"
export PATH="$sng/bin:$sat/bin:$PATH"

cd "$REPO" || exit 1
rm -f "$RESULT"
timeout 260 nix run nixpkgs#xvfb-run -- -a \
  nix develop .#packages.x86_64-linux.full -c \
  python3 scripts/live_scenarios.py "$@" >"$RESULT" 2>$RUNTIME/cc-scenario.err
rc=$?
if [ ! -s "$RESULT" ] || ! head -c1 "$RESULT" | grep -q '{'; then
  echo "{\"verdict\":\"ERROR\",\"rc\":$rc,\"stderr_tail\":\"$(tail -c 300 $RUNTIME/cc-scenario.err | tr '\n' ' ' | sed 's/"/\x27/g')\"}" >"$RESULT"
fi
cleanup
sleep 1
