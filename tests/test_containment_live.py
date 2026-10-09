"""Ring B live containment drift tests — real systemd scope, real kernel cpuset.

Proves on real hardware that the boundary every stress backend runs behind
cannot be widened from inside, and that the escape watchdog's observation
matches the kernel's own record.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from corecycler.engine import containment, execution

pytestmark = pytest.mark.contract

ALLOWED = (0, 1)


def _require_mechanism() -> None:
    problem = containment.containment_problem(refresh=True)
    if problem is not None:
        pytest.skip(f"this host cannot contain a stress test: {problem}")


@pytest.mark.req("L01.7")
def test_a_host_that_does_not_enforce_allowed_cpus_refuses_to_contain():
    problem = containment.containment_problem(refresh=True)
    if problem is None:
        pytest.skip("this host enforces AllowedCPUs; the containment tests below cover it")
    if "AllowedCPUs is not enforced" not in problem:
        pytest.skip(f"no systemd scope runs here at all: {problem}")
    with pytest.raises(containment.ContainmentUnavailable, match="does not delegate the cpuset controller"):
        containment.contain(ALLOWED)


def test_a_contained_child_cannot_escape_its_cpuset():
    _require_mechanism()
    prefix = containment.contain(ALLOWED).prefix
    probe = (
        "import json, os\n"
        "try:\n"
        "    os.sched_setaffinity(0, range(os.cpu_count()))\n"
        "except OSError:\n"
        "    pass\n"
        "print(json.dumps(sorted(os.sched_getaffinity(0))))\n"
    )
    result = subprocess.run(
        prefix + [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    import json

    reported = json.loads(result.stdout.strip())
    assert set(reported) <= set(ALLOWED), f"child widened its affinity to {reported} despite AllowedCPUs={ALLOWED}"


def test_the_watchdog_observation_matches_the_kernel_record():
    _require_mechanism()
    contained = containment.contain(ALLOWED)
    proc = subprocess.Popen(
        contained.prefix + [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=execution.make_preexec(),
    )
    try:
        deadline = time.monotonic() + 10
        observed: set[int] = set()
        while time.monotonic() < deadline:
            observed = containment.observed_tree_cpus(proc.pid)
            if observed and observed <= set(ALLOWED):
                break
            time.sleep(0.2)
        assert observed, "no thread of the contained tree was observable"
        assert observed <= set(ALLOWED), f"observed CPUs {sorted(observed)} outside AllowedCPUs={ALLOWED}"
        cgroup = containment.payload_cgroup(proc.pid, contained.unit)
        assert cgroup is not None, "payload never showed the named scope in /proc"
        assert cgroup == contained.scope, f"the kernel placed the scope at {cgroup}, not at {contained.scope}"
        assert containment.scope_effective_cpus(cgroup) == set(ALLOWED)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=10)
