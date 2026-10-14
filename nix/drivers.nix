{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.hardware.corecycler;
  build = file: pkgs.callPackage file { inherit (config.boot.kernelPackages) kernel; };
in
{
  _class = "nixos";

  options.hardware.corecycler = {
    ryzenSmu.enable = lib.mkEnableOption "the ryzen_smu kernel driver (amkillam fork, Zen 1 to Zen 5), which every Curve Optimizer and Curve Shaper read and write goes through, built against the configured kernel";
    zenpower.enable = lib.mkEnableOption "the zenpower5 kernel driver in place of k10temp, which it blacklists: Tctl, Tdie, Tccd and RAPL power, plus SVI2 voltage and current on Zen 1 to 4";
    asusEcSensors.enable = lib.mkEnableOption "the asus-ec-sensors kernel driver (zeule's copy, ahead of the in-tree one) for ASUS boards' embedded controller: VRM, chipset, CPU package and water temperatures, on boards mainline does not list yet such as the ROG CROSSHAIR X870E HERO before Linux 7.3; it loads in place of the in-tree copy";
    nct6687.enable = lib.mkEnableOption "the nct6687 kernel driver (Fred78290's nct6687d) for the Nuvoton NCT6687D-R on MSI boards, with the fan control the in-tree nct6683 refuses there; it blacklists nct6683, which would otherwise bind the chip first";
    it87.enable = lib.mkEnableOption "the it87 kernel driver (frankcrawford fork) for ITE Super I/O chips: motherboard Vcore, fans and temperatures on most Gigabyte boards";
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.ryzenSmu.enable {
      boot.kernelModules = [ "ryzen_smu" ];
      boot.extraModulePackages = [ (build ./ryzen-smu.nix) ];
    })
    (lib.mkIf cfg.zenpower.enable {
      boot.kernelModules = [ "zenpower" ];
      boot.extraModulePackages = [ (build ./zenpower.nix) ];
      boot.blacklistedKernelModules = [ "k10temp" ];
    })
    (lib.mkIf cfg.asusEcSensors.enable {
      boot.kernelModules = [ "asus_ec_sensors" ];
      boot.extraModulePackages = [ (build ./asus-ec-sensors.nix) ];
    })
    (lib.mkIf cfg.nct6687.enable {
      assertions = [
        {
          assertion = !lib.elem "nct6683" config.boot.kernelModules;
          message = "hardware.corecycler.nct6687 drives the NCT6687D in place of nct6683, but nct6683 is also loaded: stop loading it";
        }
      ];
      boot.kernelModules = [ "nct6687" ];
      boot.extraModulePackages = [ (build ./nct6687d.nix) ];
      boot.blacklistedKernelModules = [ "nct6683" ];
    })
    (lib.mkIf cfg.it87.enable {
      boot.kernelModules = [ "it87" ];
      boot.extraModulePackages = [ (build ./it87.nix) ];
    })
  ];
}
