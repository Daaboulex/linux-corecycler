{
  lib,
  fetchFromGitHub,
  kernel,
}:
let
  toolchain = import ./kernel-toolchain.nix { inherit lib; } kernel;
in
toolchain.stdenv.mkDerivation {
  pname = "zenpower";
  version = "0.5.0-unstable-2026-03-13";

  src = fetchFromGitHub {
    owner = "mattkeenan";
    repo = "zenpower5";
    rev = "66871d8e59c3741e00de2eb1f61c3b64263ed10b";
    hash = "sha256-g0zVTDi5owa6XfQN8vlFwGX+gpRIg+5q1F4EuxAk9Sk=";
  };

  patches = [ ./zenpower5-cpuid-include.patch ];

  postPatch = lib.optionalString toolchain.isClang ''
    substituteInPlace Makefile --replace-fail "-Wimplicit-fallthrough=3" "-Wimplicit-fallthrough"
  '';

  hardeningDisable = [ "pic" ];

  inherit (toolchain) nativeBuildInputs installFlags installTargets;

  makeFlags =
    toolchain.makeFlags
    ++ [
      "KERNEL_BUILD=${toolchain.build}"
    ]
    ++ lib.optionals toolchain.isClang [
      "KCFLAGS=-Wno-unused-command-line-argument"
    ];

  meta = {
    homepage = "https://github.com/mattkeenan/zenpower5";
    description = "AMD Zen family CPU sensors driver with Zen 5 support: temperature, SVI2 voltage/current (Zen 1-4), RAPL power";
    license = lib.licenses.gpl2Plus;
    platforms = [ "x86_64-linux" ];
    broken = lib.versionOlder kernel.version "4.14";
  };
}
