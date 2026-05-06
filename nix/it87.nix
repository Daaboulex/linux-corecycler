# it87 kernel module — out-of-tree fork with support for newer ITE Super I/O chips.
# Provides temperature, fan speed, and voltage monitoring (in0 = Vcore on most boards).
#
# The in-tree it87 driver lags behind on newer chip IDs (IT8686E, IT8689E, etc.).
# This fork by Frank Crawford adds 12+ additional chips common on Gigabyte AM5/AM4 boards.
#
# Supports both GCC and Clang/LLVM kernels (auto-detected from kernel makeFlags).
# Source: https://github.com/frankcrawford/it87
{
  lib,
  stdenv,
  fetchFromGitHub,
  kernel,
  llvmPackages_latest,
}:
let
  kernelUsesLLVM = builtins.any (
    flag:
    builtins.match ".*LLVM=1.*" (toString flag) != null
    || builtins.match ".*CC=clang.*" (toString flag) != null
  ) (kernel.makeFlags or [ ]);

  buildStdenv = if kernelUsesLLVM then llvmPackages_latest.stdenv else stdenv;
in
buildStdenv.mkDerivation {
  pname = "it87";
  version = "unstable-2025-12-26";

  src = fetchFromGitHub {
    owner = "frankcrawford";
    repo = "it87";
    rev = "a9eb2495220cba861ef3df63fa15265e878293b6";
    hash = "sha256-iWyOctK+TFhVCOw2LiV4NiNFEAqNXOpSdGY//VwO8Ko=";
  };

  hardeningDisable = [ "pic" ];

  nativeBuildInputs =
    kernel.moduleBuildDependencies
    ++ lib.optionals kernelUsesLLVM [
      llvmPackages_latest.lld
    ];

  makeFlags = [
    "KERNEL_BUILD=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
  ]
  ++ lib.optionals kernelUsesLLVM [
    "LLVM=1"
    "CC=clang"
    "LD=ld.lld"
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
