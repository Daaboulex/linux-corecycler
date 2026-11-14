{
  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen on Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-parts.url = "github:hercules-ci/flake-parts";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    std = {
      url = "github:Daaboulex/nix-packaging-standard/release";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.git-hooks.follows = "git-hooks";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } (
      let
        systems = [
          "x86_64-linux"
          "aarch64-linux"
        ];

        buildWith =
          pkgs:
          let

            # python3, not a pinned minor: cache.nixos.org builds pyside6 for the default interpreter only
            python = pkgs.python3;
            pythonPkgs = python.pkgs;

            mkCoreCycler =
              {
                backends ? [
                  pkgs.stress-ng
                  pkgs.stressapptest
                ],
                pnameSuffix ? "",
              }:
              pythonPkgs.buildPythonApplication {
                pname = "corecycler${pnameSuffix}";
                version = "0.0.1";
                pyproject = true;

                src = ./.;

                build-system = [
                  pythonPkgs.setuptools
                  pythonPkgs.setuptools-scm
                ];

                dependencies = [
                  pythonPkgs.pyside6
                ];

                # the slow end-to-end tests drive systemd-run scopes, which the build sandbox lacks
                nativeCheckInputs = [
                  pythonPkgs.pytest
                  pythonPkgs.hypothesis
                  pythonPkgs.pytest-cov
                  pythonPkgs.pytest-xdist
                ];
                doCheck = true;
                preCheck = ''
                  export QT_QPA_PLATFORM=offscreen
                  export HOME=$TMPDIR
                '';
                checkPhase = ''
                  runHook preCheck
                  python scripts/fit.py -- -m "not slow" -p no:cacheprovider \
                    --cov=corecycler --cov-report=term-missing --cov-fail-under=100
                  runHook postCheck
                '';

                nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
                buildInputs = [ pkgs.qt6.qtbase ];

                dontWrapQtApps = true;
                preFixup = ''
                  makeWrapperArgs+=("''${qtWrapperArgs[@]}")
                '';

                postInstall = ''
                  install -Dm644 assets/icon.svg $out/share/icons/hicolor/scalable/apps/corecycler.svg
                  install -Dm644 assets/corecycler.desktop $out/share/applications/corecycler.desktop
                  install -d $out/share/corecycler/assets
                  install -Dm644 assets/*.svg $out/share/corecycler/assets/
                '';

                postFixup = ''
                  wrapProgram $out/bin/corecycler \
                    --prefix PATH : ${
                      pkgs.lib.makeBinPath (
                        backends
                        ++ [
                          pkgs.util-linux # for setpriv (containment payload lifetime)
                          pkgs.dmidecode # for DIMM info in Memory tab
                          pkgs.libnotify # for notify-send desktop notifications
                        ]
                      )
                    }
                '';

                meta = {
                  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen";
                  license = pkgs.lib.licenses.gpl3Plus;
                  mainProgram = "corecycler";
                  platforms = pkgs.lib.platforms.linux;
                };
              };
          in
          {
            inherit pkgs;
            default = mkCoreCycler { };
            full =
              (mkCoreCycler {
                backends = [
                  pkgs.mprime
                  pkgs.y-cruncher
                  pkgs.stress-ng
                  pkgs.stressapptest
                ];
                pnameSuffix = "-full";
              }).overrideAttrs
                (o: {
                  meta = o.meta // {
                    platforms = [ "x86_64-linux" ];
                  };
                });
          };

        glueOverlay = final: _prev: {
          linux-corecycler = (buildWith final).default;
          linux-corecycler-full = (buildWith final).full;
        };
        fixOverlays =
          let
            dir = ./overlays;
            names = if builtins.pathExists dir then builtins.attrNames (builtins.readDir dir) else [ ];
          in
          map (n: (import (dir + "/${n}")).overlay) (
            builtins.filter (inputs.nixpkgs.lib.hasSuffix ".nix") names
          );

        buildFor =
          system:
          let
            pkgs = import inputs.nixpkgs {
              inherit system;
              config.allowUnfree = true;
              overlays = [ inputs.self.overlays.default ];
            };
          in
          {
            inherit pkgs;
            default = pkgs.linux-corecycler;
            full = pkgs.linux-corecycler-full;
          };
      in
      {
        inherit systems;

        imports = [ inputs.std.flakeModules.base ];

        flake = {
          nixosModules.default = import ./nix/module.nix { inherit (inputs) self; };
          nixosModules.drivers = ./nix/drivers.nix;

          overlays = {
            default = inputs.nixpkgs.lib.composeManyExtensions ([ glueOverlay ] ++ fixOverlays);
            probe = glueOverlay;
          };

          # full stays out of the built checks: mprime is unfree and its mirror fails on free runners; full-eval gates it
          packages = builtins.listToAttrs (
            map (
              system:
              let
                b = buildFor system;
              in
              {
                name = system;
                value =
                  inputs.nixpkgs.lib.optionalAttrs
                    (inputs.nixpkgs.lib.meta.availableOn b.pkgs.stdenv.hostPlatform b.full)
                    {
                      inherit (b) full;
                    };
              }
            ) systems
          );
        };

        perSystem =
          { system, ... }:
          let
            b = buildFor system;
            inherit (inputs.nixpkgs) lib;
            tunerEval =
              corecycler: host:
              (lib.nixosSystem {
                inherit system;
                modules = [
                  (import ./nix/module.nix { inherit (inputs) self; })
                  {
                    services.corecycler = {
                      enable = true;
                      deviceAccessUser = "corecycler-test";
                    }
                    // corecycler;
                    users.users.corecycler-test = {
                      isSystemUser = true;
                      group = "corecycler-test";
                    };
                    users.groups.corecycler-test = { };
                    boot.isContainer = true;
                    system.stateVersion = lib.trivial.release;
                    nixpkgs.config.allowUnfree = true;
                  }
                  host
                ];
              }).config;
            failedAssertions = config: map (a: a.message) (lib.filter (a: !a.assertion) config.assertions);
          in
          {
            packages.default = b.default;

            pre-commit.settings.hooks.ruff.enable = true;
            pre-commit.settings.hooks.taplo.excludes = [ "^pyproject\\.toml$" ];

            # a patch's blank context lines are a single space that trimming would break
            pre-commit.settings.hooks.trim-trailing-whitespace.excludes = [ "\\.patch$" ];

            pre-commit.settings.hooks.commit-req = {
              enable = true;
              name = "commit-req";
              entry = "${b.pkgs.python3}/bin/python3 scripts/check_commit_req.py";
              language = "system";
              stages = [ "commit-msg" ];
              always_run = true;
            };

            pre-commit.settings.hooks.mutation-stamp = {
              enable = true;
              name = "mutation-stamp";
              entry = "${b.pkgs.python3}/bin/python3 scripts/check_mutation_stamp.py";
              language = "system";
              stages = [ "commit-msg" ];
              always_run = true;
            };

            checks = {
              full-eval = inputs.std.lib.drvEvalCheck {
                pkgs = inputs.nixpkgs.legacyPackages.${system};
                name = "corecycler-full-eval";
                drv = b.full;
              };

              # one top-level package: a flat cli.py would collide in a merged site-packages
              python-site-packages = inputs.std.lib.pythonSitePackagesCheck {
                inherit (b) pkgs;
                drv = b.default;
                package = "corecycler";
              };

              module-eval-nixos = inputs.std.lib.nixosModuleCheck {
                inherit (inputs) nixpkgs;
                inherit system;
                module = import ./nix/module.nix { inherit (inputs) self; };
                config = {
                  nixpkgs.config.allowUnfree = true;
                  services.corecycler = {
                    enable = true;
                    deviceAccessUser = "corecycler-test";
                  };
                  users.users.corecycler-test = {
                    isSystemUser = true;
                    group = "corecycler-test";
                  };
                  users.groups.corecycler-test = { };
                };
              };

              module-drivers-split =
                let
                  inherit (inputs.nixpkgs) lib;
                  eval =
                    modules:
                    (lib.nixosSystem {
                      inherit system;
                      modules = modules ++ [
                        {
                          boot.isContainer = true;
                          system.stateVersion = lib.trivial.release;
                          nixpkgs.config.allowUnfree = true;
                        }
                      ];
                    }).config;
                  tuner = [
                    (import ./nix/module.nix { inherit (inputs) self; })
                    {
                      services.corecycler = {
                        enable = true;
                        deviceAccessUser = "corecycler-test";
                      };
                      users.users.corecycler-test = {
                        isSystemUser = true;
                        group = "corecycler-test";
                      };
                      users.groups.corecycler-test = { };
                    }
                  ];
                  drivers = eval [
                    ./nix/drivers.nix
                    {
                      hardware.corecycler = {
                        ryzenSmu.enable = true;
                        zenpower.enable = true;
                        asusEcSensors.enable = true;
                        nct6687.enable = true;
                        it87.enable = true;
                        nct6775.enable = true;
                        coretemp.enable = true;
                        spd5118.enable = true;
                        asusWmiSensors.enable = true;
                        gigabyteWmi.enable = true;
                        drivetemp.enable = true;
                      };
                    }
                  ];
                  tunerAlone = eval tuner;
                  twoSmu = eval [
                    ./nix/drivers.nix
                    {
                      hardware.corecycler.ryzenSmu.enable = true;
                      hardware.cpu.amd.ryzen-smu.enable = true;
                    }
                  ];
                  bothNuvoton = eval [
                    ./nix/drivers.nix
                    {
                      hardware.corecycler.nct6687.enable = true;
                      boot.kernelModules = [ "nct6683" ];
                    }
                  ];
                  renamed = eval (tuner ++ [ { services.corecycler.ryzenSmu = true; } ]);
                  problems =
                    lib.optional (
                      !lib.all (name: lib.elem name drivers.boot.kernelModules) [
                        "ryzen_smu"
                        "zenpower"
                        "asus_ec_sensors"
                        "nct6687"
                        "it87"
                        "nct6775"
                        "coretemp"
                        "spd5118"
                        "asus_wmi_sensors"
                        "gigabyte_wmi"
                        "drivetemp"
                      ]
                    ) "the drivers module alone does not load every driver it builds"
                    ++
                      lib.optional (lib.length drivers.boot.extraModulePackages != 5)
                        "the drivers module alone builds ${toString (lib.length drivers.boot.extraModulePackages)} drivers, not 5"
                    ++ lib.optional (
                      tunerAlone.boot.extraModulePackages != [ ]
                    ) "the tuner module alone builds a driver"
                    ++ lib.optional (
                      tunerAlone.boot.kernelModules != [ "msr" ]
                    ) "the tuner module alone loads ${toString tunerAlone.boot.kernelModules}, not only msr"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "no ryzen_smu driver") tunerAlone.warnings
                    ) "the tuner without ryzen_smu does not warn"
                    ++ lib.optional (
                      !lib.elem "ryzen_smu" renamed.boot.kernelModules
                    ) "services.corecycler.ryzenSmu no longer turns ryzen_smu on"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "drives the NCT6687D in place of nct6683") (failedAssertions bothNuvoton)
                    ) "nct6687 beside a loaded nct6683 is not refused"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "builds its own ryzen-smu") (failedAssertions twoSmu)
                    ) "ryzen_smu from here beside nixpkgs' copy is not refused"
                    ++ lib.optional (
                      failedAssertions drivers != [ ]
                    ) "the drivers module alone refuses: ${toString (failedAssertions drivers)}";
                in
                if problems == [ ] then
                  b.pkgs.runCommand "module-drivers-split" { } "touch $out"
                else
                  throw (lib.concatStringsSep "; " problems);

              module-machinecheck-poll =
                let
                  granted = config: config.systemd.services ? corecycler-machinecheck-poll;
                  byDefault = tunerEval { } { };
                  off = tunerEval { machineCheckPoll.enable = false; } { };
                  noGroup = tunerEval { deviceAccess = false; } { };
                  problems =
                    lib.optional (!granted byDefault) "the tuner by default does not grant the machine-check poll"
                    ++ lib.optional (granted off) "machineCheckPoll = false still grants the poll"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "machineCheckPoll grants the corecycler group") (failedAssertions noGroup)
                    ) "the poll without deviceAccess is not refused";
                in
                if problems == [ ] then
                  b.pkgs.runCommand "module-machinecheck-poll" { } "touch $out"
                else
                  throw (lib.concatStringsSep "; " problems);

              module-hang-reset =
                let
                  byDefault = tunerEval { } { };
                  on = tunerEval { hangReset.enable = true; } { };
                  hostWins = tunerEval { hangReset.enable = true; } { boot.kernel.sysctl."kernel.panic" = 30; };
                  blacklisted = tunerEval { hangReset.enable = true; } {
                    boot.blacklistedKernelModules = [ "sp5100_tco" ];
                  };
                  noAccess = tunerEval {
                    hangReset.enable = true;
                    deviceAccess = false;
                  } { };
                  sysctl = config: name: config.boot.kernel.sysctl.${name} or null;
                  grants = config: config.systemd.services ? corecycler-sysrq-trigger;
                  watches = config: config.systemd.user.services ? corecycler-hang-watch;
                  lingers = config: config.users.users.corecycler-test.linger == true;
                  grant = on.systemd.services.corecycler-sysrq-trigger;
                  watch = on.systemd.user.services.corecycler-hang-watch;
                  problems =
                    lib.optional (
                      sysctl byDefault "kernel.panic_on_rcu_stall" != null
                    ) "the tuner by default changes the panic policy"
                    ++ lib.optional (
                      !lib.all (name: sysctl on name == 1) [
                        "kernel.panic_on_oops"
                        "kernel.softlockup_panic"
                        "kernel.hardlockup_panic"
                        "kernel.nmi_watchdog"
                        "kernel.panic_on_rcu_stall"
                      ]
                      || sysctl on "kernel.panic" != 10
                      || on.systemd.settings.Manager.RuntimeWatchdogSec or null != "30s"
                    ) "hangReset does not set the panic policy and the watchdog feed"
                    ++ lib.optional (
                      sysctl hostWins "kernel.panic" != 30
                    ) "a host's own kernel.panic does not win over hangReset"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "lists sp5100_tco") (failedAssertions blacklisted)
                    ) "hangReset with a blacklisted watchdog driver is not refused"
                    ++ lib.optional (
                      !grants on
                      ||
                        map (command: lib.last (lib.splitString "/bin/" command)) grant.serviceConfig.ExecStart != [
                          "chgrp corecycler /proc/sysrq-trigger"
                          "chmod 0220 /proc/sysrq-trigger"
                        ]
                      || grant.serviceConfig.Type != "oneshot"
                      || !lib.elem "systemd-user-sessions.service" grant.before
                    ) "hangReset with deviceAccess does not grant the corecycler group the sysrq trigger"
                    ++ lib.optional (
                      !watches on
                      ||
                        watch.serviceConfig.ExecStart != "${lib.getExe inputs.self.packages.${system}.default} hang-watch"
                      || watch.serviceConfig.Restart != "always"
                      || watch.serviceConfig.RestartSec or null != 15
                      || watch.unitConfig.ConditionUser != "corecycler-test"
                      || !lib.elem "default.target" watch.wantedBy
                      || !lingers on
                    ) "hangReset with deviceAccess does not run corecycler hang-watch as the lingering deviceAccessUser"
                    ++ lib.optional (
                      grants byDefault || watches byDefault || lingers byDefault
                    ) "the tuner by default grants the sysrq trigger or runs the hang watch"
                    ++ lib.optional (
                      grants noAccess || watches noAccess || lingers noAccess
                    ) "hangReset without deviceAccess grants the sysrq trigger or runs the hang watch";
                in
                if problems == [ ] then
                  b.pkgs.runCommand "module-hang-reset" { } "touch $out"
                else
                  throw (lib.concatStringsSep "; " problems);

              module-boot-resume =
                let
                  byDefault = tunerEval { } { };
                  on = tunerEval {
                    autoResume = {
                      enable = true;
                      delaySeconds = 45;
                    };
                  } { };
                  noUser = tunerEval {
                    autoResume.enable = true;
                    deviceAccessUser = "";
                  } { };
                  unit = on.systemd.user.services.corecycler-resume or null;
                  timer = on.systemd.user.timers.corecycler-resume or null;
                  lingers = config: config.users.users.corecycler-test.linger == true;
                  problems =
                    lib.optional (
                      byDefault.systemd.user.services ? corecycler-resume || lingers byDefault
                    ) "the tuner by default resumes at boot"
                    ++ lib.optional (
                      unit == null
                      ||
                        unit.serviceConfig.ExecStart
                        != "${lib.getExe inputs.self.packages.${system}.default} resume --interrupted"
                      || unit.unitConfig.ConditionUser != "corecycler-test"
                    ) "autoResume does not run corecycler resume --interrupted as the deviceAccessUser"
                    ++ lib.optional (
                      timer == null || !lib.elem "timers.target" timer.wantedBy || timer.timerConfig.OnStartupSec != 45
                    ) "autoResume does not start the resume once delaySeconds have passed"
                    ++ lib.optional (!lingers on) "autoResume does not let the deviceAccessUser linger"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "autoResume needs services.corecycler.deviceAccessUser") (
                        failedAssertions noUser
                      )
                    ) "autoResume without a deviceAccessUser is not refused";
                in
                if problems == [ ] then
                  b.pkgs.runCommand "module-boot-resume" { } "touch $out"
                else
                  throw (lib.concatStringsSep "; " problems);
            }
            // inputs.nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
              # both ends of nixpkgs' kernel range: a kernel header move breaks users' builds first
              kernel-modules =
                let
                  sources = {
                    ryzen-smu = ./nix/ryzen-smu.nix;
                    zenpower = ./nix/zenpower.nix;
                    asus-ec-sensors = ./nix/asus-ec-sensors.nix;
                    nct6687d = ./nix/nct6687d.nix;
                    it87 = ./nix/it87.nix;
                  };
                  forKernel =
                    kernel:
                    let
                      drivers = lib.mapAttrsToList (_: f: b.pkgs.callPackage f { inherit kernel; }) sources;
                      tree = "${lib.getOutput "modules" kernel}/lib/modules/${kernel.modDirVersion}";
                    in
                    b.pkgs.runCommand "corecycler-drivers-${kernel.version}"
                      {
                        nativeBuildInputs = [
                          b.pkgs.binutils
                          b.pkgs.xz
                          b.pkgs.zstd
                        ];
                      }
                      ''
                        compiler() { readelf -p .comment "$1" | grep -o 'GCC: .*\|clang version .*' | sort -u; }
                        reference=$(find -L ${tree}/kernel/drivers/hwmon -name 'k10temp.ko*' | head -n 1)
                        case "$reference" in
                          *.xz) xz -dc "$reference" > k10temp.ko ;;
                          *.zst) zstd -dcq "$reference" > k10temp.ko ;;
                          *) cp "$reference" k10temp.ko ;;
                        esac
                        want=$(compiler k10temp.ko)
                        [ -n "$want" ] || { echo "the kernel's own k10temp names no compiler"; exit 1; }
                        for ko in $(find -L ${lib.concatStringsSep " " drivers} -name '*.ko'); do
                          got=$(compiler "$ko")
                          [ "$got" = "$want" ] || { echo "$ko was built by '$got', the kernel by '$want'"; exit 1; }
                        done
                        mkdir -p $out
                        printf '%s\n' ${lib.concatStringsSep " " drivers} > $out/drivers
                        echo "$want" > $out/compiler
                      '';
                in
                b.pkgs.linkFarm "corecycler-kernel-modules" {
                  default = forKernel b.pkgs.linuxPackages.kernel;
                  latest = forKernel b.pkgs.linuxPackages_latest.kernel;
                };
            };
          };
      }
    );
}
