"""Export test run history to JSON and CSV formats."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from tuner.state import TunerPhase

if TYPE_CHECKING:
    from history.db import HistoryDB


@dataclass(slots=True)
class ExportSettings:
    include_events: bool = True
    include_telemetry: bool = False
    format: str = "json"  # "json" or "csv"


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------


def export_run_json(
    db: HistoryDB,
    run_id: int,
    *,
    include_events: bool = True,
    include_telemetry: bool = False,
) -> str:
    """Export a single run as a structured JSON string."""
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    data: dict = {
        "run": asdict(run),
        "core_results": [asdict(r) for r in db.get_core_results(run_id)],
    }

    # Include tuning context if available
    if run.context_id is not None:
        ctx = db.get_context(run.context_id)
        if ctx is not None:
            data["tuning_context"] = asdict(ctx)

    if include_events:
        data["events"] = [asdict(e) for e in db.get_events(run_id)]
    if include_telemetry:
        data["telemetry"] = [asdict(s) for s in db.get_telemetry(run_id)]

    return json.dumps(data, indent=2, default=str)


def export_run_json_file(
    db: HistoryDB,
    run_id: int,
    path: Path,
    *,
    include_events: bool = True,
    include_telemetry: bool = False,
) -> None:
    text = export_run_json(
        db,
        run_id,
        include_events=include_events,
        include_telemetry=include_telemetry,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

_CSV_COLUMNS = [
    "run_id",
    "run_started_at",
    "run_status",
    "cpu_model",
    "bios_version",
    "backend",
    "stress_mode",
    "fft_preset",
    "seconds_per_core",
    "cycle_count",
    "core_id",
    "ccd",
    "cycle",
    "passed",
    "error_message",
    "error_type",
    "elapsed_seconds",
    "iterations_completed",
    "peak_freq_mhz",
    "max_temp_c",
    "min_vcore_v",
    "max_vcore_v",
]


def export_run_csv(db: HistoryDB, run_id: int) -> str:
    """Export a single run as flat per-core CSV rows with run metadata columns."""
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    results = db.get_core_results(run_id)
    return _build_csv(run, results)


def export_run_csv_file(db: HistoryDB, run_id: int, path: Path) -> None:
    text = export_run_csv(db, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def export_runs_bulk_csv(db: HistoryDB, run_ids: list[int]) -> str:
    """Export multiple runs in one CSV for comparison."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS)
    writer.writeheader()

    for run_id in run_ids:
        run = db.get_run(run_id)
        if run is None:
            continue
        results = db.get_core_results(run_id)
        for rec in results:
            writer.writerow(_make_csv_row(run, rec))

    return buf.getvalue()


def export_runs_bulk_csv_file(db: HistoryDB, run_ids: list[int], path: Path) -> None:
    text = export_runs_bulk_csv(db, run_ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_csv(run, results) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for rec in results:
        writer.writerow(_make_csv_row(run, rec))
    return buf.getvalue()


def _make_csv_row(run, rec) -> dict:
    return {
        "run_id": run.id,
        "run_started_at": run.started_at,
        "run_status": run.status,
        "cpu_model": run.cpu_model,
        "bios_version": run.bios_version,
        "backend": run.backend,
        "stress_mode": run.stress_mode,
        "fft_preset": run.fft_preset,
        "seconds_per_core": run.seconds_per_core,
        "cycle_count": run.cycle_count,
        "core_id": rec.core_id,
        "ccd": rec.ccd,
        "cycle": rec.cycle,
        "passed": rec.passed,
        "error_message": rec.error_message,
        "error_type": rec.error_type,
        "elapsed_seconds": rec.elapsed_seconds,
        "iterations_completed": rec.iterations_completed,
        "peak_freq_mhz": rec.peak_freq_mhz,
        "max_temp_c": rec.max_temp_c,
        "min_vcore_v": rec.min_vcore_v,
        "max_vcore_v": rec.max_vcore_v,
    }


# ---------------------------------------------------------------------------
# Tuner profile export / import
# ---------------------------------------------------------------------------

_IMPORTABLE_PHASES = {TunerPhase.CONFIRMED, TunerPhase.HARDENED}


def export_tuner_profile(db: HistoryDB, session_id: int) -> str:
    """Export confirmed/hardened CO offsets from a tuner session as JSON."""
    session = db.get_tuner_session(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")
    states = db.get_tuner_core_states(session_id)
    profile = {
        str(cs.core_id): cs.best_offset
        for cs in states.values()
        if cs.phase in _IMPORTABLE_PHASES and cs.best_offset is not None
    }
    config = json.loads(session.config_json) if session.config_json else {}
    hardening_tiers = config.get("hardening_tiers", [])
    tiers_passed = [f"{t['stress_mode']}:{t['fft_preset']}" for t in hardening_tiers]
    has_hardened = any(cs.phase == TunerPhase.HARDENED for cs in states.values())
    data = {
        "cpu_model": session.cpu_model,
        "core_count": len(profile),
        "bios_version": session.bios_version,
        "source_session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "primary_backend": config.get("backend", "mprime"),
        "primary_mode": config.get("stress_mode", "SSE"),
        "primary_fft": config.get("fft_preset", "SMALL"),
        "hardened": has_hardened,
        "hardening_tiers_passed": tiers_passed if has_hardened else [],
        "profile": profile,
    }
    return json.dumps(data, indent=2)


def parse_tuner_profile(json_str: str) -> dict:
    """Parse a tuner profile JSON string into a dict with int core keys."""
    data = json.loads(json_str)
    profile = {int(k): int(v) for k, v in data.get("profile", {}).items()}
    return {
        "profile": profile,
        "cpu_model": data.get("cpu_model", ""),
        "core_count": data.get("core_count", len(profile)),
        "bios_version": data.get("bios_version", ""),
        "hardened": data.get("hardened", False),
        "source_session_id": data.get("source_session_id"),
    }


def validate_tuner_profile_import(
    profile_data: dict,
    system_core_count: int,
    system_cpu_model: str,
) -> list[dict]:
    """Validate imported profile against current system. Returns list of {level, message}."""
    messages = []
    imported_max_core = max(profile_data["profile"].keys()) + 1 if profile_data["profile"] else 0
    if profile_data.get("core_count", 0) > system_core_count or imported_max_core > system_core_count:
        messages.append({
            "level": "error",
            "message": (
                f"Core count mismatch: profile has {profile_data.get('core_count', imported_max_core)}"
                f" cores, system has {system_core_count}"
            ),
        })
    if profile_data.get("cpu_model") and profile_data["cpu_model"] != system_cpu_model:
        messages.append({
            "level": "warning",
            "message": (
                f"CPU model mismatch: profile='{profile_data['cpu_model']}',"
                f" system='{system_cpu_model}'"
            ),
        })
    if not profile_data.get("profile"):
        messages.append({
            "level": "error",
            "message": "Profile contains no confirmed cores",
        })
    return messages
