# Changelog

All notable changes to this CoreCycler-for-Linux project are recorded here,
following [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Current version: 0.0.1. A per-core CPU stability tester and AMD PBO Curve
Optimizer tuner for Linux, packaged as a NixOS module with an overlay.

### Changed (2026-08-18 the app renders in the desktop's own colors)

- CoreCycler no longer paints its own chrome. The 250-line hardcoded dark
  stylesheet is gone and every widget -- buttons, tabs, tables, scrollbars, and
  the save/load dialog -- is drawn by the desktop, so a KDE session gets a
  Breeze app and a GNOME session an Adwaita one. `gui/style.py` keeps only the
  colors that carry meaning the desktop cannot supply (the core-grid legend,
  status and phase colors, chart series) and resolves them against the current
  color scheme, with panels, borders and dimmed text taken from the live
  palette. A desktop that changes its scheme while the app runs is followed.
  The six arrow SVGs the stylesheet needed are deleted with it.
- Every color the app still chooses is gated on readability: state cells,
  status and phase text carry 4.5:1 against their own background in both
  schemes, dimmed text and chart series 3:1. Three colors that shipped below
  that in the dark theme (failed cells, mem-stress cells, the deeper hardening
  phase) were corrected.

### Fixed (2026-08-18 a sudo run keeps its own runtime directory)

- Qt reported the runtime directory on every lookup, first as one root does not
  own (it was the invoking user's, set so Qt could find the Wayland socket) and
  then, once that was dropped, as one that is not set at all. Root now gets a
  runtime directory of its own, made fresh per run as the base directory spec
  requires, which is possible because the Wayland socket is named by its
  absolute path and no longer resolved against it. The stress work root is
  unchanged: it follows the invoking user, never whoever the euid happens to be.

### Fixed (2026-08-18 a sudo run took root's own KDE colors, issue #14)

- A sudo run rendered light on a dark desktop even with the session's config
  search path recovered. KConfig reads `XDG_CONFIG_HOME` before that path, so a
  `kdeglobals` any earlier root run of a Qt or KDE app left in `/root/.config`
  decided the color scheme, and because KConfig merges per key the result was a
  half-and-half palette: root's window and view colors over the user's
  everything else. A sudo run now gets an empty config home of its own, fresh
  per run, so the recovered session is what decides and one invoking user's
  leftovers never reach the next. Nothing is written into the user's config,
  which was the reason the config home stayed root's in the first place.

### Fixed (2026-08-18 the save/load dialog was unreadable, issue #14)

- The dialog was the desktop's own all along; what was wrong was the app
  painting a dark stylesheet over it while the palette stayed the desktop's, so
  alternating rows, selection and icons kept colors that could not be read
  against the forced ones. It needed neither KDE nor sudo to reproduce: with no
  platform theme at all Qt's default palette is light, and the app's forced
  `#ddd` text landed on a `#f7f7f7` row. Fixed by the change above. The
  2026-08-18 identity recovery released earlier the same day did not fix it --
  it addressed the desktop handshake, which was not what was broken.
- Under `sudo` the desktop's appearance is now recovered read-only: the
  invoking user's config search path (their `kdedefaults` and config home)
  joins root's, so their color scheme and icon theme are read, while
  `XDG_CONFIG_HOME` stays root's own, so nothing is written into their config.
  Without it a root run renders in the toolkit's default light theme however
  the user's desktop is set.

### Fixed (2026-08-12 the GUI survives close, stop and refused starts mid-test)

- Closing the window while a test ran crashed every time: the history database
  closed first, then Qt delivered the worker's still-queued `finished` signal
  into the dead connection (`sqlite3.ProgrammingError`, three times on
  2026-08-11). The close path now disconnects the handler before stopping the
  worker, late handlers return early once the window is closing, a worker
  thread that crashes surfaces on the status bar instead of dying silently,
  and the results summary counts only cores that earned a verdict -- a stopped
  run no longer reads as failed silicon.
- Stopping a memory-stress run could hang the app: `stop()` and the worker's
  own `communicate()` waited on the same child and contended the waitpid lock,
  so a tool that ignored SIGTERM was never escalated to SIGKILL. `stop()` now
  only signals the process group and the worker stays the one reaper.
- A stress work directory that cannot be created (a read-only parent, found
  live on ryzen) is reported as "Work directory unavailable" with the real
  error and no worker starts, instead of an uncaught `PermissionError` out of
  the Start button.

### Changed (2026-08-11 stress runs inside a kernel-enforced cgroup boundary)

- Every stress process launches inside a transient systemd scope with
  `AllowedCPUs` pinned to the core under test, its lifetime bound to the app
  via `setpriv --pdeathsig`. A cgroup cpuset is a boundary the payload cannot
  widen with `sched_setaffinity`, replacing the 2-second re-pin chase that let
  mprime run its load on core 0 under the tested core's name (the all-32-CPU
  escape reproduced live on 2026-08-11). `systemd-run` and `setpriv` are now
  required tools in `corecycler doctor`; `taskset` is no longer used. When no
  cgroup mechanism exists the launch is refused, never run uncontained, and
  mprime refuses to start against a missing or unreadable config instead of
  falling back to one self-pinned worker per core.
- The escape watchdog judges the kernel's own record -- each scope's
  `cpuset.cpus.effective`, not the `/proc` affinity mask, which shows every
  CPU for a cpuset-confined process and could both false-pass and false-fault.
  A scope that vanishes, never adopts the payload, or runs wider than its lane
  fails as a containment fault, never as a core verdict.
- Every stress path (solo cycling, variable load, rapid transitions, parallel
  lanes, memory stress) runs through one supervised execution loop. Solo
  cycling records a failed core's verdict and moves on instead of stopping the
  whole run, an interrupted lane never invents a pass verdict, and a machine
  check without core attribution stops the batch as unattributed instead of
  blaming the tested core. Ring B gains live containment drift tests, and
  `scripts/live_scenarios.py` drives the real GUI, workers, scopes and
  database through scripted scenarios on real hardware.

### Fixed (2026-08-11 mprime writes real options; per-user work root)

- The instruction-set selector wrote a `TortureWeak` key that is not an mprime
  option -- requesting SSE ran AVX-512 -- and forced every CpuSupports flag
  with a misspelled `CpuSupportsAVX512`. The backend now writes the real
  `CpuSupports*` overrides, verified live on 31.04b02 (SSE -> Pentium4 type-1
  FFT, AVX -> AVX, AVX2 -> FMA3, AVX512 -> AVX-512), plus
  `EnableSetAffinity=0`, which removes mprime's self-pinning at the source. A
  version contract pins the verified set, with a Ring B drift test that fails
  on an unverified mprime version.
- `Threads=1|2` is honored as written: the scheduler no longer overwrites it
  with the SMT width, so one thread means one logical CPU and two mean both
  siblings; the tuner keeps full-core stress by requesting two explicitly.
- The default work root moves from the shared `/tmp/corecycler` -- where one
  sudo run's root-owned leftovers broke every later plain run -- to a per-user
  `XDG_RUNTIME_DIR/corecycler/work` with a user-cache fallback; sudo ownership
  is repaired on creation and the old default in saved settings migrates
  automatically. A `settings.json` without a `profiles` key is no longer
  misread as corrupt, which had silently moved the user's settings aside and
  reset them.

### Fixed (2026-08-11 a stopped tuner session is never lost, only stopped)

- A session that stopped early became invisible. `list_resumable_tuner_sessions`
  returns only `running`, `paused` and `validating`, so a **quarantined**
  session (the circuit breaker, after N crash-resumes with no surviving test
  between) or an **aborted** one dropped out of the picker entirely -- while
  every core's phase, baseline, best and confirmed offsets sat intact in the
  database. Issue #13: days of tuning apparently gone, and the only way
  forward was to start from zero.
- The picker now lists every recoverable session, newest first, with when it
  started, when it was last touched, its status and how many cores it had
  confirmed. Automatic resume is unchanged and still refuses a quarantined
  session: it is offered, never taken silently, and picking one asks first.
- Re-opening a quarantined session re-engages on proven ground only. Every
  offset that would reach the hardware -- the search position and the baseline
  every other core is restored to -- drops to the most aggressive value that
  session has actually SURVIVED, stock when it has survived none. Fail bounds,
  best offsets and phases are kept, so the work is continued rather than
  discarded, and the crash streak restarts.

### Fixed (2026-08-11 a backend is found where it actually is, on every distro)

- `sudo` replaces PATH with the sudoers `secure_path` on Debian, Ubuntu, Mint,
  Fedora and Arch, so a directory the user added to PATH in their shell is gone
  inside `sudo corecycler` -- and mprime and y-cruncher ship as tarballs that
  are never on PATH to begin with. Issue #12 reported exactly that: y-cruncher
  on PATH, "not installed or not on PATH" in the dialog. Tool lookup is now one
  resolver (`config.tools`) shared by every backend, taskset, dmesg,
  journalctl, dmidecode and notify-send, resolving `CORECYCLER_<TOOL>_BIN`,
  then the path recorded in `~/.config/corecycler/tool-paths.json`, then PATH --
  and an explicit path that is not executable is refused with the reason instead
  of silently falling back.
- A missing backend is no longer a dead end: CoreCycler looks for an extracted
  mprime or y-cruncher in the invoking user's `~`, `~/Downloads`, `/opt`,
  `/usr/local`, `/usr/local/lib` and `/usr/lib`, and offers what it finds. It
  never runs a scanned binary until the user picks it -- it runs as root, and
  silently executing something found in `$HOME` is what `secure_path` exists to
  prevent. The choice is recorded, so later runs resolve it without asking.
- New `corecycler doctor`: every external tool, where it resolved from, and any
  candidate found but not yet chosen. It replaces four different lookup paths
  (a `which` subprocess, two `shutil.which` call sites, and bare-name exec) --
  `which` is not even guaranteed present, being an update-alternatives symlink
  on Debian-family systems and absent from minimal Fedora images.
- New `distro-matrix` workflow: installs what `docs/installation.md` prescribes
  inside debian:trixie, ubuntu:24.04, fedora:latest and archlinux:latest, then
  asserts every tool resolves and that a y-cruncher tarball in `$HOME` is still
  discovered with PATH scrubbed to `secure_path`. Discovery is a pinned drift
  contract (`external-tool-discovery`) with a live Ring B test.
- Corrected in the docs, each verified against the distro's own package index:
  stressapptest is AUR-only on Arch (the documented `pacman -S` line failed),
  it IS in Fedora's repos (no source build), and y-cruncher is packaged both in
  nixpkgs and in the AUR. `packages.full` now bundles y-cruncher alongside
  mprime.

### Fixed (2026-08-07 core-slot discovery reads the fuse, not the CO mailbox)

- The slot discovery added for issue #11 rested on a premise the reporting
  5600X then falsified: it assumed a fused-off slot does not answer the CO
  read, and all eight slots of that six-core CCD answered. The probe never had
  any discriminating power, so a renumbered part could not be resolved at all
  and per-core CO stayed disabled. It is replaced by the SMU's own record --
  the per-CCD SMN **core-disable fuse** (CCD n at `core_fuse_addr + (n << 25)`,
  low 8 bits, a set bit being a fused-off slot), the same ground truth
  ZenStates-Core and ryzen_monitor read. Discovery now sends no CO traffic at
  all, and the falsified premise is pinned as its own live contract so a die
  that ever did discriminate would say so instead of being assumed.
- Fuse addresses are declared per generation and only where grounded
  (`0x30081D98` Vermeer/Warhol/Chagall/Storm Peak, `0x30081CD0` Raphael/Dragon
  Range, `0x304A03DC` Granite Ridge). The APU dies and Shimada Peak carry none
  — upstream excludes the former from the fuse path and marks the latter
  uncertain — so a renumbered part there refuses by name rather than reading a
  neighbouring generation's address and mapping cores from garbage.
- Reading an SMN register is a *write* of its address, and `ryzen_smu` ships
  `smn` root-only, so the NixOS module now grants it to the `corecycler` group
  alongside the mailbox files and the driver names that exact gap when it is
  missing. The non-NixOS recipe in installation.md is corrected with it: the
  old udev rule chmod-ed 0660 without ever changing the group, which granted a
  normal user nothing, and raced the driver's own sysfs creation.

### Fixed (2026-08-06 APU PBO commands, slot-proof soundness, wiring matrix)

- APU PBO command ids were desktop-copied like the CO ids before them; they
  now carry the reference APU RSMU block (ZenStates-Core APUSettings1, ids
  corroborated by RyzenAdj's RSMU paths): `set_ppt` maps to the APU
  SetSlowLimit — the sustained package-power analogue, since no desktop PPT
  command exists on APUs — tdc/edc to TDC/EDC-VDD, htc to SetTctlMax, scalar
  get/set to 0xF and 0x3F (Cezanne) / 0x3E (Phoenix and later), boost get
  0x42 everywhere with set-all 0x47 from Phoenix on, OC mode 0x17/0x18/0x82,
  and PM-table transfer/base/version 0x65/0x66/0x6. Rembrandt's CO range is
  pinned back to its documented -30..+30. All of it is a drift contract.
- `get_fastest_core_cmd` is removed from Zen 4/5 desktop sets: RSMU 0x59
  there is SetTctlMax — the thermal-limit SETTER — and the fastest-core query
  exists only on Zen 2/3 (still wired there). The latent misfire (a
  thermal-limit write masquerading as a read in system-state detection) can
  no longer be sent, and the absence is a pinned contract.
- The core-map gap proof is now per-CCD: only a hole internal to a group's
  own 8-slot window proves physical numbering, so a firmware that compacts
  ids per CCD while keeping the window stride (a multi-CCD sibling of the
  issue-#11 class) probes instead of silently cross-pairing cores. Holes are
  only trusted when every present CPU is online — a fully-offlined core fakes
  a hole — and while discovery runs the driver refuses rather than falling
  back to legacy addressing. `validate_profile` now refuses on an unusable
  core map exactly like start/resume, dry-run reset-all refuses like the real
  path, and the CLI no-SMU message no longer blames a missing module when the
  core map was the refusal.
- New emulated verification layers: a wiring conformance matrix drives every
  generation's command set through the real driver against a recording
  mailbox (exact mailbox, command id and encoded argument per operation, and
  zero traffic where a command is absent), and an end-to-end emulated tuner
  run on the renumbered-5600X shape proves engine traffic lands only on the
  discovered slots.

### Fixed (2026-08-05 core-id renumbering on harvested parts, issue #11)

- Some BIOS/AGESA builds renumber `/proc/cpuinfo` core ids contiguously on
  harvested parts (reported on a 5600X) instead of leaving holes at the
  fused-off slots; the SMU layer assumed core id == physical slot, so CO
  reads/writes addressed dead or wrong slots and failed. `RyzenSMU.set_topology`
  now discovers the id-to-(CCD, slot) map: numberings that prove themselves
  physical (holes, or only full 8-core CCDs) are used directly with no SMU
  traffic; an ambiguous CCD is probed once with the read-only CO query and its
  cores map onto the answering slots in ascending order (the order-preserving
  mapping Windows tools build from the root-only SMN core-disable fuse); an
  undiscoverable map disables per-core CO with an explicit reason surfaced in
  the GUI, on CLI stderr, and as a tuner start/resume refusal — never a write
  to the wrong core. Discovery is gated per generation by a declared
  `uniform_8core_ccds` command-set field, so heterogeneous Zen 4c/5c dies
  (Phoenix2, Strix Point, Krackan) and Strix Halo (classic CCDs, CO tuning
  unverified there) keep the previous addressing bit-for-bit.
- Reading "all cores" (backup, context capture, system state) now iterates the
  machine's real core-id set instead of `range(n)` — on gap-preserving
  multi-CCD parts (5900X-class, ids 0-5 and 8-13) it previously queried two
  dead slots and missed the two highest cores. `set_all_co` read-back now
  verifies against the first existing core instead of literal core 0, which
  can be a fused-off slot.
- Generation routing is now one declarative model table grounded in the
  pinned ryzen_smu driver's CPUID map, AMD's 16-model-per-die block scheme,
  and InstLatx64 dumps: Chagall (0x00-0x0F) and Rembrandt (0x40-0x4F) get
  their own identities, Storm Peak covers its whole 0x10-0x1F block, Shimada
  Peak routes by model (family 0x1A, 0x00-0x0F, dump B00F81) as well as by
  name, Strix Halo resolves to its own generation, and the heterogeneous
  Zen4c/5c dies (Phoenix2 0x78, Hawk Point 2 0x7C, Krackan Point 0x60/0x68)
  route to APU generations that never engage the 8-slot core map. Unmatched
  family 0x19/0x1A models now fail closed to UNKNOWN instead of inheriting a
  desktop generation (Zen 6 CPUID sightings already sit outside every block).
- APU Curve Optimizer command IDs were desktop-copied and wrong; they now
  match the reference implementations (ZenStates-Core APUSettings, RyzenAdj):
  Cezanne sets via MP1 0x54/0x55 and reads back via RSMU 0xC3; Rembrandt,
  Phoenix, Phoenix2 and Hawk Point set via MP1 0x4B/0x4C with RSMU gets 0x2F
  and 0xE1; the Strix/Krackan class shares 0x4B/0x4C with RSMU get 0xAF. CO
  reads now ride the get-side mailbox where it differs from the set side, and
  the APU command IDs are pinned as a drift contract.

### Fixed (2026-07-20 packaging)

- The wheel installed ten flat top-level entries (`main`, `cli`, `notify` and
  the seven package directories) straight into `site-packages` — a global
  namespace. hermes-agent ships a flat `cli.py` too, so a Nix home profile
  containing both applications failed to build on the name collision.
  Everything now lives under one `corecycler` package (entry point
  `corecycler.main:main`); the packaging test pins the single-package
  invariant so nothing flat can return.

### Fixed (2026-07-16 truth-and-attribution session, ryzen-9950x3d forensics)

Field incident driving all of this: three freezes in one day during
validation while the kernel logged corrected Load-Store MCEs naming cores 9
and 12 — and the engine saw none of it, then stood ready to crash-penalize
core 7 (the deepest, most-proven offset) on the next resume.

- MCE detection had never fired: the sysfs machinecheck bankN files it
  counted are MCA control registers (constant `ffffffffffffffff`), not error
  counters, and the dmesg parser rejected the real AMD Zen decoded format
  (header killed by an info-pattern, `[Hardware Error]:` detail lines failing
  the token gate, `CPU:9` colon form never matching `CPU (\d+)`). The sysfs
  path is deleted; dmesg parsing is rewritten against the captured kernel
  lines from the incident (now regression fixtures); events carry CPU, bank,
  and CE/UE severity; both SMT siblings map to their physical core.
- Crash blame is now evidence first, never a guess: on resume after a reboot
  the engine harvests the kernel journal since the session's last activity
  (`journalctl _TRANSPORT=kernel`, cross-boot) and penalizes exactly the
  cores the kernel named. With no forensics, a persisted isolated hunt slot
  convicts its core;
  a single in-test core in the search flow keeps direct blame; anything
  ambiguous (multi-core set, or any crash under validation) triggers the
  isolated crash hunt — each suspect alone at its tuned value, every other
  core at stock, under stress + load transitions + idle watch, most suspect
  first. Two fruitless hunts pause the session for the owner instead of
  guessing. The old "penalize the most aggressive in-test core" rule is gone.
- Cross-core MCE evidence acts immediately: a corrected error on ANY core
  during ANY test backs that core off one step and demotes it to re-earn
  confirmation (uncorrected: full crash-grade penalty); its journal entry is
  kept un-survived. An error at stock (CO=0) is surfaced loudly as a non-CO
  problem instead of walking a zero offset.
- Multi-core stage verdicts no longer read only the first core: a detected
  failure on any core in a batch is reported for THAT core instead of
  vanishing behind core 0's pass (stage 2/3 pass verdicts were fictional).
- The CO drift warning compared live SMU values against session baselines,
  so reopening the app mid-validation reported the tuner's own confirmed
  offsets as foreign "drift" and promised to "restore baselines" — false on
  both counts. Drift now compares against the tuner's last journaled write
  and only fires when something outside the tuner changed the hardware.
- Clock-stretch sampling discards windows without sustained load (>= 90 %
  busy) instead of reporting boost/idle artifacts as stretch; the peak is now
  persisted per test (`peak_stretch_pct`) instead of living only in fail text.
- Stage 2/3 log lines claimed "simultaneous"/"half loaded" while
  CoreScheduler cycles cores one at a time — reworded to what actually runs
  (true parallel load is the validation-restructure work).
- Export/Validate read only `phase='confirmed'` and reported "no confirmed
  cores" on a fully HARDENED session; hardened cores are included now.

### Added (2026-07-17, durable narrative + login auto-resume)

- Schema v15: every narrative line the tuner emits is persisted to
  `tuner_events` (with boot id), so a session's story survives the terminal
  and reboots; resuming a session replays its recent story into the log.
- Login auto-resume: `corecycler --auto-resume [seconds]` waits for the
  system to settle, then resumes the active MID-RUN session (running or
  validating) — a paused session is a human choice and stays paused;
  quarantined/completed never qualify. The NixOS module gains
  `services.corecycler.autoResume.{enable,delaySeconds}` installing a login
  autostart entry; it runs sudo-less through the device-access group
  (SMU group-rw, MSR group-read, PM table world-read).
- Single-instance lock: a second corecycler exits immediately instead of
  fighting the first over the SMU.

### Added (2026-07-17, light-load coverage + real-world soak)

- The load class that actually crashes machines is now tested per core: a
  third default hardening tier runs the light-load spectrum (max-boost
  bursts, load transitions, idle watch) instead of sustained stress, and
  validation gains stage 5 — the same spectrum per core with every offset
  applied. Sustained-only testing passes cores that fail at max boost.
- Validation stage 6: the real-world soak. After a clean pass, the tuner
  applies the profile and just watches the kernel error stream (no
  synthetic load) while the machine is used normally; any hardware whisper
  demotes the named core and validation resumes after it re-earns. A dirty
  pass skips the soak; only the final clean pass earns it.
- Config: `validate_spectrum`/`spectrum_slot_seconds`,
  `validate_soak`/`soak_duration_seconds`, and hardening tiers accept
  `profile: spectrum`; all validated. Stage flow is a skip-chain (4 ->
  5 -> 6 -> finalize sentinel 7), so disabled stages fall through and the
  persisted cursor stays meaningful across versions.

### Changed (2026-07-17, simultaneous validation + observability)

- Validation stages 2/3 now genuinely run all target cores at once: one
  pinned stress process per core (`engine/parallel.py`), full package power
  draw, and per-core verdicts — every lane's pass/fail is logged, a failure
  names exactly its core, and launch failures fail closed (a missing binary
  behind taskset previously read as a clean pass).
- Live MCE polling no longer filters dmesg by level: AMD decoded
  corrected-error lines sit below err/warn and were silently invisible to
  the in-test detector (found live: a corrected error on an idle core during
  a validation slot left no trace). The line classifier is the filter.
- Two log surfaces: the human narrative stays at INFO on stderr; a rotating
  DEBUG file (`~/.local/share/corecycler/logs/corecycler.log`) records what
  verdicts drop — every dmesg poll and its events, every CO write with its
  survived state, and every validation-cursor save.
- Narration comments stripped repo-wide (175 lines): the source states
  timeless facts and constraints; the change story lives here.

### Changed (2026-07-16, incremental validation — schema v14)

- Validation progress is persisted after every transition
  (`tuner_sessions.validation_stage/index/half/dirty/requeue`): a reboot,
  app restart, or search interlude continues exactly where validation was.
  The old behavior restarted stage 1 for every core on every re-entry and
  every back-off — one field session logged 141 stage-1 tests (~12 h).
- A back-off now costs one re-test, not sixteen: a stage-1 failure retries
  only its own slot; a stage-2/3/4 failure backs off the FAILING core
  (per-core verdicts from phase 1), gives it one solo re-test with all
  offsets live, then reruns just the failed stage. Justification: raising
  one core's voltage cannot destabilize the others, so their coverage
  stays valid. Cores whose offset changed during a search interlude are
  detected by evidence (no stage-1 pass logged at their current best) and
  requeued automatically.
- DONE is stricter, not looser: if any back-off happened during a pass,
  one final complete validation pass must come through with zero
  back-offs before the profile is declared finished.

### Added (2026-07-16)

- PBO power limits (PPT W / TDC A / EDC A) captured into every tuning
  context (schema v13) and folded into the context identity — the same CO
  profile can be stable at PPT 200 W and unstable at 230 W. Read from the PM
  table with evidence-based Zen 5 header indices (live-verified on a
  9950X3D 0x620205: [2]=PPT limit, [3]=package power, [8]=TDC limit,
  [9]=TDC value, [63]=EDC-limit candidate); the old guessed [0..5] pair
  block decoded zeros and mislabeled every field on real Zen 5 silicon.
  Unknown table generations fail closed to "unknown" rather than store a
  mislabeled number.
- Schema v13: `tuning_contexts.ppt_limit_w/tdc_limit_a/edc_limit_a`,
  `tuner_sessions.unattributed_crashes/hunting_core`,
  `tuner_test_log.peak_stretch_pct`; fresh and migrated shapes stay
  identical; the DB-merge path carries the new columns.
- Tuner config: `hunt_slot_seconds` (default 60) and
  `max_unattributed_crash_hunts` (default 2), both validated.

### Fixed (2026-07-09 field-debug session, ryzen-9950x3d logs)

- mprime stale-results false-failure cascade: `cleanup(preserve_on_error=True)`
  left `results.txt` in place, `prepare()` never removed it, and mprime appends,
  so after one genuine failure every later test in that work dir re-parsed the
  old `FATAL ERROR` and failed at full duration. Observed live walking cores
  2/3/6 from -49/-44/-50 back to baseline one full 300 s "FAIL" at a time.
  Preserved post-mortem files are now renamed `failed-*`, `prepare()` cleans
  leftovers, and the scheduler polls `results.txt` every 5 s so a real error
  fails fast instead of at end-of-test.
- mprime patterns verified against Prime95 30.19b20 source (commonb.c): removed
  the benign `Worker stopped.` graceful-stop line from the fatal list (false
  positive), fixed the success pattern (`Self-test 4K passed!`, K-suffixed),
  corrected the torture-summary pattern, dropped folklore patterns with no
  source counterpart, and stopped writing `ResultsFile=`/`LogFile=` keys that
  mprime never reads.
- One database for sudo and non-sudo: history and settings paths resolved
  through `Path.home()`, so `sudo corecycler` silently used `/root/...` — a
  second database with divergent BIOS-change detection and invisible tuner
  sessions. All state now resolves to the INVOKING user (`SUDO_USER`),
  root-created files are chowned back, and any history the old bug left in
  `/root` is auto-merged into the user database once (source renamed
  `*.adopted`) via the new `HistoryDB.merge_from`.
- `sudo corecycler` aborted at startup (Qt xcb "could not connect to display",
  SIGABRT): sudo strips the display handshake env. main() now derives
  XDG_RUNTIME_DIR/WAYLAND_DISPLAY/XAUTHORITY from the invoking user's session
  and fails closed with an actionable message instead of Qt's abort when no
  display is reachable.
- Resume no longer penalizes plain app exits: crash detection (in_test flag +
  CO journal suspects) only fires when the machine actually REBOOTED since the
  session's last persisted write (`/proc/stat` btime); closing the app mid-test
  no longer walks good offsets away with a phantom "crash detected".
- Resume CO-drift warning no longer fires on the normal post-reboot state (SMU
  SRAM zeroed): `actual == 0` is expected, only a third value is drift.
- A hard crash at a CONFIRMED/HARDENED value now invalidates the confirmation:
  best_offset is demoted and the core re-enters backoff, so validation can no
  longer re-apply the exact value that crashed the machine (observed live on
  core 1 at -42). A multi-core validation crash penalizes only the most
  aggressive resident offset (matching the soft-fail policy) instead of
  demolishing the whole profile.
- Start-time/environment failures (missing binary, scheduler construction
  error, harness exception) now pause the tuner instead of being recorded as
  core stability failures that advance the search.
- Round-robin orders continue the cycle when the cursor core finishes instead
  of snapping back to core 0 (positional rotation), keeping cool-down fairness.
- Resuming a session mirrors its SAVED config into the Auto-Tuner panel, so
  the boxes show what is actually being executed.
- History tab detail pane: entering the tab auto-selected a session and then
  unconditionally cleared the detail, so it stayed collapsed until a top button
  was clicked; the 50/50 splitter math also no-oped before first layout. The
  detail now expands deterministically and the splitter state cannot
  degenerate.
- Every TunerPhase now has an explicit UI mapping (`gui/phase_style.py`,
  exhaustive at import): hardening/hardened cores no longer render as
  "pending", and abort resets the sidebar to each core's real phase.
- Fresh and migrated databases are now structurally identical (migration v12
  rebuilds `tuner_core_states` to the canonical NOT NULL shape), enforced by a
  fresh-equals-migrated schema test; plus WAL `busy_timeout` for concurrent
  access and an integrity `quick_check` on open that fails closed.

### Fixed (2026-07-09, poisoned-state prevention and exception honesty)

- Apparatus circuit breaker (`apparatus_failure_streak`, default 12): N
  consecutive FAILs on one core is physically implausible — every backoff step
  ADDS voltage and the midpoint jumps converge exponentially — so it means the
  test apparatus is lying, not the silicon (the stale-results.txt class:
  broken backend, corrupt work dir, dying disk). On trip the core rolls back
  to its most aggressive PROVEN pass (passes cannot be faked by a stale error
  file), poisoned backoff bounds are cleared, the core must re-confirm, and
  the tuner pauses loudly naming the suspicion.
- SMU revert failures now fail closed instead of being logged-and-forgotten:
  a failed post-test baseline revert leaves the aggressive offset RESIDENT,
  so the tuner pauses (search, thermal, and resume baseline-restore paths all
  covered) rather than marching on with poisoned hardware state.
- Unreadable `results.txt` is an apparatus fault, not a pass: parse_output
  previously swallowed the OSError and could silently pass a failing run; it
  now returns a "verdict unavailable" failure that the engine classifies as
  environment (pause), never as a stability verdict. Rapid-transition harness
  exceptions are classified the same way instead of as core instability.
- Global exception hooks (`sys.excepthook` + `threading.excepthook`): an
  uncaught exception logs the full traceback, force-stops the tuner (reverting
  CO toward baselines), and shows an error dialog — nothing can die silently
  mid-session anymore. Root logging is now actually configured (INFO to
  stderr; journald captures it), so the engine's root-cause breadcrumbs land
  somewhere instead of nowhere.
- Migration ALTERs no longer suppress all exceptions: the known
  partial-migration case is detected by an explicit column-existence check;
  any other failure raises instead of leaving a half-migrated schema marked
  as migrated. The impossible `create_context` fallback now raises a
  database-inconsistent error instead of returning a bogus id.
- Corrupt `settings.json` is preserved as `settings.json.corrupt` with a
  logged reason instead of being silently replaced by defaults; and
  `TunerConfig.from_json` logs which invalid/unknown fields it dropped.
- No-reboot resume no longer assumes zero baselines are resident: without a
  reboot the SMU still holds whatever was live at app exit (e.g. a mid-test
  offset), so the baseline is now written back explicitly; the reboot verdict
  is computed once and drives the drift check, crash detection, and baseline
  restore consistently. The display preflight respects an explicit
  `QT_QPA_PLATFORM` (offscreen/vnc/linuxfb need no display server).

### Fixed (2026-07-09, adversarial review + exhaustive state sweep)

- Startup/environment failures now revert the never-tested offset, persist
  the cleared in_test flag, and are handled BEFORE the journal is marked
  survived — previously the aggressive offset stayed resident during the
  pause, a later reboot+resume fabricated a crash verdict for a core that
  only had a missing binary, and the untested offset was journaled survived.
- Validation-stage scheduler failures route through the startup path instead
  of being logged as validate FAILs that backed off a healthy core.
- Engine self-pauses (apparatus breaker, SMU faults, startup) now enable the
  Resume button — previously every self-pause was a GUI dead end.
- resume()/validate_profile() refuse while a worker is still in flight —
  resuming under a live stress test rewrote SMU baselines beneath it (false
  PASS at an untested offset) and orphaned the worker thread.
- The apparatus breaker judges the search flow only: validation failures are
  legitimate consecutive backoffs, and isolation passes are not valid
  rollback evidence for the all-offsets-live context.
- Migration v12 runs in one transaction (an interrupted upgrade rolls back
  instead of bricking the DB), and the first sudo run no longer leaves the
  parent state directory root-owned.
- Exhaustive state-machine sweep (docs/tuner-state-spec.md, executed by
  tests/test_state_transition_spec.py: every phase x outcome x offset
  scenario through the real transitions) found and forced two fixes: a PASS
  at/beyond the recorded fail bound inverted the bounds and made the backoff
  binary search DIVERGE toward more aggressive values (failures now outrank
  passes — the search steps back inside the fail bound); and a persisted
  backoff row with best_offset NULL raised TypeError instead of failing
  closed to baseline.
- Evidence reconciliation on resume: a core claiming CONFIRMED/HARDENED at a
  non-baseline best with no logged pass to back it is demoted to re-confirm
  from its most aggressive proven pass (corruption or hand-edits cannot
  masquerade as proven results).
- Core-state sanity guard on the persistence boundary, both directions:
  offsets outside the sane CO range or negative counters raise instead of
  being written or loaded as truth.

### Added (2026-07-09, robustness infrastructure)

- The full unit/property suite now runs inside the nix package build
  (doCheck; offscreen Qt) — CI gates the artifact, not just packaging. The
  e2e subprocess replays stay in the dev loop (marked slow).
- `Path.home()`/`os.path.expanduser` are banned by lint outside
  `config/paths.py` (ruff TID251) — the split-database bug class is now
  unwritable.
- The closed-loop property fuzz additionally injects no-reboot APP EXITS
  (window closed/SIGKILL) at random points, exercising the no-penalty resume
  world alongside power-loss reboots.

### Added (2026-07-09)

- `docs/test-order-spec.md`: control-system spec chart for all five test
  orders (pick rule, cursor state, interruption/resume semantics, global
  invariants), executed by `tests/test_test_order_spec.py`.
- Duplicate-function guard (`tests/test_no_duplicate_functions.py`): identical
  function bodies in `src/` fail the suite; first catch (the `_item` table
  helper, duplicated in two tabs) extracted to `gui.widgets.table_item`.
- ruff enforced in the pre-commit gate (was configured but never wired); repo
  is lint-clean at line-length 120 (matching the codebase's de-facto style).

### Added

- Auto-tuner CO write-ahead journal: every Curve Optimizer write to the SMU is
  durably recorded before the hardware write, so any hard crash (idle, baseline
  restore, post-test revert, validation, or search) is attributable to the exact
  (core, value) that was resident when the machine died — not just to a core that
  happened to be mid-test.
- Resume-crash circuit breaker (`resume_crash_quarantine_threshold`, default 3):
  after that many consecutive crash-resumes with no surviving test in between, the
  tuner forces every core to stock (CO=0), quarantines the session, and reports an
  honest "unsafe" verdict instead of re-applying a profile that keeps crashing.
- Fail-closed thermal protection (`allow_missing_thermal_sensor`, default off):
  the tuner refuses to drive a stress test with no readable CPU temperature sensor
  unless explicitly allowed.
- End-to-end fault-injection test suite (`tests/test_tuner_faults.py`) covering
  unflagged hard crashes, unstable baselines, repeated resume-crashes, SMU write
  faults, the write-ahead journal, fail-closed thermal, and every test-order style.
- PBO power/current limit setters (PPT/TDC/EDC) now range-check their input and
  reject out-of-range values before any SMU write.

### Fixed

- Auto-tuner could enter an infinite resume-crash loop: an unstable baseline (or a
  crash that left no `in_test` flag) was re-applied verbatim on every resume. Crash
  backoff now treats CO=0 (stock) as the only axiomatically safe floor and an
  unstable baseline descends toward 0, so resume can never re-apply the value that
  crashed the machine, and the circuit breaker bounds the loop.
- The resume-crash circuit breaker now also engages during multi-core validation:
  a hard crash there was previously invisible to both crash detectors, so a profile
  that crashes only under combined load could be re-applied into the same crash on
  every resume.
- Per-core Curve Optimizer writes address the correct physical core on harvested and
  multi-CCD parts (5900X, 7900X, 9900X, 5600X, ...) by using the physical,
  gap-preserving core id Linux exposes (the kernel's own APIC-ID decode); the earlier
  SMU slot-probe heuristic was removed as unnecessary on Linux and unreliable
  (it depended on undocumented GET-on-disabled-slot firmware behavior).
- Backend pass/fail parsing treats a crash signal (including SIGILL and SIGFPE) as
  instability even after an earlier "passed" line, so an unstable offset is never
  reported as stable.
- Malformed kernel, sysfs, or config input (a non-decimal L3 cache id, a wrong-typed
  `config_json` field) now fails closed instead of crashing.

[Unreleased]: https://github.com/Daaboulex/linux-corecycler
