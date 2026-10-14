{ lib }:
kernel:
let
  needed = [
    "stdenv"
    "commonMakeFlags"
    "moduleBuildDependencies"
    "dev"
    "modDirVersion"
  ];
  missing = lib.filter (name: !(kernel ? ${name})) needed;
in
if missing != [ ] then
  throw "the kernel ${kernel.name or "given"} does not expose ${lib.concatStringsSep ", " missing}, so a driver cannot be built with the toolchain that built it"
else
  {
    inherit (kernel) stdenv;
    nativeBuildInputs = kernel.moduleBuildDependencies;
    makeFlags = kernel.commonMakeFlags;
    isClang = kernel.stdenv.cc.isClang or false;
    build = "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build";
  }
