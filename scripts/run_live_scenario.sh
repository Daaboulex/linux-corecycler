#!/usr/bin/env bash
set -uo pipefail

REPO="$HOME/Documents/nix/repos/linux-corecycler"
OUT="/nix/store/7wzxm14n5ah13cm611bsbcv5hb8vi1xm-corecycler-full-0.0.1"
RESULT="${RESULT:-/run/user/1000/cc-result.json}"

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
rm -rf /run/user/1000/cc-live
mkdir -p /run/user/1000/cc-live

mp=$(nix-store -qR "$OUT" | grep -m1 -- "-mprime-31")
yc=$(nix-store -qR "$OUT" | grep -m1 -- "-y-cruncher-")
sng=$(nix-store -qR "$OUT" | grep -m1 -- "-stress-ng-")
sat=$(nix-store -qR "$OUT" | grep -m1 -- "-stressapptest-")

export HOME=/run/user/1000/cc-live
export CORECYCLER_MPRIME_BIN="$mp/bin/mprime"
export CORECYCLER_Y_CRUNCHER_BIN="$yc/bin/y-cruncher"
export PATH="$sng/bin:$sat/bin:$PATH"

cd "$REPO" || exit 1
rm -f "$RESULT"
timeout 260 nix run nixpkgs#xvfb-run -- -a \
  nix develop .#packages.x86_64-linux.full -c \
  python3 scripts/live_scenarios.py "$@" >"$RESULT" 2>/run/user/1000/cc-scenario.err
rc=$?
if [ ! -s "$RESULT" ] || ! head -c1 "$RESULT" | grep -q '{'; then
  echo "{\"verdict\":\"ERROR\",\"rc\":$rc,\"stderr_tail\":\"$(tail -c 300 /run/user/1000/cc-scenario.err | tr '\n' ' ' | sed 's/"/\x27/g')\"}" >"$RESULT"
fi
cleanup
sleep 1
