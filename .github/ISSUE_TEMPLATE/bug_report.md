---
name: Bug Report
about: Report incorrect behavior, crashes, or unexpected results
labels: bug
---

<!-- markdownlint-disable MD033 -->

# Bug Report

## System Info

- **CPU**: (e.g., AMD Ryzen 9 9950X3D)
- **Zen generation**: (e.g., Zen 5)
- **Motherboard**: (e.g., MSI MEG X670E ACE)
- **BIOS version**:
- **Kernel**: (output of `uname -r`)
- **ryzen_smu version**: (if applicable — `cat /sys/kernel/ryzen_smu_drv/version`)
- **CoreCycler version**: (`corecycler --version` or git commit)
- **Install method**: (NixOS module / nix run / pip / from source)

## Description

What happened?

## Expected Behavior

What should have happened?

## Steps to Reproduce

1.
2.
3.

## Relevant Output

<details>
<summary>Terminal output / error messages</summary>

```text
paste here
```

</details>

<details>
<summary>dmesg (if MCE/crash related)</summary>

```bash
sudo dmesg | tail -50
```

</details>

## Additional Context

- [ ] Runs as root
- [ ] ryzen_smu module loaded
- [ ] Issue occurs consistently (not intermittent)
