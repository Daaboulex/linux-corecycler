<!-- markdownlint-disable MD013 -->

# Installation

The main [README](../README.md) covers the quick NixOS flake install. This page covers
the full NixOS module, other distros, kernel modules, and stress backends.

Run CoreCycler as root (`sudo`) for full functionality -- see [Running as root](#running-as-root).

## NixOS module

```nix
{
  imports = [ inputs.corecycler.nixosModules.default ];

  services.corecycler = {
    enable = true;
    deviceAccessUser = "your-username";  # required -- user added to the corecycler group
  };
}
```

The module handles the package, kernel modules, udev rules for MSR access, a systemd
oneshot for SMU sysfs permissions, and the `corecycler` group. No manual kernel-module
setup is needed.

Fuller example (AMD Zen 5 desktop, Nuvoton Super I/O, DDR5 temps):

```nix
services.corecycler = {
  enable = true;
  deviceAccessUser = "your-username";
  unfreeBackends = true;   # include mprime (best for CO tuning)
  zenpower = true;         # zenpower5: richer monitoring than k10temp
  nct6775 = true;          # Nuvoton Super I/O: motherboard Vcore fallback
  spd5118 = true;          # DDR5 DIMM temperatures via the SPD hub
};
```

### Module options

| Option | Type | Default | Description |
|---|---|---|---|
| `enable` | bool | `false` | Enable CoreCycler |
| `unfreeBackends` | bool | `false` | Include mprime (unfree). When false, stress-ng and stressapptest are bundled |
| `ryzenSmu` | bool | `true` | Load [ryzen_smu](https://github.com/amkillam/ryzen_smu) for CO read/write via SMU (Zen 1-5) |
| `zenpower` | bool | `false` | Load [zenpower5](https://github.com/mattkeenan/zenpower5) instead of k10temp -- temps, SVI2 voltage (Zen 1-4), RAPL power. Blacklists k10temp |
| `coretemp` | bool | `false` | Load in-tree coretemp for Intel CPU temperatures |
| `nct6775` | bool | `false` | Load in-tree nct6775 for Nuvoton Super I/O (Vcore, fans, temps): ASUS, MSI, ASRock |
| `it87` | bool | `false` | Load out-of-tree [it87](https://github.com/frankcrawford/it87) for ITE Super I/O (Gigabyte) |
| `cpuid` | bool | `false` | Load in-tree cpuid module for `/dev/cpu/*/cpuid` |
| `spd5118` | bool | `false` | Load spd5118 + i2c_dev for DDR5 DIMM temperature monitoring |
| `deviceAccess` | bool | `true` | Grant `deviceAccessUser` access to MSR/SMU sysfs without sudo |
| `deviceAccessUser` | string | `""` | Username for device access (required when `deviceAccess` is true) |

Out-of-tree modules (ryzen_smu, zenpower5, it87) are built against your running kernel;
both GCC and Clang/LTO kernels (e.g. CachyOS) are auto-detected. In-tree modules (msr,
nct6775, coretemp, cpuid) load via `boot.kernelModules`.

### Package-only (no module)

```nix
# FOSS-only (stress-ng + stressapptest bundled, no unfree software)
environment.systemPackages = [ inputs.corecycler.packages.${pkgs.system}.default ];

# Full (also bundles mprime -- requires allowUnfree)
environment.systemPackages = [ inputs.corecycler.packages.${pkgs.system}.full ];
```

| Variant | Backends | Unfree |
|---|---|---|
| `packages.default` | stress-ng, stressapptest | No |
| `packages.full` | stress-ng, stressapptest, mprime | Yes (mprime) |

`packages.full` is built off-CI (mprime is unfree and not on `cache.nixos.org`, so CI
only eval-gates it); build it yourself with `nix build .#full` and `allowUnfree`. Both
variants bundle taskset (util-linux) for core pinning.

## Nix (any distro)

```bash
nix run github:Daaboulex/linux-corecycler        # FOSS-only
nix run github:Daaboulex/linux-corecycler#full   # with mprime
```

## Other distros

PySide6 is installed into a venv because `pip3 install` is blocked by PEP 668 on recent
Ubuntu / Fedora / Mint; `sudo` must then use the venv's python.

### Arch Linux

```bash
sudo pacman -S python python-pyside6 stress-ng stressapptest dmidecode
yay -S mprime-bin   # AUR, optional -- unfree, best backend for CO tuning

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler && sudo python src/main.py
```

### Ubuntu / Debian

```bash
sudo apt install python3 python3-venv stress-ng stressapptest dmidecode build-essential linux-headers-$(uname -r)

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/main.py
```

### Fedora

```bash
sudo dnf install python3 stress-ng dmidecode kernel-devel gcc make

# stressapptest builds from source (not in the default repos)
git clone https://github.com/stressapptest/stressapptest.git
cd stressapptest && ./configure && make && sudo make install && cd ..

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu && cd ..

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/main.py
```

### From source (any distro)

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/main.py
```

Install stress backends and kernel modules separately (below). Requires Python 3.12+
and PySide6 >= 6.7.

## Running as root

```bash
sudo corecycler          # Nix-installed
sudo python src/main.py  # from source
```

| Feature | Without root | With root |
|---|---|---|
| Stress testing (per-core cycling) | Full | Full |
| Temperature, per-CCD temps, frequency | Full | Full |
| Package power | Via hwmon (zenpower) | Full |
| Per-core power (RAPL MSR) | Needs `/dev/cpu/N/msr` | Full |
| Clock stretch detection (APERF/MPERF) | Needs `/dev/cpu/N/msr` | Full |
| Vcore voltage | Via Super I/O or zenpower | Via Super I/O or zenpower |
| DIMM info (dmidecode) | Needs root | Full |
| Curve Optimizer (SMU read/write) | Needs `/sys/kernel/ryzen_smu_drv` | Full |

Without root the status bar warns and unavailable data shows "N/A" rather than stale
values. On non-NixOS distros, grant non-root access by setting permissions on
`/dev/cpu/*/msr` and `/sys/kernel/ryzen_smu_drv/*` (see the [udev rule](#ryzen_smu-kernel-module)),
or just run as root.

Running as root is first-class: all persistent state (the history database at
`~/.local/share/corecycler/history/history.db` and settings at
`~/.config/corecycler/`) always resolves to the INVOKING user, so root and
non-root runs share one database, and files a root run creates are handed back
to the user. History that an older version wrote to `/root` is adopted into
the user database once at startup (the source is renamed `*.adopted`). The
graphical session handshake (Wayland socket / X11 authority) is derived from
the invoking user's session automatically; when no display is reachable the
app exits with an actionable message instead of aborting.

On Zen 5, Vcore telemetry uses SVI3, which no Linux driver supports yet; the tool falls
back to the motherboard Super I/O chip (Nuvoton NCT668x/NCT677x-NCT679x, ITE
IT862x-IT877x), scanning input labels for the Vcore channel. If neither source is
available, Vcore shows "N/A".

## Kernel modules

None are required for basic stress testing; each unlocks more functionality.

| Module | Type | Purpose | Required for |
|---|---|---|---|
| `msr` | In-tree | `/dev/cpu/N/msr` for APERF/MPERF and per-core RAPL | Clock stretch, per-core power |
| `ryzen_smu` | Out-of-tree | SMU sysfs for CO read/write, PBO limits, PM table | Curve Optimizer tab, Auto-Tuner |
| `zenpower` / `zenpower5` | Out-of-tree | Richer AMD hwmon (SVI2/SVI3 voltage, RAPL) than k10temp | Better voltage/power monitoring |
| `nct6683` / `nct6775` | In-tree | Nuvoton Super I/O (Vcore via label scan) | Motherboard Vcore on Zen 5 |
| `it87` | Out-of-tree | ITE Super I/O | Motherboard Vcore on Gigabyte boards |
| `spd5118` + `i2c_dev` | In-tree | DDR5 DIMM temperature via the SPD hub | Live DIMM temperatures |
| `coretemp` | In-tree | Intel CPU temperature | Intel systems only |

On non-NixOS distros, load in-tree modules with `sudo modprobe <name>`; build
out-of-tree modules from source.

## Stress backends

You need at least one. The Nix package bundles them automatically.

| Backend | License | Sensitivity | Best for | Bundled in Nix |
|---|---|---|---|---|
| mprime (Prime95 CLI) | Unfree (free to use) | Highest | CO tuning, finding per-core limits | `packages.full` only |
| stress-ng | GPL-2.0 | Medium | General stability, quick screening | Both variants |
| y-cruncher | Freeware | Medium-High | Secondary validation, AVX-heavy | Not bundled |
| stressapptest | Apache-2.0 | High (memory) | DDR5/RAM stability, memory controller | Both variants |

**mprime** is the most sensitive backend for CO tuning: its small-FFT workloads draw
high single-core power and surface CO instability as computation errors rather than
hard crashes. It is unfree; `packages.full` includes it, or install separately:
`pkgs.mprime` (NixOS, needs `allowUnfree`), `yay -S mprime-bin` (Arch), or download from
[mersenne.org](https://www.mersenne.org/download/) and `install -m755 mprime /usr/local/bin/`.

**stress-ng** and **stressapptest** install from each distro's repos
(`apt`/`pacman`/`dnf`) and are bundled in both Nix variants. **y-cruncher** is not
packaged in nixpkgs -- download from
[numberworld.org](http://www.numberworld.org/y-cruncher/) and place on PATH.

## ryzen_smu kernel module

Required for the Curve Optimizer tab and Auto-Tuner; not for stress testing. The
[amkillam fork](https://github.com/amkillam/ryzen_smu) supports Zen 1 through Zen 5. On
NixOS the module handles it when `ryzenSmu = true` (default). On other distros:

```bash
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

ls /sys/kernel/ryzen_smu_drv/
# Expected: mp1_smu_cmd  rsmu_cmd  smu_args  version  pm_table
```

Reading/writing CO through sysfs needs root or a udev rule granting group access:

```bash
# /etc/udev/rules.d/99-ryzen-smu.rules
KERNEL=="ryzen_smu_drv", SUBSYSTEM=="platform", ATTR{smu_args}="", \
  RUN+="/bin/chmod 0660 /sys/kernel/ryzen_smu_drv/smu_args /sys/kernel/ryzen_smu_drv/rsmu_cmd /sys/kernel/ryzen_smu_drv/mp1_smu_cmd"
```
