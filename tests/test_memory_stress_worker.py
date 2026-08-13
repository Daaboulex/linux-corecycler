"""memory_tab _StressWorker and free-memory probe coverage."""

from __future__ import annotations

import signal
import subprocess
import sys as _sys
from unittest.mock import MagicMock, mock_open, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


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


def _run_worker(monkeypatch, tool, proc=None, free=2048, popen_error=None):
    import corecycler.gui.memory_tab as mt

    _qapp()
    results: list = []
    worker = mt._StressWorker(tool, 1)
    worker.done.connect(lambda ok, out: results.append((ok, out)))
    monkeypatch.setattr(mt, "_get_free_memory_mb", lambda: free)
    if popen_error is not None:
        monkeypatch.setattr("subprocess.Popen", MagicMock(side_effect=popen_error))
    elif proc is not None:
        monkeypatch.setattr("subprocess.Popen", MagicMock(return_value=proc))
    worker.run()
    return results


class TestFreeMemoryProbe:
    def test_reads_mem_available(self):
        from corecycler.gui.memory_tab import _get_free_memory_mb

        data = "MemTotal:       16000000 kB\nMemAvailable:    2097152 kB\n"
        with patch("builtins.open", mock_open(read_data=data)):
            assert _get_free_memory_mb() == 2048

    def test_absent_field_returns_none(self):
        from corecycler.gui.memory_tab import _get_free_memory_mb

        with patch("builtins.open", mock_open(read_data="MemTotal: 1 kB\n")):
            assert _get_free_memory_mb() is None

    def test_unreadable_meminfo_returns_none(self):
        from corecycler.gui.memory_tab import _get_free_memory_mb

        with patch("builtins.open", side_effect=OSError):
            assert _get_free_memory_mb() is None


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

    def test_stressapptest_sizes_from_free_memory(self, monkeypatch):
        popen = MagicMock(return_value=_proc())
        import corecycler.gui.memory_tab as mt

        _qapp()
        worker = mt._StressWorker("stressapptest", 2)
        monkeypatch.setattr(mt, "_get_free_memory_mb", lambda: 4096)
        monkeypatch.setattr("subprocess.Popen", popen)
        worker.run()
        cmd = popen.call_args[0][0]
        assert cmd[0] == "stressapptest"
        assert str(int(4096 * 0.75)) in cmd
        assert str(2 * 60) in cmd

    def test_stressapptest_defaults_when_free_memory_unknown(self, monkeypatch):
        popen = MagicMock(return_value=_proc())
        import corecycler.gui.memory_tab as mt

        _qapp()
        worker = mt._StressWorker("stressapptest", 1)
        monkeypatch.setattr(mt, "_get_free_memory_mb", lambda: None)
        monkeypatch.setattr("subprocess.Popen", popen)
        worker.run()
        assert "1024" in popen.call_args[0][0]

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
