#!/usr/bin/env bash
# check-readme-sections — verify README.md has required structural sections.
# Runs as a pre-commit hook. Checks for splice markers that indicate
# the README is managed by sync.sh.
set -euo pipefail

README="README.md"
[ -f "$README" ] || exit 0

errors=0

check_marker() {
  if ! grep -qF "<!-- BEGIN generated:$1 -->" "$README"; then
    echo "README.md: missing <!-- BEGIN generated:$1 --> marker"
    errors=$((errors + 1))
  fi
}

check_marker "badges"
check_marker "upstream"
check_marker "footer"

# Check for at least one of: ## Installation, ## Quick Start, generated:installation marker
if ! grep -qE "^## (Installation|Quick Start)" "$README" && \
   ! grep -qF "<!-- BEGIN generated:installation -->" "$README"; then
  echo "README.md: missing installation section (## Installation, ## Quick Start, or generated:installation marker)"
  errors=$((errors + 1))
fi

[ "$errors" -eq 0 ] && exit 0
echo ""
echo "Fix: run 'bash .ai-context/repo-standard/sync.sh $(basename "$PWD")' from the main nix repo"
exit 1
