{
  lib,
  fetchFromGitHub,
  kernel,
}:
let
  toolchain = import ./kernel-toolchain.nix { inherit lib; } kernel;
in
toolchain.stdenv.mkDerivation {
  pname = "asus-ec-sensors";
  version = "0.1.0-unstable-2026-10-08";

  src = fetchFromGitHub {
    owner = "zeule";
    repo = "asus-ec-sensors";
    rev = "33ac3ac4dc63ba6bb98824328e9712a965e7e875";
    hash = "sha256-vOBe9LQe4KXhx42xDi/NaE+602TikcbaMP8HnXLQ39U=";
  };

  hardeningDisable = [ "pic" ];

  inherit (toolchain) nativeBuildInputs installFlags installTargets;

  makeFlags =
    toolchain.makeFlags
    ++ [
      "KVER=${kernel.modDirVersion}"
      "KDIR=${kernel.dev}/lib/modules/${kernel.modDirVersion}"
    ]
    ++ lib.optionals toolchain.isClang [
      "KCFLAGS=-Wno-unused-command-line-argument"
    ];

  buildFlags = [ "modules" ];

  meta = {
    homepage = "https://github.com/zeule/asus-ec-sensors";
    description = "ASUS motherboard embedded-controller sensors driver, ahead of the in-tree one: VRM, chipset, CPU package and water temperatures on boards mainline does not list yet";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
