# Security Policy

## Supported Versions

This is an original project (no upstream tracking). The latest commit on the default branch is the only supported version.

## Reporting a Vulnerability

Please report security vulnerabilities privately via GitHub Security Advisories:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Provide:
   - A description of the issue
   - Steps to reproduce
   - Potential impact
   - Any suggested mitigations

You will receive an initial response within 7 days. If the report is confirmed, a fix will be prepared privately and released with an advisory.

Please do **not** open public issues for security problems.

## Threat Model

This tool directly accesses AMD CPU hardware via kernel interfaces. The security scope is broader than a typical Nix package.

### Hardware Write Surface

The tool writes to AMD SMU (System Management Unit) registers via `/sys/kernel/ryzen_smu_drv/`:

- **Curve Optimizer offsets** — per-core voltage margin adjustment (negative = undervolt, positive = overvolt)
- **PBO power limits** — PPT, TDC, EDC scalar modification
- **Boost frequency limits** — per-core and package boost override
- **OC mode** — enable/disable overclocking mode flag

All writes are volatile (reset on reboot) but can cause immediate instability, crashes, or hardware degradation during the active session.

### Hardware Damage Risk

- **Positive CO offsets increase CPU voltage.** On V-Cache processors (5800X3D, 7800X3D, 9800X3D, 9950X3D), positive offsets can degrade or permanently damage the 3D V-Cache die under thermal load.
- **The application enforces hardware-reported ranges, not "safe" ranges.** The full hardware range is exposed (e.g., -30 to +30 for Zen 3).
- **The Auto-Tuner amplifies risk** by iteratively modifying offsets during extended stress tests.

### MSR Read Surface

The tool reads Model-Specific Registers via `/dev/cpu/N/msr`:

- APERF/MPERF (effective frequency / clock stretch detection)
- RAPL energy counters (per-core and package power)

All MSR access is **read-only** (`O_RDONLY`). No MSR write path exists in the codebase.

Note: Group read access to `/dev/cpu/N/msr` exposes ALL readable MSRs on all logical CPUs, including speculation control state and microarchitectural configuration. This is an accepted trade-off for non-root monitoring.

### Privilege Model

- A dedicated `corecycler` system group provides device access without sudo.
- **MSR:** `MODE="0640"` — root read/write, group read-only, world none.
- **SMU sysfs:** `0660 root:corecycler` — group read/write (enables CO writes without root).
- Any user in the `corecycler` group can send arbitrary SMU commands, not limited to CO.

### System-Wide Side Effects

- `kernel.dmesg_restrict = 0` is set with `mkDefault` when `deviceAccess = true` (required for MCE error detection). This allows unprivileged dmesg access system-wide.

### Supply Chain

- All Nix inputs are pinned via `flake.lock` with concrete revisions and NAR hashes.
- Out-of-tree kernel module sources (ryzen_smu, zenpower5, it87) are fetched from pinned GitHub commits with integrity hashes.
- No auto-update mechanism is active (upstream type is "none").

### Out of Scope

- Bugs in the ryzen_smu kernel driver itself
- Hardware-level SMU protocol exploits
- Physical access attacks
- Vulnerabilities in upstream nixpkgs packages (stress-ng, mprime, etc.)
