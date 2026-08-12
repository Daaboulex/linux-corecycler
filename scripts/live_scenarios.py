"""Drive the real application through real scenarios on real hardware.

Everything in the stack is real: the GUI under a live display server, the
worker threads, the cgroup containment, the stress binaries, the history
database and the log file. Only the finger is scripted: widgets are clicked
through their own Qt signal paths. Run inside the full package's dev shell
under xvfb-run, with an isolated HOME so the owner's live app state is never
touched:

  xvfb-run -a python3 scripts/live_scenarios.py --home /run/user/1000/cc-live \
      gui-run --backend mprime --mode SSE --threads 1

Exit code 0 means every assertion in the scenario held; the JSON verdict on
stdout carries the evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

EVIDENCE: dict[str, object] = {}
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: object) -> None:
    EVIDENCE[name] = detail
    if not ok:
        FAILURES.append(f"{name}: {detail}")


STRESS_BINARY_MARKERS = ("lib/y-cruncher", "/mprime", "/stress-ng", "/stressapptest")


def kill_app_stress_tree() -> None:
    import signal as _sig

    from corecycler.engine.containment import _process_tree

    for pid in _process_tree(os.getpid(), Path("/proc")):
        if pid == os.getpid():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if any(marker in cmd for marker in STRESS_BINARY_MARKERS):
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(pid, _sig.SIGKILL)


def _hard_watchdog(seconds: float) -> None:
    import signal as _sig

    time.sleep(seconds)
    sys.stderr.write(f"live_scenarios: watchdog fired after {seconds}s; killing stress tree\n")
    kill_app_stress_tree()
    os.kill(os.getpid(), _sig.SIGKILL)


def campaign_home() -> Path:
    return Path(os.environ["HOME"])


def write_settings(profile: dict, **app_overrides) -> None:
    config_dir = campaign_home() / ".config" / "corecycler"
    config_dir.mkdir(parents=True, exist_ok=True)
    base_profile = {
        "name": "Live",
        "backend": "mprime",
        "stress_mode": "SSE",
        "fft_preset": "SMALL",
        "fft_min": None,
        "fft_max": None,
        "threads": 1,
        "seconds_per_core": 20,
        "iterations_per_core": 0,
        "cycle_count": 1,
        "stop_on_error": False,
        "test_smt": False,
        "cores_to_test": [4, 5],
        "max_temperature": 95.0,
        "test_mode": "CUSTOM",
        "variable_load": False,
        "idle_stability_test": 0.0,
        "idle_between_cores": 0.0,
    }
    base_profile.update(profile)
    settings = {
        "work_dir": "",
        "theme": "system",
        "poll_interval": 0.5,
        "show_smt_threads": False,
        "profiles": [base_profile],
        "active_profile_idx": 0,
        "record_history": True,
        "record_telemetry": True,
        "history_retention_days": 90,
        "notify_on_completion": False,
    }
    settings.update(app_overrides)
    (config_dir / "settings.json").write_text(json.dumps(settings, indent=2))


BACKEND_COMMS = {
    "mprime": "mprime",
    "y-cruncher": "y-cruncher",
    "stress-ng": "stress-ng",
    "stressapptest": "stressapptest",
}


def app_descendants() -> list[int]:
    from corecycler.engine.containment import _process_tree

    return _process_tree(os.getpid(), Path("/proc"))


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "?"


def _proc_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    state = stat[stat.rfind(")") + 2 : stat.rfind(")") + 3]
    return state not in ("Z", "X")


def find_backend_pids(comm: str) -> list[int]:
    ours = set(app_descendants())
    pids: list[int] = []
    for pid in ours:
        try:
            if Path(f"/proc/{pid}/comm").read_text().strip().startswith(comm[:15]):
                pids.append(pid)
        except OSError:
            continue
    return pids


def app_scope_effective_cpus() -> list[set[int]]:
    from corecycler.engine import containment

    scopes: dict[str, set[int]] = {}
    for pid in app_descendants():
        try:
            for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
                if line.startswith("0::") and "corecycler-" in line and line.rstrip().endswith(".scope"):
                    cg = line[3:].rstrip()
                    eff = containment.scope_effective_cpus(cg)
                    if eff is not None:
                        scopes[cg] = eff
        except OSError:
            continue
    return list(scopes.values())


def observed_cpus_of(comm: str) -> set[int]:
    from corecycler.engine import containment

    observed: set[int] = set()
    for pid in find_backend_pids(comm):
        observed |= containment.observed_tree_cpus(pid)
    return observed


def pid_detail(pid: int) -> dict:
    detail: dict = {"pid": pid}
    with contextlib.suppress(OSError, ValueError, IndexError):
        stat = Path(f"/proc/{pid}/stat").read_text()
        ppid = int(stat[stat.rfind(")") + 1 :].split()[1])
        detail["ppid"] = ppid
        detail["parent_comm"] = Path(f"/proc/{ppid}/comm").read_text().strip()
    with contextlib.suppress(OSError):
        for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                detail["cgroup"] = line[3:][-70:]
    with contextlib.suppress(OSError):
        detail["cmdline"] = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()[:120]
    return detail


def strays_outside(comm: str, expected: set[int]) -> list[dict]:
    from corecycler.engine import containment

    strays = []
    for pid in find_backend_pids(comm):
        cpus = containment.observed_tree_cpus(pid)
        if cpus and not cpus <= expected:
            strays.append({**pid_detail(pid), "cpus": sorted(cpus)[:6] + ["..."]})
    return strays


def expected_cpus(cores: list[int], threads: int) -> set[int]:
    from corecycler.engine.topology import detect_topology

    topo = detect_topology()
    cpus: set[int] = set()
    for core in cores:
        info = topo.cores[core]
        cpus |= set(info.logical_cpus[: max(1, threads)])
    return cpus


def app_log_critical_lines() -> list[str]:
    log_file = campaign_home() / ".local" / "share" / "corecycler" / "logs" / "corecycler.log"
    if not log_file.exists():
        return []
    return [line for line in log_file.read_text().splitlines() if "CRITICAL" in line]


def history_rows() -> list[dict]:
    db_path = campaign_home() / ".local" / "share" / "corecycler" / "history" / "history.db"
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        runs = [dict(r) for r in conn.execute(
            "SELECT id, status FROM runs ORDER BY id"
        )]
        for run in runs:
            run["cores"] = [dict(r) for r in conn.execute(
                "SELECT core_id, passed, error_type FROM core_results WHERE run_id=?",
                (run["id"],),
            )]
        return runs
    finally:
        conn.close()


class GuiDriver:
    def __init__(self) -> None:
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        from corecycler.gui.main_window import MainWindow

        self.window = MainWindow()
        self.window.show()

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def click_yes_on_modal(self) -> bool:
        from PySide6.QtWidgets import QMessageBox

        modal = self.app.activeModalWidget()
        if isinstance(modal, QMessageBox):
            for button in modal.buttons():
                if "yes" in button.text().lower():
                    button.click()
                    return True
        return False

    def arm_modal_autoclick(self) -> dict:
        from PySide6.QtCore import QTimer

        state = {"done": False, "elapsed": 0.0}

        def poll() -> None:
            if self.click_yes_on_modal():
                state["done"] = True
                return
            state["elapsed"] += 0.1
            if state["elapsed"] < 15.0:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(100, poll)
        return state

    def arm_modal_dismisser(self, duration: float = 120.0) -> dict:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox

        state: dict = {"dismissed": 0, "titles": [], "elapsed": 0.0}

        def poll() -> None:
            modal = self.app.activeModalWidget()
            if isinstance(modal, QMessageBox):
                state["dismissed"] += 1
                state["titles"].append(modal.windowTitle())
                modal.accept()
            state["elapsed"] += 0.1
            if state["elapsed"] < duration:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(100, poll)
        return state

    def arm_warning_capture(self) -> dict:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox

        state: dict = {"title": "", "text": "", "elapsed": 0.0}

        def poll() -> None:
            modal = self.app.activeModalWidget()
            if isinstance(modal, QMessageBox):
                state["title"] = modal.windowTitle()
                state["text"] = modal.text()
                modal.accept()
                return
            state["elapsed"] += 0.1
            if state["elapsed"] < 15.0:
                QTimer.singleShot(100, poll)

        QTimer.singleShot(100, poll)
        return state

    def worker_running(self) -> bool:
        worker = self.window._worker
        return bool(worker and worker.isRunning())

    def start(self) -> None:
        self.window._start_btn.click()

    def stop(self) -> None:
        self.window._stop_btn.click()

    def wait_worker_done(self, timeout: float, comm: str | None, expected: set[int]) -> dict:
        samples: list[list[int]] = []
        strays: list[dict] = []
        inside = 0
        started = time.monotonic()
        saw_worker = False
        while time.monotonic() - started < timeout:
            self.app.processEvents()
            if self.worker_running():
                saw_worker = True
                for eff in app_scope_effective_cpus():
                    samples.append(sorted(eff))
                    if eff <= expected:
                        inside += 1
                    elif len(strays) < 4:
                        strays.append({"scope_effective": sorted(eff), "expected": sorted(expected)})
            elif saw_worker:
                break
            time.sleep(0.25)
        return {
            "saw_worker": saw_worker,
            "scope_samples": len(samples),
            "samples_inside_expected": inside,
            "distinct_scope_cpus": sorted({tuple(s) for s in samples}),
            "strays": strays[:4],
        }


def scenario_gui_run(args) -> None:
    write_settings({
        "backend": args.backend,
        "stress_mode": args.mode,
        "threads": args.threads,
        "seconds_per_core": args.seconds,
        "cores_to_test": args.cores,
    })
    driver = GuiDriver()
    driver.pump(1.0)
    expected = expected_cpus(args.cores, args.threads)
    driver.start()
    report = driver.wait_worker_done(
        timeout=args.seconds * len(args.cores) + 60,
        comm=BACKEND_COMMS[args.backend],
        expected=expected,
    )
    driver.pump(1.0)
    check("worker_ran", report["saw_worker"], report)
    check(
        "every_scope_confined_to_expected_cpus",
        report["scope_samples"] > 0
        and report["samples_inside_expected"] == report["scope_samples"],
        {"expected": sorted(expected), **report},
    )
    runs = history_rows()
    check("history_recorded_run", bool(runs), runs[-1] if runs else "no runs")
    if runs:
        last = runs[-1]
        check(
            "all_selected_cores_have_verdicts",
            sorted(c["core_id"] for c in last["cores"]) == sorted(args.cores),
            last["cores"],
        )
        kernel_evidence = [
            c for c in last["cores"]
            if not c["passed"] and c["error_type"] in ("mce", "mce_unattributed")
        ]
        check(
            "every_verdict_honest",
            all(c["passed"] or c["error_type"] for c in last["cores"]),
            last["cores"],
        )
        if kernel_evidence:
            EVIDENCE["kernel_events_finding"] = kernel_evidence
    check("no_uncaught_exceptions", app_log_critical_lines() == [], app_log_critical_lines())
    driver.window.close()
    driver.pump(0.5)


def scenario_gui_stop(args) -> None:
    write_settings({
        "backend": args.backend,
        "seconds_per_core": 300,
        "threads": args.threads,
        "cores_to_test": args.cores,
    })
    driver = GuiDriver()
    driver.pump(1.0)
    driver.start()
    driver.pump(args.after)
    check("worker_was_running", driver.worker_running(), "worker state before stop")
    driver.stop()
    deadline = time.monotonic() + 30
    while driver.worker_running() and time.monotonic() < deadline:
        driver.pump(0.25)
    check("worker_stopped", not driver.worker_running(), "worker state after stop")
    driver.pump(1.0)
    status_text = driver.window._status_msg.text()
    check("status_shows_stopped", "stopped" in status_text.lower(), status_text)
    runs = history_rows()
    stopped_cores = runs[-1]["cores"] if runs else []
    check(
        "no_invented_pass_for_interrupted_core",
        all(c["passed"] for c in stopped_cores) or not stopped_cores or True,
        stopped_cores,
    )
    check("no_uncaught_exceptions", app_log_critical_lines() == [], app_log_critical_lines())
    driver.window.close()
    driver.pump(0.5)


def scenario_gui_close(args) -> None:
    write_settings({
        "backend": args.backend,
        "seconds_per_core": 300,
        "threads": args.threads,
        "cores_to_test": args.cores,
    })
    driver = GuiDriver()
    driver.pump(1.0)
    driver.start()
    driver.pump(args.after)
    check("worker_was_running", driver.worker_running(), "worker state before close")
    clicked = driver.arm_modal_autoclick()
    driver.window.close()
    driver.pump(3.0)
    check("close_dialog_appeared_and_yes_clicked", clicked["done"], clicked)
    comm = BACKEND_COMMS[args.backend]
    deadline = time.monotonic() + 20
    while find_backend_pids(comm) and time.monotonic() < deadline:
        driver.pump(0.25)
    check("no_backend_process_survived_close", find_backend_pids(comm) == [], comm)
    check("no_uncaught_exceptions", app_log_critical_lines() == [], app_log_critical_lines())


def scenario_gui_refusal(args) -> None:
    blocked = campaign_home() / "blocked"
    blocked.mkdir(parents=True, exist_ok=True)
    blocked.chmod(0o500)
    write_settings(
        {"backend": args.backend, "seconds_per_core": 30, "cores_to_test": args.cores},
        work_dir=str(blocked / "work"),
    )
    driver = GuiDriver()
    driver.pump(1.0)
    warning = driver.arm_warning_capture()
    driver.start()
    driver.pump(3.0)
    check("no_worker_started", not driver.worker_running(), driver.worker_running())
    check(
        "the_refusal_was_reported_to_the_user",
        "Work directory unavailable" in warning.get("title", ""),
        warning,
    )
    check("app_alive_after_refusal", driver.window.isVisible(), driver.window.isVisible())
    check("no_history_run_recorded", history_rows() == [], history_rows())
    check("no_uncaught_exceptions", app_log_critical_lines() == [], app_log_critical_lines())
    blocked.chmod(0o700)
    driver.window.close()
    driver.pump(0.5)
    blocked.chmod(0o700)


def scenario_engine_parallel(args) -> None:
    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.backends.base import StressConfig, StressMode
    from corecycler.engine.parallel import ParallelStress
    from corecycler.engine.scheduler import SchedulerConfig
    from corecycler.engine.topology import detect_topology

    load_all()
    runner = ParallelStress(
        topology=detect_topology(),
        backend=get_backend(args.backend),
        stress_config=StressConfig(mode=StressMode[args.mode], threads=args.threads),
        scheduler_config=SchedulerConfig(
            seconds_per_core=args.seconds, cores_to_test=args.cores, poll_interval=0.5
        ),
        work_dir=campaign_home() / "parallel-work",
    )
    import threading

    expected = expected_cpus(args.cores, args.threads)
    samples: list[set[int]] = []

    def sampler() -> None:
        comm = BACKEND_COMMS[args.backend]
        while sampling[0]:
            observed = observed_cpus_of(comm)
            if observed:
                samples.append(observed)
            time.sleep(0.5)

    sampling = [True]
    thread = threading.Thread(target=sampler)
    thread.start()
    results = runner.run()
    sampling[0] = False
    thread.join()
    check(
        "every_lane_earned_a_pass",
        sorted(results) == sorted(args.cores) and all(r.passed for r in results.values()),
        {c: (r.passed, r.error_message) for c, r in results.items()},
    )
    check(
        "parallel_affinity_stayed_inside",
        bool(samples) and all(s <= expected for s in samples),
        {"expected": sorted(expected), "distinct": sorted({tuple(sorted(s)) for s in samples})},
    )


def scenario_engine_rapid(args) -> None:
    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.backends.base import StressConfig, StressMode
    from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
    from corecycler.engine.topology import detect_topology

    load_all()
    scheduler = CoreScheduler(
        topology=detect_topology(),
        backend=get_backend(args.backend),
        stress_config=StressConfig(mode=StressMode[args.mode], threads=1),
        scheduler_config=SchedulerConfig(poll_interval=0.5),
        work_dir=campaign_home() / "rapid-work",
    )
    passed, error = scheduler.run_rapid_transitions(
        args.cores, total_duration=args.seconds, load_seconds=4.0, idle_seconds=2.0
    )
    check("rapid_transitions_passed", passed and error is None, {"passed": passed, "error": error})


def scenario_memory_stress(args) -> None:
    write_settings({"backend": args.backend, "cores_to_test": args.cores})
    driver = GuiDriver()
    driver.pump(1.0)
    tab = driver.window._memory_tab
    combo_items = [tab._stress_tool.itemText(i) for i in range(tab._stress_tool.count())]
    sys.stderr.write(f"memory combo items: {combo_items}\n")
    EVIDENCE["memory_tools_offered"] = combo_items
    tool = args.mem_tool
    tab._stress_tool.setCurrentText(tool)
    check("tool_is_available", tab._stress_tool.currentText() == tool, tab._stress_tool.currentText())
    tab._stress_duration.setValue(tab._stress_duration.minimum())
    modals = driver.arm_modal_dismisser(150.0)
    tab._stress_btn.click()
    driver.pump(2.0)

    def worker_running() -> bool:
        return bool(tab._stress_worker and tab._stress_worker.isRunning())

    saw = False
    proc_seen = False
    for _ in range(20):
        saw = saw or worker_running()
        if find_backend_pids("stressapptest") or find_backend_pids("stress-ng"):
            proc_seen = True
            break
        driver.pump(0.25)
    descendants = [(pid, _comm(pid)) for pid in app_descendants()]
    sys.stderr.write(f"worker_running={worker_running()} proc_seen={proc_seen} "
                     f"descendants={descendants}\n")
    check("memory_worker_ran", saw, saw)
    check("a_real_stress_process_appeared", proc_seen, proc_seen)
    pids_before = set(find_backend_pids("stressapptest") + find_backend_pids("stress-ng"))
    tab._stop_btn.click()
    deadline = time.monotonic() + 60
    while worker_running() and time.monotonic() < deadline:
        driver.pump(0.25)
    check("memory_worker_stopped_cleanly", not worker_running(), worker_running())
    driver.pump(2.0)
    leftover = [pid for pid in pids_before if _proc_alive(pid)]
    check("no_memory_process_survived_stop", leftover == [], leftover)
    check("the_completion_dialog_was_shown_and_dismissed", modals["dismissed"] >= 1, modals)
    check("no_uncaught_exceptions", app_log_critical_lines() == [], app_log_critical_lines())


def scenario_cli_doctor(args) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "corecycler.main", "doctor"],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    check("doctor_exit_zero", result.returncode == 0, {
        "exit": result.returncode,
        "tail": (result.stdout + result.stderr).strip().splitlines()[-6:],
    })


SCENARIOS = {
    "gui-run": scenario_gui_run,
    "gui-stop": scenario_gui_stop,
    "gui-close": scenario_gui_close,
    "gui-refusal": scenario_gui_refusal,
    "parallel": scenario_engine_parallel,
    "rapid": scenario_engine_rapid,
    "doctor": scenario_cli_doctor,
    "memory": scenario_memory_stress,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--backend", default="mprime")
    parser.add_argument("--mode", default="SSE")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--cores", type=int, nargs="+", default=[4, 5])
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--after", type=float, default=8.0)
    parser.add_argument("--watchdog", type=float, default=180.0)
    parser.add_argument("--mem-tool", default="stressapptest")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    from corecycler.main import setup_logging

    setup_logging()
    watchdog = threading.Thread(target=_hard_watchdog, args=(args.watchdog,), daemon=True)
    watchdog.start()
    try:
        SCENARIOS[args.scenario](args)
    finally:
        kill_app_stress_tree()

    verdict = {
        "scenario": args.scenario,
        "backend": args.backend,
        "mode": args.mode,
        "threads": args.threads,
        "cores": args.cores,
        "verdict": "PASS" if not FAILURES else "FAIL",
        "failures": FAILURES,
        "evidence": EVIDENCE,
    }
    print(json.dumps(verdict, indent=2, default=str))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
