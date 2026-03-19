# Testing Patterns

**Analysis Date:** 2026-03-19

## Test Framework

**Runner:**
- pytest 8.0+
- Config: `pyproject.toml` `[tool.pytest.ini_options]`
- Pythonpath configured to include `src/` at test discovery time
- See `tests/conftest.py` line 13: `sys.path.insert(0, str(Path(__file__).parent.parent / "src"))`

**Assertion Library:**
- pytest built-in assertions (no special library)
- Parametrized tests with `@pytest.mark.parametrize`

**Run Commands:**
```bash
pytest tests/                   # Run all tests
pytest tests/ -v               # Verbose output
pytest tests/test_scheduler.py -v  # Run single test file
pytest -k test_init_state      # Run by test name pattern
pytest --cov=src --cov-report=html  # Coverage report (if pytest-cov installed)
```

## Test File Organization

**Location:**
- Co-located with source code organization
- Each source module has corresponding test file: `src/engine/scheduler.py` → `tests/test_scheduler.py`
- Shared fixtures in `tests/conftest.py`

**Naming:**
- Test files: `test_*.py`
- Test classes: `Test*` (e.g., `TestSchedulerInit`, `TestParseCpuinfo`)
- Test functions: `test_*` (e.g., `test_basic_run`, `test_defaults`)
- Parametrized values as descriptive strings

**Structure:**
```
tests/
├── conftest.py           # Shared fixtures, mock data, helpers
├── test_backends.py      # Stress backend tests
├── test_detector.py      # MCE/error detection tests
├── test_monitor.py       # Hardware monitoring tests
├── test_scheduler.py     # Core scheduler tests
├── test_topology.py      # CPU topology detection tests
├── test_tuner_*.py       # Tuner/optimization tests
└── test_smu_*.py         # SMU driver tests
```

## Test Structure

**Suite Organization:**
```python
class TestSchedulerInit:
    """Group related test methods under descriptive class."""

    def test_init_state(self, simple_topo, mock_backend, tmp_path):
        """Test one specific behavior."""
        # Arrange
        cfg = SchedulerConfig()
        sched = CoreScheduler(...)

        # Act
        result = sched.run()

        # Assert
        assert sched.state == TestState.FINISHED
```

**Patterns:**
- One test method per behavior (single responsibility)
- Arrange-Act-Assert structure (implicit via setup fixtures + test body)
- Fixtures supply test data (see conftest.py for shared fixtures)
- Parametrized tests for multiple input/output scenarios

## Mocking

**Framework:** `unittest.mock` (built-in)

**Patterns:**
```python
# Simple mock object
mock_backend = MagicMock()
mock_backend.should_pass = True

# Mock return value
mock_proc = MagicMock()
mock_proc.poll.return_value = 0  # process exited successfully

# Mock side effect (exception or callable)
mock_proc.wait.side_effect = subprocess.TimeoutExpired("test", 3)

# Patch at module level
with patch("subprocess.Popen", return_value=mock_proc):
    # test code
    pass

# Patch object method
with patch.object(scheduler.detector, "check_mce", return_value=[]):
    # test code
    pass

# Patch and call original
with patch("os.killpg") as mock_killpg:
    scheduler._kill_current()
    mock_killpg.assert_called_with(12345, signal.SIGTERM)
```

**Custom Mock Backend:**
See `conftest.py` lines 396-430. `ControllableMockBackend` extends `StressBackend` with controllable behavior:
```python
class ControllableMockBackend(StressBackend):
    name = "mock"

    def __init__(self):
        self.should_pass = True
        self.error_message = None
        self.prepared_dirs: list[Path] = []

    def is_available(self) -> bool:
        return self._available

    def parse_output(self, stdout, stderr, returncode):
        return self.should_pass, self.error_message
```

**What to Mock:**
- Subprocess calls (`subprocess.Popen`)
- File I/O on /sys, /proc (patch `Path.read_text()` or use `tmp_path`)
- System calls (`os.killpg`, `os.getpgid`, `os.waitpid`)
- External hardware access (temperature reads, MCE checks)
- Time.sleep for speed (use short timeouts in tests, not full 600s)

**What NOT to Mock:**
- Pure Python logic (dataclass constructors, list operations)
- Internal method calls within same class
- JSON I/O (use real temp files instead)
- Logging (let it run, or suppress if too verbose)

## Fixtures and Factories

**Test Data:**

Topology fixtures in `conftest.py`:
```python
@pytest.fixture
def topo_dual_ccd_x3d():
    """Topology fixture: 8-core dual-CCD X3D with SMT (16 logical)."""
    topo = build_topology(CPUINFO_DUAL_CCD_SMT, num_ccds=2)
    for pc in topo.cores.values():
        ccd = 0 if pc.core_id < 4 else 1
        object.__setattr__(pc, "ccd", ccd)
    return topo

@pytest.fixture
def simple_topo():
    return make_topology(4, smt=False)
```

Backend mock fixture:
```python
@pytest.fixture
def mock_backend():
    """A controllable mock StressBackend."""
    return ControllableMockBackend()
```

Pytest built-in fixtures:
- `tmp_path` — temporary directory (auto-cleaned)
- `tmp_path` — returns `Path` object

**Location:**
- Shared fixtures: `tests/conftest.py`
- Module-specific fixtures: top of test file (rare)
- Mock data constants: `conftest.py` module level (e.g., `CPUINFO_DUAL_CCD_SMT`)

## Coverage

**Requirements:** No specific coverage target enforced in CI/CD

**Current state:** 549 test functions across 23 test files

**View Coverage:**
```bash
pytest --cov=src --cov-report=term-missing tests/
pytest --cov=src --cov-report=html tests/
# open htmlcov/index.html
```

## Test Types

**Unit Tests:**
- Scope: Single function or class method
- Example: `test_init_state` (CoreScheduler initialization)
- Use mocks for external dependencies
- Isolated, fast (< 1s each)
- Most tests in the suite are unit tests

**Integration Tests:**
- Scope: Multiple components working together
- Example: `test_callbacks_fire` (scheduler calling event handlers)
- Mock external system (subprocess, sysfs), test coordination
- Medium speed (< 5s)

**E2E Tests:**
- Framework: Not used
- Why: No E2E test framework configured. Could use pytest + subprocess for full integration with real stress binaries, but not currently implemented.

## Common Patterns

**Async Testing:**
Not applicable — single-threaded synchronous code. The scheduler uses subprocess for CPU stress tests, which are handled via `subprocess.Popen` and mocked in tests.

**Error Testing:**
```python
def test_backend_error_detected(self, simple_topo, mock_backend, tmp_path):
    """Backend parse_output returning failure should mark core as failed."""
    mock_backend.should_pass = False
    mock_backend.error_message = "FATAL ERROR"

    # ... setup ...

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch.object(sched.detector, "check_mce", return_value=[]),
        patch.object(sched.detector, "reset"),
    ):
        results = sched.run()

    for core_id in range(4):
        assert results[core_id][0].passed is False
        assert sched.core_status[core_id].errors > 0
```

**Parametrized Tests:**
```python
@pytest.mark.parametrize(
    "msg,expected",
    [
        ("MCE detected on CPU 0", "mce"),
        ("machine check exception", "mce"),
        ("Rounding was 0.5", "computation"),
        ("timeout after 600s", "timeout"),
        (None, "unknown"),
    ],
)
def test_classification(self, msg, expected):
    assert CoreScheduler._classify_error(msg) == expected
```

**File System Tests:**
Use pytest's `tmp_path` fixture:
```python
def test_work_dir_created(self, simple_topo, mock_backend, tmp_path):
    work = tmp_path / "deep" / "nested" / "work"
    sched = CoreScheduler(..., work_dir=work)

    # ... run ...

    assert work.exists()
```

**Mocking File Reads:**
```python
def test_dual_ccd_smt_core_count(self):
    topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
    assert topo.physical_cores == 8

def parse_cpuinfo_from_text(text: str) -> CPUTopology:
    topo = CPUTopology()
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = text
    with patch("engine.topology.CPUINFO", mock_path):
        _parse_cpuinfo(topo)
    return topo
```

**Multiline Patch Context:**
```python
with (
    patch("subprocess.Popen", return_value=mock_proc),
    patch.object(sched.detector, "check_mce", return_value=[]),
    patch.object(sched.detector, "reset"),
):
    results = sched.run()
```

## Test Execution Warnings

**Pytest Collection Warning:**
The pyproject.toml suppresses a warning:
```toml
[tool.pytest.ini_options]
filterwarnings = [
    # Production classes named Test* (TestProfile, TestState, TestRunLogger)
    # trigger PytestCollectionWarning on import — suppress them
    "ignore::pytest.PytestCollectionWarning",
]
```

This is because domain classes like `TestProfile` and `TestState` match pytest's test collection pattern. The warning is harmless.

---

*Testing analysis: 2026-03-19*
