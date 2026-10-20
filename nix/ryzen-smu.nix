{
  lib,
  fetchFromGitHub,
  kernel,
}:
let
  toolchain = import ./kernel-toolchain.nix { inherit lib; } kernel;

  version = "0.1.7-unstable-2026-08-15";

  src = fetchFromGitHub {
    owner = "amkillam";
    repo = "ryzen_smu";
    rev = "d2983668300dd2a598e5a7dc40e71ce0678cc270";
    hash = "sha256-OmEoycRO3hGkqueLa0i6AzmwMEbdkkPrwJkMyYxOTek=";
  };
in
toolchain.stdenv.mkDerivation {
  pname = "ryzen-smu-${kernel.version}";
  inherit version src;

  hardeningDisable = [ "pic" ];

  inherit (toolchain) nativeBuildInputs installFlags installTargets;

  makeFlags =
    toolchain.makeFlags
    ++ [
      "TARGET=${kernel.modDirVersion}"
      "KERNEL_BUILD=${toolchain.build}"
    ]
    ++ lib.optionals toolchain.isClang [
      "KCFLAGS=-Wno-unused-command-line-argument"
    ];

  meta = {
    description = "Linux kernel driver that exposes access to the SMU for AMD Ryzen Processors";
    homepage = "https://github.com/amkillam/ryzen_smu";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
