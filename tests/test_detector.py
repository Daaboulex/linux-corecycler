"""Kernel records: the parser, the /dev/kmsg reader and the journal harvest.

The AMD Zen fixtures below are REAL kernel lines, verbatim.
"""

from __future__ import annotations

import errno
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from corecycler.engine.detector import (
    CoreMachineCheck,
    ErrorDetector,
    KernelTrouble,
    UnblamedMachineCheck,
    _iso_to_journal_since,
    bank_is_core,
    harvest_kernel_mce,
    kernel_log_problem,
    last_boot_ended_cleanly,
    parse_kernel_records,
)

ZEN_BLOCK_HEADER = "mce: [Hardware Error]: Machine check events logged"
ZEN_SEVERITY = "[Hardware Error]: Corrected error, no action required."
ZEN_BLOCK_STATUS = (
    "[Hardware Error]: CPU:9 (1a:44:0) MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc204000000d0175"
)
ZEN_BLOCK_DETAILS = [
    "[Hardware Error]: Error Addr: 0x00000002a90b5060",
    "[Hardware Error]: IPID: 0x000000b000000000, Syndrome: 0x000000021a17010a",
    "[Hardware Error]: Load Store Unit Ext. Error Code: 13",
    "[Hardware Error]: cache level: L1, tx: DATA, mem-tx: EV",
]
ZEN_BLOCK = [ZEN_BLOCK_HEADER, ZEN_SEVERITY, ZEN_BLOCK_STATUS, *ZEN_BLOCK_DETAILS]
LONE_DECODER_RECORD = [ZEN_SEVERITY, ZEN_BLOCK_STATUS, *ZEN_BLOCK_DETAILS[1:]]
ZEN_UNCORRECTED_STATUS = (
    "[Hardware Error]: CPU:12 (1a:44:0) MC1_STATUS[Over|UE|MiscV|AddrV|PCC|-|SyndV|-|-|-|-]: 0xbc00000000010135"
)
ZEN_UNCORRECTED_BLOCK = [
    "[Hardware Error]: System Fatal error.",
    ZEN_UNCORRECTED_STATUS,
    "[Hardware Error]: IPID: 0x000100b000000000",
    "[Hardware Error]: Instruction Fetch Unit Ext. Error Code: 5",
]
UMC_BLOCK = [
    ZEN_SEVERITY,
    "[Hardware Error]: CPU:0 (19:61:2) MC21_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc2040000400011b",
    "[Hardware Error]: Error Addr: 0x0000000a1b2c3d40",
    "[Hardware Error]: IPID: 0x0000009600050f00, Syndrome: 0x0000000000000001",
    "[Hardware Error]: Unified Memory Controller Ext. Error Code: 0",
    "EDAC MC0: 1 CE Cannot decode normalized address on mc#0csrow#3channel#0 (csrow:3 channel:0)",
]
L3_DEFERRED_BLOCK = [
    "[Hardware Error]: Deferred error, no action required.",
    "[Hardware Error]: CPU:0 (19:50:0) MC11_STATUS[-|-|-|AddrV|PCC|-|SyndV|UECC|Deferred|Poison|Scrub]: "
    "0x8724bb0800000000",
    "[Hardware Error]: IPID: 0x000700b020150000, Syndrome: 0x0000000000000000",
    "[Hardware Error]: L3 Cache Ext. Error Code: 6",
]
FALLBACK_RECORD = [
    "mce: [Hardware Error]: CPU 7: Machine Check: 0 Bank 5: 9c20000000000108",
    "mce: [Hardware Error]: RIP !INEXACT! 33:<00007f4a2b3c4d5e>",
    "mce: [Hardware Error]: TSC 0 ADDR 1ffff8ae4b190 MISC d012000000000000 SYND 4d000000 IPID 500b000000000 ",
    "mce: [Hardware Error]: PROCESSOR 2:a20f12 TIME 1700000000 SOCKET 0 APIC e microcode a20120a",
]
SOFT_LOCKUP = [
    "watchdog: BUG: soft lockup - CPU#5 stuck for 22s! [mprime:4321]",
    "RIP: 0010:native_queued_spin_lock_slowpath+0x6e/0x2e0",
    "Call Trace:",
]


def _shape(event) -> tuple:
    match event:
        case CoreMachineCheck():
            return ("core", event.cpu, event.bank, event.corrected)
        case UnblamedMachineCheck():
            return ("unblamed", event.logged_by_cpu, event.bank, event.corrected)
        case KernelTrouble():
            return ("trouble", event.message)


def _parse(lines: list[str], *, final: bool = True) -> list[tuple]:
    events, _carry = parse_kernel_records([(1.0, line) for line in lines], final=final)
    return [_shape(event) for event in events]


def _kmsg(lines: list[str], *, first_seq: int, prio: int = 4, usec: int = 1234567890) -> str:
    return "".join(f"{prio},{first_seq + n},{usec},-;{line}\n" for n, line in enumerate(lines))


def _append(path, text: str) -> None:
    with path.open("a") as log:
        log.write(text)


@pytest.fixture
def kmsg(tmp_path):
    path = tmp_path / "kmsg"
    path.write_text(_kmsg(["Linux version 7.2.9", *ZEN_BLOCK], first_seq=1, usec=900))
    return path


@pytest.fixture
def detector(kmsg):
    reader = ErrorDetector(kmsg)
    reader.reset()
    yield reader
    reader.close()


class _Stub:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)

    def read(self, _size: int):
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, OSError):
            raise answer
        return answer

    def close(self) -> None:
        self.answers = []


class _AlwaysOverruns:
    def __init__(self) -> None:
        self.reads = 0

    def read(self, _size: int):
        self.reads += 1
        if self.reads > 100:
            raise AssertionError("the reader kept reading a log that only overruns")
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def close(self) -> None:
        self.reads = 0


class TestRecordParser:
    def test_a_decoder_record_is_one_event_graded_by_its_uc_bit(self):
        assert _parse(ZEN_BLOCK) == [("core", 9, 0, True)]

    def test_an_uncorrected_decoder_record_is_graded_by_its_uc_bit(self):
        assert _parse(ZEN_UNCORRECTED_BLOCK) == [("core", 12, 1, False)]

    def test_a_fallback_record_is_one_event_graded_by_its_uc_bit(self):
        assert _parse(FALLBACK_RECORD) == [("core", 7, 5, True)]

    def test_a_firmware_bug_threshold_line_is_not_a_machine_check(self):
        lines = [
            "mce: [Firmware Bug]: cpu 3, failed to setup threshold interrupt for bank 4, block 0 "
            "(MSRC0002004=0x0000000000000000)",
            "mce: [Firmware Bug]: Your BIOS is not setting up LVT offset 0x2 for deferred error IRQs correctly.",
        ]
        assert _parse(lines) == []

    def test_lookalikes_are_not_events(self):
        lines = [
            "traps: prime95[4321] trap invalid opcode ip:7f0000001000 sp:7ffd00000000 error:0 in prime95[1000+2000]",
            "traps: mprime[4321] general protection fault ip:7f0000001000 sp:7ffd00000000 error:0 in libc.so.6",
            "WARNING: CPU: 3 PID: 1234 at kernel/sched/core.c:1234 update_rq_clock+0x10/0x20",
            "RIP: 0010:update_rq_clock+0x10/0x20",
            "Call Trace:",
            "[Firmware Bug]: TSC ADJUST: CPU3: -1234 force to 0",
            "TSC ADJUST differs within socket(s), fixing all errors",
            "tsc: Marking TSC unstable due to clocksource watchdog",
            "clocksource: timekeeping watchdog on CPU3: Marking clocksource 'tsc' as unstable because the skew is "
            "too large:",
            "mce: CPU supports 32 MCE banks",
            ZEN_BLOCK_HEADER,
            "usb 1-1: new device",
        ]
        assert _parse(lines) == []

    def test_a_soft_lockup_is_one_kernel_trouble_event(self):
        assert _parse(SOFT_LOCKUP) == [("trouble", SOFT_LOCKUP[0])]

    @pytest.mark.parametrize(
        "line",
        [
            "watchdog: CPU4: Watchdog detected hard LOCKUP on cpu 5",
            "NMI watchdog: Watchdog detected hard LOCKUP on cpu 5",
            "Kernel panic - not syncing: Fatal machine check",
            "Oops: general protection fault, probably for non-canonical address 0xdead: 0000 [#1] SMP NOPTI",
            "kernel BUG at mm/slub.c:4321!",
        ],
    )
    def test_each_trouble_anchor_is_kernel_trouble(self, line):
        assert _parse([line]) == [("trouble", line)]

    def test_a_shared_bank_blames_no_core(self):
        assert _parse(UMC_BLOCK) == [("unblamed", 0, 21, True)]

    def test_an_l3_record_blames_no_core(self):
        assert _parse(L3_DEFERRED_BLOCK) == [("unblamed", 0, 11, False)]

    def test_a_record_with_no_bank_type_blames_no_core(self):
        assert _parse([ZEN_SEVERITY, ZEN_BLOCK_STATUS, "[Hardware Error]: Error Addr: 0x00000002a90b5060"]) == [
            ("unblamed", 9, 0, True)
        ]

    def test_the_bank_name_decides_without_an_ipid(self):
        assert _parse([ZEN_BLOCK_STATUS, "[Hardware Error]: Execution Unit Ext. Error Code: 0"]) == [
            ("core", 9, 0, True)
        ]

    def test_a_deferred_error_on_a_core_bank_is_never_corrected(self):
        deferred = ZEN_BLOCK_STATUS.replace("0xdc204000000d0175", "0xdc205000000d0175")
        assert _parse([deferred, ZEN_BLOCK_DETAILS[1]]) == [("core", 9, 0, False)]

    def test_records_in_a_row_are_one_event_each(self):
        assert _parse([*ZEN_BLOCK, *UMC_BLOCK, *SOFT_LOCKUP]) == [
            ("core", 9, 0, True),
            ("unblamed", 0, 21, True),
            ("trouble", SOFT_LOCKUP[0]),
        ]

    def test_detail_lines_without_their_record_are_not_events(self):
        assert _parse([ZEN_SEVERITY, *ZEN_BLOCK_DETAILS]) == []

    def test_an_unrelated_line_inside_a_record_does_not_close_it(self):
        unrelated = "usb 1-1: new high-speed USB device number 2 using xhci_hcd"
        assert _parse([ZEN_SEVERITY, ZEN_BLOCK_STATUS, unrelated, *ZEN_BLOCK_DETAILS[1:]]) == [("core", 9, 0, True)]

    def test_a_severity_line_closes_the_record_before_it(self):
        assert _parse([*ZEN_UNCORRECTED_BLOCK, ZEN_SEVERITY], final=False) == [("core", 12, 1, False)]

    def test_an_open_record_is_carried_until_the_final_parse(self):
        lines = [(2.5, line) for line in [ZEN_SEVERITY, ZEN_BLOCK_STATUS, ZEN_BLOCK_DETAILS[0]]]
        events, carry = parse_kernel_records(lines, final=False)
        assert events == []
        assert carry == lines[1:]
        events, carry = parse_kernel_records([*carry, (2.6, ZEN_BLOCK_DETAILS[1])], final=True)
        assert [_shape(event) for event in events] == [("core", 9, 0, True)]
        assert events[0].raw_ts == 2.5
        assert events[0].message == ZEN_BLOCK_STATUS
        assert carry == []


class TestBankClassification:
    @pytest.mark.parametrize(
        ("hwid", "mcatype", "core"),
        [
            (0xB0, 0x0, True),
            (0xB0, 0x10, True),
            (0xB0, 0x1, True),
            (0xB0, 0x2, True),
            (0xB0, 0x3, True),
            (0xB0, 0x5, True),
            (0xB0, 0x6, True),
            (0xB0, 0x4, False),
            (0xB0, 0x7, False),
            (0x2E, 0x0, False),
            (0x2E, 0x1, False),
            (0x96, 0x0, False),
            (0x96, 0x1, False),
            (0x05, 0x0, False),
            (0x00, 0x0, False),
        ],
    )
    def test_the_ipid_bank_type_decides(self, hwid, mcatype, core):
        assert bank_is_core((((mcatype << 16) | hwid) << 32) | 0x1234, None) is core

    @pytest.mark.parametrize(
        ("name", "core"),
        [
            ("Load Store Unit", True),
            ("Instruction Fetch Unit", True),
            ("L2 Cache", True),
            ("Decode Unit", True),
            ("Execution Unit", True),
            ("Floating Point Unit", True),
            ("L3 Cache", False),
            ("Coherent Slave", False),
            ("Unified Memory Controller", False),
            ("Unified Memory Controller v2", False),
            ("Reserved", False),
        ],
    )
    def test_the_bank_name_decides_without_one(self, name, core):
        assert bank_is_core(None, name) is core

    def test_the_ipid_outranks_the_bank_name(self):
        assert bank_is_core(0x0000009600050F00, "Load Store Unit") is False

    def test_nothing_known_blames_no_core(self):
        assert bank_is_core(None, None) is False


class TestKmsgReader:
    def test_records_after_reset_are_read_at_any_uptime(self, kmsg, detector):
        _append(kmsg, _kmsg([*ZEN_BLOCK, SOFT_LOCKUP[0]], first_seq=100, usec=1234567890))
        read = detector.read_new()
        assert read.problem is None
        assert [_shape(event) for event in read.events] == [("core", 9, 0, True), ("trouble", SOFT_LOCKUP[0])]
        assert read.events[0].raw_ts == pytest.approx(1234.56789)

    def test_the_read_after_a_test_is_never_rate_limited(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK, first_seq=100))
        first = detector.read_new(final=True)
        _append(kmsg, _kmsg(ZEN_UNCORRECTED_BLOCK, first_seq=200))
        second = detector.read_new(final=True)
        assert [_shape(event) for event in first.events] == [("core", 9, 0, True)]
        assert [_shape(event) for event in second.events] == [("core", 12, 1, False)]

    def test_each_record_is_read_once(self, kmsg, detector):
        _append(kmsg, _kmsg(SOFT_LOCKUP[:1], first_seq=100))
        assert len(detector.read_new().events) == 1
        assert detector.read_new(final=True).events == ()

    def test_a_record_split_across_reads_is_one_event(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK[:4], first_seq=100))
        assert detector.read_new().events == ()
        _append(kmsg, _kmsg(ZEN_BLOCK[4:6], first_seq=104))
        assert [_shape(event) for event in detector.read_new(final=True).events] == [("core", 9, 0, True)]

    def test_a_record_split_across_reads_is_reported_by_the_read_that_completes_it(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK[:4], first_seq=100))
        assert detector.read_new().events == ()
        _append(kmsg, _kmsg(ZEN_BLOCK[4:], first_seq=104))
        assert [_shape(event) for event in detector.read_new().events] == [("core", 9, 0, True)]

    @pytest.mark.parametrize("last_line", [ZEN_BLOCK_DETAILS[-1], "[Hardware Error]: internal: HWA"])
    def test_a_lone_decoder_record_is_reported_by_the_read_that_ends_it(self, kmsg, detector, last_line):
        _append(kmsg, _kmsg([*LONE_DECODER_RECORD[:-1], last_line], first_seq=100))
        assert [_shape(event) for event in detector.read_new().events] == [("core", 9, 0, True)]
        assert detector.read_new(final=True).events == ()

    def test_a_lone_fallback_record_is_reported_by_the_read_that_ends_it(self, kmsg, detector):
        _append(kmsg, _kmsg(FALLBACK_RECORD, first_seq=100))
        assert [_shape(event) for event in detector.read_new().events] == [("core", 7, 5, True)]
        assert detector.read_new(final=True).events == ()

    def test_lost_records_make_the_read_incomplete(self, detector):
        detector.close()
        lockup = _kmsg(SOFT_LOCKUP[:1], first_seq=9).encode()
        detector._file = _Stub(BrokenPipeError(errno.EPIPE, "Broken pipe"), lockup)
        read = detector.read_new()
        assert read.problem == "kernel log overran; records lost"
        assert [_shape(event) for event in read.events] == [("trouble", SOFT_LOCKUP[0])]

    def test_a_log_that_only_overruns_ends_the_read(self, detector):
        detector.close()
        detector._file = _AlwaysOverruns()
        assert detector.read_new().problem == "kernel log overran; records lost"

    def test_an_unreadable_kernel_log_is_a_problem_not_silence(self, tmp_path):
        reader = ErrorDetector(tmp_path / "absent")
        reader.reset()
        read = reader.read_new(final=True)
        assert read.events == ()
        assert "absent" in read.problem

    def test_a_failed_read_is_a_problem(self, detector):
        detector.close()
        detector._file = _Stub(OSError(errno.EIO, "Input/output error"))
        read = detector.read_new()
        assert "Input/output error" in read.problem

    def test_a_kernel_log_that_cannot_seek_is_a_problem(self, tmp_path):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        reader = ErrorDetector(fifo)
        reader.reset()
        assert "fifo" in reader.read_new().problem

    def test_a_read_before_reset_is_a_problem(self, kmsg):
        read = ErrorDetector(kmsg).read_new()
        assert read.events == ()
        assert "reset" in read.problem

    def test_a_malformed_record_is_a_problem(self, kmsg, detector):
        _append(kmsg, "not a kmsg record\n" + _kmsg(SOFT_LOCKUP[:1], first_seq=100))
        read = detector.read_new()
        assert "not a kmsg record" in read.problem
        assert len(read.events) == 1

    def test_a_user_written_record_cannot_convict_a_core(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK, first_seq=100, prio=12) + _kmsg(SOFT_LOCKUP[:1], first_seq=200, prio=14))
        read = detector.read_new(final=True)
        assert read.problem is None
        assert read.events == ()

    def test_a_low_priority_kernel_record_is_still_read(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK, first_seq=100, prio=7))
        assert len(detector.read_new(final=True).events) == 1

    def test_dictionary_continuation_lines_are_skipped(self, kmsg, detector):
        _append(kmsg, _kmsg(SOFT_LOCKUP[:1], first_seq=100) + " SUBSYSTEM=cpu\n DEVICE=+cpu:cpu5\n")
        read = detector.read_new()
        assert read.problem is None
        assert len(read.events) == 1

    def test_reset_drops_a_carried_record_and_starts_at_the_end(self, kmsg, detector):
        _append(kmsg, _kmsg(ZEN_BLOCK[:4], first_seq=100))
        assert detector.read_new().events == ()
        detector.reset()
        _append(kmsg, _kmsg(ZEN_BLOCK[4:], first_seq=104))
        assert detector.read_new(final=True).events == ()

    def test_close_twice_is_harmless(self, detector):
        detector.close()
        detector.close()
        assert "reset" in detector.read_new().problem


JOURNAL_FIXTURE = (
    "1789586704.004237 ryzen-9950x3d kernel: mce: [Hardware Error]: Machine check events logged\n"
    "1789586704.007652 ryzen-9950x3d kernel: [Hardware Error]: Corrected error, no action required.\n"
    f"1789586704.007690 ryzen-9950x3d kernel: {ZEN_BLOCK_STATUS}\n"
    "1789586704.010577 ryzen-9950x3d kernel: [Hardware Error]: Error Addr: 0x00000002a90b5060\n"
    "1789586704.010600 ryzen-9950x3d kernel: [Hardware Error]: IPID: 0x000000b000000000, Syndrome: 0x000000021a17010a\n"
    f"1789586855.556237 ryzen-9950x3d kernel: {ZEN_UNCORRECTED_STATUS}\n"
    "1789586900.000001 ryzen-9950x3d systemd[1]: Started some service.\n"
)


def _completed(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


class TestHarvestKernelMCE:
    def test_parses_records_across_the_journal(self):
        with patch("subprocess.run", return_value=_completed(JOURNAL_FIXTURE)):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert ok is True
        assert [_shape(event) for event in events] == [("core", 9, 0, True), ("unblamed", 12, 1, False)]
        assert events[0].raw_ts == pytest.approx(1789586704.007690)

    def test_since_is_passed_in_utc(self):
        with patch("subprocess.run", return_value=_completed("")) as mock_run:
            _, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert ok is True
        args = mock_run.call_args[0][0]
        assert args[args.index("--since") + 1] == "2026-07-16 15:47:30 UTC"

    def test_naive_timestamp_treated_as_utc(self):
        assert _iso_to_journal_since("2026-07-16T15:47:30") == "2026-07-16 15:47:30 UTC"

    def test_offset_timestamp_converted(self):
        assert _iso_to_journal_since("2026-07-16T17:47:30+02:00") == "2026-07-16 15:47:30 UTC"

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
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is False

    def test_empty_journal_is_ok_and_empty(self):
        with patch("subprocess.run", return_value=_completed("")):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is True

    def test_non_kernel_lines_ignored(self):
        out = f"1789586900.1 host systemd[1]: {SOFT_LOCKUP[0]}\n"
        with patch("subprocess.run", return_value=_completed(out)):
            events, ok = harvest_kernel_mce("2026-07-16T15:47:30+00:00")
        assert events == []
        assert ok is True

    def test_a_malformed_journal_timestamp_skips_its_line(self):
        out = f"1.2.3 host kernel: {SOFT_LOCKUP[0]}\n1700000000.5 host kernel: {SOFT_LOCKUP[0]}\n"
        with patch("subprocess.run", return_value=_completed(out)):
            events, ok = harvest_kernel_mce("2026-07-24T00:00:00")
        assert ok is True
        assert [event.raw_ts for event in events] == [1700000000.5]


class TestLastBootEndedCleanly:
    def test_clean_shutdown_detected(self):
        with patch("subprocess.run", return_value=_completed("some line\nJournal stopped\n")):
            assert last_boot_ended_cleanly() is True

    def test_pid1_shutdown_marker_detected(self):
        with patch("subprocess.run", return_value=_completed("unmounting\nShutting down.\n")):
            assert last_boot_ended_cleanly() is True

    def test_abrupt_end_is_dirty(self):
        with patch("subprocess.run", return_value=_completed("lmstudio.service: Failed with result 'exit-code'.\n")):
            assert last_boot_ended_cleanly() is False

    def test_unreadable_journal_fails_closed(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert last_boot_ended_cleanly() is False
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("journalctl", 5)):
            assert last_boot_ended_cleanly() is False
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="")):
            assert last_boot_ended_cleanly() is False


THIS_BOOT_KERNEL_LINE = "1791148722.100000 host kernel: Linux version 7.2.9\n"


class TestKernelLogProblem:
    def test_a_readable_log_and_journal_are_no_problem(self, kmsg):
        with patch("subprocess.run", return_value=_completed(THIS_BOOT_KERNEL_LINE)) as run:
            assert kernel_log_problem(kmsg) is None
        args = run.call_args[0][0]
        assert args[args.index("-b") + 1] == "0"
        assert "_TRANSPORT=kernel" in args

    def test_an_unopenable_kernel_log_is_a_problem(self, tmp_path):
        with patch("subprocess.run", return_value=_completed(THIS_BOOT_KERNEL_LINE)):
            assert "absent" in kernel_log_problem(tmp_path / "absent")

    def test_a_failing_journal_is_a_problem(self, kmsg):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="Permission denied")):
            assert "Permission denied" in kernel_log_problem(kmsg)

    def test_a_journal_with_no_kernel_record_this_boot_is_a_problem(self, kmsg):
        with patch("subprocess.run", return_value=_completed("")):
            assert "no kernel record" in kernel_log_problem(kmsg)

    @pytest.mark.parametrize(
        "failure",
        [FileNotFoundError(errno.ENOENT, "No such file or directory"), subprocess.TimeoutExpired("journalctl", 15)],
    )
    def test_a_journal_that_cannot_run_is_a_problem(self, kmsg, failure):
        with patch("subprocess.run", side_effect=failure):
            assert "journal" in kernel_log_problem(kmsg)
