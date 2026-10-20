<!-- markdownlint-disable MD013 -->

# Installation

## NixOS module

The flake input and the minimal module setup are in the [README](../README.md#installation).
The module handles the package, the in-tree kernel modules, a udev rule for the MSR devices, a
systemd oneshot for SMU sysfs permissions, and the `corecycler` group. The out-of-tree drivers
are their own module, `nixosModules.drivers` (`hardware.corecycler`), which the tuner module
includes: each driver is off until turned on, and a machine that wants the drivers without the
tuner imports only `nixosModules.drivers`. The old `services.corecycler.ryzenSmu`, `zenpower`
and `it87` options still work as renames. `machineCheckPoll.enable` (on by default) lets the
`corecycler` group raise the kernel's machine-check poll to 5 s, which the tuner does only
while a run lasts. `hangReset.enable` (off by default) makes a hung machine reset itself so an
unattended run resumes after the reboot: it panics on an oops, a soft or hard lockup and an RCU
stall, reboots 10 s after a panic, and has systemd feed the board's hardware watchdog; it acts on
every stall on the machine, and it refuses when the watchdog driver is blacklisted. With
`deviceAccess` it also lets the `corecycler` group write `/proc/sysrq-trigger` and runs
`corecycler hang-watch` in a user unit of `deviceAccessUser`, who then lingers, as
[Crash safety](usage.md#crash-safety) describes.
`autoResume.enable` (off by default) is what resumes that run: at boot, with no login, a user
unit of `deviceAccessUser` runs `corecycler resume --interrupted`, as
[Crash safety](usage.md#crash-safety) describes. It needs `deviceAccessUser`, who then lingers,
so that user's other user services start at boot too.

Fuller example (AMD Zen 5 desktop, Nuvoton Super I/O, DDR5 temps):

```nix
services.corecycler = {
  enable = true;
  deviceAccessUser = "your-username";
  unfreeBackends = true;   # include mprime (best for CO tuning) and y-cruncher
};
hardware.corecycler = {
  ryzenSmu.enable = true;  # the SMU driver the Curve-Tuner and Auto-Tuner talk to
  zenpower.enable = true;  # zenpower5: richer monitoring than k10temp
  nct6775.enable = true;   # Nuvoton Super I/O: motherboard Vcore fallback
  spd5118.enable = true;   # DDR5 DIMM temperatures via the SPD hub
};
```

### Module options

Every option, with its type, default and description, is declared in
[`nix/module.nix`](../nix/module.nix) and, for the drivers,
[`nix/drivers.nix`](../nix/drivers.nix).

The drivers module carries every sensor driver the tool reads, each off until a host turns it
on. The out-of-tree ones (ryzen_smu, zenpower5, asus-ec-sensors, nct6687d, it87) are built from
pinned sources against the kernel your configuration selects (`boot.kernelPackages`), with that
kernel's own compiler and linker; asus-ec-sensors and it87 install where they win over the
kernel's own copies of the same name. The in-tree ones (nct6775, nct6683, coretemp, spd5118 with
i2c_dev, asus_wmi_sensors, gigabyte_wmi, drivetemp) load by name. The tuner module itself loads
only msr, and cpuid when asked. A driver turned on here while the configuration also carries another
copy of it (nixpkgs' `hardware.cpu.amd.ryzen-smu`, a `linuxPackages.zenpower` and the like) is refused
at build, naming both: a kernel loads one module of a name.

### Package-only (no module)

```nix
# FOSS-only (stress-ng + stressapptest bundled, no unfree software)
environment.systemPackages = [ inputs.corecycler.packages.${pkgs.system}.default ];

# Full (also bundles mprime and y-cruncher, x86_64-linux only)
environment.systemPackages = [ inputs.corecycler.packages.${pkgs.system}.full ];
```

| Variant | Backends | Unfree |
|---|---|---|
| `packages.default` | stress-ng, stressapptest | No |
| `packages.full` | stress-ng, stressapptest, mprime, y-cruncher | Yes (mprime, y-cruncher) |

`packages.full` is built off-CI (its unfree backends are not on `cache.nixos.org`, so CI
only evaluates it). Build it with `nix build .#full`. The flake's own packages are built
with unfree allowed, so neither they nor the NixOS module need `allowUnfree`. Only the
overlay (`overlays.default`) builds against your nixpkgs, and its `linux-corecycler-full`
needs `allowUnfree` there.

Both variants bundle setpriv (util-linux). Core pinning uses a systemd cgroup scope
(`systemd-run -p AllowedCPUs=...`), so the host needs systemd. Without it, the stress
launches of a manual run and the Auto-Tuner are refused rather than run unpinned. The
Memory tab's test is never pinned.

## Nix (any distro)

`nix run` works without the module, see the [README](../README.md#installation).

## Other distros

PySide6 is installed into a venv because `pip3 install` is blocked by PEP 668 on recent
Ubuntu / Fedora / Mint; `sudo` must then use the venv's python. The kernel drivers are built
separately, each at the commit this repository pins: see [Kernel modules](#kernel-modules).

### Arch Linux

```bash
sudo pacman -S python pyside6 stress-ng dmidecode
yay -S stressapptest        # AUR -- not in the official repos
yay -S mprime-bin           # AUR, optional -- unfree, best backend for CO tuning
yay -S y-cruncher           # AUR, optional -- unfree, secondary validation

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler && sudo python src/corecycler/main.py
```

### Ubuntu / Debian

```bash
sudo apt install python3 python3-venv stress-ng stressapptest dmidecode build-essential linux-headers-$(uname -r)

git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && .venv/bin/pip install PySide6
sudo .venv/bin/python src/corecycler/main.py
```

### Fedora

```bash
sudo dnf install python3 stress-ng stressapptest dmidecode kernel-devel gcc make

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

Install stress backends and kernel modules separately (below). Requires Python 3.14+
and PySide6 >= 6.7.

## Device access

Stress testing and temperature monitoring work as your own user. The SMU, the
model-specific registers and some kernel interfaces need more:

| Feature | Plain user | With device access | As root |
|---|---|---|---|
| Stress testing (per-core cycling) | Full | Full | Full |
| Temperature, per-CCD temps, frequency | Full | Full | Full |
| Package power | Via hwmon (zenpower) | Same | Full |
| Per-core power (RAPL MSR) | N/A | N/A | Full |
| Clock stretch (PM table, a 16-core part on table 0x620205) | N/A | Full | Full |
| Vcore voltage | Via Super I/O or zenpower | Same | Same |
| DIMM info (dmidecode) | N/A | N/A | Full |
| Curve Optimizer (SMU read/write) | N/A | Full | Full |
| Kernel error detection (MCE via /dev/kmsg) | Needs `kernel.dmesg_restrict=0` | Full | Full |

What device access does not cover:

- **MSR reads need root.** Per-core power and the MSR fallback for package power read
  `/dev/cpu/N/msr`. The kernel opens it only for a process holding
  `CAP_SYS_RAWIO`, whatever the file's group and mode.
- **DIMM details need root.** `dmidecode` reads the raw DMI table, which only root may open.
- **MCE detection needs `/dev/kmsg`.** Where `kernel.dmesg_restrict` is 1, a non-root run
  cannot open it, so no test can pass and every result is "unverified". The sysctl below
  is part of device access, not an extra.
- **The Auto-Tuner needs the kernel journal.** It refuses to start unless
  `journalctl -b 0 -n 1 _TRANSPORT=kernel` prints a kernel record of this boot. Device
  access does not grant journal access.

Without access the status bar warns and unavailable data shows "N/A" rather than stale
values.

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

The MSR rule matches what the NixOS module installs. It sets the group and mode on
`/dev/cpu/N/msr`, but the kernel still refuses the open without `CAP_SYS_RAWIO`, so it
grants no MSR reads today.

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
bus does refuse, desktop notifications are unavailable under sudo.

## Kernel modules

None are required for basic stress testing; each unlocks more functionality.

| Module | Type | Purpose | Required for |
|---|---|---|---|
| `msr` | In-tree | `/dev/cpu/N/msr` for per-core and package RAPL (root only) | Per-core power |
| [`ryzen_smu`](https://github.com/amkillam/ryzen_smu) (amkillam fork) | Out-of-tree | SMU sysfs for CO read/write, PBO limit reads, PM table | Curve-Tuner tab, Auto-Tuner, clock stretch |
| `zenpower` ([zenpower5](https://github.com/mattkeenan/zenpower5)) | Out-of-tree | Richer AMD hwmon than k10temp: SVI2 voltage and current (Zen 1-4), RAPL power | Better voltage/power monitoring |
| `nct6683` / `nct6775` | In-tree | Nuvoton Super I/O (Vcore via label scan): NCT6683/6686/6687 on modern MSI boards, NCT6775-NCT6799 on ASUS, MSI and ASRock boards | Motherboard Vcore |
| [`nct6687`](https://github.com/Fred78290/nct6687d) (Fred78290) | Out-of-tree | Fuller NCT6687 support than the in-tree nct6683, which it replaces | Motherboard Vcore on MSI boards |
| [`it87`](https://github.com/frankcrawford/it87) (frankcrawford fork) | Out-of-tree | ITE Super I/O, common on Gigabyte boards | Motherboard Vcore |
| [`asus_ec_sensors`](https://github.com/zeule/asus-ec-sensors) (zeule) | Out-of-tree | ASUS embedded controller: VRM, chipset and board readings, for newer boards than the kernel's own copy names | Motherboard voltages on ASUS boards |
| `asus_wmi_sensors` | In-tree | ASUS WMI sensor interface on older ASUS boards | Motherboard voltages on ASUS boards |
| `spd5118` + `i2c_dev` | In-tree | DDR5 DIMM temperature via the SPD hub | Live DIMM temperatures |
| `coretemp` | In-tree | Intel CPU temperature | Intel systems only |

On Zen 5, Vcore telemetry uses SVI3, which no Linux driver supports yet. The tool falls
back to the motherboard Super I/O chip (Nuvoton NCT668x/NCT677x-NCT679x, ITE
IT862x-IT877x), scanning input labels for the Vcore channel. If neither source is
available, Vcore shows "N/A".

On NixOS each row is a switch in the drivers module ([Module options](#module-options)). On
other distros, load an in-tree module with `sudo modprobe <name>`, and build an out-of-tree one
at the commit this repository pins and tests, the `rev` in its recipe: `nix/ryzen-smu.nix`,
`nix/zenpower.nix`, `nix/nct6687d.nix`, `nix/it87.nix` or `nix/asus-ec-sensors.nix`.
[ryzen_smu kernel module](#ryzen_smu-kernel-module) shows the steps; the others take the same
checkout and their own README's build.

## Stress backends

You need at least one.

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

Required for the Curve-Tuner tab and Auto-Tuner; not for stress testing. The
[amkillam fork](https://github.com/amkillam/ryzen_smu) supports Zen 1 through Zen 5. On
NixOS turn on `hardware.corecycler.ryzenSmu.enable`. On other distros, from the
linux-corecycler checkout, build the commit pinned in `nix/ryzen-smu.nix`:

```bash
rev=$(sed -n 's/^ *rev = "\(.*\)";$/\1/p' nix/ryzen-smu.nix) && test -n "$rev" &&
  git clone https://github.com/amkillam/ryzen_smu.git ../ryzen_smu &&
  git -C ../ryzen_smu checkout -q "$rev" &&
  cd ../ryzen_smu && make && sudo make install && sudo modprobe ryzen_smu

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

Why `smn` is in that list, and the access it grants, is in [Privilege Model](../SECURITY.md#privilege-model).
