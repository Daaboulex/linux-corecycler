# Coding Conventions

**Analysis Date:** 2026-03-19

## Naming Patterns

**Files:**
- Lowercase with underscores: `scheduler.py`, `cpu_usage.py`, `smu_driver.py`
- Module grouping by functionality: `engine/` for stress testing orchestration, `monitor/` for hardware monitoring, `smu/` for CPU tuning, `gui/` for UI
- Test files: `test_*.py` (e.g., `test_scheduler.py`, `test_topology.py`)
- No abbreviations except for domain-specific terms (SMU, MCE, CCD, FFT, RAPL, X3D)

**Functions:**
- `snake_case` for all functions and methods
- Private methods prefixed with `_`: `_parse_cpuinfo()`, `_verify_affinity()`, `_read_core_usage()`
- Utility functions at module level or as `@staticmethod` for stateless operations
- Example: `read_cpu_temperature()`, `detect_topology()`, `_classify_error()`

**Variables:**
- `snake_case` for all variables: `core_id`, `stress_config`, `work_dir`
- Use descriptive names: `seconds_per_core` not `spc`; `logical_cpus` not `cpus`
- Constants in UPPERCASE: `DMESG_MIN_INTERVAL`, `CPUINFO`, `CONFIG_DIR`
- Suffix `_count` for counters: `physical_cores`, `logical_cpus_count`
- Suffix `_map` or `_dict` for mappings: `logical_map`, `cores_seen`

**Types:**
- Dataclass names are PascalCase: `CoreTestStatus`, `SchedulerConfig`, `TestProfile`, `CPUTopology`
- Enum names are PascalCase: `TestState`, `TestMode`, `StressMode`, `FFTPreset`
- Use `dataclass(slots=True)` for memory efficiency on data-holding classes
- Frozen dataclasses for immutable data: `@dataclass(frozen=True, slots=True)` — see `LogicalCPU`, `PhysicalCore`

## Code Style

**Formatting:**
- Tool: Ruff (configured in `pyproject.toml`)
- Line length: 100 characters
- Use ruff to format: `ruff format src/`
- Import sorting handled by ruff (enabled with "I" rule)

**Linting:**
- Tool: Ruff
- Enabled rules: E, F, W, I, UP, B, SIM, TCH
- Target: Python 3.12+
- Example configuration from `pyproject.toml`:
  ```toml
  [tool.ruff]
  target-version = "py312"
  line-length = 100
  [tool.ruff.lint]
  select = ["E", "F", "W", "I", "UP", "B", "SIM", "TCH"]
  ```

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first)
2. Standard library: `import os`, `import sys`, `from pathlib import Path`
3. External packages: `import pytest`, `from PySide6.QtCore import Qt`
4. Local imports: `from engine.scheduler import CoreScheduler`
5. TYPE_CHECKING block for type hints that create circular imports:
   ```python
   from typing import TYPE_CHECKING
   if TYPE_CHECKING:
       from .backends.base import StressBackend
   ```

**Path Aliases:**
- No path aliases configured
- Relative imports within same package are rare; prefer absolute imports
- Example: `from engine.backends.base import StressConfig` (from any module in `src/`)

## Error Handling

**Patterns:**
- Specific exception catching, not bare `except:`
- Use `contextlib.suppress()` for intentional no-op exception handling:
  ```python
  with contextlib.suppress(OSError, ProcessLookupError):
      os.killpg(pgid, signal.SIGKILL)
  ```
- Wrap file I/O in try/except for graceful degradation:
  ```python
  try:
      text = path.read_text()
  except (OSError, ValueError):
      return None  # lenient — don't block test
  ```
- Lenient error handling in safety-critical code — detect hardware errors without crashing
  - Example from `scheduler.py`: Temperature checks return True if unreadable (don't block test)
  - Example from `detector.py`: MCE checks wrap all sysfs reads, return empty list on error

**Exception Types Used:**
- `OSError`, `PermissionError`, `ProcessLookupError`, `FileNotFoundError` — system/file operations
- `ValueError` — invalid data parsing (e.g., integer conversion failures)
- `RuntimeError` — general execution problems
- Custom: None (no custom exceptions defined)

## Logging

**Framework:** Python standard `logging` module

**Usage:**
- Each module initializes logger at module level: `log = logging.getLogger(__name__)`
- Located near imports: see `scheduler.py` line 24
- Log levels:
  - `log.warning()` — recoverable issues (e.g., CPU thermal threshold exceeded)
  - `log.info()` — state changes (e.g., CPU temperature back within safe zone)
  - `log.debug()` — diagnostic info (e.g., "Cannot list /sys directory: Permission denied")

**Patterns:**
- Include context in log messages: `log.warning("CPU temperature %.1f C exceeds safety limit %.1f C", temp, limit)`
- Use `%` formatting in log calls (deferred), not f-strings
- Log before terminating operations: `log.warning()` before `sys.exit()` or `return False`

## Comments

**When to Comment:**
- Explain WHY, not WHAT
- Describe algorithm rationale or non-obvious domain knowledge
- Example from `scheduler.py`: Comments explain C-state transition testing, hysteresis logic, process group cleanup
- No comments for obvious code: `x = 5  # set x to 5` is bad
- Block comments for section headers: `# --- temperature safety check ---`

**Docstrings:**
- Module docstring: One-liner describing purpose (e.g., `"""Core cycling scheduler — runs stress tests per-core with error detection."""`)
- Function/method docstrings: Google-style for complex functions
- Example from `scheduler.py` (line 179):
  ```python
  @staticmethod
  def _read_cpu_temperature() -> float | None:
      """Read CPU temperature from hwmon (Tctl/Tdie for AMD, coretemp for Intel).

      Returns temperature in celsius or None if unavailable.
      """
  ```
- Short functions/properties: Docstring optional if name is self-explanatory
- No type annotations in docstrings (use Python 3.10+ `|` union syntax in code)

## Function Design

**Size:** Keep functions under 50 lines where possible. `_run_stress_phase()` at 173 lines is an exception for critical logic.

**Parameters:**
- Use dataclass objects for multiple related parameters
- Example: `SchedulerConfig(seconds_per_core=600, cycle_count=1, ...)` instead of 10 boolean params
- Type hints required (enforced by TCH lint rule)
- Use `|` for union types: `Path | None` not `Optional[Path]`

**Return Values:**
- Explicit return types in signature
- Tuple returns for multiple values: `tuple[bool, str | None]` for (passed, error_msg)
- None as explicit return for operations with side effects: `def cleanup(...) -> None:`
- Boolean predicates should be clear: `passed` not `success`; `has_errors` not `error_flag`

## Module Design

**Exports:**
- No explicit `__all__` lists
- Import at module level: `from engine.scheduler import CoreScheduler`
- Implementation detail functions prefixed with `_` (private by convention)

**Barrel Files:**
- No barrel files (`__init__.py`) for re-exporting
- `__init__.py` files exist but are minimal or empty
- Example: `src/engine/__init__.py` is empty — users import from submodules directly

**Dataclass and Enum Patterns:**
- Use `@dataclass(slots=True)` for memory efficiency
- Use `field(default_factory=...)` for mutable defaults:
  ```python
  @dataclass(slots=True)
  class SchedulerConfig:
      cores_to_test: list[int] | None = None
      variable_load: bool = False
  ```
- Use `Enum` with `auto()` for auto-incrementing values:
  ```python
  class TestState(Enum):
      IDLE = auto()
      RUNNING = auto()
      STOPPING = auto()
      FINISHED = auto()
  ```

**Configuration Management:**
- Dataclass instances hold configuration: `SchedulerConfig`, `TestProfile`, `StressConfig`, `AppSettings`
- JSON I/O using `asdict()` and `dataclass(**dict)` constructor
- Example from `settings.py`: `asdict(settings)` converts to JSON-friendly dict

---

*Convention analysis: 2026-03-19*
