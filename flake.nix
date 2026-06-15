{
  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen on Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      git-hooks,
    }:
    let
      supportedSystems = [
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      pkgsFor =
        system:
        import nixpkgs {
          localSystem.system = system;
          config.allowUnfree = true;
        };
    in
    {
      # NixOS module — kernel modules, device access, udev rules, package
      nixosModules.default = import ./nix/module.nix { inherit self; };

      # Overlay — makes pkgs.linux-corecycler and pkgs.linux-corecycler-full available
      overlays.default =
        final: _prev:
        let
          system = _prev.stdenv.hostPlatform.system;
        in
        nixpkgs.lib.optionalAttrs (builtins.elem system supportedSystems) {
          linux-corecycler = self.packages.${system}.default;
          linux-corecycler-full = self.packages.${system}.full;
        };

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);

      checks = forAllSystems (system: {
        pre-commit = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style.enable = true;
            typos.enable = true;
            rumdl.enable = true;
            check-readme-sections = {
              enable = true;
              name = "check-readme-sections";
              entry = "bash scripts/check-readme-sections.sh";
              files = "README\\.md$";
              language = "system";
            };
          };
        };
      });

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          # Default python3 (not a pinned minor): Hydra only builds/caches
          # pyside6 for the default interpreter, so pinning python312 forced a
          # ~50-min from-source pyside6 build on every CI run. python3 keeps
          # the heavy Qt bindings a cache.nixos.org hit. requires-python in
          # pyproject.toml still allows >=3.12 for downstream users.
          python = pkgs.python3;
          pythonPkgs = python.pkgs;

          # Shared build function — backends list is the only difference
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

              nativeCheckInputs = [ pythonPkgs.pytest ];
              doCheck = false;

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
                        pkgs.util-linux # for taskset
                        pkgs.dmidecode # for DIMM info in Memory tab
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
          # FOSS-only: stress-ng only (no unfree software)
          default = mkCoreCycler { };

          # Full: includes mprime (unfree)
          full = mkCoreCycler {
            backends = [
              pkgs.mprime
              pkgs.stress-ng
              pkgs.stressapptest
            ];
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python3; # matches packages: cached pyside6, see note above
        in
        {
          default = pkgs.mkShell {
            packages = [
              (python.withPackages (
                ps: with ps; [
                  pyside6
                  pytest
                  ruff
                ]
              ))
              pkgs.qt6.qtbase
              pkgs.mprime
              pkgs.stress-ng
              pkgs.stressapptest
              pkgs.util-linux # taskset
              pkgs.nil
            ];

            inputsFrom = [ self.checks.${system}.pre-commit ];

            env.QT_QPA_PLATFORM_PLUGIN_PATH = "${pkgs.qt6.qtbase}/lib/qt-6/plugins/platforms";

            shellHook = ''
              ${self.checks.${system}.pre-commit.shellHook}
              echo "corecycler dev shell"
              echo "  Run:  python src/main.py"
              echo "  Test: pytest tests/"
            '';
          };

          # Minimal environment for CI: just the Python test deps, no
          # mprime/stress-ng/nil/pre-commit. Keeps the test job's closure a
          # pure cache.nixos.org fetch (no unfree mprime build, no shellHook).
          ci = pkgs.mkShell {
            packages = [
              (python.withPackages (
                ps: with ps; [
                  pyside6
                  pytest
                ]
              ))
            ];
          };
        }
      );
    };
}
