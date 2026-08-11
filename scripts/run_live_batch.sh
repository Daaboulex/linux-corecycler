#!/usr/bin/env bash
set -uo pipefail

RUNNER="$HOME/Documents/nix/repos/linux-corecycler/scripts/run_live_scenario.sh"
BATCH="/run/user/1000/cc-batch.jsonl"
: >"$BATCH"

run_one() {
  local label="$1"; shift
  RESULT="/run/user/1000/cc-one.json" bash "$RUNNER" "$@"
  printf '%s\t%s\n' "$label" "$(cat /run/user/1000/cc-one.json 2>/dev/null)" >>"$BATCH"
}

run_one "refusal-mprime"        gui-refusal --backend mprime --cores 4 5 --watchdog 90
run_one "parallel-mprime-2t"    parallel --backend mprime --mode SSE --threads 2 --cores 4 5 --seconds 15 --watchdog 120
run_one "rapid-mprime"          rapid --backend mprime --mode SSE --cores 4 5 --seconds 20 --watchdog 120
run_one "doctor"                doctor --watchdog 60
run_one "gui-mprime-AVX-1t"     gui-run --backend mprime --mode AVX --threads 1 --cores 4 5 --seconds 15 --watchdog 120
run_one "gui-mprime-AVX2-2t"    gui-run --backend mprime --mode AVX2 --threads 2 --cores 4 5 --seconds 15 --watchdog 120
run_one "gui-stressng-AVX-2t"   gui-run --backend stress-ng --mode AVX --threads 2 --cores 4 5 --seconds 15 --watchdog 120

echo "BATCH-COMPLETE" >>"$BATCH"
