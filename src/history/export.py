"""Export test run history to JSON and CSV formats."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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
