<!-- markdownlint-disable MD013 -->

# CoreCycler

<!-- BEGIN generated:badges -->
[![CI](https://github.com/Daaboulex/linux-corecycler/actions/workflows/ci.yml/badge.svg)](https://github.com/Daaboulex/linux-corecycler/actions/workflows/ci.yml)
[![NixOS unstable](https://img.shields.io/badge/NixOS-unstable-78C0E8?logo=nixos&logoColor=white)](https://nixos.org)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/License-GPLv3-blue.svg)](./LICENSE)
<!-- END generated:badges -->

Per-core CPU stability testing and AMD PBO Curve Optimizer tuning for Linux -- a graphical tool that stress-tests one core at a time at full single-threaded boost, optionally reads and writes per-core Curve Optimizer offsets via the AMD SMU, and includes an **automatic crash-safe tuner** that searches each core's most aggressive stable undervolt for you. A Linux counterpart to [CoreCycler](https://github.com/sp00n/corecycler) (Windows).

> **Status:** actively developed and tested on an **AMD Ryzen 9 9950X3D** (Zen 5, dual-CCD X3D, AM5). Other AMD Ryzen processors (Zen 2-5) should work but are less thoroughly tested; Intel CPUs are supported for stress testing only (no Curve Optimizer). Found a problem on other hardware? [Open an issue](https://github.com/Daaboulex/linux-corecycler/issues) with your CPU model.

<!-- BEGIN generated:upstream -->
## Project

| | |
|---|---|
| **Type** | Original project (no upstream) |
| **License** | GPL-3.0-or-later |
| **Platforms** | Linux (`x86_64`) |

<!-- END generated:upstream -->

## What Is This?

AMD Precision Boost Overdrive Curve Optimizer (CO) adjusts the voltage-frequency curve per core: a negative offset lowers voltage at a given frequency so the core boosts higher within its limits. Each core is different -- one may hold -30 while another is unstable past -10 -- so finding the right value needs **per-core** testing.

All-core tests (Prime95 all threads, Cinebench) cannot find this: under all-core load every core runs at lower clocks and voltages than it would alone, so a core that passes all-core can still crash when it boosts to its single-threaded peak. CoreCycler tests one core at a time at full boost, cycling through every core, and adds idle and variable-load tests because CO instability often shows up on C-state wake and load transitions, not under sustained load.

Design principles:

- **Safety first** -- CO writes go only through the SMU and are volatile (reset on reboot); BIOS PBO settings are never touched. The automatic tuner can never settle on a configuration that crashes the machine (see [Safety](#safety)).
- **Per-core, full-boost** -- one core under load at a time, the only way to expose per-core CO limits.
- **Correctness over "did not crash"** -- instability is judged by MCE events and self-checking compute workloads, not merely the absence of a crash.
- **Capability-driven** -- topology, sensors, and SMU support are probed; nothing is assumed.

## Safety

Read this before using the Curve Optimizer features. Full scope in [SECURITY.md](./SECURITY.md).

- **CO writes are volatile** -- they live only in the CPU's runtime SMU state and **always reset on reboot**. There is no path to BIOS/UEFI NVRAM; a reboot fully restores your BIOS settings.
- **Your BIOS PBO settings are never modified** -- only the runtime SMU state changes, and only when you explicitly write a value.
- **Stress testing changes nothing** -- it only runs workloads pinned to cores; it writes no CO, voltage, or frequency.
- **CO is written in only two places** -- the Curve Optimizer tab (per-core, each write behind a confirmation dialog, with dry-run and backup/restore) and the Auto-Tuner. They are mutually exclusive -- the tab is locked while the tuner runs.
- **The automatic tuner is crash-safe** -- a CO write-ahead journal records every value before it is applied, CO=0 (stock) is the only floor it trusts, and a resume-crash circuit breaker forces all cores to stock and quarantines a profile that keeps crashing -- so no sequence of crashes, reboots, or resumes can loop the machine into re-crashing. See [Crash safety](docs/usage.md#crash-safety).
- **Thermal protection** -- a configurable temperature limit (default 95C) pauses testing; the tuner fails closed if no temperature sensor is readable.

## Installation

<!-- BEGIN generated:installation -->
Add the flake input and enable the NixOS module:

```nix
{
  inputs.corecycler.url = "github:Daaboulex/linux-corecycler";
}
```

```nix
{
  imports = [ inputs.corecycler.nixosModules.default ];

  services.corecycler = {
    enable = true;
    deviceAccessUser = "your-username";  # required -- grants MSR/SMU access without sudo
  };
}
```

The module handles the package, kernel modules (ryzen_smu and friends), udev rules, and
permissions. Or run it directly:

```bash
nix run github:Daaboulex/linux-corecycler        # FOSS-only
nix run github:Daaboulex/linux-corecycler#full   # with mprime (unfree)
```

Other distros (Arch, Ubuntu, Fedora, from source), the full module options, and backend
setup are in [docs/installation.md](docs/installation.md).
<!-- END generated:installation -->

## Usage

Launch with `sudo corecycler` (root is recommended for full telemetry and SMU access).
Pick a backend and preset in the Configuration tab and click **Start Test**, or use the
**Auto-Tuner** tab to search every core's stable offset automatically. The full guide --
manual tuning workflow, the Auto-Tuner, and reading results -- is in
[docs/usage.md](docs/usage.md).

## Documentation

- [Installation](docs/installation.md) -- all distros, NixOS module options, backends, kernel modules.
- [Hardware support](docs/hardware.md) -- per-generation Curve Optimizer / SMU support.
- [Usage guide](docs/usage.md) -- presets, manual tuning, the Auto-Tuner, crash safety, reading results.
- [Architecture](docs/architecture.md) -- source layout, development, driver sources.

## Development

```bash
nix develop          # Python 3.12 + pytest + ruff + hypothesis
pytest               # run the suite
ruff check src       # lint
nix flake check      # build + every check (what CI runs)
```

<!-- BEGIN generated:options -->
## Options

This module declares options under `services.corecycler`; see the
[module options](docs/installation.md#module-options) reference or
[`nix/module.nix`](nix/module.nix).
<!-- END generated:options -->

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

<!-- BEGIN generated:footer -->
---

*Maintained as part of the [Daaboulex](https://github.com/Daaboulex) NixOS ecosystem.*
<!-- END generated:footer -->
