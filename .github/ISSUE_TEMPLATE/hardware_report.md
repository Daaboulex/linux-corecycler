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
<summary>Core numbering (ALL cores -- needed for any Curve Optimizer issue)</summary>

The SMU addresses physical core slots and counts the fused-off ones, so on a
harvested CPU the whole core-id list is what shows whether your firmware leaves
holes at the dead slots or renumbers around them. One core's entry cannot show
that, which is why both commands below cover every core.

```bash
lscpu -e
grep -E '^(processor|physical id|core id|cpu cores|apicid)' /proc/cpuinfo
```

</details>

<details>
<summary>SMU sysfs (if available)</summary>

File ownership and mode matter as much as the listing: Curve Optimizer needs
group access to `smu_args`, the mailbox file, and `smn`.

```bash
ls -la /sys/kernel/ryzen_smu_drv/ 2>/dev/null
cat /sys/kernel/ryzen_smu_drv/version 2>/dev/null
```

</details>

<details>
<summary>CoreCycler SMU log (if Curve Optimizer misbehaves)</summary>

The driver says exactly why it refused, including what the core-disable fuse
reported. Paste that rather than reading SMU registers by hand -- the fuse
address differs per CPU generation and a wrong one returns a plausible but
wrong answer.

```bash
grep -iE 'smu|core map' ~/.local/share/corecycler/logs/corecycler.log | tail -40
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
