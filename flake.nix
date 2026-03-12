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
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };
        python = pkgs.python312;
        pythonPkgs = python.pkgs;
      in
      {
        packages.default = pythonPkgs.buildPythonApplication {
          pname = "corecyclerlx";
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
                pkgs.lib.makeBinPath [
                  pkgs.mprime
                  pkgs.stress-ng
                  pkgs.util-linux # for taskset
                ]
              }
          '';

          meta = {
            description = "Per-core CPU stability tester and PBO Curve Optimizer tuner for AMD Ryzen";
            license = pkgs.lib.licenses.gpl3Plus;
            mainProgram = "corecyclerlx";
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
            pkgs.mprime
            pkgs.stress-ng
            pkgs.util-linux # taskset
          ];

          env.QT_QPA_PLATFORM_PLUGIN_PATH = "${pkgs.qt6.qtbase}/lib/qt-6/plugins/platforms";

          shellHook = ''
            echo "corecyclerlx dev shell"
            echo "  Run:  python src/main.py"
            echo "  Test: pytest tests/"
          '';
        };
      }
    );
}
