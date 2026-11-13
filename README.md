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
| **Platforms** | Linux (`x86_64`, `aarch64`) |

<!-- END generated:upstream -->

## What Is This?

AMD Precision Boost Overdrive Curve Optimizer (CO) adjusts the voltage-frequency curve per core: a negative offset lowers voltage at a given frequency so the core boosts higher within its limits. Each core is different -- one may hold -30 while another is unstable past -10 -- so finding the right value needs **per-core** testing.

All-core tests (Prime95 all threads, Cinebench) cannot find this: under all-core load every core runs at lower clocks and voltages than it would alone, so a core that passes all-core can still crash when it boosts to its single-threaded peak. CoreCycler tests one core at a time at full boost, cycling through every core, and adds idle and variable-load tests because CO instability often shows up on C-state wake and load transitions, not under sustained load.

## Safety

Read this before using the Curve Optimizer features. Full scope in [SECURITY.md](./SECURITY.md).

- **CO writes are volatile.** They live only in the CPU's runtime SMU state and reset on reboot. Your BIOS settings are never written.
- **Two things write offsets.** The Curve-Tuner tab, which also writes the Curve Shaper as `corecycler shaper --set` does, and the tuner driver, `python -m corecycler.tuner.live_hw`, which writes only with `--write`; the Auto-Tuner tab and `corecycler tune`, `resume`, `validate`, `continue` and `restart` start it with that flag. Stress testing alone writes nothing. A tuner driver holds the CO writer lock for its whole run and the Curve-Tuner tab takes it for each write, so one driver runs at a time and the tab writes nothing while one runs. The driver is its own process: closing or crashing the GUI leaves a running session running.
- **The Auto-Tuner is crash-safe.** See [Crash safety](docs/usage.md#crash-safety).
- **Thermal protection.** A configurable temperature limit (default 95 C) stops the test. A manual run then ends. The Auto-Tuner cools down and retries the same offset, and fails closed when no temperature sensor is readable.

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
    deviceAccessUser = "your-username";  # required: SMU and CO access without sudo; MSR reads still need root
  };
  hardware.corecycler.ryzenSmu.enable = true;  # the SMU driver the Curve-Tuner and Auto-Tuner talk to
}
```

The module handles the package, the msr kernel module, udev rules, and permissions. The
sensor drivers are a separate module, `nixosModules.drivers`, which the tuner module
includes: ryzen_smu, zenpower5, asus-ec-sensors, nct6687d and it87 built from pinned sources
with your kernel's own toolchain, and the in-tree board sensors loaded by name. Each is off
until turned on, and a machine can import the drivers module alone. Or run it directly:

```bash
nix run github:Daaboulex/linux-corecycler        # FOSS-only
nix run github:Daaboulex/linux-corecycler#full   # with mprime (unfree)
```

Other distros (Arch, Ubuntu, Fedora, from source), the full module options, and backend
setup are in [docs/installation.md](docs/installation.md).
<!-- END generated:installation -->

## Usage

Launch with `corecycler`. Curve Optimizer access needs SMU permissions. Grant them to your
user once (`deviceAccessUser` on NixOS, a group and a oneshot service elsewhere, see
[Device access](docs/installation.md#device-access)). Clock stretch reads the SMU's PM table,
which that access covers. Per-core power and MSR package power read model-specific
registers, which need root today: run `sudo corecycler` for those.
Pick a backend and preset in the Configuration tab and click **Start Test**, or use the
**Auto-Tuner** tab to search every core's stable offset automatically. If a backend is
reported as not found, `corecycler doctor` prints where it looked and what it found. The
full guide -- manual tuning workflow, the Auto-Tuner, and reading results -- is in
[docs/usage.md](docs/usage.md).

## Documentation

- [Installation](docs/installation.md) -- all distros, NixOS module options, backends, kernel modules.
- [Hardware support](docs/hardware.md) -- per-generation Curve Optimizer / SMU support.
- [Usage guide](docs/usage.md) -- presets, manual tuning, the Auto-Tuner, crash safety, reading results.
- [Architecture](docs/architecture.md) -- source layout, driver sources.

## Development

The dev shell, the test suite and the gates are in [docs/development.md](docs/development.md);
how to contribute is in [CONTRIBUTING.md](CONTRIBUTING.md).

<!-- BEGIN generated:options -->
## Options

This module declares options under `services.corecycler`; see the
[module options](docs/installation.md#module-options) reference or
[`nix/module.nix`](nix/module.nix).
<!-- END generated:options -->

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

<!-- BEGIN generated:footer -->
<!-- END generated:footer -->
