# NixOS module for CoreCycler — per-core CPU stability tester and
# PBO Curve Optimizer tuner for AMD Ryzen.
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
  machineCheckPoll = "/sys/devices/system/machinecheck/machinecheck0/check_interval";
  blacklistedWatchdogs = lib.filter (driver: lib.elem driver config.boot.blacklistedKernelModules) [
    "sp5100_tco"
    "iTCO_wdt"
    "wdat_wdt"
  ];
  smuDriverLoaded = lib.any (name: builtins.elem name config.boot.kernelModules) [
    "ryzen_smu"
    "ryzen-smu"
  ];
in
{
  _class = "nixos";

  imports = [
    ./drivers.nix
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "ryzenSmu" ]
      [ "hardware" "corecycler" "ryzenSmu" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "zenpower" ]
      [ "hardware" "corecycler" "zenpower" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "it87" ]
      [ "hardware" "corecycler" "it87" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "coretemp" ]
      [ "hardware" "corecycler" "coretemp" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "nct6775" ]
      [ "hardware" "corecycler" "nct6775" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "nct6683" ]
      [ "hardware" "corecycler" "nct6683" "enable" ]
    )
    (lib.mkRenamedOptionModule
      [ "services" "corecycler" "spd5118" ]
      [ "hardware" "corecycler" "spd5118" "enable" ]
    )
  ];

  options.services.corecycler = {
    enable = lib.mkEnableOption "CoreCycler per-core CPU stability tester and PBO Curve Optimizer tuner";

    unfreeBackends = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to include the unfree backends (mprime, y-cruncher). When false, only the FOSS backends (stress-ng, stressapptest) are bundled.";
    };

    # --- Utility modules ---

    cpuid = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the in-tree cpuid module. Exposes /dev/cpu/*/cpuid for CPUID leaf access. Useful for CPU topology and feature detection.";
    };

    # --- Device access ---

    deviceAccess = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to grant the deviceAccessUser access to the SMU sysfs files via a dedicated group and a systemd oneshot for the ryzen_smu files, so CO access needs no sudo. A udev rule also gives the group read mode on the MSR devices, but the kernel refuses an MSR open without CAP_SYS_RAWIO, so per-core power and MSR package power still need root; clock stretch reads the PM table the group already has. The oneshot includes `smn`, which is arbitrary SMN register access. See SECURITY.md before enabling this for a user you would not trust with the SMU mailbox.";
    };

    deviceAccessUser = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = "Username to grant device access to (added to the corecycler group). Required when deviceAccess is true.";
    };

    machineCheckPoll.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to let the corecycler group raise the kernel's machine-check poll to 5 s while a tuning run lasts, so a corrected error is logged within seconds instead of minutes. The tuner puts the old value back when the run ends or pauses, and a reboot restores the kernel default after a killed run. Needs deviceAccess.";
    };

    hangReset.enable = lib.mkEnableOption "resetting the machine by itself when a test hangs it, so an unattended run resumes after the reboot: the kernel panics on an oops, a soft or hard lockup and an RCU stall (a core that stops dead while the others run on) and reboots 10 s later, and systemd feeds the board's hardware watchdog every 30 s for a hang that stops everything. Each value is a default the host can override. It acts on every stall on the machine, not only during a run";

    autoResume = {
      enable = lib.mkEnableOption "resuming the active tuner session automatically after login (freeze-and-continue without clicks). Runs sudo-less via the corecycler device-access group.";
      delaySeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 120;
        description = "Settle time after login before the session resumes.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.deviceAccess -> cfg.deviceAccessUser != "";
        message = "services.corecycler.deviceAccessUser must be set when deviceAccess is enabled.";
      }
      {
        assertion = cfg.hangReset.enable -> blacklistedWatchdogs == [ ];
        message = "services.corecycler.hangReset needs the board's hardware watchdog driver, but boot.blacklistedKernelModules lists ${lib.concatStringsSep ", " blacklistedWatchdogs}: take it off the blacklist (CachyOS's settings put it there by default) or turn hangReset off.";
      }
      {
        assertion = cfg.machineCheckPoll.enable -> cfg.deviceAccess;
        message = "services.corecycler.machineCheckPoll grants the corecycler group, which only deviceAccess creates: enable deviceAccess or turn machineCheckPoll off.";
      }
    ];

    warnings =
      lib.optional (!smuDriverLoaded)
        "services.corecycler: no ryzen_smu driver is loaded, so the Curve-Tuner tab and the Auto-Tuner cannot read or write the CPU; enable hardware.corecycler.ryzenSmu.enable (this repository's drivers module) or nixpkgs' hardware.cpu.amd.ryzen-smu.enable";

    environment.systemPackages = [ package ];

    # Login autostart: the app itself enforces the guards (mid-run sessions
    # only, single-instance lock, settle delay).
    environment.etc."xdg/autostart/corecycler-autoresume.desktop" = lib.mkIf cfg.autoResume.enable {
      text = ''
        [Desktop Entry]
        Type=Application
        Name=CoreCycler auto-resume
        Exec=${lib.getExe package} --auto-resume ${toString cfg.autoResume.delaySeconds}
        X-GNOME-Autostart-enabled=true
      '';
    };

    # --- Device access via dedicated group (no sudo) ---
    users.groups.corecycler = lib.mkIf cfg.deviceAccess { };
    users.users = lib.optionalAttrs (cfg.deviceAccess && cfg.deviceAccessUser != "") {
      ${cfg.deviceAccessUser}.extraGroups = [ "corecycler" ];
    };

    services.udev.extraRules = lib.mkIf cfg.deviceAccess ''
      SUBSYSTEM=="msr", KERNEL=="msr[0-9]*", GROUP="corecycler", MODE="0640"
    '';

    # --- Kernel modules ---
    # In-tree modules loaded via boot.kernelModules, out-of-tree via extraModulePackages
    boot.kernelModules = [
      "msr"
    ]
    ++ lib.optional cfg.cpuid "cpuid";

    # SMU sysfs: grant group read/write for Curve Optimizer access.
    # A systemd oneshot is used because tmpfiles z-rules and udev module events
    # both race with sysfs creation in module_init(). ConditionPathExists +
    # After=systemd-modules-load.service guarantees paths exist.
    systemd.services.corecycler-smu-permissions = lib.mkIf cfg.deviceAccess {
      description = "Set ryzen_smu sysfs permissions for corecycler group";
      after = [ "systemd-modules-load.service" ];
      wantedBy = [ "multi-user.target" ];
      unitConfig.ConditionPathExists = "/sys/kernel/ryzen_smu_drv/smu_args";
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart =
          let
            paths = [
              "/sys/kernel/ryzen_smu_drv/smu_args"
              "/sys/kernel/ryzen_smu_drv/mp1_smu_cmd"
              "/sys/kernel/ryzen_smu_drv/rsmu_cmd"
              # An SMN read is a write of the address, so per-core Curve
              # Optimizer on a harvested part with renumbered core ids needs
              # this too: it is the only way to read the core-disable fuse
              # that says which physical slots are fused off.
              "/sys/kernel/ryzen_smu_drv/smn"
            ];
          in
          pkgs.writeShellScript "corecycler-smu-perms" ''
            set -euo pipefail
            for f in ${lib.concatStringsSep " " paths}; do
              chgrp corecycler "$f"
              chmod 0660 "$f"
            done
          '';
      };
    };

    systemd.services.corecycler-machinecheck-poll = lib.mkIf cfg.machineCheckPoll.enable {
      description = "Let the corecycler group set the machine-check poll interval";
      wantedBy = [ "multi-user.target" ];
      unitConfig.ConditionPathExists = machineCheckPoll;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = [
          "${pkgs.coreutils}/bin/chgrp corecycler ${machineCheckPoll}"
          "${pkgs.coreutils}/bin/chmod 0664 ${machineCheckPoll}"
        ];
      };
    };

    systemd.settings.Manager.RuntimeWatchdogSec = lib.mkIf cfg.hangReset.enable (lib.mkDefault "30s");

    # Allow unprivileged dmesg access for MCE error detection
    boot.kernel.sysctl = lib.mkMerge [
      (lib.mkIf cfg.deviceAccess {
        "kernel.dmesg_restrict" = lib.mkDefault 0;
      })
      (lib.mkIf cfg.hangReset.enable (
        lib.mapAttrs (_: lib.mkDefault) {
          "kernel.panic" = 10;
          "kernel.panic_on_oops" = 1;
          "kernel.softlockup_panic" = 1;
          "kernel.hardlockup_panic" = 1;
          "kernel.nmi_watchdog" = 1;
          "kernel.panic_on_rcu_stall" = 1;
        }
      ))
    ];
  };
}
