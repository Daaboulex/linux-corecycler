"""Tests for kernel-log error detection (MCE parsing, dmesg, journal harvest).

The AMD Zen fixtures below are REAL kernel lines captured from a Ryzen 9
9950X3D (2026-07-16) during the corrected-MCE storm that preceded a hard
freeze — the exact format the previous parser silently rejected.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.detector import (
    ErrorDetector,
    ErrorState,
    MCEEvent,
    _get_dmesg_raw_timestamp,
    _is_mce_error_line,
    _iso_to_journal_since,
    classify_mce_line,
    harvest_kernel_mce,
)

# Real AMD Zen 5 decoded MCA block (kernel: prefix stripped), one error event.
ZEN_BLOCK_HEADER = "mce: [Hardware Error]: Machine check events logged"
ZEN_BLOCK_STATUS = (
    "[Hardware Error]: CPU:9 (1a:44:0) "
    "MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc204000000d0175"
)
ZEN_BLOCK_DETAILS = [
    "[Hardware Error]: Corrected error, no action required.",
    "[Hardware Error]: Error Addr: 0x00000002a90b5060",
    "[Hardware Error]: IPID: 0x000000b000000000, Syndrome: 0x000000021a17010a",
    "[Hardware Error]: Load Store Unit Ext. Error Code: 13",
    "[Hardware Error]: cache level: L1, tx: DATA, mem-tx: EV",
]
ZEN_UNCORRECTED_STATUS = (
    "[Hardware Error]: CPU:12 (1a:44:0) "
    "MC1_STATUS[Over|UE|MiscV|AddrV|PCC|-|SyndV|-|-|-|-]: 0xbc00000000010135"
)


# ===========================================================================
# MCEEvent / ErrorState
# ===========================================================================


class TestMCEEvent:
    def test_creation(self):
        ev = MCEEvent(
            timestamp=1234.5, cpu=3, bank=5, message="MCE bank 5 error", corrected=True
        )
        assert ev.cpu == 3
        assert ev.bank == 5
        assert ev.corrected is True

    def test_uncorrected(self):
        ev = MCEEvent(timestamp=0, cpu=0, bank=0, message="", corrected=False)
        assert ev.corrected is False


class TestErrorState:
    def test_no_errors(self):
        assert ErrorState().has_errors is False

    def test_has_mce(self):
        state = ErrorState()
        state.mce_events.append(
            MCEEvent(timestamp=0, cpu=0, bank=0, message="test", corrected=True)
        )
        assert state.has_errors is True

    def test_has_computation_error(self):
        state = ErrorState()
        state.computation_errors.append("bad result")
        assert state.has_errors is True


# ===========================================================================
# classify_mce_line — the single line classifier
# ===========================================================================


class TestClassifyZenDecodedBlock:
    """One AMD decoded block must yield exactly ONE event, from the STATUS line."""

    def test_status_line_carries_cpu_bank_severity(self):
        ev = classify_mce_line(ZEN_BLOCK_STATUS)
        assert ev is not None
        assert ev.cpu == 9
        assert ev.bank == 0
        assert ev.corrected is True

    def test_uncorrected_flags(self):
        ev = classify_mce_line(ZEN_UNCORRECTED_STATUS)
        assert ev is not None
        assert ev.cpu == 12
        assert ev.bank == 1
        assert ev.corrected is False

    def test_header_line_is_not_an_event(self):
        # "Machine check events logged" announces the block; counting it would
        # double-count the status line's event (and it names no CPU).
        assert classify_mce_line(ZEN_BLOCK_HEADER) is None

    def test_detail_lines_are_not_events(self):
        for line in ZEN_BLOCK_DETAILS:
            assert classify_mce_line(line) is None, line

    def test_whole_block_yields_exactly_one_event(self):
        block = [ZEN_BLOCK_HEADER, ZEN_BLOCK_STATUS, *ZEN_BLOCK_DETAILS]
        events = [e for e in (classify_mce_line(line) for line in block) if e]
        assert len(events) == 1
        assert events[0].cpu == 9


class TestClassifyClassicFormats:
    def test_space_cpu_form(self):
        ev = classify_mce_line("mce: [Hardware Error]: CPU 3 Bank 5: status 0xbc00")
        assert ev is not None
        assert ev.cpu == 3
        assert ev.bank == 5

    def test_corrected_keyword(self):
        ev = classify_mce_line("corrected error, machine check on CPU 3 Bank 0")
        assert ev is not None
        assert ev.corrected is True

    def test_uncorrected_keyword(self):
        ev = classify_mce_line(
            "uncorrected mce CPU 2 Bank 3: fatal machine check exception"
        )
        assert ev is not None
        assert ev.corrected is False

    def test_no_cpu_number(self):
        ev = classify_mce_line("fatal machine check exception detected")
        assert ev is not None
        assert ev.cpu == -1

    def test_no_bank_number(self):
        ev = classify_mce_line("mce: [Hardware Error]: CPU 0: severity corrected")
        assert ev is not None
        assert ev.bank == -1

    def test_kernel_oops_is_unattributed_event(self):
        ev = classify_mce_line("BUG: unable to handle page fault for address")
        assert ev is not None
        assert ev.cpu == -1
        assert ev.corrected is False

    def test_boot_info_lines_are_not_events(self):
        for line in (
            "mce: CPU supports 32 MCE banks",
            "mce: using 32 MCE banks",
            "mce: CMCI storm subsided",
        ):
            assert classify_mce_line(line) is None, line

    def test_ordinary_lines_are_not_events(self):
        assert classify_mce_line("usb 1-1: new device") is None
        assert classify_mce_line("ext4: mounted filesystem") is None


# ===========================================================================
# ErrorDetector.check_mce — dmesg polling with consume-once semantics
# ===========================================================================


def _dmesg_result(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fresh_detector(baseline: float) -> ErrorDetector:
    det = ErrorDetector()
    det._dmesg_baseline_ts = baseline
    det._last_dmesg_time = 0.0
    return det


class TestCheckMCE:
    def test_zen_block_detected_from_dmesg(self):
        out = (
            f"100.10 {ZEN_BLOCK_HEADER}\n"
            f"100.11 {ZEN_BLOCK_STATUS}\n"
            + "".join(f"100.12 {line}\n" for line in ZEN_BLOCK_DETAILS)
        )
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", return_value=_dmesg_result(out)):
            events = det.check_mce()
        assert len(events) == 1
        assert events[0].cpu == 9
        assert events[0].corrected is True

    def test_events_delivered_exactly_once(self):
        out = f"100.11 {ZEN_BLOCK_STATUS}\n"
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", return_value=_dmesg_result(out)):
            first = det.check_mce()
            det._last_dmesg_time = time.monotonic() - 10  # bypass rate limit
            second = det.check_mce()
        assert len(first) == 1
        assert second == []

    def test_rate_limit_returns_empty_between_reads(self):
        out = f"100.11 {ZEN_BLOCK_STATUS}\n"
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", return_value=_dmesg_result(out)) as mock_run:
            det.check_mce()
            events = det.check_mce()  # immediately again — inside the interval
        assert events == []
        assert mock_run.call_count == 1

    def test_pre_baseline_lines_skipped(self):
        out = (
            f"99.00 {ZEN_BLOCK_STATUS}\n"
            f"100.50 {ZEN_UNCORRECTED_STATUS}\n"
        )
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", return_value=_dmesg_result(out)):
            events = det.check_mce()
        assert len(events) == 1
        assert events[0].cpu == 12

    def test_no_baseline_skips_detection(self):
        det = ErrorDetector()  # baseline 0.0
        with patch("subprocess.run", return_value=_dmesg_result("100.1 " + ZEN_BLOCK_STATUS)):
            assert det.check_mce() == []

    def test_dmesg_failure_returns_empty(self):
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            assert det.check_mce() == []

    def test_dmesg_timeout_returns_empty(self):
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dmesg", 5)):
            assert det.check_mce() == []

    def test_dmesg_missing_returns_empty(self):
        det = _fresh_detector(baseline=100.0)
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert det.check_mce() == []

    def test_reset_clears_seen_and_rebaselines(self):
        det = ErrorDetector()
        with patch(
            "engine.detector._get_dmesg_raw_timestamp", return_value=500.0
        ):
            det._seen.add((1.0, 2, 3))
            det.reset()
        assert det._dmesg_baseline_ts == pytest.approx(500.0)
        assert det._seen == set()


# ===========================================================================
# harvest_kernel_mce — cross-boot journal forensics
# ===========================================================================

JOURNAL_FIXTURE = (
    "1789586704.004237 ryzen-9950x3d kernel: mce: [Hardware Error]: Machine check events logged\n"
    "1789586704.007652 ryzen-9950x3d kernel: [Hardware Error]: Corrected error, no action required.\n"
    f"1789586704.007690 ryzen-9950x3d kernel: {ZEN_BLOCK_STATUS}\n"
    "1789586704.010577 ryzen-9950x3d kernel: [Hardware Error]: Error Addr: 0x00000002a90b5060\n"
    f"1789586855.556237 ryzen-9950x3d kernel: {ZEN_UNCORRECTED_STATUS}\n"
    "1789586900.000001 ryzen-9950x3d systemd[1]: Started some service.\n"
)


class TestHarvestKernelMCE:
    def test_parses_events_across_block(self):
        with patch("subprocess.run", return_value=_dmesg_result(JOURNAL_FIXTURE)):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert ok is True
        assert [(e.cpu, e.corrected) for e in events] == [(9, True), (12, False)]

    def test_since_is_passed_in_utc(self):
        with patch("subprocess.run", return_value=_dmesg_result("")) as mock_run:
            _, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert ok is True
        args = mock_run.call_args[0][0]
        assert args[args.index("--since") + 1] == "2026-07-16 15:47:30 UTC"

    def test_naive_timestamp_treated_as_utc(self):
        assert _iso_to_journal_since("2026-07-16T15:47:30") == "2026-07-16 15:47:30 UTC"

    def test_offset_timestamp_converted(self):
        assert (
            _iso_to_journal_since("2026-07-16T17:47:30+02:00")
            == "2026-07-16 15:47:30 UTC"
        )

    def test_bad_timestamp_fails_closed(self):
        events, ok = harvest_kernel_mce("not-a-timestamp")
        assert events == []
        assert ok is False

    def test_journalctl_missing_fails_closed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is False

    def test_journalctl_error_fails_closed(self):
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="boom"),
        ):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is False

    def test_empty_journal_is_ok_and_empty(self):
        with patch("subprocess.run", return_value=_dmesg_result("")):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is True

    def test_non_kernel_lines_ignored(self):
        out = "1789586900.1 host systemd[1]: mce: [Hardware Error]: CPU 3 Bank 5: x\n"
        with patch("subprocess.run", return_value=_dmesg_result(out)):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is True


# ===========================================================================
# _is_mce_error_line — kept for classic formats
# ===========================================================================


class TestIsMCEErrorLine:
    @pytest.mark.parametrize(
        "line",
        [
            "mce: [hardware error]: cpu 0 bank 5: status 0xbc00000000010135",
            "corrected error, machine check on cpu 3 bank 0",
            "uncorrected mce cpu 2 bank 3: fatal machine check exception",
            "fatal machine check exception detected",
            "mce: cpu 0: mca: bank 1 status 0xbe00",
            "mce: [hardware error]: cpu 5 severity: fatal",
        ],
    )
    def test_real_mce_errors_detected(self, line):
        assert _is_mce_error_line(line) is True

    @pytest.mark.parametrize(
        "line",
        [
            "mce: cpu supports 32 mce banks",
            "machine check events logged",
            "mce: using 32 mce banks",
            "mce: cmci storm subsided",
            "mce_cpu_quirks: setting threshold",
            "machine check polling timer started",
        ],
    )
    def test_boot_info_messages_excluded(self, line):
        assert _is_mce_error_line(line) is False

    def test_non_mce_lines_excluded(self):
        assert _is_mce_error_line("usb 1-1: new device") is False
        assert _is_mce_error_line("ext4: mounted filesystem") is False


# ===========================================================================
# _get_dmesg_raw_timestamp
# ===========================================================================


class TestGetDmesgTimestamp:
    def test_normal(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="12345.678 first line\n12346.789 last line\n"
            )
            assert _get_dmesg_raw_timestamp() == pytest.approx(12346.789)

    def test_bracketed_timestamp(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="[12346.789] last line\n"
            )
            assert _get_dmesg_raw_timestamp() == pytest.approx(12346.789)

    def test_empty_output(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            assert _get_dmesg_raw_timestamp() == 0.0

    def test_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dmesg", 5)):
            assert _get_dmesg_raw_timestamp() == 0.0

    def test_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _get_dmesg_raw_timestamp() == 0.0
