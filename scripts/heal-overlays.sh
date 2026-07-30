#!/usr/bin/env bash
set -uo pipefail

# Canonical heal-overlays — Nix Packaging Standard.
#
# Source of truth: github.com/Daaboulex/nix-packaging-standard (heal-overlays.sh),
# synced into each packaging repo's scripts/heal-overlays.sh by sync.sh — DO NOT
# edit per-repo copies; std-conformance enforces byte-identity.
#
# EVERY temporary divergence from nixpkgs -- a regression bridge, a dependency
# the packaged upstream needs but nixpkgs' package does not carry yet, or a
# version pin -- lives in overlays/<name>.nix, one fix per file (mirrors the
# main config's parts/overlays/_fixes convention), and carries a predicate that
# retires it:
#
#   {
#     meta = { reason = "..."; added = "YYYY-MM-DD"; upstream = "..."; };
#     dropWhen = pkgs: <bool>;             # true => healed, delete me
#     # ...or, when the breakage is invisible to eval:
#     dropWhenBuilds = pkgs: <derivation>; # builds => healed, delete me
#     overlay = final: prev: { ... };
#   }
#
# Exactly one predicate, and it must TEST the actual condition. A version-string
# proxy is forbidden: it fires on an unrelated bump, the fix is dropped while the
# breakage is still live, and the repo re-breaks later.
#
# The repo composes overlays.default = glue + fixes and exports overlays.probe
# = the glue WITHOUT fixes. This script probes each fix against a probe pkgs
# (the repo's locked nixpkgs + overlays.probe, allowUnfree): dropWhen is
# evaluated, dropWhenBuilds is BUILT (a build-phase failure -- a broken test
# suite, a pkg-config version reject -- is not eval-visible, so guessing from
# eval would be the version-proxy mistake in another form). A fix that fires is
# HEALED and its file is deleted from the tree.
#
# This script decides and removes; it does NOT verify. maintenance.yml runs it
# INSIDE the lock-update job, right after `nix flake update`, so every fix is
# judged against the inputs the repo is moving TO -- probing the committed lock
# instead would read a fix needed only on the NEW nixpkgs as healed, drop it,
# and re-break on the next bump. The caller's single verification build then
# gates the lock bump and the removals together, restoring the fixes if that
# build is red.
#
# Contract: exit 0 = ran (see outputs), exit 1 = malformed fix / probe error /
# failed removal (fail closed), exit 2 = environment error. Outputs
# (GITHUB_OUTPUT or /tmp/heal-outputs.env):
#   kept=<names>   healed=<names>

OUTPUT_FILE="${GITHUB_OUTPUT:-/tmp/heal-outputs.env}"
: >"$OUTPUT_FILE"
output() { echo "$1=$2" >>"$OUTPUT_FILE"; }
log() { echo "==> $*"; }
err() { echo "::error::$*"; }

output "kept" ""
output "healed" ""

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
    "let v = import ./${f}; in { reason = v.meta.reason; added = v.meta.added; hasDrop = v ? dropWhen; hasBuildDrop = v ? dropWhenBuilds; hasOverlay = v ? overlay; }" \
    >/tmp/heal-meta.json 2>/tmp/heal-meta.err; then
    err "overlays/${name}.nix: malformed — needs meta.reason, meta.added, overlay, and exactly one of dropWhen / dropWhenBuilds ($(tail -1 /tmp/heal-meta.err))"
    exit 1
  fi
  if ! jq -e '(.hasDrop != .hasBuildDrop) and .hasOverlay and (.reason | type == "string" and length > 0) and (.added | test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$"))' \
    /tmp/heal-meta.json >/dev/null; then
    err "overlays/${name}.nix: meta.reason must be a nonempty string, meta.added a YYYY-MM-DD date, overlay must exist, and EXACTLY ONE of dropWhen / dropWhenBuilds must be set"
    exit 1
  fi
  probePkgs="
    let
      flake = builtins.getFlake (toString ./.);
      pkgs = import flake.inputs.nixpkgs {
        system = builtins.currentSystem;
        config.allowUnfree = true;
        overlays = [ flake.overlays.probe ];
      };
    in"
  if jq -e '.hasBuildDrop' /tmp/heal-meta.json >/dev/null; then
    # The probe target must always EVALUATE — a predicate that cannot even
    # instantiate is malformed, not "still needed", so it fails closed here
    # instead of silently pinning the fix forever.
    if ! nix eval --raw --impure --expr \
      "${probePkgs} ((import ./${f}).dropWhenBuilds pkgs).drvPath" \
      >/dev/null 2>/tmp/heal-probe.err; then
      err "overlays/${name}.nix: dropWhenBuilds must evaluate to a derivation — fix the predicate ($(tail -1 /tmp/heal-probe.err))"
      exit 1
    fi
    # Now the real question: does it BUILD without the fix? A failure here is
    # the expected "still broken" answer, never an error.
    if nix build --no-link --impure --expr \
      "${probePkgs} (import ./${f}).dropWhenBuilds pkgs" >/tmp/heal-build.log 2>&1; then
      verdict=true
    else
      verdict=false
    fi
  else
    verdict=$(nix eval --json --impure --expr \
      "${probePkgs} (import ./${f}).dropWhen pkgs" 2>/tmp/heal-probe.err) || {
      err "overlays/${name}.nix: dropWhen probe errored — fix the predicate ($(tail -1 /tmp/heal-probe.err))"
      exit 1
    }
  fi
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
output "healed" "${healed[*]}"
exit 0
