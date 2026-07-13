#!/usr/bin/env bash
set -uo pipefail

# Canonical heal-overlays — Nix Packaging Standard.
#
# Source of truth: github.com/Daaboulex/nix-packaging-standard (heal-overlays.sh),
# synced into each packaging repo's scripts/heal-overlays.sh by sync.sh — DO NOT
# edit per-repo copies; std-conformance enforces byte-identity.
#
# Temporary nixpkgs fixes live in overlays/<name>.nix, one fix per file
# (mirrors the main config's parts/overlays/_fixes convention):
#
#   {
#     meta = { reason = "..."; added = "YYYY-MM-DD"; upstream = "..."; };
#     dropWhen = pkgs: <bool>;             # true => nixpkgs healed, delete me
#     overlay = final: prev: { ... };
#   }
#
# The repo composes overlays.default = glue + fixes and exports overlays.probe
# = the glue WITHOUT fixes. This script evaluates each fix's dropWhen against a
# probe pkgs (the repo's locked nixpkgs + overlays.probe, allowUnfree); a fix
# whose dropWhen fires is HEALED: the file is deleted and the full check suite
# (the same .#checks CI builds) verifies the removal. The calling workflow
# (maintenance.yml) pushes on green and files an issue on red.
#
# Contract: exit 0 = ran (see outputs), exit 1 = malformed fix / probe error /
# failed removal (fail closed), exit 2 = environment error. Outputs
# (GITHUB_OUTPUT or /tmp/heal-outputs.env):
#   kept=<names>   healed=<names>   verify_exit=<code>

OUTPUT_FILE="${GITHUB_OUTPUT:-/tmp/heal-outputs.env}"
: >"$OUTPUT_FILE"
output() { echo "$1=$2" >>"$OUTPUT_FILE"; }
log() { echo "==> $*"; }
err() { echo "::error::$*"; }

output "kept" ""
output "healed" ""
output "verify_exit" "0"

if [ ! -d overlays ]; then
  log "No overlays/ directory — nothing to heal"
  exit 0
fi

shopt -s nullglob
files=(overlays/*.nix)
if [ ${#files[@]} -eq 0 ]; then
  err "overlays/ exists but holds no .nix fix — remove the empty directory"
  exit 1
fi

command -v nix >/dev/null 2>&1 || {
  err "nix not available"
  exit 2
}
command -v jq >/dev/null 2>&1 || {
  err "jq not available"
  exit 2
}

healed=()
kept=()
for f in "${files[@]}"; do
  name=$(basename "$f" .nix)
  # Shape check first — a malformed fix is a hard error, never skipped.
  if ! nix eval --json --impure --expr \
    "let v = import ./${f}; in { reason = v.meta.reason; added = v.meta.added; hasDrop = v ? dropWhen; hasOverlay = v ? overlay; }" \
    >/tmp/heal-meta.json 2>/tmp/heal-meta.err; then
    err "overlays/${name}.nix: malformed — needs meta.reason, meta.added, dropWhen, overlay ($(tail -1 /tmp/heal-meta.err))"
    exit 1
  fi
  if ! jq -e '.hasDrop and .hasOverlay and (.reason | type == "string" and length > 0) and (.added | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))' \
    /tmp/heal-meta.json >/dev/null; then
    err "overlays/${name}.nix: meta.reason must be a nonempty string, meta.added a YYYY-MM-DD date, and dropWhen + overlay must exist"
    exit 1
  fi
  # Probe dropWhen against pkgs WITHOUT the fixes (overlays.probe).
  verdict=$(nix eval --json --impure --expr "
    let
      flake = builtins.getFlake (toString ./.);
      pkgs = import flake.inputs.nixpkgs {
        system = builtins.currentSystem;
        config.allowUnfree = true;
        overlays = [ flake.overlays.probe ];
      };
    in
    (import ./${f}).dropWhen pkgs" 2>/tmp/heal-probe.err) || {
    err "overlays/${name}.nix: dropWhen probe errored — fix the predicate ($(tail -1 /tmp/heal-probe.err))"
    exit 1
  }
  if [ "$verdict" = "true" ]; then
    log "overlays/${name}.nix: HEALED — nixpkgs provides this again; removing"
    healed+=("$name")
  else
    log "overlays/${name}.nix: still needed"
    kept+=("$name")
  fi
done

output "kept" "${kept[*]:-}"
if [ ${#healed[@]} -eq 0 ]; then
  exit 0
fi

for n in "${healed[@]}"; do
  git rm -q "overlays/${n}.nix" || {
    err "could not git rm overlays/${n}.nix (untracked?)"
    exit 1
  }
done

# Verify the removal against the FULL check suite — the same target ci.yml
# builds — so a heal that only LOOKS healed can never reach main.
SYS=$(nix eval --impure --raw --expr 'builtins.currentSystem')
nix run nixpkgs#nix-fast-build -- --skip-cached --no-nom --flake ".#checks.$SYS" 2>&1 | tee /tmp/heal-verify.log
verify="${PIPESTATUS[0]}"
output "healed" "${healed[*]}"
output "verify_exit" "$verify"
if [ "$verify" -ne 0 ]; then
  err "healed overlay removal failed verification — restoring overlays/ (see log)"
  git checkout -- overlays/
fi
exit 0
