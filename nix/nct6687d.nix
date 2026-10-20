{
  lib,
  fetchFromGitHub,
  kernel,
}:
let
  toolchain = import ./kernel-toolchain.nix { inherit lib; } kernel;
in
toolchain.stdenv.mkDerivation {
  pname = "nct6687d";
  version = "0-unstable-2026-10-13";

  src = fetchFromGitHub {
    owner = "Fred78290";
    repo = "nct6687d";
    rev = "060728e8d04e3ee17b5e003a53e0bcb34c60de92";
    hash = "sha256-qAAIwX+R14cMjY9UUO7bfBJDpqrNpILmJg/IW5tOPZ4=";
  };

  hardeningDisable = [ "pic" ];

  inherit (toolchain) nativeBuildInputs installFlags installTargets;

  makeFlags = toolchain.makeFlags ++ [
    "KVER=${kernel.modDirVersion}"
    "KDIR=${toolchain.build}"
  ];

  buildFlags = [ "modules" ];

  meta = {
    homepage = "https://github.com/Fred78290/nct6687d";
    description = "Nuvoton NCT6687D-R sensor driver for MSI boards, with the fan control the in-tree nct6683 refuses there";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
