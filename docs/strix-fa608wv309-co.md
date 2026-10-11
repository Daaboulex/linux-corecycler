# Verified HX 370 Curve Optimizer protocol

This profile is limited to ASUS FA608WV.309, SMU 11.93.11.0, Ryzen AI 9 HX
370 (family 0x1A, model 0x24, stepping 0), with all 12 cores and SMT online.
It does not generalize this protocol to other Strix Point or Strix Halo images.

## Firmware evidence

The official [ASUS update](https://www.asus.com/us/supportonly/fa608wv/helpdesk_bios/)
contains the SMU and CCX firmware. Analysis used full decompressed bodies from
[PSPTool](https://github.com/PSPReverse/PSPTool), with Xtensa assembly checked
against the decompiler output. Body SHA256 values:

- SMU: `92d3b77a027eae84c02c07a508e831831660f3622811c679eae2165f7eb8048f`
- CCX: `d96ac821d6d22808198d88891b9487d966299e12ccfa906e2796c0dac78f6964`

RSMU 0xAF, the previous getter, forwards internal CCX 0x0F and queries a
floating-point curve, returning 600 for the existing request encoding.
RSMU 0x5E maps to SMU handler 0x494A8, forwards internal CCX 0x53, and reaches
CCX handler 0x145BC. That handler loads the same signed integer field written
by MP1 0x4B (SMU 0x48E5C -> CCX 0x47 -> 0x1445C).

The getter and setter both encode group in bits 31:28 and slot in bits 27:20.
The presence check accepts group 0 slots 0,2,4,6 and group 1 slots 0..7.
SMU initialization at 0x28A40 compacts populated slots in ascending order
into APIC core numbering, then adds SMT thread bits. Live CPUID confirms:

| Linux physical core IDs | Firmware group | Firmware slots |
| --- | --- | --- |
| 0,1,2,3 | 0 | 0,2,4,6 |
| 8,9,10,11,12,13,14,15 | 1 | 0,1,2,3,4,5,6,7 |

RSMU 0x82 reports capability bits. Its bit 2 uses the same permission test as
the CO setter; the tested machine reports 0xE6. Presence and capability do not
substitute for validating the setter, getter, and restoration together.

## Driver prerequisite and checks

The tested ryzen_smu 0.1.7 source masks a fast firmware rejection as success
because it checks `tmp != SMU_Return_OK && !retries`. Direct reads of response
SMN 0x03B10A80 confirmed firmware 0xFF while sysfs reported 1 on absent slots.
The driver must propagate non-success responses before enabling this profile.

Profile initialization issues only read commands. It verifies exact identity,
SMU version, online SMT/L3 layout, absent-slot rejection, capability bit 2,
all 16 slot statuses, and full signed-32-bit margin bounds. Any mismatch keeps
CO writes blocked. The shared command set remains unchanged; the verified
instance uses getter 0x5E and an explicit core map.

## Hardware validation and limits

Two read passes found zero margins on all 12 populated slots and 0xFF on the
four absent slots. After fixing the driver, its status matched the actual
hardware response on all 32 probes.

Minimal -1/set/read/restore tests passed for group/slot 0:0, 0:2, and 1:1.
Each checked all 12 margins before, during, and after the change: only the
selected slot changed, and every baseline was restored. The normal CoreCycler
API separately backed up 12 values, changed Linux core 1 to -1, verified the
other cores were unchanged, and restored all 12 values successfully.

The GUI then reported Connected and 12 zero CO values. No full auto-tuning
or stability session was run. Effective voltage reduction under load was not
independently measured. No firmware, fuse, or BIOS setting was modified.

Tests cover all core addresses, backup/restoration, rejected requests,
incorrect firmware/layout/capability, invalid full-width margins, and removal
of the profile when the SMU version changes.
