#!/usr/bin/env bash
set -euo pipefail

# Canonical auto-updater — Nix Packaging Standard.
#
# Source of truth: <nix-repo>/repo-standard/update.sh. Synced into each
# repos/*-nix clone by repo-standard/sync.sh. DO NOT edit per-repo copies —
# edit here and re-sync (CI verifies the copy matches this checksum).
#
# Reads config from .github/update.json (schema: repo-standard/update.schema.json).
# Contract: exit 0 = success / no update, exit 1 = update failed,
#           exit 2 = network or API error (retry next run).

OUTPUT_FILE="${GITHUB_OUTPUT:-/tmp/update-outputs.env}"
: >"$OUTPUT_FILE"

output() { echo "$1=$2" >>"$OUTPUT_FILE"; }
log() { echo "==> $*"; }
warn() { echo "::warning::$*"; }
err() { echo "::error::$*"; }

# re_esc <str> — backslash-escape every regex metacharacter so a dynamic
# string (a Nix identifier from update.json) is matched literally in
# grep -P, sed -E, and plain sed expressions. Nix identifiers only contain
# [A-Za-z0-9_'-]; escaping is defensive, not strictly required, but cheap.
re_esc() { printf '%s' "$1" | sed 's/[^[:alnum:]_]/\\&/g'; }

# --- Read config ---------------------------------------------------------
if [ ! -f .github/update.json ]; then
  log "No .github/update.json — skipping update"
  output "updated" "false"
  exit 0
fi

CONFIG=$(cat .github/update.json)
UPSTREAM_TYPE=$(echo "$CONFIG" | jq -r '.upstream.type')
PACKAGE=$(echo "$CONFIG" | jq -r '.package')
# packageFile: legacy default for hashes/build. versionFile/versionAttr
# locate the canonical package version (may differ — e.g. version literal in
# flake.nix while package.nix only takes it as an argument).
PACKAGE_FILE=$(echo "$CONFIG" | jq -r '.packageFile // "package.nix"')
VERSION_FILE=$(echo "$CONFIG" | jq -r --arg d "$PACKAGE_FILE" '.versionFile // $d')
VERSION_ATTR=$(echo "$CONFIG" | jq -r '.versionAttr // "version"')
VERSION_ATTR_RE=$(re_esc "$VERSION_ATTR")
# versionScheme: "literal" (default) writes the upstream string verbatim;
# "unstable-date" writes "<latest-tag>-unstable-<YYYY-MM-DD>" for
# commit-tracked repos (nixpkgs VCS-snapshot convention) — see below.
VERSION_SCHEME=$(echo "$CONFIG" | jq -r '.versionScheme // "literal"')
# revFile: file holding the package's own src `rev` literal (commit-tracked
# upstreams). Defaults to the version file; set it explicitly when a repo
# carries several `rev = "..."` literals (e.g. a bundled dependency's rev)
# so the updater bumps the package src rev and not a dependency's.
REV_FILE=$(echo "$CONFIG" | jq -r --arg d "$VERSION_FILE" '.revFile // $d')

output "package_name" "$PACKAGE"

# --- No-upstream / custom repos skip ------------------------------------
if [ "$UPSTREAM_TYPE" = "none" ] || [ "$UPSTREAM_TYPE" = "null" ]; then
  log "Upstream type is 'none' — skipping"
  output "updated" "false"
  exit 0
fi
if [ "$UPSTREAM_TYPE" = "custom" ]; then
  log "Upstream type is 'custom' — repo provides its own update logic"
  output "updated" "false"
  exit 0
fi

# --- Get current version -------------------------------------------------
# Handles both `version = "x"` and parameterized `<attr>Version ? "x"` forms.
# The negative lookbehind keeps `versionAttr=version` from matching the tail
# of an identifier like `portmasterVersion`.
if [ "$VERSION_FILE" = "version.json" ]; then
  CURRENT_VERSION=$(jq -r '.version // .rev' version.json)
else
  CURRENT_VERSION=$(grep -oP "(?<![A-Za-z_])${VERSION_ATTR_RE}\s*[?=]\s*\"\K[^\"]+" \
    "$VERSION_FILE" 2>/dev/null | head -1 || true)
fi
if [ -z "$CURRENT_VERSION" ]; then
  err "Could not read current version (attr '$VERSION_ATTR' in '$VERSION_FILE')"
  output "updated" "false"
  output "error_type" "version-read"
  exit 1
fi
output "old_version" "$CURRENT_VERSION"
log "Current version: $CURRENT_VERSION ($VERSION_ATTR in $VERSION_FILE)"

# --- Fetch latest upstream version --------------------------------------
fetch_latest() {
  local retries=3 delay=2 i
  for i in $(seq 1 $retries); do
    if RESULT=$(eval "$1" 2>/dev/null) && [ -n "$RESULT" ] && [ "$RESULT" != "null" ]; then
      echo "$RESULT"
      return 0
    fi
    log "Retry $i/$retries (waiting ${delay}s)..."
    sleep $delay
    delay=$((delay * 2))
  done
  return 1
}

FULL_REV=""
case "$UPSTREAM_TYPE" in
github-release)
  OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
  REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
  LATEST_TAG=$(fetch_latest "curl -sfL 'https://api.github.com/repos/$OWNER/$REPO/releases/latest' | jq -r '.tag_name'") || {
    warn "Failed to fetch latest release from $OWNER/$REPO"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_TAG#v}"
  output "upstream_url" "https://github.com/$OWNER/$REPO/releases/tag/$LATEST_TAG"
  ;;

github-tag)
  OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
  REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
  LATEST_TAG=$(fetch_latest "curl -sfL 'https://api.github.com/repos/$OWNER/$REPO/tags?per_page=1' | jq -r '.[0].name'") || {
    warn "Failed to fetch tags from $OWNER/$REPO"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_TAG#v}"
  output "upstream_url" "https://github.com/$OWNER/$REPO/releases/tag/$LATEST_TAG"
  ;;

github-commit)
  OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
  REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
  BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
  LATEST_COMMIT=$(fetch_latest "curl -sfL 'https://api.github.com/repos/$OWNER/$REPO/commits/$BRANCH' | jq -r '.sha'") || {
    warn "Failed to fetch commits from $OWNER/$REPO"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_COMMIT:0:7}"
  FULL_REV="$LATEST_COMMIT"
  output "upstream_url" "https://github.com/$OWNER/$REPO/commit/$LATEST_COMMIT"
  ;;

gitlab-tag)
  HOST=$(echo "$CONFIG" | jq -r '.upstream.host // "gitlab.com"')
  PROJECT=$(echo "$CONFIG" | jq -r '.upstream.project')
  ENCODED="${PROJECT//\//%2F}"
  LATEST_TAG=$(fetch_latest "curl -sfL 'https://$HOST/api/v4/projects/$ENCODED/repository/tags?per_page=1' | jq -r '.[0].name'") || {
    warn "Failed to fetch tags from $PROJECT"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_TAG#v}"
  output "upstream_url" "https://$HOST/$PROJECT/-/releases/$LATEST_TAG"
  ;;

gitlab-commit)
  HOST=$(echo "$CONFIG" | jq -r '.upstream.host // "gitlab.com"')
  PROJECT=$(echo "$CONFIG" | jq -r '.upstream.project')
  ENCODED="${PROJECT//\//%2F}"
  BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
  LATEST_COMMIT=$(fetch_latest "curl -sfL 'https://$HOST/api/v4/projects/$ENCODED/repository/branches/$BRANCH' | jq -r '.commit.id'") || {
    warn "Failed to fetch from GitLab $PROJECT"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_COMMIT:0:7}"
  FULL_REV="$LATEST_COMMIT"
  output "upstream_url" "https://$HOST/$PROJECT/-/commit/$LATEST_COMMIT"
  ;;

gitea-commit)
  HOST=$(echo "$CONFIG" | jq -r '.upstream.host')
  OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
  REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
  BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
  LATEST_COMMIT=$(fetch_latest "curl -sfL 'https://$HOST/api/v1/repos/$OWNER/$REPO/branches/$BRANCH' | jq -r '.commit.id'") || {
    warn "Failed to fetch from Gitea $HOST/$OWNER/$REPO"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_COMMIT:0:7}"
  FULL_REV="$LATEST_COMMIT"
  output "upstream_url" "https://$HOST/$OWNER/$REPO/commit/$LATEST_COMMIT"
  ;;

git-ls-remote)
  URL=$(echo "$CONFIG" | jq -r '.upstream.url')
  BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
  LATEST_COMMIT=$(fetch_latest "git ls-remote '$URL' 'refs/heads/$BRANCH' | cut -f1") || {
    warn "Failed to ls-remote $URL"
    output "updated" "false"
    exit 2
  }
  LATEST_VERSION="${LATEST_COMMIT:0:7}"
  FULL_REV="$LATEST_COMMIT"
  output "upstream_url" "$URL"
  ;;

*)
  err "Unknown upstream type: $UPSTREAM_TYPE"
  output "updated" "false"
  exit 2
  ;;
esac

# --- versionScheme: unstable-date ---------------------------------------
# Commit-tracked repos default to a bare 7-char SHA as the version, which
# is not orderable by builtins.compareVersions. "unstable-date" instead
# writes "<base>-unstable-<YYYY-MM-DD>" (the nixpkgs VCS-snapshot
# convention). The base comes from update.json `versionBase`; the rev (the
# real SHA) still tracks every commit. Comparison switches to the rev,
# since the date string would otherwise differ every day and loop forever.
if [ "$VERSION_SCHEME" = "unstable-date" ]; then
  if [ -z "$FULL_REV" ]; then
    err "versionScheme 'unstable-date' requires a commit-tracked upstream type"
    output "updated" "false"
    output "error_type" "config-error"
    exit 1
  fi
  CURRENT_REV=""
  [ -f "$REV_FILE" ] && CURRENT_REV=$(grep -oP 'rev\s*=\s*"\K[^"]+' "$REV_FILE" 2>/dev/null | head -1 || true)
  if [ -n "$CURRENT_REV" ] && [ "$CURRENT_REV" = "$FULL_REV" ]; then
    log "Already up to date (rev $FULL_REV)"
    output "updated" "false"
    exit 0
  fi
  BASE=$(echo "$CONFIG" | jq -r '.versionBase // empty')
  [ -z "$BASE" ] && BASE="${CURRENT_VERSION%%-unstable-*}"
  if [ -z "$BASE" ]; then
    err "versionScheme 'unstable-date': no versionBase in update.json and current version is empty"
    output "updated" "false"
    output "error_type" "config-error"
    exit 1
  fi
  LATEST_VERSION="${BASE}-unstable-$(date -u +%Y-%m-%d)"
fi

log "Latest version: $LATEST_VERSION"
output "new_version" "$LATEST_VERSION"

# --- Compare -------------------------------------------------------------
# unstable-date already compared by rev above; the date string can collide
# (two commits the same day) so it must not gate the write here.
if [ "$VERSION_SCHEME" != "unstable-date" ] && [ "$CURRENT_VERSION" = "$LATEST_VERSION" ]; then
  log "Already up to date"
  output "updated" "false"
  exit 0
fi
log "Update found: $CURRENT_VERSION -> $LATEST_VERSION"
output "updated" "true"

# --- Write the new version ----------------------------------------------
# regex-escape the current version so dots etc. are literal in the sed match.
esc() { printf '%s' "$1" | sed 's/[][\\.^$*/]/\\&/g'; }

if [ "$VERSION_FILE" = "version.json" ]; then
  jq --arg v "$LATEST_VERSION" --arg r "${FULL_REV:-$LATEST_VERSION}" \
    '.version = $v | .rev = $r | .date = (now | strftime("%Y-%m-%d"))' \
    version.json >version.json.tmp && mv version.json.tmp version.json
else
  ESC_CUR=$(esc "$CURRENT_VERSION")
  # Preserve the `<attr> = ` / `<attr> ? ` prefix; swap only the quoted value.
  # The leading `\b` word boundary mirrors the read-side lookbehind so a
  # short attr (`version`) cannot match the tail of an identifier
  # (`myversion`); it must not be a `|` alternation — `|` is the s delimiter.
  sed -i -E "s|\b(${VERSION_ATTR_RE}[[:space:]]*[?=][[:space:]]*)\"${ESC_CUR}\"|\1\"${LATEST_VERSION}\"|" \
    "$VERSION_FILE"
  # Confirm the new value landed on the target attr — not merely somewhere
  # in the file (a hash or a dependency string could match a bare literal).
  LATEST_VERSION_RE=$(re_esc "$LATEST_VERSION")
  if ! grep -qP "(?<![A-Za-z_])${VERSION_ATTR_RE}\s*[?=]\s*\"${LATEST_VERSION_RE}\"" "$VERSION_FILE"; then
    err "Version write did not take effect on attr '$VERSION_ATTR' in $VERSION_FILE"
    output "error_type" "version-write"
    exit 1
  fi
  # Commit-tracked repos: also bump the `rev` attr in the configured file.
  if [ -n "$FULL_REV" ] && [ -f "$REV_FILE" ]; then
    CUR_REV=$(grep -oP 'rev\s*=\s*"\K[^"]+' "$REV_FILE" | head -1 || true)
    [ -n "$CUR_REV" ] && sed -i "s|rev = \"$CUR_REV\"|rev = \"$FULL_REV\"|" "$REV_FILE"
  fi
fi

# --- Extract hashes (iterative build-fail-parse) ------------------------
# update.json `hashes` entries are either a bare field name (auto-located in
# the first *.nix file declaring it) or {"field","file"} to disambiguate when
# a name like `hash` appears in several files.
#
# A version bump invalidates EVERY fixed-output hash at once, so a field
# cannot be isolated by dummying it alone (the others are stale too, and the
# build fails unpredictably). Instead: dummy all declared hashes, then build
# repeatedly — each failed build names one fixed-output derivation and its
# correct hash; the drv name maps to a declared field. Repeat until clean.
DUMMY_HASH="sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

declare -a HF_FIELD=() HF_FILE=()
# HF_DONE[idx] — the real hash a field has already been resolved to this
# run. Used to detect an undeclared fixed-output derivation: if a build
# mismatch routes to a field already resolved to a *different* hash, the
# extractor is oscillating — fail clearly instead of looping to the cap.
declare -A HF_DONE=()
mapfile -t HASH_ENTRIES < <(echo "$CONFIG" | jq -c '.hashes // [] | .[]')
for entry in "${HASH_ENTRIES[@]}"; do
  if [ "$(echo "$entry" | jq -r 'type')" = "string" ]; then
    f=$(echo "$entry" | jq -r '.')
    f_re=$(re_esc "$f")
    file=$(grep -rlP "${f_re}\s*[?=]\s*\"sha256-" --include='*.nix' . 2>/dev/null | head -1 || true)
  else
    f=$(echo "$entry" | jq -r '.field')
    file=$(echo "$entry" | jq -r '.file')
  fi
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    err "Hash field '$f': could not resolve a .nix file ('$file')"
    output "error_type" "hash-extraction"
    exit 1
  fi
  HF_FIELD+=("$f")
  HF_FILE+=("$file")
done

# set_hash <field> <file> <value> — replaces the field's sha256 value,
# preserving whichever operator (`=` or `?`) was used.
set_hash() {
  local field_re
  field_re=$(re_esc "$1")
  sed -i "s|\(${field_re}[[:space:]]*[?=][[:space:]]*\)\"sha256-[^\"]*\"|\1\"${3}\"|" "$2"
}

if [ "${#HF_FIELD[@]}" -gt 0 ]; then
  for i in "${!HF_FIELD[@]}"; do
    set_hash "${HF_FIELD[$i]}" "${HF_FILE[$i]}" "$DUMMY_HASH"
  done
  HASH_MAX=$((${#HF_FIELD[@]} + 2))
  BUILD_OUTPUT=""
  for ((iter = 1; iter <= HASH_MAX; iter++)); do
    BUILD_OUTPUT=$(nix build .#default --no-link 2>&1 || true)
    echo "$BUILD_OUTPUT" | grep -q 'hash mismatch in fixed-output' || break
    BLOCK=$(echo "$BUILD_OUTPUT" | grep -A3 'hash mismatch in fixed-output derivation' | head -4)
    DRV=$(echo "$BLOCK" | grep -oP "derivation '\K[^']+" | head -1)
    GOT=$(echo "$BLOCK" | grep -oP 'got:\s+sha256-\K\S+' | head -1 || true)
    [ -z "$GOT" ] && GOT=$(echo "$BLOCK" | grep -oP 'use\s+"sha256-\K[^"]+' | head -1 || true)
    if [ -z "$GOT" ]; then
      err "hash mismatch reported but no replacement hash could be parsed"
      output "error_type" "hash-extraction"
      exit 1
    fi
    # Map the failing derivation's name to a declared hash field. Cargo
    # vendor derivations vary by nixpkgs version: legacy `*-vendor.tar.gz`
    # / `*cargo*` and the newer fetchCargoVendor `*-vendor-staging`. Go is
    # always `*-go-modules`, npm `*-npm-deps`.
    case "$DRV" in
    *go-modules*) WANT="vendorHash" ;;
    *npm-deps* | *-npm-*) WANT="npmDepsHash" ;;
    *cargo* | *vendor.tar* | *vendor-staging*) WANT="cargoHash" ;;
    *) WANT="" ;;
    esac
    IDX=-1
    for i in "${!HF_FIELD[@]}"; do
      if [ -n "$WANT" ] && [ "${HF_FIELD[$i]}" = "$WANT" ]; then
        IDX=$i
        break
      fi
    done
    if [ "$IDX" -lt 0 ]; then
      # source hash: the declared field that is not a known vendor field
      for i in "${!HF_FIELD[@]}"; do
        case "${HF_FIELD[$i]}" in
        vendorHash | npmDepsHash | cargoHash) ;;
        *)
          IDX=$i
          break
          ;;
        esac
      done
    fi
    if [ "$IDX" -lt 0 ]; then
      err "undeclared hash for derivation ${DRV##*/}: not a known vendor type and no declared source hash field — add it to .github/update.json hashes[]"
      output "error_type" "hash-extraction"
      exit 1
    fi
    # Oscillation guard: a mismatch routed to an already-resolved field
    # means an undeclared FOD is being misrouted onto it — fail clearly.
    PREV="${HF_DONE[$IDX]:-}"
    if [ -n "$PREV" ] && [ "$PREV" != "sha256-$GOT" ]; then
      err "undeclared hash for derivation ${DRV##*/}: routed onto the already-resolved '${HF_FIELD[$IDX]}' field — a fixed-output derivation is missing from .github/update.json hashes[]"
      output "error_type" "hash-extraction"
      exit 1
    fi
    HF_DONE[$IDX]="sha256-$GOT"
    log "Hash '${HF_FIELD[$IDX]}' (drv ${DRV##*/}): sha256-$GOT"
    set_hash "${HF_FIELD[$IDX]}" "${HF_FILE[$IDX]}" "sha256-$GOT"
  done
  if echo "$BUILD_OUTPUT" | grep -q 'hash mismatch in fixed-output'; then
    err "Hash extraction did not converge after $HASH_MAX iterations"
    output "error_type" "hash-extraction"
    exit 1
  fi
fi

# --- Verification chain --------------------------------------------------
log "Step 1/3: nix flake check --no-build"
if ! nix flake check --no-build 2>&1; then
  err "Eval check failed"
  output "error_type" "eval-error"
  exit 1
fi

log "Step 2/3: nix build (clean)"
if ! nix build .#default --no-link --print-build-logs 2>&1; then
  err "Build failed"
  output "error_type" "build-error"
  exit 1
fi

VERIFY_BINARY=$(echo "$CONFIG" | jq -r '.verify.binary // empty')
# Split verify.args on whitespace so a multi-token value ("db --version")
# is passed as separate argv entries, not one argument.
read -ra VERIFY_ARGS <<<"$(echo "$CONFIG" | jq -r '.verify.args // "--version"')"
VERIFY_CHECK=$(echo "$CONFIG" | jq -r '.verify.check // empty')

log "Step 3/3: artifact verification"
nix build .#default
if [ -n "$VERIFY_BINARY" ]; then
  ./result/bin/"$VERIFY_BINARY" "${VERIFY_ARGS[@]}" 2>&1 || {
    err "Binary verification failed"
    output "error_type" "verification-error"
    exit 1
  }
  if file ./result/bin/"$VERIFY_BINARY" 2>/dev/null | grep -q ELF; then
    MISSING=$(ldd ./result/bin/"$VERIFY_BINARY" 2>&1 | grep "not found" || true)
    [ -n "$MISSING" ] && {
      err "Missing shared libraries: $MISSING"
      output "error_type" "missing-deps"
      exit 1
    }
  fi
elif [ "$VERIFY_CHECK" = "elf" ]; then
  FOUND=$(find result/bin/ result/lib/ -type f 2>/dev/null | while read -r f; do
    file "$f" 2>/dev/null | grep -q ELF && {
      echo "$f"
      break
    }
  done)
  [ -z "$FOUND" ] && {
    err "No ELF artifact found under result/bin or result/lib"
    output "error_type" "verification-error"
    exit 1
  }
elif [ "$VERIFY_CHECK" = "wrapper" ]; then
  FOUND=$(find result/bin/ \( -type f -o -type l \) 2>/dev/null | head -1 || true)
  { [ -n "$FOUND" ] && [ -x "$FOUND" ]; } || {
    err "No executable wrapper found under result/bin"
    output "error_type" "verification-error"
    exit 1
  }
elif [ "$VERIFY_CHECK" = "desktop" ]; then
  find result/share/applications/ -name "*.desktop" 2>/dev/null | head -1 | grep -q . ||
    warn "No desktop file found"
fi
rm -f result

log "Update verified: $CURRENT_VERSION -> $LATEST_VERSION"
exit 0
