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
pip install -e ".[dev]" pytest-cov
pytest tests/ -v
```

`pip install -e ".[dev]"` installs PySide6, pytest, ruff and hypothesis. The `dev` extra
lacks pytest-cov, which the coverage gate below needs, so it is installed by name.

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

### PySide6 in the suite

When PySide6 is missing, `tests/conftest.py` fakes `PySide6.QtCore`, so the engine and
tuner tests run without Qt. The GUI tests import `PySide6.QtWidgets` and need the real
package.

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

## Code Style

### Python

- **Python 3.12+** required
- **Ruff** lints and formats, configured in `pyproject.toml`. The pre-commit hooks run
  `ruff check --fix` and `ruff format`
- Line length: 120 characters
- Type hints on function signatures. Ruff does not check them, and some older functions
  lack them
- Dataclasses with `slots=True` for data structures
- `StrEnum` where the value is stored or compared as a string (tuner phases, outcomes);
  plain `Enum` elsewhere
- No comments unless explaining *why* (not *what*)

### Nix

- `nixfmt` for formatting (enforced by pre-commit)
- `lib.mkOption` not `with lib;`
- Flake check must pass: `nix flake check`

## Adding a Stress Test Backend

CoreCycler uses a backend registry. Adding a new backend is 6 steps:

1. Create `src/corecycler/engine/backends/<name>.py`
2. Subclass `StressBackend` from `base.py` and implement:
   - `get_command(config, work_dir) -> list[str]` — build the
     command line (`self.require_binary()` gives the resolved path)
   - `findings(output, work_dir) -> tuple[LaneSignal, ...]`: map the tool's own text
     (its output, plus any results file it keeps in `work_dir`) to typed `Outcome`s
     through a `FindingTable` and `scan()`. Exit statuses, CPU time, kernel events and
     the final verdict are the Supervisor's job, never the backend's
   - `get_supported_modes() -> list[StressMode]` — SSE, AVX, AVX2, etc.
3. Add `@register_backend("display-name")` decorator
4. Import the module in `load_all()` in `src/corecycler/engine/backends/__init__.py`.
   Nothing else imports it, so without this line it never registers
5. Add a matching `ExternalTool` entry to `TOOLS` in `src/corecycler/config/tools.py`, the
   one place a binary is located ([why](docs/installation.md#where-corecycler-looks-for-a-backend))
6. Add the key to the backend set pinned in `_pin_external_tool_discovery()` in
   `tests/contract_inventory.py`. The `external-tool-discovery` contract fails until the
   registry, `TOOLS` and that set agree

Once `load_all()` registers it, the GUI's backend lists show it. No GUI file changes.

See `src/corecycler/engine/backends/mprime.py` for a reference implementation.

## SMU / Hardware Contributions

Hardware-touching code (SMU driver, hwmon, topology) has higher review standards:

- **Test with mock sysfs.** Unit tests never need real hardware. Only the Ring B contract
  tests (`-m contract`, above) touch it
- **Range-check all SMU arguments** — CO values, PBO limits
- **Log every SMU write** — debug logging for hardware operations
- **Document the source** — cite ZenStates-Core, kernel driver docs, or AMD documentation
- **Safety: all SMU writes are volatile** — never modify BIOS/UEFI

### Adding Support for a New CPU

1. Add `CPUGeneration` enum value in `src/corecycler/smu/commands.py`
2. Add `SMUCommandSet` entry in `COMMAND_SETS` with correct opcodes
3. Add its family and model range to `_MODEL_TABLE`. Only a name-based adjustment goes in
   `detect_generation()`
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
