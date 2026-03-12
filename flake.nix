{
  description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen on Linux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;
        pythonPkgs = python.pkgs;
      in
      {
        packages.default = pythonPkgs.buildPythonApplication {
          pname = "linux-corecycler";
          version = "0.2.0";
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

          # Install icon and desktop file
          postInstall = ''
            install -Dm644 assets/icon.svg $out/share/icons/hicolor/scalable/apps/linux-corecycler.svg
            install -Dm644 assets/linux-corecycler.desktop $out/share/applications/linux-corecycler.desktop
          '';

          # Make stress test backends available on PATH at runtime
          postFixup = ''
            wrapProgram $out/bin/linux-corecycler \
              --prefix PATH : ${
                pkgs.lib.makeBinPath [
                  pkgs.stress-ng
                  pkgs.util-linux # for taskset
                ]
              }
          '';

          meta = {
            description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen";
            license = pkgs.lib.licenses.gpl3Plus;
            mainProgram = "linux-corecycler";
            platforms = pkgs.lib.platforms.linux;
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (
              ps: with ps; [
                pyside6
                pytest
                ruff
              ]
            ))
            pkgs.qt6.qtbase
            pkgs.stress-ng
            pkgs.util-linux # taskset
          ];

          env.QT_QPA_PLATFORM_PLUGIN_PATH = "${pkgs.qt6.qtbase}/lib/qt-6/plugins/platforms";

          shellHook = ''
            echo "linux-corecycler dev shell"
            echo "  Run:  python src/main.py"
            echo "  Test: pytest tests/"
          '';
        };
      }
    );
}
