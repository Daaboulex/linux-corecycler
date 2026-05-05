#!/usr/bin/env bash
# update-readme-options — splice options reference into README.md markers.
# If docs/OPTIONS.md exists, inserts a link. Otherwise generates a basic table
# from nix eval of the module's top-level options.
set -euo pipefail

README="README.md"
BEGIN_MARKER="<!-- BEGIN generated:options -->"
END_MARKER="<!-- END generated:options -->"

[ -f "$README" ] || exit 0
grep -qF "$BEGIN_MARKER" "$README" || exit 0

generate_content() {
  if [ -f "docs/OPTIONS.md" ]; then
    echo "## Options"
    echo ""
    echo "See [docs/OPTIONS.md](docs/OPTIONS.md) for the full options reference."
  else
    # Try to extract options from flake module evaluation
    local options_output
    options_output=$(nix eval --raw ".#optionsDocs" 2>/dev/null) || true
    if [ -n "$options_output" ]; then
      echo "## Options"
      echo ""
      echo "$options_output"
    else
      # Fallback: grep for mkEnableOption/mkOption in nix files
      local opts
      opts=$(grep -rh "mkEnableOption\|= lib.mkOption" ./*.nix ./**/*.nix 2>/dev/null \
        | grep -v "^#" \
        | sed 's/.*mkEnableOption "\([^"]*\)".*/| `enable` | bool | \1 |/' \
        | sed 's/.*= lib.mkOption.*//' \
        | grep -v "^$" \
        | head -20)
      if [ -n "$opts" ]; then
        echo "## Options"
        echo ""
        echo "| Option | Type | Description |"
        echo "|--------|------|-------------|"
        echo "$opts"
      fi
    fi
  fi
}

# Generate new content
content=$(generate_content)
[ -z "$content" ] && exit 0

# Splice into README between markers
awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" -v content="$content" '
  $0 == begin { print; print content; skip=1; next }
  $0 == end { print; skip=0; next }
  !skip { print }
' "$README" > "$README.tmp" && mv "$README.tmp" "$README"
