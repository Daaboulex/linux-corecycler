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
      url = "github:Daaboulex/nix-packaging-standard?ref=v2.40.1";
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

        # Shared builder for a system: the FOSS `default` and the `full` (mprime,
        # unfree) variants from one mkCoreCycler. Used by both perSystem (default
        # -> a built check) and flake.packages (full -> an off-CI eval gate).
        # Keyed on a package set, so the overlay can build against the consumer's
        # nixpkgs instead of handing back a build against the one pinned here.
        buildWith =
          pkgs:
          let

            # Default python3 (not a pinned minor): Hydra only builds/caches
            # pyside6 for the default interpreter, so pinning python312 forced a
            # ~50-min from-source pyside6 build on every CI run. python3 keeps
            # the heavy Qt bindings a cache.nixos.org hit. requires-python in
            # pyproject.toml is >=3.14, the first with compression.zstd.
            python = pkgs.python3;
            pythonPkgs = python.pkgs;

            # Shared build function - backends list is the only difference
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

                # The full unit/property suite gates the BUILD (offscreen Qt,
                # HOME in the sandbox tmpdir). The e2e subprocess replays
                # ("slow") stay outside the sandbox: they exercise systemd-run
                # scopes + wall-clock polling and belong to the dev loop, not
                # the gate.
                nativeCheckInputs = [
                  pythonPkgs.pytestCheckHook
                  pythonPkgs.hypothesis
                  pythonPkgs.pytest-cov
                  pythonPkgs.pytest-xdist
                ];
                doCheck = true;
                preCheck = ''
                  export QT_QPA_PLATFORM=offscreen
                  export HOME=$TMPDIR
                '';
                disabledTestMarks = [ "slow" ];

                # Coverage ratchet: line coverage may never fall below this floor.
                # Raise the floor to the percentage this check prints whenever a
                # batch lands. It is measured WITHOUT the slow tests, so it
                # trails the out-of-sandbox number.
                pytestFlags = [
                  "-n"
                  "auto"
                  "--dist"
                  "worksteal"
                  "--cov=corecycler"
                  "--cov-report=term-missing"
                  "--cov-fail-under=100"
                ];

                # Qt6 runtime needs
                nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
                buildInputs = [ pkgs.qt6.qtbase ];

                dontWrapQtApps = true;
                preFixup = ''
                  makeWrapperArgs+=("''${qtWrapperArgs[@]}")
                '';

                # Install icon, desktop file, and asset SVGs
                postInstall = ''
                  install -Dm644 assets/icon.svg $out/share/icons/hicolor/scalable/apps/corecycler.svg
                  install -Dm644 assets/corecycler.desktop $out/share/applications/corecycler.desktop
                  install -d $out/share/corecycler/assets
                  install -Dm644 assets/*.svg $out/share/corecycler/assets/
                '';

                # Make stress test backends available on PATH at runtime
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
            # FOSS-only: stress-ng + stressapptest (no unfree software).
            default = mkCoreCycler { };
            # Full: includes mprime (unfree, fetched from a flaky external mirror,
            # x86_64-only) and y-cruncher (unfree, x86_64-only). meta.platforms
            # reflects that so the standard's drvEvalCheck skips `full` on aarch64,
            # while the FOSS `default` still builds on both arches.
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
          # NixOS module - kernel modules, device access, udev rules, package
          nixosModules.default = import ./nix/module.nix { inherit (inputs) self; };
          nixosModules.drivers = ./nix/drivers.nix;

          overlays = {
            default = inputs.nixpkgs.lib.composeManyExtensions ([ glueOverlay ] ++ fixOverlays);
            probe = glueOverlay;
          };

          # OFF-CI exception (standard README "declared == built"): `full` pulls
          # mprime -- unfree, and fetched from an external mirror that does not
          # build reliably on a free runner. Expose it as a real `nix build .#full`
          # target via flake.packages (NOT perSystem.packages, which base aliases
          # into BUILT checks); CI eval-gates it with drvEvalCheck below instead of
          # realizing the unfree mprime closure.
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
            # The FOSS default builds on CI (stress-ng/stressapptest are cached).
            packages.default = b.default;

            # Python lint gate — same config as pyproject [tool.ruff].
            pre-commit.settings.hooks.ruff.enable = true;
            pre-commit.settings.hooks.taplo.excludes = [ "^pyproject\\.toml$" ];

            # A unified diff's blank context lines are a single space; trimming
            # them makes the patch stop applying.
            pre-commit.settings.hooks.trim-trailing-whitespace.excludes = [ "\\.patch$" ];

            pre-commit.settings.hooks.commit-task = {
              enable = true;
              name = "commit-task";
              entry = "${b.pkgs.python3}/bin/python3 scripts/check_commit_task.py";
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
              # Eval-only gate for the off-CI `full`: force its full build graph to
              # EVALUATE (catching dep/version/unfree breakage) without realizing the
              # uncached, unfree mprime closure. The real build happens off-CI.
              full-eval = inputs.std.lib.drvEvalCheck {
                pkgs = inputs.nixpkgs.legacyPackages.${system};
                name = "corecycler-full-eval";
                drv = b.full;
              };

              # Built-output ground truth: the wheel ships exactly one top-level
              # package -- a flat module (cli.py) collides with any other app in a
              # merged site-packages. tests/test_packaging.py is the fast pytest
              # mirror of the same invariant.
              python-site-packages = inputs.std.lib.pythonSitePackagesCheck {
                inherit (b) pkgs;
                drv = b.default;
                package = "corecycler";
              };

              # Force full evaluation of the NixOS module (options + assertions +
              # every mkIf path) without building the closure.
              module-eval-nixos = inputs.std.lib.nixosModuleCheck {
                inherit (inputs) nixpkgs;
                inherit system;
                module = import ./nix/module.nix { inherit (inputs) self; };
                config = {
                  nixpkgs.config.allowUnfree = true; # mprime backend is unfree
                  services.corecycler = {
                    enable = true;
                    deviceAccessUser = "corecycler-test";
                  };
                  # the eval fixture must declare the user the module grants access to
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
                        it87.enable = true;
                      };
                    }
                  ];
                  tunerAlone = eval tuner;
                  renamed = eval (tuner ++ [ { services.corecycler.ryzenSmu = true; } ]);
                  problems =
                    lib.optional (
                      !lib.all (name: lib.elem name drivers.boot.kernelModules) [
                        "ryzen_smu"
                        "zenpower"
                        "it87"
                      ]
                    ) "the drivers module alone does not load ryzen_smu, zenpower and it87"
                    ++
                      lib.optional (lib.length drivers.boot.extraModulePackages != 3)
                        "the drivers module alone builds ${toString (lib.length drivers.boot.extraModulePackages)} drivers, not 3"
                    ++ lib.optional (
                      tunerAlone.boot.extraModulePackages != [ ]
                    ) "the tuner module alone builds a driver"
                    ++ lib.optional (
                      !lib.any (lib.hasInfix "no ryzen_smu driver") tunerAlone.warnings
                    ) "the tuner without ryzen_smu does not warn"
                    ++ lib.optional (
                      !lib.elem "ryzen_smu" renamed.boot.kernelModules
                    ) "services.corecycler.ryzenSmu no longer turns ryzen_smu on";
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
                  sysctl = config: name: config.boot.kernel.sysctl.${name} or null;
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
                    ) "hangReset with a blacklisted watchdog driver is not refused";
                in
                if problems == [ ] then
                  b.pkgs.runCommand "module-hang-reset" { } "touch $out"
                else
                  throw (lib.concatStringsSep "; " problems);
            }
            // inputs.nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
              # The out-of-tree modules compile against the user's own kernel, so an
              # upstream header move is a user-visible FTBFS nothing here would catch.
              # Both ends of the range nixpkgs offers are built on purpose.
              kernel-modules =
                let
                  sources = {
                    ryzen-smu = ./nix/ryzen-smu.nix;
                    zenpower = ./nix/zenpower.nix;
                    it87 = ./nix/it87.nix;
                  };
                  forKernel =
                    kernel:
                    inputs.nixpkgs.lib.mapAttrs' (
                      name: f:
                      inputs.nixpkgs.lib.nameValuePair "${name}-${kernel.version}" (
                        b.pkgs.callPackage f { inherit kernel; }
                      )
                    ) sources;
                in
                b.pkgs.linkFarm "corecycler-kernel-modules" (
                  forKernel b.pkgs.linuxPackages.kernel // forKernel b.pkgs.linuxPackages_latest.kernel
                );
            };
          };
      }
    );
}
