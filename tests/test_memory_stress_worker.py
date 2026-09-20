"""memory_tab _StressWorker: sizing from the one memory budget, run and stop."""

from __future__ import annotations

import signal
import subprocess
import sys as _sys
from unittest.mock import MagicMock

import pytest

from corecycler.engine.memory_budget import MemoryBudget, MemoryBudgetUnavailable

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _budget(usable_mb: int, minimum_mb: int = 5) -> MemoryBudget:
    return MemoryBudget(
        total_mb=16384,
        available_mb=12288,
        cgroup_limit_mb=None,
        cgroup_used_mb=None,
        ceiling_mb=12288,
        headroom_mb=12288 - usable_mb,
        usable_mb=usable_mb,
        instances=1,
        per_instance_mb=usable_mb,
        minimum_per_instance_mb=minimum_mb,
    )


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _proc(stdout="Status: PASS\n", stderr="", returncode=0, timeout_first=False):
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.poll.return_value = None
    if timeout_first:
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("cmd", 1),
            (stdout, stderr),
        ]
    else:
        proc.communicate.return_value = (stdout, stderr)
    return proc


def _run_worker(monkeypatch, tool, proc=None, budget=None, popen_error=None):
    import corecycler.gui.memory_tab as mt

    _qapp()
    results: list = []
    worker = mt._StressWorker(tool, 1)
    worker.done.connect(lambda ok, out: results.append((ok, out)))
    monkeypatch.setattr(mt, "memory_budget", lambda *_a, **_kw: budget or _budget(2048))
    if popen_error is not None:
        monkeypatch.setattr("subprocess.Popen", MagicMock(side_effect=popen_error))
    elif proc is not None:
        monkeypatch.setattr("subprocess.Popen", MagicMock(return_value=proc))
    worker.run()
    return results


class TestStressWorkerRun:
    def test_unknown_tool_reports_failure(self, monkeypatch):
        results = _run_worker(monkeypatch, "bogus-tool")
        assert results == [(False, "Unknown tool: bogus-tool")]

    def test_stressapptest_pass(self, monkeypatch):
        results = _run_worker(monkeypatch, "stressapptest", proc=_proc())
        assert results[0][0] is True

    def test_stressapptest_failure_text(self, monkeypatch):
        results = _run_worker(monkeypatch, "stressapptest", proc=_proc(stdout="miscompare\n"))
        assert results[0][0] is False

    def test_stressapptest_is_sized_by_the_one_budget(self, monkeypatch):
        popen = MagicMock(return_value=_proc())
        import corecycler.gui.memory_tab as mt

        _qapp()
        worker = mt._StressWorker("stressapptest", 2)
        asked = []

        def fake_budget(instances, minimum_per_instance_mb, **_kw):
            asked.append((instances, minimum_per_instance_mb))
            return _budget(9216)

        monkeypatch.setattr(mt, "memory_budget", fake_budget)
        monkeypatch.setattr("subprocess.Popen", popen)
        worker.run()
        cmd = popen.call_args[0][0]
        assert cmd[0] == "stressapptest"
        assert cmd[cmd.index("-M") + 1] == "9216"
        assert str(2 * 60) in cmd
        assert asked[0][0] == 1
        assert asked[0][1] >= 1

    def test_a_share_below_the_minimum_launches_nothing_and_names_the_numbers(self, monkeypatch):
        popen = MagicMock(return_value=_proc())
        monkeypatch.setattr("subprocess.Popen", popen)
        results = _run_worker(monkeypatch, "stressapptest", budget=_budget(3, minimum_mb=5))
        assert not popen.called
        assert results[0][0] is False
        assert "1 x 3 MB" in results[0][1]
        assert "minimum 5 MB" in results[0][1]

    def test_an_unreadable_budget_is_reported_not_guessed(self, monkeypatch):
        import corecycler.gui.memory_tab as mt

        popen = MagicMock(return_value=_proc())
        monkeypatch.setattr("subprocess.Popen", popen)
        _qapp()
        results: list = []
        worker = mt._StressWorker("stressapptest", 1)
        worker.done.connect(lambda ok, out: results.append((ok, out)))

        def refuse(*_a, **_kw):
            raise MemoryBudgetUnavailable("/proc/meminfo lacks MemAvailable")

        monkeypatch.setattr(mt, "memory_budget", refuse)
        worker.run()
        assert not popen.called
        assert results == [(False, "/proc/meminfo lacks MemAvailable")]

    def test_stress_ng_uses_returncode(self, monkeypatch):
        results = _run_worker(monkeypatch, "stress-ng --vm", proc=_proc(returncode=0))
        assert results[0][0] is True
        results = _run_worker(monkeypatch, "stress-ng --vm", proc=_proc(returncode=3))
        assert results[0][0] is False

    def test_timeout_kills_process_group_then_collects(self, monkeypatch):
        proc = _proc(timeout_first=True)
        monkeypatch.setattr("os.getpgid", lambda _pid: 999)
        killed: list = []
        monkeypatch.setattr("os.killpg", lambda pgid, s: killed.append((pgid, s)))
        results = _run_worker(monkeypatch, "stressapptest", proc=proc)
        assert killed and killed[0][0] == 999
        assert results[0][0] is True

    def test_launch_failure_is_reported(self, monkeypatch):
        results = _run_worker(monkeypatch, "stressapptest", popen_error=OSError("no binary"))
        assert results[0][0] is False
        assert "no binary" in results[0][1]


class TestStressWorkerStop:
    def _worker(self, proc):
        import corecycler.gui.memory_tab as mt

        _qapp()
        worker = mt._StressWorker("stressapptest", 1)
        worker._process = proc
        return worker

    def test_stop_without_process_is_a_noop(self):
        self._worker(None).stop()

    def test_stop_after_exit_is_a_noop(self):
        proc = _proc()
        proc.poll.return_value = 0
        signals: list = []
        import os

        old = os.killpg
        os.killpg = lambda pgid, s: signals.append(s)
        try:
            self._worker(proc).stop()
        finally:
            os.killpg = old
        assert signals == []

    def test_stop_signals_the_group_with_sigkill(self, monkeypatch):
        proc = _proc()
        monkeypatch.setattr("os.getpgid", lambda _pid: 999)
        signals: list = []
        monkeypatch.setattr("os.killpg", lambda pgid, s: signals.append((pgid, s)))
        self._worker(proc).stop()
        assert signals == [(999, signal.SIGKILL)]

    def test_stop_never_waits_on_the_process(self, monkeypatch):
        proc = _proc()
        monkeypatch.setattr("os.getpgid", lambda _pid: 999)
        monkeypatch.setattr("os.killpg", lambda pgid, s: None)
        self._worker(proc).stop()
        proc.wait.assert_not_called()
        proc.communicate.assert_not_called()

    def test_stop_tolerates_a_vanished_group(self, monkeypatch):
        proc = _proc()
        monkeypatch.setattr("os.getpgid", MagicMock(side_effect=ProcessLookupError))
        self._worker(proc).stop()
        proc.wait.assert_not_called()
