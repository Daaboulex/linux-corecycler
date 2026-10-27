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

### Hardware Write Surface

The tool changes two hardware settings, both sent to the AMD SMU (System Management Unit) through `/sys/kernel/ryzen_smu_drv/`: the per-core **Curve Optimizer offset** and, on Ryzen 9000, the **Curve Shaper**, a margin for each of five frequency bands at three temperatures that applies to every core on top of its offset (negative = undervolt, positive = overvolt, for both). The Curve Optimizer writers are the Curve-Tuner tab, the Auto-Tuner (its tab or `corecycler tune`) and the developer rig `python -m corecycler.tuner.live_hw --write`. The Curve Shaper writers are the Curve-Tuner tab and `corecycler shaper --set`; both refuse while a tuning session runs.

The SMU driver (`src/corecycler/smu/driver.py`) also has setters for the PPT, TDC and EDC limits, the boost limit and the PBO scalar. No GUI or CLI path calls them. There is no OC-mode write.

All writes are volatile (reset on reboot) but can cause immediate instability, crashes, or hardware degradation during the active session.

### Hardware Damage Risk

- **Positive CO offsets and Curve Shaper margins increase CPU voltage.** On V-Cache processors (5800X3D, 7800X3D, 9800X3D, 9950X3D), positive offsets can degrade or permanently damage the 3D V-Cache die under thermal load.
- **The application enforces a fixed CO range per CPU generation, not a "safe" range.** The range comes from a static table in `src/corecycler/smu/commands.py`, not from the hardware (e.g., -30 to +30 for Zen 3). Every Curve Shaper margin is held to -50 to +30 (`SHAPER_MARGINS` in `src/corecycler/smu/driver.py`), also not a safe range.
- **The Auto-Tuner amplifies risk** by iteratively modifying offsets during extended stress tests.

### MSR Read Surface

The tool reads Model-Specific Registers via `/dev/cpu/N/msr`:

- MPERF, read once to check that the device opens
- RAPL energy counters (per-core and package power)

All MSR access is **read-only** (`O_RDONLY`). No MSR write path exists in the codebase.

The kernel's msr driver refuses to open `/dev/cpu/N/msr` without `CAP_SYS_RAWIO`, whatever the file mode, so MSR reads happen only in a root run.

### External Binary Execution

The tool executes third-party stress binaries (mprime, y-cruncher, stress-ng,
stressapptest) plus `systemd-run`, `setpriv`, `journalctl`, `dmidecode` and
`notify-send`, as root whenever CoreCycler itself is run under sudo.

Each is resolved in `src/corecycler/config/tools.py`, in the order given in
[Where CoreCycler looks for a backend](docs/installation.md#where-corecycler-looks-for-a-backend).
The environment variable and `~/.config/corecycler/tool-paths.json` are user-writable, so
whoever can write them chooses what a sudo run executes. That is the same trust the user
already holds by typing `sudo corecycler`.

### Privilege Model

- A dedicated `corecycler` system group provides SMU and SMN access without sudo. It gives no MSR access.
- **MSR:** `MODE="0640"`: root read/write, group read-only, world none. The kernel still refuses the group without `CAP_SYS_RAWIO`.
- **SMU sysfs:** `0660 root:corecycler` — group read/write (enables CO writes without root).
- **Machine-check poll:** `/sys/devices/system/machinecheck/machinecheck0/check_interval` is
  `0664 root:corecycler` when `machineCheckPoll.enable` is on (the default with
  `deviceAccess`). A group member can change how often the kernel polls every CPU's
  machine-check banks; the tuner sets 5 s for its run and puts the old value back.
- **Sysrq trigger:** `/proc/sysrq-trigger` is `0220 root:corecycler` from each boot when
  `hangReset.enable` and `deviceAccess` are both on, so the hang watch can reset a hung
  machine without root. A group member can then run every sysrq function, not only the sync
  and reboot the watch uses: an immediate reboot or power-off, a kernel crash, killing every
  process.
- Any user in the `corecycler` group can send arbitrary SMU commands, not limited to CO.
- **SMN (`smn`):** also `0660 root:corecycler`. Reading an SMN register is a *write* of its
  address, so read access is unobtainable without write access, and write access to that file
  is arbitrary read/write of the SMN address space — a wider surface than the SMU mailbox
  beside it, reaching data-fabric registers unrelated to CO. It is granted because the
  per-CCD core-disable fuse is the only thing that identifies the fused-off physical core
  slots on a harvested CPU whose firmware renumbers core ids, and on Vermeer the CCD-disable
  fuses are the only thing that names each CCD's physical number; without it the tool refuses
  per-core CO on those parts (every Vermeer among them) rather than applying it to the wrong
  core. Grant the group only to a user already trusted with the SMU mailbox.

### System-Wide Side Effects

- `kernel.dmesg_restrict = 0` is set with `mkDefault` when `deviceAccess = true` (required for MCE error detection). This allows unprivileged dmesg access system-wide.

### Supply Chain

- All Nix inputs are pinned via `flake.lock` with concrete revisions and NAR hashes.
- Out-of-tree kernel module sources (ryzen_smu, zenpower5, it87) are fetched from pinned GitHub commits with integrity hashes.
- A scheduled workflow (`.github/workflows/maintenance.yml`) runs `nix flake update`, builds every check on each declared system, and pushes the new `flake.lock` to the default branch only when all of them pass.

### Out of Scope

- Bugs in the ryzen_smu kernel driver itself
- Hardware-level SMU protocol exploits
- Physical access attacks
- Vulnerabilities in upstream nixpkgs packages (stress-ng, mprime, etc.)
