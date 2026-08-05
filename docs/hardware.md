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
| Zen 3 (Chagall) | TR PRO 5965WX, 5995WX | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | Threadripper PRO 5000, Vermeer SMU commands (model 0x08) |
| Zen 3 (Cezanne) | 5600G, 5700G | -30 to +30 | MP1 set, RSMU get | PPT/TDC/EDC | -- | APU command set (set 0x54/0x55, get 0xC3) |
| Zen 3 (Rembrandt) | 6800U, 6900HX | -30 to +30 | MP1 set, RSMU get | PPT/TDC/EDC | -- | APU, Phoenix-class CO commands (set 0x4B/0x4C, get 0x2F) |
| Zen 3D (Warhol) | 5800X3D | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | V-Cache; be conservative (>-25 risky) |
| Zen 4 (Raphael) | 7600X, 7700X, 7900X, 7950X | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Extended negative range |
| Zen 4 X3D (Raphael) | 7800X3D, 7900X3D, 7950X3D | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Raphael |
| Zen 4 (Phoenix) | 7840U, 7840HS, 8845HS | -50 to +30 | MP1 set, RSMU get | PPT/TDC/EDC | -- | APU (0x74/0x75, classic 8-core CCX; set 0x4B/0x4C, get 0xE1) |
| Zen 4 (Phoenix2/HP2) | 7540U, 8500G, Ryzen 5 220 | -50 to +30 | MP1 set, RSMU get | PPT/TDC/EDC | -- | Heterogeneous 2+4c CCX (0x78/0x7C); Phoenix commands, no per-core slot map |
| Zen 4 (Dragon Range) | 7945HX, 7845HX | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Mobile, same silicon as Raphael |
| Zen 4 (Storm Peak) | 7980X, 7970X TR | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Threadripper PRO |
| Zen 5 (Granite Ridge) | 9600X, 9700X, 9900X, 9950X | -50 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Widest negative CO range |
| Zen 5 X3D (Granite Ridge) | 9800X3D, 9900X3D, 9950X3D | -50 to +10 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Granite Ridge |
| Zen 5 (Strix Point) | Ryzen AI 9 HX 370 | -50 to +10 | MP1 set, RSMU get | PPT/TDC/EDC | -- | Heterogeneous 4+8c, two CCX (set 0x4B/0x4C, get 0xAF); per-core index space publicly unresolved, all-core validated |
| Zen 5 (Krackan Point) | Ryzen AI 7 350, AI 5 330 | -50 to +10 | MP1 set, RSMU get | PPT/TDC/EDC | -- | Heterogeneous 4+4c / 1+3c (models 0x60/0x68); Strix commands |
| Zen 5 (Strix Halo) | Ryzen AI Max | -50 to +10 | MP1 set, RSMU get | PPT/TDC/EDC | -- | Classic 8-core CCDs, Strix commands; CO tuning unverified on this die |
| Zen 5 (Shimada Peak) | TR 9980X, PRO 9995WX | -50 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Model 0x08 (dump B00F81); different SMU addresses (`get_co=0xA3`) |

On harvested or multi-CCD parts (5900X 6+6, 5600X 6-of-8, 9900X, ...) the SMU
addresses physical core slots, with gaps where cores are fused off. The kernel's
`/proc/cpuinfo` `core id` (decoded from the APIC ID via CPUID `Fn8000_001E`) carries
that physical, gap-preserving numbering on most machines, and the driver then derives
the slot directly (`core id % 8`, CCD from L3 topology). Some BIOS/AGESA builds
instead renumber core ids contiguously on harvested parts (reported on a 5600X,
issue #11), which hides the fused-off slots. The driver detects this: a numbering
that proves itself physical (holes, or only full 8-core CCDs) is used as-is with no
SMU traffic, while an ambiguous CCD is probed once with the read-only CO query and
its cores map onto the answering slots in ascending order -- the same
order-preserving mapping Windows tools build from the SMN core-disable fuse (root-
only here, since `ryzen_smu` exposes `smn` to root alone). If the probe cannot
isolate the fused-off slots, per-core CO is disabled with an explicit reason (GUI
banner, CLI stderr, tuner refuses to start) instead of ever writing to the wrong
core. Discovery is gated per generation on the verified classic 8-slot-per-CCD
layout; heterogeneous Zen 4c/5c dies (Phoenix2, Strix Point, Strix Halo) keep the
legacy core-id addressing untouched.

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
| Zen 5 / 5D | Yes | Full | -50 to +10 |
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
