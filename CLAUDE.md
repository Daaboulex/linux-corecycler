# linux-corecycler

## Standard
This repo follows the **Daaboulex Nix Packaging Standard v1**.

## Repo Config
- **Package**: `corecyclerlx`
- **Upstream**: none (own code / no upstream tracking)
- **Hashes**: none
- **Verify**: `result/bin/corecyclerlx --help`
- **Schedule**: N/A (no upstream tracking)

## Session Protocol
1. Read `AI-progress.json` and `AI-tasks.json`
2. Run `nix flake check --no-build`
3. Pick ONE task, complete it, verify it
4. Update `AI-progress.json` before ending

## Hard Rules
- **Verification first**: `nix flake check --no-build` + `nix build` before claiming done
- **Format before commit**: `nix fmt` (enforced by git hooks via `nix develop`)
- **Never auto-commit**: show diff, user approves
- **Never commit as Claude**: commit as `Daaboulex <39669593+Daaboulex@users.noreply.github.com>`
- **No Co-Authored-By trailers**
- **Don't restructure** code you weren't asked to touch
- **One task per session** — finish, verify, move on

## Required File Structure
```
.github/workflows/{ci.yml,update.yml,maintenance.yml}
.github/update.json
scripts/update.sh
.editorconfig
.gitignore
LICENSE
README.md
CLAUDE.md
GEMINI.md
flake.nix (formatter + checks + devShells + git-hooks)
```

## Flake Requirements
- Inputs: `nixpkgs/nixos-unstable` + `git-hooks.nix` (with `nixpkgs.follows`)
- No `flake-utils` — use `nixpkgs.lib.genAttrs`
- Pattern: `localSystem.system = system`
- Always export: `formatter`, `checks` (pre-commit), `devShells` (shellHook + nil)
- Package repos: `packages` + `overlays.default`
- Module repos: `nixosModules.default` and/or `homeManagerModules.default`

## Workflows
1. **ci.yml** — on PR/push: eval, format check
2. **maintenance.yml** — weekly: flake.lock update, stale branch cleanup

## AI Tracking
- `AI-progress.json` / `AI-tasks.json` — local session tracking (gitignored)
- Update `AI-progress.json` at end of session with verified accomplishments only
- `verifiedBy` required: `nix-flake-check`, `nix-eval`, `build-passed`, `human-tested`

## Commands
```bash
nix develop      # Enter dev shell (installs git hooks)
nix fmt          # Format code
nix build      # Build package
nix flake check  # Run all checks
```
