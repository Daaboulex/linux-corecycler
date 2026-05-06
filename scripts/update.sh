#!/usr/bin/env bash
set -euo pipefail

# Generic update script for Daaboulex Nix Packaging Standard
# Reads config from .github/update.json
# Contract: exit 0 = success/no-update, exit 1 = failed, exit 2 = network error

OUTPUT_FILE="${GITHUB_OUTPUT:-/tmp/update-outputs.env}"
: > "$OUTPUT_FILE"

output() {
  # Use delimiter syntax to prevent newline injection into GITHUB_OUTPUT
  local delim="EOF_$(date +%s%N)"
  printf '%s<<%s\n%s\n%s\n' "$1" "$delim" "$2" "$delim" >> "$OUTPUT_FILE"
}

log() { echo "==> $*"; }
warn() { echo "::warning::$*"; }
err() { echo "::error::$*"; }

# --- Read config ---
if [ ! -f .github/update.json ]; then
  log "No .github/update.json — skipping update"
  output "updated" "false"
  exit 0
fi

CONFIG=$(cat .github/update.json)
UPSTREAM_TYPE=$(echo "$CONFIG" | jq -r '.upstream.type')
PACKAGE=$(echo "$CONFIG" | jq -r '.package')
PACKAGE_FILE=$(echo "$CONFIG" | jq -r '.packageFile // "package.nix"')
HASH_FIELDS=$(echo "$CONFIG" | jq -r '.hashes // [] | .[]')

output "package_name" "$PACKAGE"

# --- No-upstream repos skip ---
if [ "$UPSTREAM_TYPE" = "none" ] || [ "$UPSTREAM_TYPE" = "null" ]; then
  log "Upstream type is 'none' — skipping"
  output "updated" "false"
  exit 0
fi

# --- Get current version ---
if [ "$PACKAGE_FILE" = "version.json" ]; then
  CURRENT_VERSION=$(jq -r '.version // .rev' version.json)
else
  CURRENT_VERSION=$(grep -oP 'version\s*=\s*"\K[^"]+' "$PACKAGE_FILE" | head -1)
fi
output "old_version" "$CURRENT_VERSION"
log "Current version: $CURRENT_VERSION"

# --- Fetch latest upstream version ---
fetch_latest() {
  # Execute command passed as array arguments (no eval)
  local retries=3 delay=2
  for i in $(seq 1 $retries); do
    if RESULT=$("$@" 2>/dev/null) && [ -n "$RESULT" ]; then
      echo "$RESULT"
      return 0
    fi
    log "Retry $i/$retries (waiting ${delay}s)..."
    sleep $delay
    delay=$((delay * 2))
  done
  return 1
}

# Helper to fetch JSON field from URL
_fetch_json_field() {
  curl -sfL "$1" | jq -r "$2"
}

case "$UPSTREAM_TYPE" in
  github-release)
    OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
    REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
    API_URL="https://api.github.com/repos/$OWNER/$REPO/releases/latest"
    LATEST_TAG=$(fetch_latest _fetch_json_field "$API_URL" '.tag_name') || {
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
    LATEST_TAG=$(fetch_latest _fetch_json_field "https://api.github.com/repos/$OWNER/$REPO/tags?per_page=1" '.[0].name') || {
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
    LATEST_COMMIT=$(fetch_latest _fetch_json_field "https://api.github.com/repos/$OWNER/$REPO/commits/$BRANCH" '.sha') || {
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
    LATEST_TAG=$(fetch_latest _fetch_json_field "https://$HOST/api/v4/projects/$ENCODED/repository/tags?per_page=1" '.[0].name') || {
      warn "Failed to fetch tags from $PROJECT"
      output "updated" "false"
      exit 2
    }
    LATEST_VERSION="${LATEST_TAG#v}"
    output "upstream_url" "https://$HOST/$PROJECT/-/releases/$LATEST_TAG"
    ;;

  gitea-commit)
    HOST=$(echo "$CONFIG" | jq -r '.upstream.host')
    OWNER=$(echo "$CONFIG" | jq -r '.upstream.owner')
    REPO=$(echo "$CONFIG" | jq -r '.upstream.repo')
    BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
    LATEST_COMMIT=$(fetch_latest _fetch_json_field "https://$HOST/api/v1/repos/$OWNER/$REPO/branches/$BRANCH" '.commit.id') || {
      warn "Failed to fetch from Gitea $HOST/$OWNER/$REPO"
      output "updated" "false"
      exit 2
    }
    LATEST_VERSION="${LATEST_COMMIT:0:7}"
    FULL_REV="$LATEST_COMMIT"
    output "upstream_url" "https://$HOST/$OWNER/$REPO/commit/$LATEST_COMMIT"
    ;;

  gitlab-commit)
    HOST=$(echo "$CONFIG" | jq -r '.upstream.host // "gitlab.com"')
    PROJECT=$(echo "$CONFIG" | jq -r '.upstream.project')
    ENCODED="${PROJECT//\//%2F}"
    BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
    LATEST_COMMIT=$(fetch_latest _fetch_json_field "https://$HOST/api/v4/projects/$ENCODED/repository/branches/$BRANCH" '.commit.id') || {
      warn "Failed to fetch from GitLab $PROJECT"
      output "updated" "false"
      exit 2
    }
    LATEST_VERSION="${LATEST_COMMIT:0:7}"
    FULL_REV="$LATEST_COMMIT"
    output "upstream_url" "https://$HOST/$PROJECT/-/commit/$LATEST_COMMIT"
    ;;

  git-ls-remote)
    URL=$(echo "$CONFIG" | jq -r '.upstream.url')
    BRANCH=$(echo "$CONFIG" | jq -r '.upstream.branch // "main"')
    _git_ls_remote_sha() { git ls-remote "$1" "refs/heads/$2" | cut -f1; }
    LATEST_COMMIT=$(fetch_latest _git_ls_remote_sha "$URL" "$BRANCH") || {
      warn "Failed to ls-remote $URL"
      output "updated" "false"
      exit 2
    }
    LATEST_VERSION="${LATEST_COMMIT:0:7}"
    FULL_REV="$LATEST_COMMIT"
    output "upstream_url" "$URL"
    ;;

  custom)
    log "Custom upstream type — should use custom scripts/update.sh"
    output "updated" "false"
    exit 0
    ;;

  *)
    err "Unknown upstream type: $UPSTREAM_TYPE"
    output "updated" "false"
    exit 2
    ;;
esac

log "Latest version: $LATEST_VERSION"
output "new_version" "$LATEST_VERSION"

# --- Compare versions ---
if [ "$CURRENT_VERSION" = "$LATEST_VERSION" ]; then
  log "Already up to date"
  output "updated" "false"
  exit 0
fi

log "Update found: $CURRENT_VERSION → $LATEST_VERSION"
output "updated" "true"

# --- Update version in package file ---
if [ "$PACKAGE_FILE" = "version.json" ]; then
  jq --arg v "$LATEST_VERSION" --arg r "${FULL_REV:-$LATEST_VERSION}" \
    '.version = $v | .rev = $r | .date = (now | strftime("%Y-%m-%d"))' \
    version.json > version.json.tmp && mv version.json.tmp version.json
else
  sed -i "s|version = \"$CURRENT_VERSION\"|version = \"$LATEST_VERSION\"|" "$PACKAGE_FILE"
  # Update rev for commit-tracking repos
  if [ -n "${FULL_REV:-}" ]; then
    CURRENT_REV=$(grep -oP 'rev\s*=\s*"\K[^"]+' "$PACKAGE_FILE" | head -1)
    if [ -n "$CURRENT_REV" ]; then
      sed -i "s|rev = \"$CURRENT_REV\"|rev = \"$FULL_REV\"|" "$PACKAGE_FILE"
    fi
  fi
fi

# --- Extract hashes (iterative build-fail-parse) ---
# IMPORTANT: hashes in update.json MUST be ordered by evaluation dependency:
# source hash first (fetcher fails before build), then vendor hashes (cargoHash, npmDepsHash, vendorHash).
# Each iteration sets one hash to dummy, builds to extract the correct value, then restores it.
# If nix outputs multiple "got: sha256-..." lines, head -1 takes the first (the fetcher hash).
DUMMY_HASH="sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

for FIELD in $HASH_FIELDS; do
  log "Extracting hash for: $FIELD"

  # Get current hash pattern
  CURRENT_HASH=$(grep -oP "${FIELD}\s*=\s*\"sha256-\K[^\"]*" "$PACKAGE_FILE" | head -1)
  if [ -z "$CURRENT_HASH" ]; then
    warn "Could not find $FIELD in $PACKAGE_FILE — skipping"
    continue
  fi

  # Set to dummy hash
  sed -i "s|${FIELD} = \"sha256-${CURRENT_HASH}\"|${FIELD} = \"${DUMMY_HASH}\"|" "$PACKAGE_FILE"

  # Build and extract correct hash
  log "Building to extract $FIELD hash..."
  BUILD_OUTPUT=$(nix build .#default 2>&1 || true)
  NEW_HASH=$(echo "$BUILD_OUTPUT" | grep -oP 'got:\s+sha256-\K\S+' | head -1)

  if [ -z "$NEW_HASH" ]; then
    err "Failed to extract $FIELD hash"
    output "error_type" "hash-extraction"
    output "error_log" "/tmp/update.log"
    exit 1
  fi

  log "Extracted $FIELD: sha256-$NEW_HASH"
  sed -i "s|${FIELD} = \"${DUMMY_HASH}\"|${FIELD} = \"sha256-${NEW_HASH}\"|" "$PACKAGE_FILE"
done

# --- Verification chain ---
log "Running verification chain..."

# 1. Eval check
log "Step 1/4: nix flake check --no-build"
if ! nix flake check --no-build 2>&1; then
  err "Eval check failed"
  output "error_type" "eval-error"
  exit 1
fi

# 2. Clean build
log "Step 2/4: nix build (clean)"
if ! nix build .#default --no-link --print-build-logs 2>&1; then
  err "Build failed"
  output "error_type" "build-error"
  exit 1
fi

# 3. Binary verification
VERIFY_BINARY=$(echo "$CONFIG" | jq -r '.verify.binary // empty')
VERIFY_ARGS=$(echo "$CONFIG" | jq -r '.verify.args // "--version"')
VERIFY_CHECK=$(echo "$CONFIG" | jq -r '.verify.check // empty')

if [ -n "$VERIFY_BINARY" ]; then
  log "Step 3/4: Binary verification (./result/bin/$VERIFY_BINARY $VERIFY_ARGS)"
  nix build .#default  # need result symlink
  if ! ./result/bin/"$VERIFY_BINARY" "$VERIFY_ARGS" 2>&1; then
    err "Binary verification failed"
    output "error_type" "verification-error"
    exit 1
  fi
elif [ "$VERIFY_CHECK" = "elf" ]; then
  log "Step 3/4: ELF verification"
  nix build .#default
  FOUND=$(find result/bin/ \( -type f -o -type l \) -executable 2>/dev/null | head -1)
  if [ -z "$FOUND" ]; then
    FOUND=$(find result/lib/ -name "*.so" 2>/dev/null | head -1)
  fi
  if [ -n "$FOUND" ]; then
    file "$FOUND" | grep -q ELF || { err "Not an ELF binary: $FOUND"; output "error_type" "verification-error"; exit 1; }
  fi
elif [ "$VERIFY_CHECK" = "wrapper" ]; then
  log "Step 3/4: Wrapper verification"
  nix build .#default
  FOUND=$(find result/bin/ \( -type f -o -type l \) 2>/dev/null | head -1)
  [ -n "$FOUND" ] && [ -x "$FOUND" ] || { err "No executable wrapper found"; output "error_type" "verification-error"; exit 1; }
elif [ "$VERIFY_CHECK" = "eval" ]; then
  log "Step 3/4: Eval verification (already passed in step 1)"
elif [ "$VERIFY_CHECK" = "desktop" ]; then
  log "Step 3/4: Desktop file verification"
  nix build .#default
  find result/share/applications/ -name "*.desktop" 2>/dev/null | head -1 | grep -q . || warn "No desktop file found"
else
  log "Step 3/4: No binary verification configured — skipping"
fi

# 4. Runtime dependency check (ldd)
if [ -n "$VERIFY_BINARY" ]; then
  log "Step 4/4: ldd check"
  if file ./result/bin/"$VERIFY_BINARY" 2>/dev/null | grep -q ELF; then
    MISSING=$(ldd ./result/bin/"$VERIFY_BINARY" 2>&1 | grep "not found" || true)
    if [ -n "$MISSING" ]; then
      err "Missing shared libraries:"
      echo "$MISSING"
      output "error_type" "missing-deps"
      exit 1
    fi
  fi
else
  log "Step 4/4: ldd check — skipping (no binary configured)"
fi

# Clean up build artifact
rm -f result

log "Update verified: $CURRENT_VERSION → $LATEST_VERSION"
exit 0
