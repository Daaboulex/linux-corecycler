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
  sysrqTrigger = "/proc/sysrq-trigger";
  hangWatch = cfg.hangReset.enable && cfg.deviceAccess;
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

    cpuid = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to load the in-tree cpuid module. Exposes /dev/cpu/*/cpuid for CPUID leaf access. Useful for CPU topology and feature detection.";
    };

    deviceAccess = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Whether to grant the deviceAccessUser access to the SMU sysfs files via a dedicated group and a systemd oneshot for the ryzen_smu files, so CO access needs no sudo. A udev rule also gives the group read mode on the MSR devices, but the kernel refuses an MSR open without CAP_SYS_RAWIO, so per-core power and MSR package power still need root; clock stretch reads the PM table the group already has. The oneshot includes `smn`, which is arbitrary SMN register access; an SMN read writes the address first, and it is the only way to read the core-disable fuse that maps a harvested part's physical cores and, on every Vermeer, the CCD-disable fuses that number its CCDs. The group may also read the kernel log (kernel.dmesg_restrict 0, a default the host can override) for machine-check events. See SECURITY.md before enabling this for a user you would not trust with the SMU mailbox. With hangReset on, the group can also write /proc/sysrq-trigger, which runs every sysrq function, an immediate reboot among them.";
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

    hangReset.enable = lib.mkEnableOption "resetting the machine by itself when a test hangs it, so an unattended run resumes after the reboot: the kernel panics on an oops, a soft or hard lockup and an RCU stall (a core that stops dead while the others run on) and reboots 10 s later, and systemd feeds the board's hardware watchdog every 30 s for a hang that stops everything. Each value is a default the host can override. It acts on every stall on the machine, not only during a run. With deviceAccess on, a user unit of deviceAccessUser, who then lingers, runs `corecycler hang-watch`, which resets the machine through sysrq when two checks 15 s apart find the session's heartbeat, in a test or between tests, stopped for 120 s while its process lives, or no write to the directory of its database finished for 120 s; a freeze that stops the watch itself while the board's watchdog is still fed is left to the panics and a reset by hand";

    autoResume = {
      enable = lib.mkEnableOption "resuming the one interrupted tuner session at boot, without a login, through `corecycler resume --interrupted`: a user unit of deviceAccessUser runs it once delaySeconds have passed since that user's systemd manager started, which lingering starts at boot, and again a minute after a refusal (the SMU driver not loaded yet, a GUI already driving, another heavy test holding the shared lock), three times at most. A paused session is never resumed by itself, and with two interrupted sessions neither is. The user lingers, so their other user services start at boot too";
      delaySeconds = lib.mkOption {
        type = lib.types.ints.positive;
        default = 120;
        description = "The settle time after boot before the interrupted session resumes.";
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
      {
        assertion = cfg.autoResume.enable -> cfg.deviceAccessUser != "";
        message = "services.corecycler.autoResume needs services.corecycler.deviceAccessUser, the user whose tuning session it resumes at boot.";
      }
    ];

    warnings =
      lib.optional (!smuDriverLoaded)
        "services.corecycler: no ryzen_smu driver is loaded, so the Curve-Tuner tab and the Auto-Tuner cannot read or write the CPU; enable hardware.corecycler.ryzenSmu.enable (this repository's drivers module) or nixpkgs' hardware.cpu.amd.ryzen-smu.enable";

    environment.systemPackages = [ package ];

    systemd.user.timers.corecycler-resume = lib.mkIf cfg.autoResume.enable {
      description = "Resume the interrupted CoreCycler tuning session once the boot settles";
      unitConfig.ConditionUser = cfg.deviceAccessUser;
      wantedBy = [ "timers.target" ];
      timerConfig.OnStartupSec = cfg.autoResume.delaySeconds;
    };

    systemd.user.services.corecycler-resume = lib.mkIf cfg.autoResume.enable {
      description = "Resume the one interrupted CoreCycler tuning session";
      unitConfig.ConditionUser = cfg.deviceAccessUser;
      startLimitBurst = 4;
      startLimitIntervalSec = 600;
      serviceConfig = {
        Type = "simple";
        ExecStart = "${lib.getExe package} resume --interrupted";
        Restart = "no";
        RestartForceExitStatus = "5 7";
        RestartSec = 60;
      };
    };

    users.groups.corecycler = lib.mkIf cfg.deviceAccess { };
    users.users = lib.mkMerge [
      (lib.optionalAttrs (cfg.deviceAccess && cfg.deviceAccessUser != "") {
        ${cfg.deviceAccessUser}.extraGroups = [ "corecycler" ];
      })
      (lib.optionalAttrs (cfg.autoResume.enable && cfg.deviceAccessUser != "") {
        ${cfg.deviceAccessUser}.linger = true;
      })
      (lib.optionalAttrs (hangWatch && cfg.deviceAccessUser != "") {
        ${cfg.deviceAccessUser}.linger = true;
      })
    ];

    systemd.services.corecycler-sysrq-trigger = lib.mkIf hangWatch {
      description = "Let the corecycler group write the sysrq trigger for the hang watch";
      wantedBy = [ "multi-user.target" ];
      before = [ "systemd-user-sessions.service" ];
      unitConfig.ConditionPathExists = sysrqTrigger;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = [
          "${pkgs.coreutils}/bin/chgrp corecycler ${sysrqTrigger}"
          "${pkgs.coreutils}/bin/chmod 0220 ${sysrqTrigger}"
        ];
      };
    };

    systemd.user.services.corecycler-hang-watch = lib.mkIf hangWatch {
      description = "Reset the machine when a CoreCycler test hangs it";
      unitConfig.ConditionUser = cfg.deviceAccessUser;
      wantedBy = [ "default.target" ];
      serviceConfig = {
        ExecStart = "${lib.getExe package} hang-watch";
        Restart = "always";
        RestartSec = 15;
        StandardError = "journal";
        SyslogLevelPrefix = true;
      };
    };

    services.udev.extraRules = lib.mkIf cfg.deviceAccess ''
      SUBSYSTEM=="msr", KERNEL=="msr[0-9]*", GROUP="corecycler", MODE="0640"
    '';

    boot.kernelModules = [
      "msr"
    ]
    ++ lib.optional cfg.cpuid "cpuid";

    systemd.services.corecycler-smu-permissions = lib.mkIf cfg.deviceAccess {
      description = "Set ryzen_smu sysfs permissions for the corecycler group once the module has made them; tmpfiles z-rules and udev module events race module_init";
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
