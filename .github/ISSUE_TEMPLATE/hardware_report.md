---
name: Hardware Compatibility Report
about: Report how CoreCycler works (or doesn't) on your specific hardware
labels: hardware
---

<!-- markdownlint-disable MD033 -->

# Hardware Compatibility Report

## Hardware

- **CPU**: (exact model, e.g., AMD Ryzen 7 9700X)
- **Zen generation**: (e.g., Zen 5)
- **Core count**: (physical / logical, e.g., 8/16)
- **Harvested?**: (yes if fewer cores than the full die, e.g., 9900X has 12 of 16)
- **X3D?**: (yes/no)
- **Motherboard**: (brand + model)
- **Super I/O chip**: (run `sensors` and note the chip name, e.g., nct6687-isa-0a20)

## What Works

- [ ] CPU detection (model name, family, generation)
- [ ] Topology detection (correct core count, CCD count)
- [ ] Temperature monitoring (Tctl, Tdie, Tccd)
- [ ] Vcore voltage reading (source: k10temp / zenpower / Super I/O)
- [ ] CO read (current offsets readable)
- [ ] CO write (offsets applied and read-back verified)
- [ ] Stress testing (which backends tested)
- [ ] Auto-tuner (completed a session)

## What Doesn't Work

Describe any issues, unexpected behavior, or missing functionality.

## Diagnostic Output

<details>
<summary>sensors output</summary>

```text
sensors
```

</details>

<details>
<summary>/proc/cpuinfo (first entry only)</summary>

```text
head -30 /proc/cpuinfo
```

</details>

<details>
<summary>SMU sysfs (if available)</summary>

```bash
ls -la /sys/kernel/ryzen_smu_drv/ 2>/dev/null
cat /sys/kernel/ryzen_smu_drv/version 2>/dev/null
```

</details>

<details>
<summary>hwmon devices</summary>

```bash
for d in /sys/class/hwmon/hwmon*; do
  echo "=== $d ==="
  cat "$d/name" 2>/dev/null
  ls "$d"/in*_label 2>/dev/null | while read f; do
    echo "  $(basename $f): $(cat $f)"
  done
done
```

</details>
