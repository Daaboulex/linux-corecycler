# NixOS module for CoreCyclerLx — per-core CPU stability tester and
# PBO Curve Optimizer tuner for AMD Ryzen.
#
# Handles kernel modules (ryzen_smu, zenpower5), device access (udev,
# tmpfiles, group), and the corecyclerlx package itself.
#
# Usage in a consumer flake:
#   imports = [ inputs.linux-corecycler.nixosModules.default ];
#   services.corecyclerlx.enable = true;
{ self }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.corecyclerlx;
  inherit (pkgs.stdenv.hostPlatform) system;
  package =
    if cfg.unfreeBackends then self.packages.${system}.full else self.packages.${system}.default;
  zenpower = pkgs.callPackage ./zenpower.nix {
    inherit (config.boot.kernelPackages) kernel;
  };
  ryzenSmuPkg = pkgs.callPackage ./ryzen-smu.nix {
    inherit (config.boot.kernelPackages) kernel;
  };
in
{
  _class = "nixos";

  options.services.corecyclerlx = {
    enable = lib.mkEnableOption "CoreCyclerLx per-core CPU stability tester and PBO Curve Optimizer tuner";

    unfreeBackends = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to include unfree backends (mprime). When false, only FOSS backends (stress-ng) are bundled.";
    };

    ryzenSmu = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to load the ryzen_smu kernel module for Curve Optimizer read/write via SMU.";
    };

    zenpower = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to use zenpower5 instead of k10temp for Vcore/Vsoc voltage monitoring via SVI2. Replaces k10temp (blacklisted).";
    };

    deviceAccess = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to grant the deviceAccessUser access to MSR devices and SMU sysfs via a dedicated group and udev rules. No sudo required.";
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
        message = "services.corecyclerlx.deviceAccessUser must be set when deviceAccess is enabled.";
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

    # Kernel modules
    boot.kernelModules = [
      "msr"
    ]
    ++ lib.optional cfg.ryzenSmu "ryzen_smu"
    ++ lib.optional cfg.zenpower "zenpower";

    # Out-of-tree kernel modules — custom derivations that build with
    # clang for CachyOS LTO kernels, gcc otherwise
    boot.extraModulePackages =
      lib.optional cfg.ryzenSmu ryzenSmuPkg ++ lib.optional cfg.zenpower zenpower;

    # Blacklist k10temp when zenpower is used (they conflict)
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
