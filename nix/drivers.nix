{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.hardware.corecycler;
  build = file: pkgs.callPackage file { inherit (config.boot.kernelPackages) kernel; };
  families = {
    ryzenSmu = "ryzen-smu";
    zenpower = "zenpower";
    asusEcSensors = "asus-ec-sensors";
    nct6687 = "nct6687d";
    it87 = "it87";
  };
  copiesOf =
    family: lib.filter (p: lib.hasPrefix family (lib.getName p)) config.boot.extraModulePackages;
in
{
  _class = "nixos";

  options.hardware.corecycler = {
    ryzenSmu.enable = lib.mkEnableOption "the ryzen_smu kernel driver (amkillam fork, Zen 1 to Zen 5), which every Curve Optimizer and Curve Shaper read and write goes through, built against the configured kernel";
    zenpower.enable = lib.mkEnableOption "the zenpower5 kernel driver in place of k10temp, which it blacklists: Tctl, Tdie, Tccd and RAPL power, plus SVI2 voltage and current on Zen 1 to 4";
    asusEcSensors.enable = lib.mkEnableOption "the asus-ec-sensors kernel driver (zeule's copy, ahead of the in-tree one) for ASUS boards' embedded controller: VRM, chipset, CPU package and water temperatures, on boards mainline does not list yet before Linux 7.3; it loads in place of the in-tree copy";
    nct6687.enable = lib.mkEnableOption "the nct6687 kernel driver (Fred78290's nct6687d) for the Nuvoton NCT6687D-R on MSI boards, with the fan control the in-tree nct6683 refuses there; it blacklists nct6683, which would otherwise bind the chip first";
    nct6775.enable = lib.mkEnableOption "the in-tree nct6775 driver for Nuvoton NCT6775 to NCT6799 Super I/O chips (most ASUS and ASRock boards, older MSI ones): motherboard voltages, fans and temperatures";
    nct6683.enable = lib.mkEnableOption "the in-tree nct6683 driver for Nuvoton NCT6683, NCT6686 and NCT6687 chips (newer MSI and some ASRock boards); it only reads them on MSI boards, where nct6687 also controls the fans";
    coretemp.enable = lib.mkEnableOption "the in-tree coretemp driver: Intel CPU core and package temperatures";
    spd5118.enable = lib.mkEnableOption "the in-tree spd5118 driver with i2c_dev: DDR5 DIMM temperatures from each module's SPD hub";
    asusWmiSensors.enable = lib.mkEnableOption "the in-tree asus_wmi_sensors driver for older ASUS boards (the X370 to X399 era) through the BIOS WMI interface; the kernel's own documentation warns that WMI BIOS bugs on some of these boards can stop the fans or pin them at full speed";
    gigabyteWmi.enable = lib.mkEnableOption "the in-tree gigabyte_wmi driver: six temperature channels on Gigabyte boards through the BIOS WMI interface";
    drivetemp.enable = lib.mkEnableOption "the in-tree drivetemp driver: SATA drive temperatures";
    it87.enable = lib.mkEnableOption "the it87 kernel driver (frankcrawford fork) for ITE Super I/O chips: motherboard Vcore, fans and temperatures on most Gigabyte boards";
  };

  config = lib.mkMerge [
    {
      assertions = lib.mapAttrsToList (option: family: {
        assertion = !(cfg.${option}.enable && lib.length (copiesOf family) > 1);
        message = "hardware.corecycler.${option} builds its own ${family}, but boot.extraModulePackages also carries ${
          lib.concatMapStringsSep ", " lib.getName (copiesOf family)
        }: a kernel loads one module of a name, so turn the other copy off (nixpkgs' ryzen_smu: hardware.cpu.amd.ryzen-smu.enable)";
      }) families;
    }
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
    (lib.mkIf cfg.nct6775.enable { boot.kernelModules = [ "nct6775" ]; })
    (lib.mkIf cfg.nct6683.enable { boot.kernelModules = [ "nct6683" ]; })
    (lib.mkIf cfg.coretemp.enable { boot.kernelModules = [ "coretemp" ]; })
    (lib.mkIf cfg.spd5118.enable {
      boot.kernelModules = [
        "i2c_dev"
        "spd5118"
      ];
    })
    (lib.mkIf cfg.asusWmiSensors.enable { boot.kernelModules = [ "asus_wmi_sensors" ]; })
    (lib.mkIf cfg.gigabyteWmi.enable { boot.kernelModules = [ "gigabyte_wmi" ]; })
    (lib.mkIf cfg.drivetemp.enable { boot.kernelModules = [ "drivetemp" ]; })
    (lib.mkIf cfg.it87.enable {
      boot.kernelModules = [ "it87" ];
      boot.extraModulePackages = [ (build ./it87.nix) ];
    })
  ];
}
