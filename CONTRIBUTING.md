# Contributing to CoreCycler

## Quick Start

### NixOS (recommended)

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
nix develop                  # ruff, nixfmt, pre-commit hooks
ruff check src/              # the Python lint gate

# The suite runs in the package build's own environment (the dev shell has no Python):
nix develop '.#packages.x86_64-linux.default' --command python -m pytest -m 'not slow'
```

### Other Distros

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
python3 -m venv .venv && source .venv/bin/activate   # PEP 668: system pip is managed
pip install -e ".[dev]"   # installs pytest, ruff, hypothesis
pip install PySide6       # Qt6 bindings (required for tuner engine tests)
pytest tests/ -v
```

## Workflow

1. Fork the repo and create a feature branch (`feat/my-feature` or `fix/my-bug`)
2. Make your changes
3. Run the full test suite (see [Testing](#testing)) -- coverage must stay at 100%
4. Run linting: `ruff check src/`
5. Run flake check: `nix flake check` (needs Nix)
6. Submit a PR against `main`

## Testing

### Running Tests

Prefix each with `nix develop '.#packages.x86_64-linux.default' --command` on NixOS.

```bash
# Everything the gate runs
python -m pytest -m "not slow" --cov=corecycler --cov-report=term --cov-fail-under=100

# Specific module
python -m pytest tests/test_smu_commands.py -v

# What is missing coverage
python -m pytest -m "not slow" --cov=corecycler --cov-report=term-missing

# Ring B live contract tests: real binaries and hardware, never in the sandbox.
# Without the variable an absent resource skips; with it, it fails loudly.
CORECYCLER_HW_CONTRACTS=1 python -m pytest -m contract

# The privileged tier on top: MSR (CAP_SYS_RAWIO), dmidecode (root) and the SMU
# mailbox (corecycler group). Without these rights those checks skip by name
# rather than failing; run this before trusting a hardware change.
sudo -E env CORECYCLER_HW_CONTRACTS=1 CORECYCLER_HW_PRIVILEGED=1 python -m pytest -m contract
```

**Line coverage may never drop below 100%** -- the package build enforces it, so a new
branch without a test fails the build, not review.

### Test Requirements

- **All new code must have tests.** No exceptions.
- **All existing tests must pass.** Run the full suite before submitting.
- **Test the behavior, not the implementation.** Assert outcomes, not internal state.
- **Use existing fixtures.** See `tests/conftest.py` for topology
  builders, mock SMU sysfs, and backend mocks.

### Test Categories

PySide6 is needed for the whole suite, not only the GUI files: `conftest.py`'s autouse
fixtures import the tuner engine, which imports Qt.

| Category | Files | Description |
|----------|-------|-------------|
| SMU commands | `test_smu_commands.py` | Encoding/decoding, generation detection |
| SMU driver | `test_smu_driver.py` | CO read/write, slot probing, backup/restore |
| Monitor | `test_monitor.py` | hwmon, frequency, power monitoring |
| Topology | `test_topology.py` | CPU detection, CCD layout, X3D |
| Tuner engine | `test_tuner_engine.py` | State machine, crash recovery, scheduling |
| Backends | `test_backends.py` | Stress test command generation, output parsing |
| External tools | `test_tools.py`, `test_tool_prompt.py` | Binary resolution, the missing-tool prompt |
| Contracts | `test_contracts.py`, `test_tool_discovery_live.py` | Drift pins (Ring A) and live checks (Ring B) |
| History | `test_history_*.py` | SQLite persistence, export, context |

### Property-Based Tests (Hypothesis)

The tuner state machine has property-based tests using
[Hypothesis](https://hypothesis.readthedocs.io/). These generate
random pass/fail sequences and assert that invariants always hold:

- Offset never exceeds `max_offset`
- Offset never goes past baseline
- Phase is always a valid `TunerPhase`
- `crash_count` only increases
- Every core reaches a terminal state
- `best_offset` only gets more aggressive during search

To run: `pytest tests/test_tuner_engine.py -k "Invariant" -v`

### Adding Tests for New CPU Generations

1. Add a mock `/proc/cpuinfo` string to `tests/conftest.py` using `_gen_cpuinfo()`
2. Add generation detection test to `tests/test_smu_commands.py::TestDetectGeneration`
3. Add CO encoding round-trip test if the generation has CO support
4. If the generation has unique behavior (different SMU addresses,
   harvested cores), add a dedicated test

### Mock Infrastructure

| Fixture | Purpose | Location |
|---------|---------|----------|
| `_gen_cpuinfo()` | Generate mock /proc/cpuinfo text | `conftest.py` |
| `build_topology()` | Build CPUTopology from mock cpuinfo | `conftest.py` |
| `mock_sysfs` | Factory for fake sysfs trees | `conftest.py` |
| `mock_backend` | Controllable stress backend | `conftest.py` |
| `smu_dir` | Fake ryzen_smu sysfs | `test_smu_driver.py` |
| `zen3_cmds` / `zen5_cmds` | SMU command set fixtures | `test_smu_driver.py` |

## Code Style

### Python

- **Python 3.12+** required
- **Ruff** for linting (`ruff check src/`), configured in `pyproject.toml`. There is no
  Python auto-formatter: `ruff format` is not part of the gate and running it would
  reformat the tree
- Line length: 120 characters
- Type hints on all function signatures
- Dataclasses with `slots=True` for data structures
- `StrEnum` for enumerations (catches typos at import time)
- No comments unless explaining *why* (not *what*)

### Nix

- `nixfmt` for formatting (enforced by pre-commit)
- `lib.mkOption` not `with lib;`
- Flake check must pass: `nix flake check`

## Adding a Stress Test Backend

CoreCycler uses a backend registry. Adding a new backend is 4 steps:

1. Create `src/corecycler/engine/backends/<name>.py`
2. Subclass `StressBackend` from `base.py` and implement:
   - `get_command(config, work_dir) -> list[str]` — build the
     command line (`self.require_binary()` gives the resolved path)
   - `parse_output(stdout, stderr, returncode) -> tuple[bool, str | None]`
     — detect pass/fail
   - `get_supported_modes() -> list[StressMode]` — SSE, AVX, AVX2, etc.
3. Add `@register_backend("display-name")` decorator
4. Add a matching `ExternalTool` entry to `TOOLS` in `src/corecycler/config/tools.py`,
   which is how the binary is found and how `corecycler doctor` reports it. Locating a
   binary lives there and nowhere else: PATH alone cannot find a tool under `sudo`
   (secure_path) or one extracted from a tarball. The `external-tool-discovery`
   contract fails if a registered backend has no entry.

The GUI discovers backends automatically — no GUI code changes needed.

See `src/corecycler/engine/backends/mprime.py` for a reference implementation.

## SMU / Hardware Contributions

Hardware-touching code (SMU driver, hwmon, topology) has higher review standards:

- **Test with mock sysfs** — never depend on real hardware in tests
- **Range-check all SMU arguments** — CO values, PBO limits
- **Log every SMU write** — debug logging for hardware operations
- **Document the source** — cite ZenStates-Core, kernel driver docs, or AMD documentation
- **Safety: all SMU writes are volatile** — never modify BIOS/UEFI

### Adding Support for a New CPU

1. Add `CPUGeneration` enum value in `src/corecycler/smu/commands.py`
2. Add `SMUCommandSet` entry in `COMMAND_SETS` with correct opcodes
3. Add detection logic in `detect_generation()`
4. Add test fixture in `tests/conftest.py`
5. Add parametrized detection test in `tests/test_smu_commands.py`
6. If encoding differs, update `encode_co_arg()` / `decode_co_arg()`

### Adding a Super I/O Chip

1. Add the chip name prefix to `_SUPERIO_CHIPS` in `src/corecycler/monitor/hwmon.py`
2. If the Vcore input is not `in0`, the label-scanning code handles it automatically
3. If the chip needs a new kernel module, add an option to `nix/module.nix`
4. Add a test in `tests/test_monitor.py`

## Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(hwmon): add NCT6687 Super I/O support
fix(smu): correct slot mapping for harvested cores
docs: update hardware support table
test(tuner): add crash-during-hardening gap test
```

The scope names the area touched -- e.g. `tuner`, `smu`, `gui`, `engine`,
`backends`, `history`, `topology`, `tests`, `ci`, `nix`.

## Review Process

- Maintainer reviews all PRs
- Pre-commit hooks must pass (format, lint, markdown, spelling)
- All tests must pass in CI
- Hardware changes need mock sysfs tests — never "works on my machine"
- Safety-critical code (SMU writes, process management) gets extra scrutiny
