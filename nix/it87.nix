# it87 kernel module — out-of-tree fork with support for newer ITE Super I/O chips.
# Provides temperature, fan speed, and voltage monitoring (in0 = Vcore on most boards).
#
# The in-tree it87 driver lags behind on newer chip IDs (IT8686E, IT8689E, etc.).
# This fork by Frank Crawford adds 12+ additional chips common on Gigabyte AM5/AM4 boards.
#
# Source: https://github.com/frankcrawford/it87
{
  lib,
  fetchFromGitHub,
  kernel,
}:
let
  toolchain = import ./kernel-toolchain.nix { inherit lib; } kernel;
in
toolchain.stdenv.mkDerivation {
  pname = "it87";
  version = "unstable-2025-12-26";

  src = fetchFromGitHub {
    owner = "frankcrawford";
    repo = "it87";
    rev = "a9eb2495220cba861ef3df63fa15265e878293b6";
    hash = "sha256-iWyOctK+TFhVCOw2LiV4NiNFEAqNXOpSdGY//VwO8Ko=";
  };

  hardeningDisable = [ "pic" ];

  inherit (toolchain) nativeBuildInputs;

  makeFlags =
    toolchain.makeFlags
    ++ [
      "KERNEL_BUILD=${toolchain.build}"
    ]
    ++ lib.optionals toolchain.isClang [
      "KCFLAGS=-Wno-unused-command-line-argument"
    ];

  installPhase = ''
    runHook preInstall
    install -D it87.ko -t "$out/lib/modules/${kernel.modDirVersion}/kernel/drivers/hwmon/"
    runHook postInstall
  '';

  meta = {
    homepage = "https://github.com/frankcrawford/it87";
    description = "ITE IT87xx Super I/O hwmon driver — extended fork with 38+ chip support";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
  };
}
