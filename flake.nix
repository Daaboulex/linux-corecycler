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
        "aarch64-linux"
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
      overlays.default = final: _prev: {
        linux-corecycler = self.packages.${final.stdenv.hostPlatform.system}.default;
        linux-corecycler-full = self.packages.${final.stdenv.hostPlatform.system}.full;
      };

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);

      checks = forAllSystems (system: {
        pre-commit = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style.enable = true;
          };
        };
      });

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          python = pkgs.python312;
          pythonPkgs = python.pkgs;

          # Shared build function — backends list is the only difference
          mkCoreCyclerLx =
            {
              backends ? [
                pkgs.stress-ng
                pkgs.stressapptest
              ],
              pnameSuffix ? "",
            }:
            pythonPkgs.buildPythonApplication {
              pname = "corecyclerlx${pnameSuffix}";
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

              # Qt6 runtime needs
              nativeBuildInputs = [ pkgs.qt6.wrapQtAppsHook ];
              buildInputs = [ pkgs.qt6.qtbase ];

              dontWrapQtApps = true;
              preFixup = ''
                makeWrapperArgs+=("''${qtWrapperArgs[@]}")
              '';

              # Install icon, desktop file, and asset SVGs
              postInstall = ''
                install -Dm644 assets/icon.svg $out/share/icons/hicolor/scalable/apps/corecyclerlx.svg
                install -Dm644 assets/corecyclerlx.desktop $out/share/applications/corecyclerlx.desktop
                install -d $out/share/corecyclerlx/assets
                install -Dm644 assets/*.svg $out/share/corecyclerlx/assets/
              '';

              # Make stress test backends available on PATH at runtime
              postFixup = ''
                wrapProgram $out/bin/corecyclerlx \
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
                mainProgram = "corecyclerlx";
                platforms = pkgs.lib.platforms.linux;
              };
            };
        in
        {
          # FOSS-only: stress-ng only (no unfree software)
          default = mkCoreCyclerLx { };

          # Full: includes mprime (unfree)
          full = mkCoreCyclerLx {
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
          python = pkgs.python312;
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
              echo "corecyclerlx dev shell"
              echo "  Run:  python src/main.py"
              echo "  Test: pytest tests/"
            '';
          };
        }
      );
    };
}
