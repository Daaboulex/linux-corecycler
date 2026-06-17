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
      url = "github:Daaboulex/nix-packaging-standard?ref=v2.5.0";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.git-hooks.follows = "git-hooks";
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" ];
      imports = [ inputs.std.flakeModules.base ];

      flake = {
        # NixOS module - kernel modules, device access, udev rules, package
        nixosModules.default = import ./nix/module.nix { self = inputs.self; };

        # Overlay - makes pkgs.linux-corecycler and pkgs.linux-corecycler-full available
        overlays.default = final: _prev: {
          linux-corecycler = inputs.self.packages.${final.stdenv.hostPlatform.system}.default;
          linux-corecycler-full = inputs.self.packages.${final.stdenv.hostPlatform.system}.full;
        };
      };

      perSystem =
        { system, ... }:
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
          packages = {
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
          };

          # Force full evaluation of the NixOS module (options + assertions +
          # every mkIf path) without building the closure.
          checks.module-eval-nixos = inputs.std.lib.nixosModuleCheck {
            inherit (inputs) nixpkgs;
            inherit system;
            module = import ./nix/module.nix { self = inputs.self; };
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
    };
}
