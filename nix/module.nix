# NixOS module for CoreCycler — per-core CPU stability tester and
# PBO Curve Optimizer tuner for AMD Ryzen.
#
# Handles all kernel modules needed for monitoring and SMU access:
#   Out-of-tree: ryzen_smu (AMD SMU), zenpower5 (AMD hwmon), it87 (ITE Super I/O)
#   In-tree:     msr, nct6775 (Nuvoton Super I/O), coretemp (Intel), cpuid
#
# Also handles device access (udev, tmpfiles, group) and the corecycler package.
#
# Usage in a consumer flake:
#   imports = [ inputs.linux-corecycler.nixosModules.default ];
#   services.corecycler = {
#     enable = true;
#     deviceAccessUser = "myuser";
#   };
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.corecycler;
  inherit (pkgs.stdenv.hostPlatform) system;
  package =
    if cfg.unfreeBackends then self.packages.${system}.full else self.packages.${system}.default;
  zenpowerPkg = pkgs.callPackage ./zenpower.nix {
    inherit (config.boot.kernelPackages) kernel;
  };
  ryzenSmuPkg = pkgs.callPackage ./ryzen-smu.nix {
    inherit (config.boot.kernelPackages) kernel;
  };
  it87Pkg = pkgs.callPackage ./it87.nix {
    inherit (config.boot.kernelPackages) kernel;
  };
in
{
  _class = "nixos";

  options.services.corecycler = {
    enable = lib.mkEnableOption "CoreCycler per-core CPU stability tester and PBO Curve Optimizer tuner";

    unfreeBackends = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to include unfree backends (mprime). When false, only FOSS backends (stress-ng) are bundled.";
    };

    # --- AMD SMU access ---

    ryzenSmu = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to load the ryzen_smu kernel module (amkillam fork) for Curve Optimizer read/write via SMU. Supports Zen 1 through Zen 5.";
    };

    # --- CPU hwmon drivers ---

    zenpower = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to use zenpower5 instead of k10temp for AMD CPU monitoring. Provides Tctl/Tdie/Tccd temps, SVI2 voltage/current (Zen 1-4), and RAPL power. Replaces k10temp (blacklisted). Zen 1 through Zen 5.";
    };

    coretemp = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the in-tree coretemp module for Intel CPU temperature monitoring. Per-core and per-package DTS readings. Only needed on Intel systems.";
    };

    # --- Super I/O (motherboard voltage/fan/temp) ---

    nct6775 = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the in-tree nct6775 module for Nuvoton Super I/O chips. Provides motherboard Vcore (in0), fan speeds, and temperatures. Common on ASUS, MSI, ASRock boards. Needed for Zen 5 Vcore fallback on Nuvoton boards.";
    };

    it87 = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the out-of-tree it87 module (frankcrawford fork) for ITE Super I/O chips. Provides motherboard Vcore (in0), fan speeds, and temperatures. Common on Gigabyte boards. Supports 38+ chip models including IT8686E, IT8689E. Needed for Zen 5 Vcore fallback on ITE boards.";
    };

    # --- Utility modules ---

    cpuid = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the in-tree cpuid module. Exposes /dev/cpu/*/cpuid for CPUID leaf access. Useful for CPU topology and feature detection.";
    };

    spd5118 = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load spd5118 and i2c_dev modules for DDR5 DIMM temperature monitoring via the SPD5118 hub chip.";
    };

    # --- Device access ---

    deviceAccess = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to grant the deviceAccessUser access to MSR devices and SMU sysfs via a dedicated group and udev rules. No sudo required for monitoring and CO access.";
    };

    deviceAccessUser = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Username to grant device access to (added to the corecycler group). Required when deviceAccess is true.";
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.deviceAccess -> cfg.deviceAccessUser != "";
        message = "services.corecycler.deviceAccessUser must be set when deviceAccess is enabled.";
      }
    ];

    environment.systemPackages = [ package ];

    # --- Device access via dedicated group (no sudo) ---
    users.groups.corecycler = lib.mkIf cfg.deviceAccess { };
    users.users.${cfg.deviceAccessUser}.extraGroups = lib.mkIf cfg.deviceAccess [ "corecycler" ];

    # MSR devices: grant group read access for APERF/MPERF (clock stretch)
    # and RAPL energy counters (per-core + package power)
    services.udev.extraRules = lib.mkIf cfg.deviceAccess ''
      SUBSYSTEM=="msr", KERNEL=="msr[0-9]*", GROUP="corecycler", MODE="0640"
    '';

    # --- Kernel modules ---
    # In-tree modules loaded via boot.kernelModules, out-of-tree via extraModulePackages
    boot.kernelModules = [
      "msr" # always needed for APERF/MPERF and RAPL MSR access
    ]
    ++ lib.optional cfg.ryzenSmu "ryzen_smu"
    ++ lib.optional cfg.zenpower "zenpower"
    ++ lib.optional cfg.coretemp "coretemp"
    ++ lib.optional cfg.nct6775 "nct6775"
    ++ lib.optional cfg.it87 "it87"
    ++ lib.optional cfg.cpuid "cpuid"
    ++ lib.optionals cfg.spd5118 [
      "i2c_dev"
      "spd5118"
    ];

    # Out-of-tree kernel modules — custom derivations that build with
    # clang for CachyOS LTO kernels, gcc otherwise
    boot.extraModulePackages =
      lib.optional cfg.ryzenSmu ryzenSmuPkg
      ++ lib.optional cfg.zenpower zenpowerPkg
      ++ lib.optional cfg.it87 it87Pkg;

    # Blacklist k10temp when zenpower is used (they conflict — same PCI device)
    boot.blacklistedKernelModules = lib.mkIf cfg.zenpower [ "k10temp" ];

    # SMU sysfs: grant group read/write for Curve Optimizer access
    systemd.tmpfiles.rules = lib.mkIf (cfg.deviceAccess && cfg.ryzenSmu) [
      "z /sys/kernel/ryzen_smu_drv/smu_args 0660 root corecycler - -"
      "z /sys/kernel/ryzen_smu_drv/mp1_smu_cmd 0660 root corecycler - -"
      "z /sys/kernel/ryzen_smu_drv/rsmu_cmd 0660 root corecycler - -"
    ];

    # Allow unprivileged dmesg access for MCE error detection
    boot.kernel.sysctl = lib.mkIf cfg.deviceAccess {
      "kernel.dmesg_restrict" = lib.mkDefault 0;
    };
  };
}
