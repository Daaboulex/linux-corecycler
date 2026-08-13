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
      url = "github:Daaboulex/nix-packaging-standard?ref=v2.22.1";
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
        buildFor =
          system:
          let
            # mprime (the "full" backend) is unfree.
            pkgs = import inputs.nixpkgs {
              inherit system;
              config.allowUnfree = true;
            };

            # Default python3 (not a pinned minor): Hydra only builds/caches
            # pyside6 for the default interpreter, so pinning python312 forced a
            # ~50-min from-source pyside6 build on every CI run. python3 keeps
            # the heavy Qt bindings a cache.nixos.org hit. requires-python in
            # pyproject.toml still allows >=3.12 for downstream users.
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
      in
      {
        inherit systems;

        imports = [ inputs.std.flakeModules.base ];

        flake = {
          # NixOS module - kernel modules, device access, udev rules, package
          nixosModules.default = import ./nix/module.nix { inherit (inputs) self; };

          # Overlay - pkgs.linux-corecycler (FOSS) and pkgs.linux-corecycler-full
          overlays.default = final: _prev: {
            linux-corecycler = inputs.self.packages.${final.stdenv.hostPlatform.system}.default;
            linux-corecycler-full = inputs.self.packages.${final.stdenv.hostPlatform.system}.full;
          };

          # OFF-CI exception (standard README "declared == built"): `full` pulls
          # mprime -- unfree, and fetched from an external mirror that does not
          # build reliably on a free runner. Expose it as a real `nix build .#full`
          # target via flake.packages (NOT perSystem.packages, which base aliases
          # into BUILT checks); CI eval-gates it with drvEvalCheck below instead of
          # realizing the unfree mprime closure.
          packages = builtins.listToAttrs (
            map (system: {
              name = system;
              value.full = (buildFor system).full;
            }) systems
          );
        };

        perSystem =
          { system, ... }:
          let
            b = buildFor system;
          in
          {
            # The FOSS default builds on CI (stress-ng/stressapptest are cached).
            packages.default = b.default;

            # Python lint gate — same config as pyproject [tool.ruff].
            pre-commit.settings.hooks.ruff.enable = true;

            # Eval-only gate for the off-CI `full`: force its full build graph to
            # EVALUATE (catching dep/version/unfree breakage) without realizing the
            # uncached, unfree mprime closure. The real build happens off-CI.
            checks.full-eval = inputs.std.lib.drvEvalCheck {
              pkgs = inputs.nixpkgs.legacyPackages.${system};
              name = "corecycler-full-eval";
              drv = b.full;
            };

            # Built-output ground truth: the wheel ships exactly one top-level
            # package -- a flat module (cli.py) collides with any other app in a
            # merged site-packages. tests/test_packaging.py is the fast pytest
            # mirror of the same invariant.
            checks.python-site-packages = inputs.std.lib.pythonSitePackagesCheck {
              inherit (b) pkgs;
              drv = b.default;
              package = "corecycler";
            };

            # Force full evaluation of the NixOS module (options + assertions +
            # every mkIf path) without building the closure.
            checks.module-eval-nixos = inputs.std.lib.nixosModuleCheck {
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
          };
      }
    );
}
