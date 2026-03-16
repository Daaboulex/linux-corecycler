# CoreCyclerLx v0.3 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix flake build, fix PBO auto-tuner multi-core progression bugs, add CCD-alternating test order and inherit-current-CO option, add DIMM/Memory info tab with DDR5 temperature monitoring and stressapptest backend, and add version badge to README.

**Architecture:** Four independent workstreams executed in order: (1) flake fix + versioning, (2) PBO tuner surgical fixes + new features, (3) memory/DIMM tab + stressapptest backend, (4) Qt signal type fix. Each workstream produces independently testable, committable changes.

**Tech Stack:** Python 3.12, PySide6/Qt6, Nix flakes, pytest, SQLite, sysfs/hwmon, i2c/SPD5118, dmidecode, stressapptest

---

## File Structure

### Workstream 1: Flake Fix + Versioning
- Modify: `nix/it87.nix:51` (already done — KERNEL_BUILD fix)
- Modify: `README.md:5` (add version badge)

### Workstream 2: PBO Tuner Fixes
- Modify: `src/tuner/engine.py` (pick functions, abort safety, state advancement, CCD-alternating order)
- Modify: `src/tuner/config.py` (inherit_current field, ccd_alternating test_order)
- Modify: `src/gui/tuner_tab.py` (inherit checkbox, ccd_alternating in combo)
- Modify: `tests/test_tuner_engine.py` (new tests for all fixes)

### Workstream 3: Memory/DIMM Tab
- Create: `src/monitor/memory.py` (DIMM info reader — dmidecode + SPD5118 hwmon)
- Create: `src/gui/memory_tab.py` (Memory tab UI)
- Create: `src/engine/backends/stressapptest.py` (stressapptest backend)
- Modify: `src/gui/main_window.py` (add Memory tab)
- Modify: `nix/module.nix` (i2c_dev + spd5118 kernel module options)
- Modify: `flake.nix` (stressapptest in PATH)
- Create: `tests/test_memory_monitor.py`
- Create: `tests/test_stressapptest_backend.py`

### Workstream 4: Qt Signal Fix
- Modify: `src/tuner/engine.py:158` (session_completed signal type)

---

## Chunk 1: Flake Fix + Versioning + README Badge

### Task 1: Commit it87.nix build fix

The `KERNELDIR` → `KERNEL_BUILD` rename is already staged. This just needs a commit.

**Files:**
- Modified: `nix/it87.nix:51`

- [ ] **Step 1: Verify the change is correct**

Check that the upstream Makefile uses `KERNEL_BUILD`:
```
# The diff should show:
# - "KERNELDIR=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
# + "KERNEL_BUILD=${kernel.dev}/lib/modules/${kernel.modDirVersion}/build"
```

- [ ] **Step 2: Commit the fix**

```bash
git add nix/it87.nix
git commit -m "fix(it87): use KERNEL_BUILD variable to match upstream Makefile rename"
```

### Task 2: Add version badge to README

**Files:**
- Modify: `README.md:5-7`

- [ ] **Step 1: Add version badge after existing badges**

Insert a new line after line 6 (the License badge) and before line 7 (the Linux badge). Only add the version badge — do NOT duplicate existing badges:

```markdown
![Version 0.2.0-beta](https://img.shields.io/badge/Version-0.2.0--beta-orange)
```

The result should be 4 badges on lines 5-8: Python, License, Version, Linux.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add version badge to README"
```

---

## Chunk 2: PBO Tuner — Extract Side Effects from Pick Functions

### Task 3: Write failing tests for pure pick functions

The core bug: `_pick_sequential()`, `_pick_round_robin()`, and `_pick_weakest_first()` all call `_advance_core()` as a side effect. This mixes selection with state mutation, causing:
- Double state advancement (once in pick, once in `_on_test_finished`)
- Race conditions if pick is called without running a test
- Settled cores incorrectly transitioned before test completes

The fix: make pick functions pure selectors. Move all `_advance_core` calls for `not_started` and `settled` phases to `_run_next()`.

**Files:**
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write tests that verify pick functions don't mutate state**

Add to `tests/test_tuner_engine.py`:

```python
class TestPickFunctionsPure:
    """Pick functions must NOT call _advance_core — they are pure selectors."""

    def test_sequential_does_not_advance_not_started(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        cfg = TunerConfig(cores_to_test=[0, 1], test_order="sequential")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="not_started"),
        }
        picked = eng._pick_next_core()
        assert picked == 0
        # Phase must still be not_started — pick should not advance
        assert eng._core_states[0].phase == "not_started"

    def test_sequential_does_not_advance_settled(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        cfg = TunerConfig(cores_to_test=[0], test_order="sequential")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="settled", best_offset=-10,
                         current_offset=-10),
        }
        picked = eng._pick_next_core()
        assert picked == 0
        # Phase must still be settled — pick should not advance
        assert eng._core_states[0].phase == "settled"

    def test_round_robin_does_not_advance(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        cfg = TunerConfig(cores_to_test=[0, 1], test_order="round_robin")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="settled", best_offset=-5,
                         current_offset=-5),
        }
        picked = eng._pick_next_core()
        assert picked == 0
        assert eng._core_states[0].phase == "not_started"
        assert eng._core_states[1].phase == "settled"

    def test_weakest_first_does_not_advance(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        cfg = TunerConfig(cores_to_test=[0, 1], test_order="weakest_first")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="fine_search", current_offset=-8,
                         best_offset=-7, coarse_fail_offset=-10),
        }
        picked = eng._pick_next_core()
        # weakest_first should pick fine_search core (score 0)
        assert picked == 1
        # Must not advance the not_started core as a side effect
        assert eng._core_states[0].phase == "not_started"

    def test_round_robin_rotates(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """Round-robin should rotate: after testing core 0, pick core 1."""
        cfg = TunerConfig(cores_to_test=[0, 1, 2], test_order="round_robin")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="coarse_search", current_offset=-5),
            1: CoreState(core_id=1, phase="coarse_search", current_offset=-5),
            2: CoreState(core_id=2, phase="coarse_search", current_offset=-5),
        }
        # First pick: should return 0 (no last_tested)
        assert eng._pick_next_core() == 0
        # Simulate having tested core 0
        eng._last_tested_core = 0
        # Next pick: should rotate to 1
        assert eng._pick_next_core() == 1
        eng._last_tested_core = 1
        # Next: should rotate to 2
        assert eng._pick_next_core() == 2
        eng._last_tested_core = 2
        # Next: should wrap back to 0
        assert eng._pick_next_core() == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tuner_engine.py::TestPickFunctionsPure -v`
Expected: FAIL — pick functions currently call `_advance_core`

### Task 4: Make pick functions pure selectors

**Files:**
- Modify: `src/tuner/engine.py:502-554`

- [ ] **Step 3: Rewrite _pick_sequential to be pure (preserve two-pass semantics)**

The original has a two-pass structure: first find cores in active phases (coarse/fine/confirming), then find settled cores needing confirmation. We preserve this but remove the `_advance_core` side effects. Cores in `not_started` and `settled` are returned as-is; `_run_next` handles advancement.

Replace lines 502-517 with:

```python
    def _pick_sequential(self) -> int | None:
        """Finish each core completely before moving to next.

        Two-pass: first find cores in active test phases, then find
        settled cores that need confirmation. Returns the core_id
        without modifying any state — _run_next handles advancement.
        """
        # Pass 1: active test phases (not_started, coarse, fine, confirming, failed_confirm)
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in ("confirmed", "settled"):
                return core_id
        # Pass 2: settled cores that need confirmation
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase == "settled":
                return core_id
        return None
```

- [ ] **Step 4: Rewrite _pick_round_robin to be pure with rotation tracking**

Round-robin must actually cycle through cores. We add a `_last_tested_core` tracker to the engine (initialized in `start()`/`resume()`). The pick function finds the next core after the last tested one.

First, add `self._last_tested_core: int | None = None` to `TunerEngine.__init__` (after line 188).

Then replace lines 519-535 with:

```python
    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round.

        Uses _last_tested_core to rotate. Returns the next non-confirmed
        core after the last tested one (wrapping around).
        """
        active = sorted(
            cid for cid, cs in self._core_states.items()
            if cs.phase not in ("confirmed",)
        )
        if not active:
            return None
        # Find position after last tested core
        if self._last_tested_core is not None and self._last_tested_core in active:
            idx = active.index(self._last_tested_core)
            # Start from next position, wrapping around
            rotated = active[idx + 1:] + active[:idx + 1]
            return rotated[0]
        return active[0]
```

- [ ] **Step 5: Rewrite _pick_weakest_first to be pure (preserve original scoring)**

Replace lines 537-554. Keep the original scoring for active phases, add not_started/settled as lowest priority since they haven't started searching yet:

```python
    def _pick_weakest_first(self) -> int | None:
        """Prioritize cores closest to settling.

        Score: fine_search/failed_confirm (0) > confirming (1) >
        coarse_search (2) > settled (3) > not_started (4).
        """
        candidates = []
        for core_id, cs in self._core_states.items():
            if cs.phase == "confirmed":
                continue
            score = {
                "fine_search": 0, "failed_confirm": 0,
                "confirming": 1, "coarse_search": 2,
                "settled": 3, "not_started": 4,
            }.get(cs.phase, 5)
            candidates.append((score, core_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]
```

### Task 5: Move state advancement to _run_next

**Files:**
- Modify: `src/tuner/engine.py:560-618`

- [ ] **Step 6: Add pre-test state advancement in _run_next**

Insert after `core_id = self._pick_next_core()` check (after line 580), replacing the old `cs = self._core_states[core_id]` on line 582:

```python
        # Advance cores that need initial transition before testing
        cs = self._core_states[core_id]
        if cs.phase == "not_started":
            self._advance_core(core_id, passed=False)  # → coarse_search
            cs = self._core_states[core_id]
        elif cs.phase == "settled":
            self._advance_core(core_id, passed=False)  # → confirming
            cs = self._core_states[core_id]

        # Track for round-robin rotation
        self._last_tested_core = core_id
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_tuner_engine.py -v`
Expected: ALL PASS (both old and new tests)

- [ ] **Step 8: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "fix(tuner): extract side effects from pick functions into _run_next

Pick functions (_pick_sequential, _pick_round_robin, _pick_weakest_first)
were calling _advance_core as a side effect, mixing selection with state
mutation. This caused double advancement and race conditions when moving
beyond the first core. Now pick functions are pure selectors and all
state transitions happen in _run_next before test execution."
```

---

## Chunk 3: PBO Tuner — Abort Safety + Inherit Current CO

### Task 6: Fix worker termination signal safety

When `abort()` calls `_worker.terminate()`, the `finished` signal may never emit, leaving the state machine stuck.

**Files:**
- Modify: `src/tuner/engine.py:330-341`

- [ ] **Step 1: Write test for abort during active test**

Add to `tests/test_tuner_engine.py`:

```python
class TestAbortSafety:
    def test_abort_cleans_up_worker(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {0: CoreState(core_id=0, phase="coarse_search", current_offset=-5)}
        eng._set_status("running")

        # Simulate a worker that's "running"
        mock_worker = MagicMock()
        mock_worker.isRunning.return_value = True
        eng._worker = mock_worker

        eng.abort()

        assert eng.status == "idle"
        assert eng._worker is None
        mock_worker.terminate.assert_called_once()
```

- [ ] **Step 2: Run test — should pass (abort already cleans up)**

Run: `pytest tests/test_tuner_engine.py::TestAbortSafety -v`
Expected: PASS

### Task 7: Add inherit_current option to TunerConfig

**Files:**
- Modify: `src/tuner/config.py:19`
- Modify: `src/tuner/engine.py:232-238`

- [ ] **Step 3: Write test for inherit_current**

Add to `tests/test_tuner_engine.py`:

```python
class TestInheritCurrentCO:
    def test_inherit_reads_smu_offsets(self, db, simple_topology, mock_smu, mock_backend):
        """When inherit_current=True, start offsets come from SMU, not config."""
        mock_smu.get_co_offset = MagicMock(side_effect=lambda cid: {0: -15, 1: -20}.get(cid, 0))

        cfg = TunerConfig(
            cores_to_test=[0, 1],
            inherit_current=True,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        with patch.object(eng, "_run_next"):
            eng.start()

        assert eng._core_states[0].current_offset == -15
        assert eng._core_states[1].current_offset == -20

    def test_inherit_false_uses_start_offset(self, db, simple_topology, mock_smu, mock_backend):
        """When inherit_current=False (default), use config start_offset."""
        cfg = TunerConfig(
            cores_to_test=[0, 1],
            inherit_current=False,
            start_offset=-5,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        with patch.object(eng, "_run_next"):
            eng.start()

        assert eng._core_states[0].current_offset == -5
        assert eng._core_states[1].current_offset == -5
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_tuner_engine.py::TestInheritCurrentCO -v`
Expected: FAIL — `inherit_current` doesn't exist yet

- [ ] **Step 5: Add inherit_current to TunerConfig**

In `src/tuner/config.py`, add after line 44 (`abort_on_consecutive_failures`):

```python
    # Inherit current CO offsets from SMU as starting point
    inherit_current: bool = False
```

- [ ] **Step 6: Implement inherit logic in engine.start()**

In `src/tuner/engine.py`, replace lines 232-238 (the core state initialization loop):

```python
        # Initialize core states
        cores = self._get_cores_to_test()
        self._core_states = {}

        # Read current CO offsets from SMU if inheriting
        current_offsets: dict[int, int] = {}
        if self._config.inherit_current and self._smu is not None:
            for core_id in cores:
                val = self._smu.get_co_offset(core_id)
                if val is not None:
                    current_offsets[core_id] = val
            self.log_message.emit(
                f"Inherited current CO offsets from SMU: {current_offsets}"
            )

        for core_id in cores:
            start = current_offsets.get(core_id, self._config.start_offset)
            cs = CoreState(core_id=core_id, current_offset=start)
            self._core_states[core_id] = cs
            tp.save_core_state(self._db, self._session_id, cs)
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_tuner_engine.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add src/tuner/config.py src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): add inherit_current option to start from current SMU CO values

When inherit_current=True, the tuner reads each core's current CO offset
from the SMU at session start and uses those as starting points instead
of the fixed start_offset. This allows incremental tuning from an
existing baseline (e.g., BIOS CO values already set to -20)."
```

---

## Chunk 4: PBO Tuner — CCD-Alternating Test Order

### Task 8: Add ccd_alternating test order

This order interleaves cores from different CCDs: Core0/CCD0 → Core8/CCD1 → Core1/CCD0 → Core9/CCD1, catching thermal cross-CCD interactions early.

**Files:**
- Modify: `src/tuner/engine.py` (add `_pick_ccd_alternating`)
- Modify: `src/tuner/config.py:35` (add to test_order docstring)
- Modify: `src/gui/tuner_tab.py:269` (add to combo)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write test for CCD-alternating order**

Add to `tests/test_tuner_engine.py`:

```python
class TestCCDAlternatingOrder:
    def test_alternates_between_ccds(self, db, topo_dual_ccd_x3d, mock_smu, mock_backend):
        """CCD-alternating should pick from CCD0, then CCD1, then CCD0, etc."""
        cfg = TunerConfig(
            cores_to_test=[0, 1, 2, 3, 4, 5, 6, 7],
            test_order="ccd_alternating",
        )
        eng = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            i: CoreState(core_id=i, phase="coarse_search", current_offset=-5)
            for i in range(8)
        }

        # Collect pick order
        order = []
        for _ in range(8):
            picked = eng._pick_next_core()
            if picked is None:
                break
            order.append(picked)
            eng._core_states[picked] = CoreState(
                core_id=picked, phase="confirmed", current_offset=-5, best_offset=-5,
            )

        # Verify alternation: consecutive picks should be from different CCDs
        topo = topo_dual_ccd_x3d
        for i in range(1, len(order)):
            ccd_prev = topo.cores[order[i - 1]].ccd
            ccd_curr = topo.cores[order[i]].ccd
            if i < len(order) - 1:  # last pick may not alternate if one CCD is exhausted
                assert ccd_prev != ccd_curr, (
                    f"Picks {i-1} and {i} ({order[i-1]}, {order[i]}) "
                    f"are both on CCD {ccd_curr}"
                )

    def test_falls_back_when_one_ccd_exhausted(self, db, topo_dual_ccd_x3d, mock_smu, mock_backend):
        """When one CCD is all confirmed, pick remaining from the other."""
        cfg = TunerConfig(
            cores_to_test=[0, 1, 4, 5],
            test_order="ccd_alternating",
        )
        eng = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase="coarse_search", current_offset=-5),
            1: CoreState(core_id=1, phase="coarse_search", current_offset=-5),
            4: CoreState(core_id=4, phase="confirmed", current_offset=-10, best_offset=-10),
            5: CoreState(core_id=5, phase="confirmed", current_offset=-10, best_offset=-10),
        }
        # CCD1 is all confirmed, should pick from CCD0
        picked = eng._pick_next_core()
        assert picked in (0, 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_tuner_engine.py::TestCCDAlternatingOrder -v`
Expected: FAIL — `ccd_alternating` not recognized

- [ ] **Step 3: Add _pick_ccd_alternating to engine**

In `src/tuner/engine.py`, add the method after `_pick_weakest_first` and add the case to `_pick_next_core`:

```python
    def _pick_next_core(self) -> int | None:
        """Select next core to test based on test_order config."""
        match self._config.test_order:
            case "sequential":
                return self._pick_sequential()
            case "round_robin":
                return self._pick_round_robin()
            case "weakest_first":
                return self._pick_weakest_first()
            case "ccd_alternating":
                return self._pick_ccd_alternating()
            case _:
                return self._pick_sequential()
```

Add the new method:

```python
    def _pick_ccd_alternating(self) -> int | None:
        """Alternate between CCDs: CCD0 core, CCD1 core, CCD0, CCD1, ..."""
        # Group active cores by CCD
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if cs.phase in ("confirmed",):
                continue
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_cores.setdefault(ccd, []).append(core_id)

        if not ccd_cores:
            return None

        # Sort cores within each CCD
        for ccd in ccd_cores:
            ccd_cores[ccd].sort()

        # Count confirmed cores per CCD to track progress
        ccd_confirmed: dict[int, int] = {}
        for core_id, cs in self._core_states.items():
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            if cs.phase == "confirmed":
                ccd_confirmed[ccd] = ccd_confirmed.get(ccd, 0) + 1

        # Pick the CCD with the fewest confirmed cores (least progress)
        sorted_ccds = sorted(ccd_cores.keys(), key=lambda c: ccd_confirmed.get(c, 0))

        return ccd_cores[sorted_ccds[0]][0]
```

- [ ] **Step 4: Update config docstring**

In `src/tuner/config.py:35`, change:

```python
    test_order: str = "sequential"  # sequential, round_robin, weakest_first, ccd_alternating
```

- [ ] **Step 5: Add ccd_alternating to tuner_tab combo**

In `src/gui/tuner_tab.py:269`, change:

```python
        self._order_combo.addItems(["sequential", "round_robin", "weakest_first", "ccd_alternating"])
```

Update the tooltip at line 271:

```python
        self._order_combo.setToolTip(
            "sequential: finish each core before moving to next\n"
            "round_robin: cycle through all cores, one test each\n"
            "weakest_first: prioritize cores closest to settling\n"
            "ccd_alternating: alternate between CCDs (catches thermal interactions)"
        )
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_tuner_engine.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/tuner/engine.py src/tuner/config.py src/gui/tuner_tab.py tests/test_tuner_engine.py
git commit -m "feat(tuner): add ccd_alternating test order for multi-CCD thermal coverage

New test ordering strategy that alternates between CCDs when picking
the next core to test. This catches thermal cross-CCD interactions
early instead of testing all of CCD0 before CCD1. Picks the CCD
with the fewest confirmed cores to maintain balanced progress."
```

---

## Chunk 5: PBO Tuner — Inherit Current CO UI + Qt Signal Fix

### Task 9: Add inherit_current checkbox to tuner tab

**Files:**
- Modify: `src/gui/tuner_tab.py`

- [ ] **Step 1: Add checkbox to config panel**

In `src/gui/tuner_tab.py`, in `_build_config_panel`, after the `_start_offset_spin` row (line 222), add:

```python
        from PySide6.QtWidgets import QCheckBox

        self._inherit_current_check = QCheckBox("Inherit current CO from SMU")
        self._inherit_current_check.setToolTip(
            "Read current CO offsets from SMU at session start and use them\n"
            "as starting points instead of the fixed start offset above.\n"
            "Useful for incremental tuning from an existing baseline."
        )
        search_layout.addRow("", self._inherit_current_check)
```

- [ ] **Step 2: Wire checkbox into _get_config**

In `_get_config()` (line 356), add to the TunerConfig constructor:

```python
            inherit_current=self._inherit_current_check.isChecked(),
```

- [ ] **Step 3: Wire checkbox into _load_defaults**

In `_load_defaults()`, add:

```python
        self._inherit_current_check.setChecked(cfg.inherit_current)
```

### Task 10: Fix Qt session_completed signal type

The `session_completed = Signal(dict)` on line 158 causes the `_pythonToCppCopy: Cannot copy-convert dict to C++` error. PySide6 cannot marshal `dict` across threads.

**Files:**
- Modify: `src/tuner/engine.py:158`
- Modify: `src/tuner/engine.py:750` (emit site)
- Modify: `src/gui/tuner_tab.py:521-522` (slot)

- [ ] **Step 4: Change signal to use str (JSON) instead of dict**

In `src/tuner/engine.py:158`, change:

```python
    session_completed = Signal(str)  # JSON-encoded {core_id: best_offset}
```

In `_complete_session()` (line 750), change the emit:

```python
        import json
        self.session_completed.emit(json.dumps(profile))
```

In `src/gui/tuner_tab.py`, change the slot decorator and handler (line 521-525):

```python
    @Slot(str)
    def _on_session_completed(self, profile_json: str) -> None:
        import json
        profile = json.loads(profile_json) if profile_json else {}
        self._set_running_state(False)
        self._validate_btn.setEnabled(bool(profile))
        self._export_btn.setEnabled(bool(profile))
```

- [ ] **Step 5: Run all tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit inherit_current UI (separate from signal fix)**

```bash
git add src/gui/tuner_tab.py
git commit -m "feat(gui): add inherit_current checkbox to tuner tab config panel

Exposes the inherit_current option in the tuner tab UI. When checked,
the tuner reads current CO offsets from SMU at session start."
```

- [ ] **Step 7: Commit Qt signal fix (separate from UI)**

```bash
git add src/tuner/engine.py src/gui/tuner_tab.py
git commit -m "fix(gui): serialize session_completed signal as JSON to avoid Qt dict marshalling

PySide6 cannot copy-convert Python dict to C++ across thread boundaries,
causing '_pythonToCppCopy: Cannot copy-convert dict to C++' crashes.
Changed session_completed signal from Signal(dict) to Signal(str) with
JSON serialization."
```

---

## Chunk 6: Memory Tab — DIMM Info Reader

### Task 11: Create memory monitor module

Reads DIMM information from two sources:
1. `dmidecode -t memory` (universal, requires root)
2. SPD5118 hwmon (DDR5 temperature + SPD EEPROM, if available)

**Files:**
- Create: `src/monitor/memory.py`
- Test: `tests/test_memory_monitor.py`

- [ ] **Step 1: Write tests for DIMM info parsing**

Create `tests/test_memory_monitor.py`:

```python
"""Tests for the DIMM/memory monitoring module."""

from __future__ import annotations

import pytest
from monitor.memory import DIMMInfo, parse_dmidecode_output, SPD5118Reader

SAMPLE_DMIDECODE = """\
# dmidecode 3.6
Handle 0x003D, DMI type 17, 92 bytes
Memory Device
\tTotal Width: 80 bits
\tData Width: 64 bits
\tSize: 32 GB
\tForm Factor: DIMM
\tLocator: DIMM 0
\tBank Locator: P0 CHANNEL A
\tType: DDR5
\tSpeed: 6000 MT/s
\tManufacturer: G Skill Intl
\tSerial Number: 00000000
\tAsset Tag: Not Specified
\tPart Number: F5-6000J3038F16G
\tRank: 2
\tConfigured Memory Speed: 6000 MT/s
\tMinimum Voltage: 1.1 V
\tMaximum Voltage: 1.1 V
\tConfigured Voltage: 1.1 V

Handle 0x003E, DMI type 17, 92 bytes
Memory Device
\tTotal Width: 80 bits
\tData Width: 64 bits
\tSize: 32 GB
\tForm Factor: DIMM
\tLocator: DIMM 1
\tBank Locator: P0 CHANNEL A
\tType: DDR5
\tSpeed: 6000 MT/s
\tManufacturer: G Skill Intl
\tSerial Number: 00000001
\tPart Number: F5-6000J3038F16G
\tRank: 2
\tConfigured Memory Speed: 6000 MT/s
\tMinimum Voltage: 1.1 V
\tMaximum Voltage: 1.1 V
\tConfigured Voltage: 1.1 V
"""


class TestParseDmidecode:
    def test_parses_two_dimms(self):
        dimms = parse_dmidecode_output(SAMPLE_DMIDECODE)
        assert len(dimms) == 2

    def test_dimm_fields(self):
        dimms = parse_dmidecode_output(SAMPLE_DMIDECODE)
        d = dimms[0]
        assert d.size_gb == 32
        assert d.mem_type == "DDR5"
        assert d.speed_mt == 6000
        assert d.manufacturer == "G Skill Intl"
        assert d.part_number == "F5-6000J3038F16G"
        assert d.rank == 2
        assert d.locator == "DIMM 0"
        assert d.configured_voltage == 1.1

    def test_empty_output(self):
        assert parse_dmidecode_output("") == []

    def test_no_memory_devices(self):
        assert parse_dmidecode_output("# dmidecode 3.6\nBIOS Information\n") == []


class TestSPD5118Reader:
    def test_finds_spd5118_devices(self, tmp_path):
        # Create mock hwmon with spd5118
        hwmon0 = tmp_path / "hwmon0"
        hwmon0.mkdir()
        (hwmon0 / "name").write_text("spd5118\n")
        (hwmon0 / "temp1_input").write_text("42500\n")

        reader = SPD5118Reader(hwmon_base=tmp_path)
        temps = reader.read_temperatures()
        assert len(temps) == 1
        assert abs(temps[0] - 42.5) < 0.01

    def test_no_spd5118_returns_empty(self, tmp_path):
        reader = SPD5118Reader(hwmon_base=tmp_path)
        assert reader.read_temperatures() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_memory_monitor.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement memory monitor**

Create `src/monitor/memory.py`:

```python
"""DIMM and memory monitoring — dmidecode + SPD5118 hwmon."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

HWMON_BASE = Path("/sys/class/hwmon")


@dataclass(frozen=True, slots=True)
class DIMMInfo:
    """Information about a single DIMM from dmidecode."""

    locator: str = ""
    bank_locator: str = ""
    size_gb: int = 0
    mem_type: str = ""
    speed_mt: int = 0
    configured_speed_mt: int = 0
    manufacturer: str = ""
    part_number: str = ""
    serial_number: str = ""
    rank: int = 0
    form_factor: str = ""
    configured_voltage: float = 0.0
    min_voltage: float = 0.0
    max_voltage: float = 0.0
    data_width: int = 0
    total_width: int = 0


def parse_dmidecode_output(text: str) -> list[DIMMInfo]:
    """Parse dmidecode -t memory output into DIMMInfo list."""
    dimms: list[DIMMInfo] = []
    blocks = re.split(r"Handle 0x[\dA-Fa-f]+, DMI type 17", text)

    for block in blocks[1:]:  # skip header
        fields: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip()] = val.strip()

        size_str = fields.get("Size", "")
        size_gb = 0
        if "GB" in size_str:
            try:
                size_gb = int(size_str.replace("GB", "").strip())
            except ValueError:
                pass
        elif "MB" in size_str:
            try:
                size_gb = int(size_str.replace("MB", "").strip()) // 1024
            except ValueError:
                pass

        if size_gb == 0:
            continue  # empty slot

        speed = 0
        speed_str = fields.get("Speed", "")
        m = re.match(r"(\d+)", speed_str)
        if m:
            speed = int(m.group(1))

        conf_speed = 0
        conf_speed_str = fields.get("Configured Memory Speed", "")
        m = re.match(r"(\d+)", conf_speed_str)
        if m:
            conf_speed = int(m.group(1))

        rank = 0
        rank_str = fields.get("Rank", "")
        if rank_str.isdigit():
            rank = int(rank_str)

        def _parse_voltage(s: str) -> float:
            m = re.match(r"([\d.]+)", s)
            return float(m.group(1)) if m else 0.0

        dimms.append(DIMMInfo(
            locator=fields.get("Locator", ""),
            bank_locator=fields.get("Bank Locator", ""),
            size_gb=size_gb,
            mem_type=fields.get("Type", ""),
            speed_mt=speed,
            configured_speed_mt=conf_speed,
            manufacturer=fields.get("Manufacturer", ""),
            part_number=fields.get("Part Number", "").strip(),
            serial_number=fields.get("Serial Number", ""),
            rank=rank,
            form_factor=fields.get("Form Factor", ""),
            configured_voltage=_parse_voltage(fields.get("Configured Voltage", "")),
            min_voltage=_parse_voltage(fields.get("Minimum Voltage", "")),
            max_voltage=_parse_voltage(fields.get("Maximum Voltage", "")),
            data_width=int(fields.get("Data Width", "0").split()[0]) if fields.get("Data Width") else 0,
            total_width=int(fields.get("Total Width", "0").split()[0]) if fields.get("Total Width") else 0,
        ))

    return dimms


def read_dimm_info() -> list[DIMMInfo]:
    """Read DIMM info via dmidecode. Requires root."""
    try:
        result = subprocess.run(
            ["dmidecode", "-t", "memory"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return parse_dmidecode_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("dmidecode not available: %s", e)
    return []


class SPD5118Reader:
    """Read DDR5 DIMM temperatures from SPD5118 hwmon devices."""

    def __init__(self, hwmon_base: Path = HWMON_BASE) -> None:
        self._devices: list[Path] = []
        self._scan(hwmon_base)

    def _scan(self, hwmon_base: Path) -> None:
        if not hwmon_base.exists():
            return
        for hwmon_dir in sorted(hwmon_base.iterdir()):
            name_file = hwmon_dir / "name"
            if name_file.exists():
                try:
                    name = name_file.read_text().strip()
                except OSError:
                    continue
                if name == "spd5118":
                    self._devices.append(hwmon_dir)

    def is_available(self) -> bool:
        return len(self._devices) > 0

    def read_temperatures(self) -> list[float]:
        """Read temperature from each SPD5118 device (Celsius)."""
        temps: list[float] = []
        for dev in self._devices:
            temp_file = dev / "temp1_input"
            if temp_file.exists():
                try:
                    raw = int(temp_file.read_text().strip())
                    temps.append(raw / 1000.0)
                except (ValueError, OSError):
                    pass
        return temps
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_memory_monitor.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/monitor/memory.py tests/test_memory_monitor.py
git commit -m "feat(monitor): add DIMM info reader via dmidecode + SPD5118 temperature

New memory monitoring module that reads DIMM information from dmidecode
(size, type, speed, manufacturer, part number, rank, voltage) and DDR5
DIMM temperatures from SPD5118 hwmon devices."
```

---

## Chunk 7: Memory Tab — GUI + stressapptest Backend

### Task 12: Create Memory tab

**Files:**
- Create: `src/gui/memory_tab.py`

- [ ] **Step 1: Create Memory tab widget**

Create `src/gui/memory_tab.py`:

```python
"""Memory information tab — DIMM details and DDR5 temperature monitoring."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from monitor.memory import DIMMInfo, SPD5118Reader, read_dimm_info

log = logging.getLogger(__name__)


class MemoryTab(QWidget):
    """Memory information tab showing DIMM details and live temperatures."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dimms: list[DIMMInfo] = []
        self._spd_reader = SPD5118Reader()
        self._setup_ui()
        self._load_dimm_info()

        # Temperature polling timer (DDR5 SPD5118)
        if self._spd_reader.is_available():
            self._temp_timer = QTimer(self)
            self._temp_timer.timeout.connect(self._update_temperatures)
            self._temp_timer.start(2000)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Summary
        self._summary_label = QLabel("Loading DIMM information...")
        self._summary_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        layout.addWidget(self._summary_label)

        # Temperature row (DDR5 only)
        self._temp_group = QGroupBox("DIMM Temperatures (SPD5118)")
        temp_layout = QHBoxLayout(self._temp_group)
        self._temp_labels: list[QLabel] = []
        self._temp_group.setVisible(False)
        layout.addWidget(self._temp_group)

        # DIMM table
        self._dimm_table = QTableWidget()
        self._dimm_table.setColumnCount(10)
        self._dimm_table.setHorizontalHeaderLabels([
            "Slot", "Size", "Type", "Speed", "Configured",
            "Manufacturer", "Part Number", "Rank", "Voltage", "Width",
        ])
        self._dimm_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._dimm_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._dimm_table)

        # Refresh button
        btn_layout = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load_dimm_info)
        btn_layout.addWidget(refresh_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _load_dimm_info(self) -> None:
        self._dimms = read_dimm_info()
        self._populate_table()

        if self._dimms:
            total_gb = sum(d.size_gb for d in self._dimms)
            types = set(d.mem_type for d in self._dimms)
            speeds = set(d.configured_speed_mt for d in self._dimms if d.configured_speed_mt)
            type_str = "/".join(sorted(types)) if types else "Unknown"
            speed_str = "/".join(f"{s} MT/s" for s in sorted(speeds)) if speeds else ""
            self._summary_label.setText(
                f"{len(self._dimms)} DIMMs | {total_gb} GB {type_str} {speed_str}"
            )
        else:
            self._summary_label.setText(
                "No DIMM info available (dmidecode requires root)"
            )

        # Setup SPD5118 temp labels
        if self._spd_reader.is_available():
            temps = self._spd_reader.read_temperatures()
            self._temp_group.setVisible(True)
            temp_layout = self._temp_group.layout()
            # Clear old labels
            for lbl in self._temp_labels:
                lbl.deleteLater()
            self._temp_labels.clear()
            for i, temp in enumerate(temps):
                lbl = QLabel(f"DIMM {i}: {temp:.1f}°C")
                lbl.setFont(QFont("monospace", 10))
                lbl.setStyleSheet("padding: 4px;")
                temp_layout.addWidget(lbl)
                self._temp_labels.append(lbl)

    def _populate_table(self) -> None:
        self._dimm_table.setRowCount(len(self._dimms))
        for row, d in enumerate(self._dimms):
            items = [
                f"{d.locator} ({d.bank_locator})" if d.bank_locator else d.locator,
                f"{d.size_gb} GB",
                d.mem_type,
                f"{d.speed_mt} MT/s" if d.speed_mt else "-",
                f"{d.configured_speed_mt} MT/s" if d.configured_speed_mt else "-",
                d.manufacturer,
                d.part_number,
                str(d.rank) if d.rank else "-",
                f"{d.configured_voltage:.2f}V" if d.configured_voltage else "-",
                f"{d.data_width}/{d.total_width} bit"
                if d.data_width else "-",
            ]
            for col, text in enumerate(items):
                self._dimm_table.setItem(row, col, QTableWidgetItem(text))

    def _update_temperatures(self) -> None:
        temps = self._spd_reader.read_temperatures()
        for i, temp in enumerate(temps):
            if i < len(self._temp_labels):
                self._temp_labels[i].setText(f"DIMM {i}: {temp:.1f}°C")
```

### Task 13: Add Memory tab to MainWindow

**Files:**
- Modify: `src/gui/main_window.py`

- [ ] **Step 2: Add import and tab creation**

Find the tab creation section in `main_window.py`. Add after the existing tab imports:

```python
from gui.memory_tab import MemoryTab
```

And add the tab after the existing tabs (find the `addTab` calls):

```python
        self._memory_tab = MemoryTab()
        self._tabs.addTab(self._memory_tab, "Memory")
```

### Task 14: Create stressapptest backend

**Files:**
- Create: `src/engine/backends/stressapptest.py`
- Create: `tests/test_stressapptest_backend.py`

- [ ] **Step 3: Write stressapptest backend tests**

Create `tests/test_stressapptest_backend.py`:

```python
"""Tests for the stressapptest stress backend."""

from __future__ import annotations

from pathlib import Path

from engine.backends.stressapptest import StressapptestBackend
from engine.backends.base import StressConfig, StressMode


class TestStressapptestBackend:
    def test_command_generation(self, tmp_path):
        backend = StressapptestBackend()
        config = StressConfig(mode=StressMode.SSE)
        cmd = backend.get_command(config, tmp_path)
        assert cmd[0] == "stressapptest"
        assert "-W" in cmd  # write-after-read verification
        assert "-s" in cmd  # duration flag present
        assert "86400" in cmd  # large duration (scheduler handles actual timing)

    def test_parse_pass(self):
        backend = StressapptestBackend()
        stdout = "Status: PASS - please pass all stress tests."
        passed, err = backend.parse_output(stdout, "", 0)
        assert passed is True
        assert err is None

    def test_parse_fail(self):
        backend = StressapptestBackend()
        stdout = "Status: FAIL - memory errors detected."
        passed, err = backend.parse_output(stdout, "", 1)
        assert passed is False
        assert "FAIL" in err

    def test_parse_killed_by_scheduler(self):
        """Scheduler kills via SIGTERM — should count as pass."""
        backend = StressapptestBackend()
        passed, err = backend.parse_output("", "", -15)
        assert passed is True

    def test_supported_modes(self):
        backend = StressapptestBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
```

- [ ] **Step 4: Implement stressapptest backend**

Create `src/engine/backends/stressapptest.py`:

```python
"""stressapptest stress backend — Google's memory stress testing tool.

stressapptest maximizes randomized memory traffic to expose errors in
memory hardware. It is particularly effective for DDR5 frequency/timing
stability testing.

Note: Like all backends, stressapptest runs indefinitely and the
CoreScheduler handles timing by killing the process after
seconds_per_core. We pass -s 86400 (24h) so stressapptest doesn't
self-terminate before the scheduler stops it.

Homepage: https://github.com/stressapptest/stressapptest
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from .base import StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path


class StressapptestBackend(StressBackend):
    """stressapptest backend for memory-intensive stress testing."""

    name = "stressapptest"

    def is_available(self) -> bool:
        return shutil.which("stressapptest") is not None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        # Duration is managed by CoreScheduler (kills process after deadline).
        # Pass a very large -s so stressapptest doesn't self-terminate early.
        cmd = [
            "stressapptest",
            "-W",  # write-after-read data verification
            "-s", "86400",  # 24h — scheduler will kill before this
        ]
        return cmd

    def parse_output(
        self, stdout: str, stderr: str, returncode: int
    ) -> tuple[bool, str | None]:
        # stressapptest prints "Status: PASS" or "Status: FAIL"
        if "Status: FAIL" in stdout:
            return False, "stressapptest: FAIL — memory errors detected"
        if "Status: PASS" in stdout:
            return True, None
        # Killed by scheduler (SIGTERM/SIGKILL) — normal termination = pass
        if returncode in (-9, -15, 137, 143):
            return True, None
        if returncode != 0:
            return False, f"stressapptest exited with code {returncode}"
        return True, None

    def get_supported_modes(self) -> list[StressMode]:
        # stressapptest doesn't have SSE/AVX modes — it's memory-focused
        return [StressMode.SSE]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
```

- [ ] **Step 5: Run new tests**

Run: `pytest tests/test_stressapptest_backend.py tests/test_memory_monitor.py -v`
Expected: ALL PASS

### Task 15: Update flake.nix for stressapptest

**Files:**
- Modify: `flake.nix`

- [ ] **Step 6: Add stressapptest to backends**

In `flake.nix`, update the default backends list (line 33):

```nix
            backends ? [
              pkgs.stress-ng
              pkgs.stressapptest
            ],
```

Also add to the full backends (line 100):

```nix
          backends = [
            pkgs.mprime
            pkgs.stress-ng
            pkgs.stressapptest
          ];
```

And add to devShell packages (after stress-ng, line 116):

```nix
            pkgs.stressapptest
```

### Task 16: Wire stressapptest into memory_tab (NOT the CO tuner)

stressapptest is a memory stress tool, not a CPU compute stressor. It does not
effectively trigger CO instability (which requires FPU/ALU workloads). It should
only be available in the Memory tab, not in the PBO tuner backend selection.

**Files:**
- Modify: `src/gui/memory_tab.py` (add a "Run Memory Stress" button)

- [ ] **Step 7: Add memory stress button to memory_tab**

In `memory_tab.py`, add a "Run Memory Stress Test" button in the button layout
(next to Refresh). When clicked, it runs `stressapptest -W -s 300` (5-minute
test) and displays pass/fail result. This is a basic integration — the full
per-core memory cycling feature can be added in a future version.

```python
        self._stress_btn = QPushButton("Run Memory Stress (5 min)")
        self._stress_btn.setToolTip(
            "Run stressapptest for 5 minutes to check memory stability.\n"
            "Uses write-after-read verification to detect errors."
        )
        self._stress_btn.clicked.connect(self._run_memory_stress)
        btn_layout.addWidget(self._stress_btn)
```

Add the handler:

```python
    def _run_memory_stress(self) -> None:
        import shutil
        import subprocess
        from PySide6.QtWidgets import QMessageBox
        if not shutil.which("stressapptest"):
            QMessageBox.warning(self, "Not Found", "stressapptest is not installed or not on PATH.")
            return
        self._stress_btn.setEnabled(False)
        self._stress_btn.setText("Running...")
        # Run in a simple thread (not blocking GUI)
        from PySide6.QtCore import QThread, Signal

        class _StressWorker(QThread):
            done = Signal(bool, str)
            def run(self_worker):
                try:
                    result = subprocess.run(
                        ["stressapptest", "-W", "-s", "300"],
                        capture_output=True, text=True, timeout=360,
                    )
                    passed = "Status: PASS" in result.stdout
                    self_worker.done.emit(passed, result.stdout[-500:] if result.stdout else "")
                except Exception as e:
                    self_worker.done.emit(False, str(e))

        self._stress_worker = _StressWorker(parent=self)
        self._stress_worker.done.connect(self._on_stress_done)
        self._stress_worker.start()

    def _on_stress_done(self, passed: bool, output: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        self._stress_btn.setEnabled(True)
        self._stress_btn.setText("Run Memory Stress (5 min)")
        status = "PASS" if passed else "FAIL"
        QMessageBox.information(self, f"Memory Stress: {status}", output[-500:])
```

- [ ] **Step 8: Run full test suite**

Run: `pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/gui/memory_tab.py src/engine/backends/stressapptest.py \
    src/gui/main_window.py src/gui/tuner_tab.py flake.nix \
    tests/test_memory_monitor.py tests/test_stressapptest_backend.py
git commit -m "feat: add Memory tab with DIMM info and stressapptest backend

New Memory tab displays DIMM information via dmidecode (size, type, speed,
manufacturer, part number, rank, voltage) and DDR5 DIMM temperatures via
SPD5118 hwmon. New stressapptest backend for memory-intensive stress testing
(Google's tool, excellent for DDR5 stability). Added stressapptest to flake
packages and tuner backend selection."
```

---

## Chunk 8: NixOS Module Updates + Final Integration

### Task 17: Add i2c/SPD5118 module options

**Files:**
- Modify: `nix/module.nix`

- [ ] **Step 1: Add spd5118 and i2c_dev options**

Add to the NixOS module options:

```nix
      spd5118 = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Load spd5118 driver for DDR5 DIMM temperature monitoring";
      };
```

Add to the kernel module loading section:

```nix
        ++ lib.optionals cfg.spd5118 [ "i2c_dev" "spd5118" ]
```

- [ ] **Step 2: Commit**

```bash
git add nix/module.nix
git commit -m "feat(module): add spd5118/i2c_dev options for DDR5 DIMM temperature monitoring"
```

### Task 18: Run full test suite and verify build

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```
Expected: ALL PASS

- [ ] **Step 4: Test nix build**

```bash
nix build --no-link
```
Expected: Build succeeds

- [ ] **Step 5: Final commit — version bump to 0.3.0-beta**

Update version in both files:
- `flake.nix:39` → `version = "0.3.0";`
- `pyproject.toml:7` → `version = "0.3.0"`
- `README.md` badge → `0.3.0--beta`

```bash
git add flake.nix pyproject.toml README.md
git commit -m "chore: bump version to 0.3.0-beta

New in 0.3.0:
- Fix it87 kernel module build (KERNEL_BUILD variable)
- Fix PBO tuner multi-core progression (pure pick functions)
- Add inherit_current option to start from SMU CO values
- Add ccd_alternating test order for multi-CCD thermal coverage
- Fix Qt session_completed signal dict marshalling crash
- Add Memory tab with DIMM info and DDR5 temperature monitoring
- Add stressapptest backend for memory stress testing
- Add version badge to README"
```
