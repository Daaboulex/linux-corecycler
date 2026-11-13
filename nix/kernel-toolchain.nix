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
  let
    build = "${kernel.dev}/lib/modules/${kernel.modDirVersion}/build";
  in
  {
    inherit build;
    inherit (kernel) stdenv;
    nativeBuildInputs = kernel.moduleBuildDependencies;
    makeFlags = kernel.commonMakeFlags;
    installFlags = [
      "-C"
      build
      "M=$(PWD)"
      "INSTALL_MOD_PATH=${placeholder "out"}"
    ];
    installTargets = [ "modules_install" ];
    isClang = kernel.stdenv.cc.isClang or false;
  }
