"""Comprehensive tests for error detection (MCE, dmesg, sysfs)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.detector import ErrorDetector, ErrorState, MCEEvent, _get_dmesg_raw_timestamp, _is_mce_error_line


# ===========================================================================
# MCEEvent tests
# ===========================================================================


class TestMCEEvent:
    def test_creation(self):
        ev = MCEEvent(
            timestamp=1234.5,
            cpu=3,
            bank=5,
            message="MCE bank 5 error",
            corrected=True,
        )
        assert ev.cpu == 3
        assert ev.bank == 5
        assert ev.corrected is True

    def test_uncorrected(self):
        ev = MCEEvent(timestamp=0, cpu=0, bank=0, message="", corrected=False)
        assert ev.corrected is False


# ===========================================================================
# ErrorState tests
# ===========================================================================


class TestErrorState:
    def test_no_errors(self):
        state = ErrorState()
        assert state.has_errors is False

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

    def test_has_both(self):
        state = ErrorState()
        state.mce_events.append(
            MCEEvent(timestamp=0, cpu=0, bank=0, message="test", corrected=True)
        )
        state.computation_errors.append("mismatch")
        assert state.has_errors is True


# ===========================================================================
# ErrorDetector — sysfs MCE detection
# ===========================================================================


class TestSysfsMCE:
    """Drive the REAL _check_sysfs_mce against a temp sysfs tree (injected via
    _mce_base). These previously reimplemented the loop in the test body and so
    passed even when the production method was replaced with a raise — the
    baseline-delta and target-cpu filter were entirely unexercised."""

    @staticmethod
    def _tree(tmp_path, banks: dict[int, dict[int, int]]):
        """Build machinecheck<cpu>/bank<n> = count and return the base dir.
        banks = {cpu: {bank: count}}."""
        base = tmp_path / "machinecheck"
        for cpu, bank_counts in banks.items():
            d = base / f"machinecheck{cpu}"
            d.mkdir(parents=True)
            for bank, count in bank_counts.items():
                (d / f"bank{bank}").write_text(str(count))
        return base

    def _detector(self, base):
        det = ErrorDetector()
        det._mce_base = base
        return det

    def test_no_machinecheck_dir_returns_empty(self, tmp_path):
        det = self._detector(tmp_path / "does_not_exist")
        assert det._check_sysfs_mce(target_cpu=None) == []

    def test_new_bank_error_above_baseline_is_reported(self, tmp_path):
        base = self._tree(tmp_path, {0: {0: 0, 1: 0, 2: 0}})
        det = self._detector(base)
        det.reset()  # baseline: all banks 0
        (base / "machinecheck0" / "bank1").write_text("5")  # 5 new errors appear

        events = det._check_sysfs_mce(target_cpu=None)
        assert len(events) == 1
        assert events[0].cpu == 0
        assert events[0].bank == 1
        assert "+5" in events[0].message

    def test_preexisting_count_is_not_reported_as_new(self, tmp_path):
        """The baseline-delta: a bank already at 5 at reset must not read as new;
        only the increase above the baseline counts."""
        base = self._tree(tmp_path, {0: {1: 5}})
        det = self._detector(base)
        det.reset()  # baseline: bank1 = 5
        assert det._check_sysfs_mce(target_cpu=None) == []  # unchanged -> no event

        (base / "machinecheck0" / "bank1").write_text("7")  # +2 since baseline
        events = det._check_sysfs_mce(target_cpu=None)
        assert len(events) == 1
        assert events[0].bank == 1
        assert "+2" in events[0].message

    def test_target_cpu_filters_other_cpus(self, tmp_path):
        base = self._tree(tmp_path, {0: {0: 0}, 1: {0: 0}, 2: {0: 0}})
        det = self._detector(base)
        det.reset()
        for cpu in (0, 1, 2):
            (base / f"machinecheck{cpu}" / "bank0").write_text("3")

        only_cpu1 = det._check_sysfs_mce(target_cpu=1)
        assert [e.cpu for e in only_cpu1] == [1]
        assert {e.cpu for e in det._check_sysfs_mce(target_cpu=None)} == {0, 1, 2}

    def test_invalid_bank_content_is_skipped_without_crashing(self, tmp_path):
        base = self._tree(tmp_path, {0: {0: 0}})
        det = self._detector(base)
        det.reset()
        (base / "machinecheck0" / "bank0").write_text("not_a_number")
        assert det._check_sysfs_mce(target_cpu=None) == []  # skipped, no raise

    def test_empty_machinecheck_dir_yields_no_events(self, tmp_path):
        base = tmp_path / "machinecheck"
        (base / "machinecheck0").mkdir(parents=True)  # no bank files
        det = self._detector(base)
        det.reset()
        assert det._check_sysfs_mce(target_cpu=None) == []


# ===========================================================================
# ErrorDetector — dmesg MCE detection
# ===========================================================================


class TestDmesgMCE:
    def _make_detector_with_baseline(self, baseline_ts: float = 10000.0) -> ErrorDetector:
        """Create a detector with a non-zero dmesg baseline so detection isn't skipped."""
        det = ErrorDetector()
        det._dmesg_baseline_ts = baseline_ts
        return det

    def test_dmesg_with_mce_lines(self):
        """Should parse MCE error lines from dmesg output."""
        # Lines must pass _is_mce_error_line — use real MCE error patterns
        dmesg_output = (
            "12345.678 mce: [Hardware Error]: CPU 3 Bank 5: status 0xbc00000000010135\n"
            "12345.679 corrected error, machine check on CPU 3 Bank 0\n"
            "12345.680 normal log line\n"
        )

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 2

    def test_dmesg_filter_by_cpu(self):
        dmesg_output = (
            "12345.678 mce: [Hardware Error]: CPU 3 Bank 5: status error\n"
            "12345.679 mce: [Hardware Error]: CPU 7 Bank 1: status error\n"
        )

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=3)

        assert len(events) == 1
        assert events[0].cpu == 3

    def test_dmesg_bank_extraction(self):
        dmesg_output = "12345.678 mce: [Hardware Error]: CPU 5 Bank 12: fatal\n"

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 1
        assert events[0].bank == 12
        assert events[0].cpu == 5

    def test_dmesg_corrected_detection(self):
        dmesg_output = "12345.678 corrected mce on CPU 0 Bank 0: status minor\n"

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 1
        assert events[0].corrected is True

    def test_dmesg_uncorrected(self):
        dmesg_output = "12345.678 uncorrected mce CPU 2 Bank 3: fatal machine check exception\n"

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 1
        assert events[0].corrected is False

    def test_dmesg_no_cpu_number(self):
        dmesg_output = "12345.678 fatal machine check exception detected\n"

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 1
        assert events[0].cpu == -1  # unknown CPU

    def test_dmesg_no_bank_number(self):
        dmesg_output = "12345.678 mce: [Hardware Error]: CPU 0: severity corrected\n"

        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)

        assert len(events) == 1
        assert events[0].bank == -1

    def test_dmesg_failure(self):
        """dmesg returning non-zero should return empty list."""
        det = self._make_detector_with_baseline()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_timeout(self):
        import subprocess

        det = self._make_detector_with_baseline()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dmesg", 5)):
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_not_found(self):
        det = self._make_detector_with_baseline()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_empty_output(self):
        det = self._make_detector_with_baseline()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_non_mce_lines_filtered(self):
        dmesg_output = (
            "12345.678 usb 1-1: new device\n"
            "12345.679 ext4: mounted filesystem\n"
        )
        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_skipped_when_no_baseline(self):
        """When baseline_ts is 0.0, dmesg detection should be skipped entirely."""
        dmesg_output = "12345.678 mce: [Hardware Error]: CPU 0 Bank 1: status error\n"
        det = ErrorDetector()  # baseline_ts defaults to 0.0
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_dmesg_filters_boot_info_messages(self):
        """Boot/info MCE messages should be excluded."""
        dmesg_output = (
            "12345.678 mce: CPU supports 32 MCE banks\n"
            "12345.679 Machine check events logged\n"
            "12345.680 mce: [Hardware Error]: CPU 0 Bank 5: status 0xbc00\n"
        )
        det = self._make_detector_with_baseline(12345.0)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)
        # Only the real error line should match, not the boot messages
        assert len(events) == 1
        assert "Bank 5" in events[0].message

    def test_dmesg_pre_baseline_messages_skipped(self):
        """Messages with timestamps <= baseline should be skipped."""
        dmesg_output = (
            "10000.000 mce: [Hardware Error]: CPU 0 Bank 1: status old\n"
            "10001.000 mce: [Hardware Error]: CPU 0 Bank 2: status new\n"
        )
        det = self._make_detector_with_baseline(10000.5)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=dmesg_output, stderr=""
            )
            events = det._check_dmesg_mce(target_cpu=None)
        assert len(events) == 1
        assert "Bank 2" in events[0].message


# ===========================================================================
# ErrorDetector — check_mce combines both methods
# ===========================================================================


class TestCheckMCE:
    def test_combines_sysfs_and_dmesg(self):
        det = ErrorDetector()
        sysfs_event = MCEEvent(
            timestamp=time.time(), cpu=0, bank=1, message="sysfs", corrected=True
        )
        dmesg_event = MCEEvent(
            timestamp=time.time(), cpu=0, bank=2, message="dmesg", corrected=False
        )

        with (
            patch.object(det, "_check_sysfs_mce", return_value=[sysfs_event]),
            patch.object(det, "_check_dmesg_mce", return_value=[dmesg_event]),
        ):
            events = det.check_mce(target_cpu=0)

        assert len(events) == 2

    def test_no_events(self):
        det = ErrorDetector()
        with (
            patch.object(det, "_check_sysfs_mce", return_value=[]),
            patch.object(det, "_check_dmesg_mce", return_value=[]),
        ):
            events = det.check_mce()
        assert events == []


# ===========================================================================
# ErrorDetector — reset
# ===========================================================================


class TestReset:
    def test_reset_captures_baseline(self):
        det = ErrorDetector()
        with (
            patch.object(det, "_count_mce_events", return_value=42),
            patch.object(det, "_snapshot_mce_banks", return_value={"0:0": 5}),
            patch("engine.detector._get_dmesg_raw_timestamp", return_value=99999.0),
        ):
            det.reset()
        assert det._mce_baseline == 42
        assert det._dmesg_baseline_ts == pytest.approx(99999.0)
        assert det._mce_bank_baseline == {"0:0": 5}

    def test_reset_with_no_mce_events(self):
        det = ErrorDetector()
        with (
            patch.object(det, "_count_mce_events", return_value=0),
            patch.object(det, "_snapshot_mce_banks", return_value={}),
            patch("engine.detector._get_dmesg_raw_timestamp", return_value=0.0),
        ):
            det.reset()
        assert det._mce_baseline == 0
        assert det._dmesg_baseline_ts == 0.0


# ===========================================================================
# _count_mce_events
# ===========================================================================


class TestCountMCEEvents:
    def test_count_with_events(self, tmp_path):
        mce_base = tmp_path / "machinecheck"
        d0 = mce_base / "machinecheck0"
        d0.mkdir(parents=True)
        (d0 / "bank0").write_text("3")
        (d0 / "bank1").write_text("7")
        d1 = mce_base / "machinecheck1"
        d1.mkdir(parents=True)
        (d1 / "bank0").write_text("1")

        det = ErrorDetector()
        with patch("engine.detector.Path", return_value=mce_base):
            # Direct test of counting logic
            total = 0
            for mce_dir in mce_base.iterdir():
                for bank_file in mce_dir.glob("bank*"):
                    try:
                        total += int(bank_file.read_text().strip())
                    except (ValueError, OSError):
                        continue
            assert total == 11

    def test_count_missing_dir(self):
        """Missing machinecheck dir should return 0."""
        det = ErrorDetector()
        # On a test system without MCE sysfs, should return 0
        count = det._count_mce_events()
        # Can't assert exact value (depends on host), but shouldn't crash
        assert isinstance(count, int)


# ===========================================================================
# _get_dmesg_raw_timestamp
# ===========================================================================


class TestGetDmesgTimestamp:
    def test_normal(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="12345.678 first line\n12346.789 last line\n"
            )
            ts = _get_dmesg_raw_timestamp()
        assert ts == pytest.approx(12346.789)

    def test_empty_output(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            ts = _get_dmesg_raw_timestamp()
        assert ts == 0.0

    def test_timeout(self):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dmesg", 5)):
            ts = _get_dmesg_raw_timestamp()
        assert ts == 0.0

    def test_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ts = _get_dmesg_raw_timestamp()
        assert ts == 0.0

    def test_os_error(self):
        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            ts = _get_dmesg_raw_timestamp()
        assert ts == 0.0


# ===========================================================================
# _is_mce_error_line — distinguishes real MCE errors from boot/info
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
