<!-- markdownlint-disable MD013 -->

# Installation

The main [README](../README.md) covers the quick NixOS flake install. This page covers
the full NixOS module, other distros, kernel modules, and stress backends.

Full telemetry and the Curve Optimizer need access to MSR and SMU devices -- grant it to
your user once and no `sudo` is needed, see [Device access](#device-access).

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
  unfreeBackends = true;   # include mprime (best for CO tuning) and y-cruncher
  zenpower = true;         # zenpower5: richer monitoring than k10temp
  nct6775 = true;          # Nuvoton Super I/O: motherboard Vcore fallback
  spd5118 = true;          # DDR5 DIMM temperatures via the SPD hub
};
```

### Module options

| Option | Type | Default | Description |
|---|---|---|---|
| `enable` | bool | `false` | Enable CoreCycler |
| `unfreeBackends` | bool | `false` | Include mprime and y-cruncher (unfree). When false, stress-ng and stressapptest are bundled |
| `ryzenSmu` | bool | `true` | Load [ryzen_smu](https://github.com/amkillam/ryzen_smu) for CO read/write via SMU (Zen 1-5) |
| `zenpower` | bool | `false` | Load [zenpower5](https://github.com/mattkeenan/zenpower5) instead of k10temp -- temps, SVI2 voltage (Zen 1-4), RAPL power. Blacklists k10temp |
| `coretemp` | bool | `false` | Load in-tree coretemp for Intel CPU temperatures |
| `nct6775` | bool | `false` | Load in-tree nct6775 for Nuvoton NCT6775-NCT6799 Super I/O (Vcore, fans, temps): ASUS, MSI, ASRock |
| `nct6683` | bool | `false` | Load in-tree nct6683 for Nuvoton NCT6683/6686/6687 Super I/O: modern MSI (B550, B650, X570, X670) |
| `it87` | bool | `false` | Load out-of-tree [it87](https://github.com/frankcrawford/it87) for ITE Super I/O (Gigabyte) |
| `cpuid` | bool | `false` | Load in-tree cpuid module for `/dev/cpu/*/cpuid` |
| `spd5118` | bool | `false` | Load spd5118 + i2c_dev for DDR5 DIMM temperature monitoring |
| `deviceAccess` | bool | `true` | Grant `deviceAccessUser` access to MSR/SMU sysfs without sudo |
| `deviceAccessUser` | string | `""` | Username for device access (required when `deviceAccess` is true) |
| `autoResume.enable` | bool | `false` | Resume the active tuner session after login, sudo-less via the device-access group |
| `autoResume.delaySeconds` | int | `120` | Settle time after login before that resume runs |

Out-of-tree modules (ryzen_smu, zenpower5, it87) are built against your running kernel;
both GCC and Clang/LTO kernels (e.g. CachyOS) are auto-detected. In-tree modules (msr,
nct6775, nct6683, coretemp, cpuid, spd5118 + i2c_dev) load via `boot.kernelModules`.

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
| `packages.full` | stress-ng, stressapptest, mprime, y-cruncher | Yes (mprime, y-cruncher) |

`packages.full` is built off-CI (its unfree backends are not on `cache.nixos.org`, so CI
only eval-gates it); build it yourself with `nix build .#full` and `allowUnfree`. Both
variants bundle setpriv (util-linux); core pinning uses a systemd cgroup scope
(`systemd-run -p AllowedCPUs=...`), so the host needs systemd — without it, stress
launches are refused rather than run unpinned.

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
sudo pacman -S python pyside6 stress-ng dmidecode
yay -S stressapptest        # AUR -- not in the official repos
yay -S mprime-bin           # AUR, optional -- unfree, best backend for CO tuning
yay -S y-cruncher           # AUR, optional -- unfree, secondary validation

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler && sudo python src/corecycler/main.py
```

### Ubuntu / Debian

```bash
sudo apt install python3 python3-venv stress-ng stressapptest dmidecode build-essential linux-headers-$(uname -r)

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/corecycler/main.py
```

### Fedora

```bash
sudo dnf install python3 stress-ng stressapptest dmidecode kernel-devel gcc make

git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu && cd ..

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/corecycler/main.py
```

### From source (any distro)

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/corecycler/main.py
```

Install stress backends and kernel modules separately (below). Requires Python 3.12+
and PySide6 >= 6.7.

## Device access

Stress testing and temperature monitoring work as your own user. Everything that reads a
model-specific register or the SMU needs permission on those devices:

| Feature | Plain user | With device access | As root |
|---|---|---|---|
| Stress testing (per-core cycling) | Full | Full | Full |
| Temperature, per-CCD temps, frequency | Full | Full | Full |
| Package power | Via hwmon (zenpower) | Full | Full |
| Per-core power (RAPL MSR) | N/A | Full | Full |
| Clock stretch detection (APERF/MPERF) | N/A | Full | Full |
| Vcore voltage | Via Super I/O or zenpower | Same | Same |
| DIMM info (dmidecode) | N/A | N/A | Full |
| Curve Optimizer (SMU read/write) | N/A | Full | Full |
| Kernel error detection (MCE via dmesg) | Needs `kernel.dmesg_restrict=0` | Full | Full |

Two limits worth knowing. `dmidecode` reads the raw DMI table, which only root may open,
so the Memory tab's DIMM details need a root run whatever else you grant. And MCE
detection shells out to `dmesg`: where `kernel.dmesg_restrict` is 1, a non-root run reads
nothing and reports no kernel errors, so the sysctl below is part of device access, not an
extra. Without access the status bar warns and unavailable data shows "N/A" rather than
stale values.

### Grant it to your user (no sudo)

The better option: a GUI then runs as your own user, in your own session, with your own
settings and file ownership.

On NixOS the module does it -- set `deviceAccess = true` (the default) and
`deviceAccessUser` to your username. On other distros there are three parts. The group,
the MSR udev rule and unrestricted `dmesg`:

```bash
sudo groupadd -f corecycler && sudo usermod -aG corecycler "$USER"
echo 'SUBSYSTEM=="msr", KERNEL=="msr[0-9]*", GROUP="corecycler", MODE="0640"' \
  | sudo tee /etc/udev/rules.d/99-corecycler-msr.rules
echo 'kernel.dmesg_restrict = 0' | sudo tee /etc/sysctl.d/99-corecycler-dmesg.conf
sudo sysctl --system && sudo udevadm control --reload && sudo modprobe msr
```

Then the SMU permissions oneshot, which is under
[ryzen_smu kernel module](#ryzen_smu-kernel-module) because it has to be ordered after the
module loads. Log out and back in for the group to take effect.

### Or run as root

```bash
sudo corecycler          # Nix-installed
sudo python src/corecycler/main.py  # from source
```

Running as root is supported: all persistent state (the history database at
`~/.local/share/corecycler/history/history.db` and settings at
`~/.config/corecycler/`) always resolves to the INVOKING user, so root and
non-root runs share one database, and files a root run creates are handed back
to the user. History that an older version wrote to `/root` is adopted into
the user database once at startup (the source is renamed `*.adopted`). The
graphical session handshake (Wayland socket / X11 authority) and the desktop's
appearance are derived from the invoking user's session automatically -- the
appearance read-only, so nothing is written into their configuration; when no
display is reachable the app exits with an actionable message instead of
aborting.

One thing depends on your distribution: a D-Bus session bus authenticates by peer
credentials, and some refuse a connection from another user (CachyOS does). A run tests
the bus before using it, so nothing is left calling an address it cannot reach; where the
bus does refuse, desktop notifications are unavailable under sudo. Nothing else about a
sudo run is second class: the Wayland socket is reached by its own path rather than
through your runtime directory, which is what root borrowing that directory used to
complain about once per lookup.

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
| y-cruncher | Freeware | Medium-High | Secondary validation, AVX-heavy | `packages.full` only |
| stressapptest | Apache-2.0 | High (memory) | DDR5/RAM stability, memory controller | Both variants |

**mprime** is the most sensitive backend for CO tuning: its small-FFT workloads draw
high single-core power and surface CO instability as computation errors rather than
hard crashes. It is unfree; `packages.full` includes it, or install separately:
`pkgs.mprime` (NixOS, needs `allowUnfree`), `yay -S mprime-bin` (Arch), or download from
[mersenne.org](https://www.mersenne.org/download/) and `install -m755 mprime /usr/local/bin/`.

**stress-ng** installs from every distro's repos and is bundled in both Nix variants;
**stressapptest** likewise, except on Arch where it is AUR-only. **y-cruncher** is
packaged as `pkgs.y-cruncher` (bundled in `packages.full`) and as the AUR `y-cruncher`;
everywhere else download the tarball from
[numberworld.org](http://www.numberworld.org/y-cruncher/) and extract it anywhere --
CoreCycler finds it (see [Where CoreCycler looks for a
backend](#where-corecycler-looks-for-a-backend)).

## Where CoreCycler looks for a backend

`corecycler doctor` prints every external tool, where it resolved from, and any
candidate it found but has not been told to use.

A backend is located in this order, most explicit first:

1. `CORECYCLER_<TOOL>_BIN` -- e.g.
   `CORECYCLER_Y_CRUNCHER_BIN=/home/you/y-cruncher/y-cruncher`, `CORECYCLER_MPRIME_BIN`,
   `CORECYCLER_STRESS_NG_BIN`.
2. The path recorded in `~/.config/corecycler/tool-paths.json`, which the GUI writes
   when you pick a binary in the missing-backend dialog.
3. `PATH`.

An explicit path that is not an executable file is refused with the reason -- it never
silently falls back to a different binary.

**Why a tool on your PATH can still be "not found": `sudo` replaces PATH.** Debian,
Ubuntu, Linux Mint, Fedora and Arch all ship `Defaults secure_path=...` in
`/etc/sudoers`, so a directory you added to PATH in your shell is gone inside
`sudo corecycler`; a desktop launcher never sees a shell PATH at all. mprime and
y-cruncher are extracted tarballs, so this is their normal state.

CoreCycler therefore also looks for an extracted mprime or y-cruncher in the invoking
user's `~`, `~/Downloads`, `/opt`, `/usr/local`, `/usr/local/lib` and `/usr/lib`, and
offers what it finds. It never runs a binary found that way until you pick it: it runs
as root, and silently executing something found in `$HOME` is exactly what `secure_path`
exists to prevent. Once picked, the path is recorded and every later run resolves it
without asking.

## ryzen_smu kernel module

Required for the Curve Optimizer tab and Auto-Tuner; not for stress testing. The
[amkillam fork](https://github.com/amkillam/ryzen_smu) supports Zen 1 through Zen 5. On
NixOS the module handles it when `ryzenSmu = true` (default). On other distros:

```bash
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

ls /sys/kernel/ryzen_smu_drv/
# Expected to include: mp1_smu_cmd  rsmu_cmd  smu_args  smn  version  pm_table
```

Reading/writing CO through sysfs needs root, or group ownership of the four SMU
files. A udev rule races the driver's own sysfs creation, so grant them from a
oneshot ordered after the module load -- the same thing the NixOS module does:

```bash
sudo groupadd -f corecycler && sudo usermod -aG corecycler "$USER"

# /etc/systemd/system/corecycler-smu-permissions.service
[Unit]
After=systemd-modules-load.service
ConditionPathExists=/sys/kernel/ryzen_smu_drv/smu_args

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'cd /sys/kernel/ryzen_smu_drv && chgrp corecycler smu_args mp1_smu_cmd rsmu_cmd smn && chmod 0660 smu_args mp1_smu_cmd rsmu_cmd smn'

[Install]
WantedBy=multi-user.target
```

`smn` is in that list because an SMN register read is a *write* of the address,
and the SMU's core-disable fuse -- read over SMN -- is the only thing that says
which physical core slots are fused off on a harvested CPU whose BIOS renumbers
core ids. Without it, per-core CO is refused on those parts rather than applied
to the wrong core. Granting the group write access to `smn` also grants it
arbitrary SMN register access; it is the same trust boundary as the SMU mailbox
files next to it, so grant it only to a user you would already trust with those.
