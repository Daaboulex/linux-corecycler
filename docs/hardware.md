<!-- markdownlint-disable MD013 -->

# Hardware support

Stress testing works on **any x86-64 CPU**, including Intel: per-core cycling, MCE
detection, topology detection, temperature monitoring, and every stress backend work
without the `ryzen_smu` module. Only the Curve Optimizer features (SMU read/write) are
AMD-specific.

## Curve Optimizer (SMU) support

| Generation | Example CPUs | CO Range | SMU Mailbox | PBO Limits | Boost Limit | Notes |
|---|---|---|---|---|---|---|
| Zen 1 / Zen+ | 1800X, 2700X | -- | RSMU | PPT/TDC/EDC | Read only | No CO -- PBO limits and scalar only (Matisse SMU fallback) |
| Zen 2 (Matisse) | 3600X, 3700X, 3900X, 3950X | -- | RSMU | PPT/TDC/EDC | Read only | No CO -- PBO limits and scalar only |
| Zen 2 (Castle Peak) | 3960X, 3970X, 3990X | -- | RSMU | PPT/TDC/EDC | Read only | Threadripper, no CO |
| Zen 3 (Vermeer) | 5600X, 5800X, 5900X, 5950X | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | Full CO support |
| Zen 3 (Cezanne) | 5600G, 5700G | -30 to +30 | MP1 | PPT/TDC/EDC | -- | APU, same CO commands as Vermeer |
| Zen 3 (Rembrandt) | 6800U, 6900HX | -30 to +30 | MP1 | PPT/TDC/EDC | -- | APU, uses Cezanne CO commands |
| Zen 3D (Warhol) | 5800X3D | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | V-Cache; be conservative (>-25 risky) |
| Zen 4 (Raphael) | 7600X, 7700X, 7900X, 7950X | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Extended negative range |
| Zen 4 X3D (Raphael) | 7800X3D, 7900X3D, 7950X3D | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Raphael |
| Zen 4 (Phoenix) | 7840U, 7840HS, 8845HS | -50 to +30 | RSMU | PPT/TDC/EDC | -- | APU (Phoenix / Hawk Point) |
| Zen 4 (Dragon Range) | 7945HX, 7845HX | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Mobile, same silicon as Raphael |
| Zen 4 (Storm Peak) | 7980X, 7970X TR | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Threadripper PRO |
| Zen 5 (Granite Ridge) | 9600X, 9700X, 9900X, 9950X | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Widest negative CO range |
| Zen 5 X3D (Granite Ridge) | 9800X3D, 9900X3D, 9950X3D | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Granite Ridge |
| Zen 5 (Strix Point) | Ryzen AI 9 HX 370 | -60 to +10 | RSMU | PPT/TDC/EDC | -- | APU |
| Zen 5 (Strix Halo) | Ryzen AI Max | -60 to +10 | RSMU | PPT/TDC/EDC | -- | APU, uses Strix Point commands |
| Zen 5 (Shimada Peak) | Zen 5 Threadripper | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Different SMU addresses (`get_co=0xA3`) |

On harvested or multi-CCD parts (5900X 6+6, 5600X 6-of-8, 9900X, ...) the kernel
renumbers cores contiguously while the SMU addresses physical slots; the driver probes
the SMU to build the correct kernel-core to physical-slot mapping so a CO write always
lands on the intended core.

All generations support PBO scalar read/write (1.0x to 10.0x) and OC mode
enable/disable. SMU features require the
[ryzen_smu](https://github.com/amkillam/ryzen_smu) kernel module (amkillam fork) and
root or appropriate sysfs permissions.

### CPU support summary

| Generation | Stress Testing | Curve Optimizer | CO Range |
|---|---|---|---|
| Zen 1 / Zen+ | Yes | No (PBO limits/scalar only) | -- |
| Zen 2 | Yes | No (PBO limits/scalar only) | -- |
| Zen 3 / 3D | Yes | Full | -30 to +30 |
| Zen 4 / 4D | Yes | Full | -50 to +30 |
| Zen 5 / 5D | Yes | Full | -60 to +10 |
| Intel | Yes | No | -- |

## Curve Shaper (Zen 5)

Curve Shaper is a Zen 5 feature that adjusts voltage across 5 frequency regions and 3
temperature points (15 tuning parameters total), stacking on top of Curve Optimizer
offsets. **Curve Shaper is BIOS-only** -- there are no known SMU commands to read or
write it at runtime, so this tool cannot interact with it. Configure Curve Shaper in
your BIOS alongside CO, then use this tool to validate the combined result.

## PBO Boost Override and BCLK

This tool imposes no artificial limits on boost clocks. A BIOS PBO Boost Override of
+200 MHz is respected; a BCLK of 105 MHz or higher (AM5) scales the effective clocks
and the tool adapts. System-state detection reads the actual max frequency from
`cpufreq` sysfs.
