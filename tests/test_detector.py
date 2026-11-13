"""Kernel records: the parser, the /dev/kmsg reader and the journal harvest.

The AMD Zen fixtures below are REAL kernel lines, verbatim.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from corecycler.engine import detector as detector_mod
from corecycler.engine.detector import (
    BootEnd,
    CoreMachineCheck,
    ErrorDetector,
    HarvestFrom,
    JournalHarvest,
    KernelTrouble,
    ProgramFault,
    UnblamedMachineCheck,
    _clock_ns,
    _journalctl,
    bank_is_core,
    boot_end,
    harvest_after,
    kernel_log_problem,
    parse_kernel_records,
    read_boot_id,
    reset_reasons,
)
from tests._machine import core_bank_record

REAL_MCE_SETTLE_S = detector_mod.mce_settle_s

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
WIREPLUMBER_17_11_57 = (
    "wireplumber[2884]: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4 in "
    "libasound.so.2.0.0[5ae17,7f6d4a368000+b6000] likely on CPU 23 (core 7, socket 0)"
)
WIREPLUMBER_CODE = (
    "Code: 89 c7 e8 9a 4f ff ff 48 85 c0 74 1e 48 8b 50 10 <48> 8b 02 48 85 c0 74 12 48 8b 40 08 48 85 c0 74 09"
)


def _shape(event) -> tuple:
    match event:
        case CoreMachineCheck():
            return ("core", event.cpu, event.bank, event.corrected)
        case UnblamedMachineCheck():
            return ("unblamed", event.logged_by_cpu, event.bank, event.corrected)
        case KernelTrouble():
            return ("trouble", event.message)
        case ProgramFault():
            return ("program", event.process, event.pid, event.cpu, event.core)


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


class _NeverRunsDry:
    GIVE_UP_AFTER = 100_000

    def __init__(self, *, overruns: bool) -> None:
        self.overruns = overruns
        self.reads = 0

    def read(self, _size: int):
        self.reads += 1
        if self.reads > self.GIVE_UP_AFTER:
            raise AssertionError("the reader kept reading a log that never runs dry")
        if self.overruns and self.reads % 2:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        return _kmsg(SOFT_LOCKUP[:1], first_seq=self.reads).encode()

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
            "systemd-coredump[3001]: Process 2884 (wireplumber) of user 1000 dumped core.",
            "potentially unexpected fatal signal 11.",
            "wireplumber: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4",
            "vkd3d[123] segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4",
            "traps: Missing ENDBR: foo+0x0/0x10",
            "traps: mprime[4321] took a while",
            WIREPLUMBER_CODE,
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


class TestProgramFaults:
    @pytest.mark.req("PROGRAM_FAULT.parse")
    def test_the_17_11_57_line_reads_as_wireplumber_on_cpu_23_core_7(self):
        """The line the 17:11 freeze logged as the design records it, its elided addresses in the kernel's format."""
        events, carry = parse_kernel_records([(61917.5, WIREPLUMBER_17_11_57), (61917.5, WIREPLUMBER_CODE)], final=True)
        assert events == [
            ProgramFault(process="wireplumber", pid=2884, cpu=23, core=7, message=WIREPLUMBER_17_11_57, raw_ts=61917.5)
        ]
        assert events[0].named == "wireplumber (pid 2884), CPU 23, core 7"
        assert carry == []

    @pytest.mark.req("PROGRAM_FAULT.parse")
    @pytest.mark.parametrize(
        ("line", "fault"),
        [
            (
                "traps: prime95[4321] trap invalid opcode ip:7f0000001000 sp:7ffd00000000 error:0 in "
                "prime95[1000+2000]",
                ("program", "prime95", 4321, None, None),
            ),
            (
                "traps: mprime[4321] general protection fault ip:7f0000001000 sp:7ffd00000000 error:0 in libc.so.6",
                ("program", "mprime", 4321, None, None),
            ),
            (
                "Isolated Web Co[5021]: segfault at 10 ip 00007f3c1a2b4d5e sp 00007ffc9e8a7b10 error 6 in "
                "libxul.so[2b4d5e,7f3c18000000+5a00000] likely on CPU 3 (core 3, socket 0)",
                ("program", "Isolated Web Co", 5021, 3, 3),
            ),
            (
                "a[1] b[3001]: segfault at 0 ip 0000000000401136 sp 00007ffd2b8e6f40 error 4 "
                "likely on CPU 16 (core 0, socket 1)",
                ("program", "a[1] b", 3001, 16, 0),
            ),
            (
                "python3[77]: segfault at 0 ip 0000000000401136 sp 00007ffd2b8e6f40 error 4 in python3.14[401000+1000]",
                ("program", "python3", 77, None, None),
            ),
        ],
        ids=["trap", "general-protection", "name-with-spaces", "name-with-brackets", "no-cpu-named"],
    )
    def test_each_program_fault_line_names_its_process_and_the_cpu_and_core_the_kernel_named(self, line, fault):
        assert _parse([line]) == [fault]

    @pytest.mark.req("PROGRAM_FAULT.parse")
    def test_a_fault_naming_no_cpu_says_so(self):
        (fault,) = parse_kernel_records(
            [(1.0, "traps: foo[12] trap divide error ip:401136 sp:7ffd2b8e6f40 error:0 in foo[401000+1000]")],
            final=True,
        )[0]
        assert fault.named == "foo (pid 12), no CPU named"

    @pytest.mark.req("PROGRAM_FAULT.parse")
    def test_a_program_fault_inside_a_machine_check_record_is_its_own_event_and_leaves_the_record_open(self):
        assert _parse([ZEN_SEVERITY, ZEN_BLOCK_STATUS, WIREPLUMBER_17_11_57, *ZEN_BLOCK_DETAILS[1:]]) == [
            ("program", "wireplumber", 2884, 23, 7),
            ("core", 9, 0, True),
        ]

    @pytest.mark.req("PROGRAM_FAULT.evidence")
    def test_a_flood_of_program_faults_keeps_the_latest_and_counts_the_earlier_ones_dropped(self):
        kept = detector_mod.PROGRAM_FAULTS_KEPT
        faults = [ProgramFault("loop", n, None, None, f"traps: loop[{n}] trap int3", float(n)) for n in range(kept + 3)]
        assert detector_mod.kept_faults(faults) == (tuple(faults[3:]), 3)
        assert detector_mod.kept_faults(faults[:2]) == (tuple(faults[:2]), 0)

    @pytest.mark.req("PROGRAM_FAULT.outside")
    def test_a_program_fault_of_the_dead_boot_is_harvested_stamped_by_that_boot_s_clock(self, machine):
        dead = machine.boot_id
        machine.kernel(WIREPLUMBER_17_11_57, mono_s=689.5)
        machine.reboot(clean=False)
        read = harvest_after(dead, HarvestFrom(mono_ns=None, realtime_us=None), next_boot=False)
        assert read == JournalHarvest(
            (ProgramFault("wireplumber", 2884, 23, 7, WIREPLUMBER_17_11_57, 689.5),), (), None
        )


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

    def test_a_flood_that_never_runs_dry_is_read_in_bounded_bites(self, detector):
        detector.close()
        flood = _NeverRunsDry(overruns=True)
        detector._file = flood
        first = detector.read_new()
        bite = flood.reads
        assert bite <= detector_mod.KMSG_MAX_READS
        assert first.events
        detector.read_new()
        assert flood.reads > bite

    def test_a_final_read_that_leaves_records_unread_is_a_problem(self, detector):
        detector.close()
        detector._file = _NeverRunsDry(overruns=False)
        assert detector.read_new().problem is None
        assert detector.read_new(final=True).problem == "kernel log flood: records left unread"

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

    def test_a_path_the_reader_refuses_after_opening_leaves_no_descriptor_open(self, tmp_path):
        reader = ErrorDetector(tmp_path)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(50):
            reader.reset()
        after = len(os.listdir("/proc/self/fd"))
        assert "unreadable" in reader.read_new().problem
        reader.close()
        assert after == before

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

    def test_a_whole_read_stamps_the_moment_it_started(self, machine, detector):
        detector.read_new()
        assert detector.read_started_ns == machine.mono_ns
        machine.advance(5)
        detector.read_new()
        assert detector.read_started_ns == machine.mono_ns

    def test_a_read_that_reports_a_problem_keeps_the_last_whole_reads_stamp(self, machine, kmsg, detector):
        detector.read_new()
        whole = machine.mono_ns
        machine.advance(5)
        _append(kmsg, "not a kmsg record\n")
        assert detector.read_new().problem is not None
        assert detector.read_started_ns == whole

    def test_a_kernel_log_never_opened_was_never_read(self, kmsg):
        reader = ErrorDetector(kmsg)
        assert reader.read_new().problem is not None
        assert reader.read_started_ns is None


def _completed(stdout: str) -> MagicMock:
    return MagicMock(returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def real_journal(monkeypatch):
    monkeypatch.setattr(detector_mod, "_journalctl", _journalctl)


WHOLE_BOOT = HarvestFrom(mono_ns=None, realtime_us=None)


def _cpus(events) -> list[int]:
    return [event.cpu for event in events]


class TestReadBootId:
    def test_the_boot_id_is_read_in_the_form_journalctl_takes(self, machine):
        boot = read_boot_id()
        assert boot == machine.boot_id
        assert machine.journalctl(["-b", boot, "-n", "1", "-o", "json"], 1).returncode == 0

    def test_an_unreadable_boot_id_file_raises(self, machine):
        machine.boot_id_path.unlink()
        with pytest.raises(OSError):
            read_boot_id()

    @pytest.mark.parametrize("content", ["", "not-a-boot-id\n", "0123456789abcdef0123456789abcdef\n"])
    def test_a_malformed_boot_id_raises(self, machine, content):
        machine.boot_id_path.write_text(content)
        with pytest.raises(ValueError, match="not a boot id"):
            read_boot_id()


class TestBootEnd:
    def test_a_boot_journald_stopped_ended_cleanly(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=True)
        assert boot_end(dead) == BootEnd(clean=True, problem=None)

    def test_a_boot_that_ends_without_journald_stopping_is_dirty(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=False)
        assert boot_end(dead) == BootEnd(clean=False, problem=None)

    @pytest.mark.parametrize("last_line", ["Shutting down.", "Journal stopped"])
    def test_a_shutdown_line_not_from_journald_is_not_clean(self, machine, last_line):
        dead = machine.boot_id
        machine.kernel_now([last_line])
        machine.reboot(clean=False)
        assert boot_end(dead) == BootEnd(clean=False, problem=None)

    @pytest.mark.req("TRUTH.BOOT_END_UNREAD")
    @pytest.mark.parametrize("identifier", ["systemd-coredump", "systemd-journald"])
    def test_a_last_entry_over_4096_bytes_is_read_whole_and_ends_the_boot_uncleanly(self, machine, identifier):
        dead = machine.boot_id
        machine.daemon_now("x" * 5000, identifier)
        machine.reboot(clean=False)
        assert boot_end(dead) == BootEnd(clean=False, problem=None)

    def test_an_unrecorded_boot_is_a_problem(self):
        assert boot_end("") == BootEnd(clean=False, problem="the session's boot is not recorded")

    def test_a_boot_the_journal_no_longer_holds_is_a_problem(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=True)
        machine.forget_boot(dead)
        end = boot_end(dead)
        assert end.clean is False
        assert f"No journal boot entry found for the specified boot ({dead}+0)." in end.problem


class TestResetReasons:
    FLOOD = (
        "x86/amd: Previous system reset reason [0x08000800]: an uncorrected error caused a data fabric sync flood event"
    )

    def test_the_boot_s_reset_reason_lines_before_userspace_are_read(self, machine):
        machine.reboot(clean=False)
        machine.kernel(self.FLOOD, mono_s=1)
        machine.kernel(
            "x86/amd: Previous system reset reason [0x00200800]: ACPI power state transition occurred", mono_s=1
        )
        machine.kernel("amdgpu 0000:7c:00.0: amdgpu: SMU is initialized successfully!", mono_s=1)
        machine.kernel("x86/amd: Previous system reset reason [0x00000800]: a late copy", mono_s=5)
        lines, problem = reset_reasons(machine.boot_id)
        assert lines == (
            self.FLOOD,
            "x86/amd: Previous system reset reason [0x00200800]: ACPI power state transition occurred",
        )
        assert problem is None

    def test_a_reset_reason_over_4096_bytes_is_read_whole(self, machine):
        machine.reboot(clean=False)
        long = f"{self.FLOOD} {'x' * 5000}"
        machine.kernel(long, mono_s=1)
        assert reset_reasons(machine.boot_id) == ((long,), None)

    def test_a_boot_the_journal_does_not_hold_is_a_problem_not_an_empty_answer(self, machine):
        machine.reboot(clean=False)
        gone = machine.boot_id
        machine.reboot(clean=False)
        machine.forget_boot(gone)
        lines, problem = reset_reasons(gone)
        assert lines == ()
        assert gone in problem


class TestHarvestAfter:
    def test_the_dead_boot_is_read_after_its_monotonic_bound_whatever_the_wall_clock_says(self, machine):
        dead = machine.boot_id
        machine.kernel_now(core_bank_record(1))
        bound = machine.mono_ns
        for line in core_bank_record(2):
            machine.kernel(line, mono_s=bound / 1e9 + 1, realtime=1.0)
        machine.reboot(clean=False)
        read = harvest_after(dead, HarvestFrom(mono_ns=bound, realtime_us=None), next_boot=False)
        assert (_cpus(read.dead_boot), read.problem) == ([2], None)

    def test_a_boot_is_read_up_to_and_including_its_upper_bound(self, machine):
        for line in core_bank_record(1):
            machine.kernel(line, mono_s=10)
        for line in core_bank_record(2):
            machine.kernel(line, mono_s=11)
        read = harvest_after(
            machine.boot_id,
            HarvestFrom(mono_ns=0, realtime_us=None),
            next_boot=False,
            through_mono_ns=10_000_000_000,
        )
        assert (_cpus(read.dead_boot), read.problem) == ([1], None)

    def test_a_record_the_upper_bound_splits_is_read_whole_by_the_read_its_first_line_falls_in(self, machine):
        lines = core_bank_record(1)
        machine.kernel(lines[0], mono_s=10.000)
        machine.kernel(lines[1], mono_s=10.001)
        for line in lines[2:]:
            machine.kernel(line, mono_s=10.003)
        bound = 10_002_000_000
        first = harvest_after(
            machine.boot_id, HarvestFrom(mono_ns=0, realtime_us=None), next_boot=False, through_mono_ns=bound
        )
        second = harvest_after(machine.boot_id, HarvestFrom(mono_ns=bound, realtime_us=None), next_boot=False)
        events = (*first.dead_boot, *second.dead_boot)
        assert [type(event) for event in events] == [CoreMachineCheck]
        assert (_cpus(first.dead_boot), first.problem, second.problem) == ([1], None, None)

    @pytest.mark.req("L18.d.3.split")
    @pytest.mark.parametrize("bound", [10_000_000_000, 10_000_500_000])
    def test_a_record_a_bounded_read_finds_unfinished_is_left_whole_to_the_read_from_just_before_its_opener(
        self, machine, bound
    ):
        lines = core_bank_record(1)
        for line in lines[:2]:
            machine.kernel(line, mono_s=10.000)
        machine.kernel(lines[2], mono_s=10.0004)
        first = harvest_after(
            machine.boot_id, HarvestFrom(mono_ns=0, realtime_us=None), next_boot=False, through_mono_ns=bound
        )
        for line in lines[3:]:
            machine.kernel(line, mono_s=10.001)
        held = HarvestFrom(mono_ns=first.unfinished_from_ns - 1, realtime_us=None)
        second = harvest_after(machine.boot_id, held, next_boot=False)
        assert (first.dead_boot, first.unfinished_from_ns) == ((), 10_000_000_000)
        assert ([type(event) for event in second.dead_boot], second.unfinished_from_ns) == ([CoreMachineCheck], None)

    @pytest.mark.req("L18.d.3.split")
    def test_a_whole_record_sharing_its_stamp_with_an_unfinished_one_is_read_once(self, machine):
        whole, unfinished = core_bank_record(1), core_bank_record(2)
        for line in (*whole, *unfinished[:2]):
            machine.kernel(line, mono_s=10.000)
        first = harvest_after(
            machine.boot_id, HarvestFrom(mono_ns=0, realtime_us=None), next_boot=False, through_mono_ns=10_000_300_000
        )
        for line in unfinished[2:]:
            machine.kernel(line, mono_s=10.001)
        held = HarvestFrom(mono_ns=first.unfinished_from_ns - 1, realtime_us=None)
        second = harvest_after(machine.boot_id, held, next_boot=False)
        assert (_cpus(first.dead_boot), _cpus(second.dead_boot)) == ([], [1, 2])

    def test_a_record_one_nanosecond_past_the_upper_bound_after_120_days_up_is_left_to_the_next_read(self, machine):
        for line in core_bank_record(1):
            machine.kernel(line, mono_s=10368000.000015)
        bound = 10_368_000_000_014_999
        first = harvest_after(
            machine.boot_id, HarvestFrom(mono_ns=0, realtime_us=None), next_boot=False, through_mono_ns=bound
        )
        second = harvest_after(machine.boot_id, HarvestFrom(mono_ns=bound, realtime_us=None), next_boot=False)
        assert (_cpus(first.dead_boot), _cpus(second.dead_boot)) == ([], [1])

    def test_without_a_monotonic_bound_the_dead_boot_is_read_after_its_wall_clock_bound(self, machine):
        dead = machine.boot_id
        machine.kernel_now(core_bank_record(1))
        last_write_us = machine.boot.entries[-1].realtime_us
        machine.kernel_now(core_bank_record(2))
        machine.reboot(clean=False)
        read = harvest_after(dead, HarvestFrom(mono_ns=None, realtime_us=last_write_us), next_boot=False)
        assert (_cpus(read.dead_boot), read.problem) == ([2], None)

    def test_each_half_keeps_its_records_apart_stamped_by_their_boots_clock(self, machine):
        dead = machine.boot_id
        for line in core_bank_record(1):
            machine.kernel(line, mono_s=4000)
        machine.reboot(clean=False)
        for line in core_bank_record(2, corrected=False):
            machine.kernel(line, mono_s=1)
        read = harvest_after(dead, WHOLE_BOOT, next_boot=True)
        assert [(event.cpu, event.raw_ts) for event in read.dead_boot] == [(1, 4000.0)]
        assert [(event.cpu, event.raw_ts) for event in read.next_boot] == [(2, 1.0)]

    def test_the_next_boot_is_read_only_before_its_first_userspace_entry(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=False)
        for line in core_bank_record(1, corrected=False):
            machine.kernel(line, mono_s=1)
        for line in core_bank_record(2, corrected=False):
            machine.kernel(line, mono_s=5)
        machine.reboot(clean=False)
        for line in core_bank_record(3, corrected=False):
            machine.kernel(line, mono_s=1)
        read = harvest_after(dead, WHOLE_BOOT, next_boot=True)
        assert (read.dead_boot, _cpus(read.next_boot), read.problem) == ((), [1], None)

    def test_a_kernel_line_over_4096_bytes_is_read_whole_beside_the_records(self, machine):
        dead = machine.boot_id
        machine.kernel_now(["x" * 5000], core_bank_record(2))
        machine.reboot(clean=False)
        read = harvest_after(dead, WHOLE_BOOT, next_boot=False)
        assert (_cpus(read.dead_boot), read.problem) == ([2], None)

    def test_a_clean_end_reads_the_dead_boot_alone(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=True)
        for line in core_bank_record(1, corrected=False):
            machine.kernel(line, mono_s=1)
        assert harvest_after(dead, WHOLE_BOOT, next_boot=False) == JournalHarvest((), (), None)

    def test_a_record_a_user_process_wrote_to_the_kernel_log_is_not_read(self, machine):
        dead = machine.boot_id
        machine.kernel_now(core_bank_record(1), facility=1)
        machine.kernel_now(core_bank_record(2))
        machine.reboot(clean=False)
        assert _cpus(harvest_after(dead, WHOLE_BOOT, next_boot=False).dead_boot) == [2]

    @pytest.mark.parametrize(
        "stored", ["-1", "0", "all", "9C2F9903AD5C4BDA916BA213C34DBA2A", "9c2f9903-ad5c-4bda-916b-a213c34dba2a"]
    )
    def test_a_stored_boot_id_that_is_not_a_boot_id_is_refused_before_journalctl(self, machine, stored):
        assert boot_end(stored) == BootEnd(clean=False, problem=f"the session's boot id {stored!r} is not a boot id")
        assert harvest_after(stored, WHOLE_BOOT, next_boot=True) == JournalHarvest(
            (), (), f"the session's boot id {stored!r} is not a boot id"
        )

    def test_an_unrecorded_boot_reads_nothing(self):
        assert harvest_after("", WHOLE_BOOT, next_boot=True) == JournalHarvest(
            (), (), "the session's boot is not recorded"
        )

    def test_an_unreadable_last_write_still_reads_the_next_boot(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=False)
        for line in core_bank_record(1, corrected=False):
            machine.kernel(line, mono_s=1)
        read = harvest_after(dead, None, next_boot=True)
        assert (read.dead_boot, _cpus(read.next_boot), read.problem) == (
            (),
            [1],
            "the session's last write time is unreadable",
        )

    def test_a_rotated_dead_boot_names_no_next_boot_to_read(self, machine):
        dead = machine.boot_id
        machine.reboot(clean=False)
        for line in core_bank_record(1, corrected=False):
            machine.kernel(line, mono_s=1)
        machine.forget_boot(dead)
        read = harvest_after(dead, WHOLE_BOOT, next_boot=True)
        assert (read.dead_boot, read.next_boot) == ((), ())
        assert f"No journal boot entry found for the specified boot ({dead}+0)." in read.problem
        assert f"boot {dead} is not in the journal's boot list, so the boot after it cannot be named" in read.problem

    def test_the_newest_boot_has_no_next_boot_to_read(self, machine):
        assert harvest_after(machine.boot_id, WHOLE_BOOT, next_boot=True) == JournalHarvest((), (), None)


def _entry(message, *, mono_us=5, identifier="kernel") -> str:
    return json.dumps(
        {
            "__MONOTONIC_TIMESTAMP": str(mono_us),
            "__REALTIME_TIMESTAMP": "1791148722100000",
            "SYSLOG_IDENTIFIER": identifier,
            "MESSAGE": message,
        }
    )


COREDUMP_OF_WIREPLUMBER = json.dumps(
    {
        "__CURSOR": "s=5d1c0c3b;i=2f7a1;b=0123456789abcdef0123456789abcdef;m=2914a51f;t=6403f2d8a6c3d;x=91c2",
        "__REALTIME_TIMESTAMP": "1792163517412093",
        "__MONOTONIC_TIMESTAMP": "689204511",
        "_BOOT_ID": "0123456789abcdef0123456789abcdef",
        "_TRANSPORT": "journal",
        "PRIORITY": "2",
        "SYSLOG_IDENTIFIER": "systemd-coredump",
        "MESSAGE_ID": "fc2e22bc6ee647b6b90729ab34a250b1",
        "CODE_FUNC": "submit_coredump",
        "COREDUMP_PID": "2884",
        "COREDUMP_COMM": "wireplumber",
        "COREDUMP_SIGNAL": "11",
        "COREDUMP_SIGNAL_NAME": "SIGSEGV",
        "COREDUMP_EXE": "/run/current-system/sw/bin/wireplumber",
        "COREDUMP_PROC_AUXV": [33, 0, 0, 0, 0, 0, 0, 0],
        "COREDUMP_PROC_MAPS": None,
        "COREDUMP_ENVIRON": None,
        "COREDUMP_PROC_MOUNTINFO": None,
        "MESSAGE": None,
    }
)


@pytest.mark.usefixtures("real_journal")
class TestJournalReadFailures:
    DEAD = "0123456789abcdef0123456789abcdef"
    NEXT = "fedcba9876543210fedcba9876543210"
    LISTING = json.dumps([{"index": 0, "boot_id": NEXT}, {"index": -1, "boot_id": DEAD}])

    @pytest.mark.req("TRUTH.BOOT_END_UNREAD")
    @pytest.mark.parametrize(
        "last", [_entry(None, identifier="systemd-coredump"), COREDUMP_OF_WIREPLUMBER], ids=["null", "coredump-17-11"]
    )
    def test_a_last_entry_from_another_program_ends_the_boot_uncleanly_unread(self, last):
        with patch("subprocess.run", return_value=_completed(last + "\n")):
            assert boot_end(self.DEAD) == BootEnd(clean=False, problem=None)

    @pytest.mark.req("TRUTH.BOOT_END_UNREAD")
    def test_journald_s_own_last_entry_with_a_null_message_is_a_problem(self):
        with patch("subprocess.run", return_value=_completed(_entry(None, identifier="systemd-journald") + "\n")):
            end = boot_end(self.DEAD)
        assert end == BootEnd(
            clean=False,
            problem=f"journalctl -b {self.DEAD} -n 1 -o json --all printed an unreadable entry: "
            "TypeError('MESSAGE is a NoneType')",
        )

    @pytest.mark.parametrize(
        "failure",
        [FileNotFoundError(errno.ENOENT, "No such file or directory"), subprocess.TimeoutExpired("journalctl", 15)],
    )
    @pytest.mark.req("L01.g")
    def test_a_journal_that_cannot_run_is_a_problem(self, failure):
        with patch("subprocess.run", side_effect=failure):
            end = boot_end(self.DEAD)
            read = harvest_after(self.DEAD, WHOLE_BOOT, next_boot=True)
        assert end.problem.startswith(f"journalctl -b {self.DEAD} -n 1 -o json --all failed: ")
        assert (read.dead_boot, read.next_boot) == ((), ())
        assert "journalctl --list-boots -o json failed: " in read.problem

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            "{not json\n",
            '["an array"]\n',
            '{"SYSLOG_IDENTIFIER": "systemd-journald"}\n',
            _entry({"not": "a message"}, identifier="systemd-journald") + "\n",
            _entry([300], identifier="systemd-journald") + "\n",
        ],
        ids=["no-entry", "not-json", "not-an-object", "no-message", "message-object", "message-out-of-range"],
    )
    def test_an_unreadable_last_entry_is_a_problem(self, stdout):
        with patch("subprocess.run", return_value=_completed(stdout)):
            end = boot_end(self.DEAD)
        assert end.clean is False
        assert "printed an unreadable entry" in end.problem

    @pytest.mark.parametrize(
        "stdout",
        [
            "{not json\n",
            '["an array"]\n',
            '{"__MONOTONIC_TIMESTAMP": "1", "__REALTIME_TIMESTAMP": "2"}\n',
            _entry("x", mono_us="soon") + "\n",
            _entry(None) + "\n",
            _entry([300]) + "\n",
        ],
        ids=["not-json", "not-an-object", "no-message", "bad-stamp", "null-message", "message-out-of-range"],
    )
    def test_an_unreadable_kernel_record_is_a_problem(self, stdout):
        with patch("subprocess.run", return_value=_completed(stdout)):
            read = harvest_after(self.DEAD, WHOLE_BOOT, next_boot=False)
        assert read.dead_boot == ()
        assert "printed an unreadable entry" in read.problem

    def test_a_message_held_as_bytes_is_decoded(self):
        stopped = _entry(list(b"Journal stopped"), identifier="systemd-journald")
        with patch("subprocess.run", return_value=_completed(stopped + "\n")):
            assert boot_end(self.DEAD) == BootEnd(clean=True, problem=None)

    @pytest.mark.parametrize("listing", ["{oops", '{"index": 0}', '[{"index": 0}]'])
    def test_an_unreadable_boot_list_is_a_problem(self, listing):
        with patch("subprocess.run", side_effect=[_completed(""), _completed(listing)]):
            read = harvest_after(self.DEAD, WHOLE_BOOT, next_boot=True)
        assert "journalctl --list-boots printed an unreadable list" in read.problem

    def test_a_next_boot_the_journal_cannot_read_is_a_problem(self):
        refused = MagicMock(returncode=1, stdout="", stderr="Permission denied")
        with patch("subprocess.run", side_effect=[_completed(""), _completed(self.LISTING), refused]):
            read = harvest_after(self.DEAD, WHOLE_BOOT, next_boot=True)
        assert read.problem.startswith(f"journalctl -b {self.NEXT} -n +1 -o json")
        assert read.problem.endswith("exited 1: Permission denied")

    def test_a_next_boot_with_no_userspace_entry_is_read_whole(self):
        records = "".join(_entry(line, mono_us=900_000_000) + "\n" for line in core_bank_record(4, corrected=False))
        with patch(
            "subprocess.run",
            side_effect=[_completed(""), _completed(self.LISTING), _completed(""), _completed(records)],
        ):
            read = harvest_after(self.DEAD, WHOLE_BOOT, next_boot=True)
        assert (read.dead_boot, _cpus(read.next_boot), read.problem) == ((), [4], None)


THIS_BOOT_KERNEL_LINE = "1791148722.100000 host kernel: Linux version 7.2.9\n"


@pytest.mark.usefixtures("real_journal")
class TestKernelLogProblem:
    def test_a_readable_log_and_journal_are_no_problem(self, kmsg):
        with patch("subprocess.run", return_value=_completed(THIS_BOOT_KERNEL_LINE)) as run:
            assert kernel_log_problem(kmsg) is None
        args = run.call_args[0][0]
        assert args[args.index("-b") + 1] == "0"
        assert "_TRANSPORT=kernel" in args

    @pytest.mark.req("L01.4")
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


@pytest.mark.parametrize(
    "clock", [time.CLOCK_MONOTONIC, time.CLOCK_PROCESS_CPUTIME_ID], ids=["monotonic", "process-cputime"]
)
def test_the_clock_seam_reads_the_clock_it_is_given(clock):
    before = time.clock_gettime_ns(clock)
    assert before <= _clock_ns(clock) <= time.clock_gettime_ns(clock)


@pytest.mark.req("L18.d.4.poll")
def test_the_machine_check_poll_must_be_at_most_five_seconds(tmp_path):
    path = tmp_path / "check_interval"
    path.write_text("5\n")
    assert detector_mod.mce_poll_problem(path) is None
    path.write_text("6\n")
    assert detector_mod.mce_poll_problem(path) == (
        f"machine-check poll every 6 s, above 5 s, so corrected errors log late: echo 5 | sudo tee {path},"
        " or on NixOS services.corecycler.machineCheckPoll.enable"
    )
    path.write_text("300\n")
    assert detector_mod.mce_poll_problem(path).startswith("machine-check poll every 300 s, above 5 s")
    path.write_text("often\n")
    assert detector_mod.mce_poll_problem(path).startswith("machine-check poll interval unreadable: ")
    assert detector_mod.mce_poll_problem(tmp_path / "absent").startswith("machine-check poll interval unreadable: ")


@pytest.mark.req("EC.G1")
@pytest.mark.parametrize(
    ("written", "settle"),
    [("0\n", 1.0), ("2\n", 3.0), ("5\n", 6.0), ("300\n", 6.0), ("-3\n", 6.0), ("often\n", 6.0), (None, 6.0)],
)
def test_a_test_settles_one_machine_check_poll_and_the_timer_s_rounding_at_most_the_five_second_poll(
    tmp_path, written, settle
):
    path = tmp_path / "check_interval"
    if written is not None:
        path.write_text(written)
    assert REAL_MCE_SETTLE_S(path) == settle


def test_a_run_raises_a_slow_machine_check_poll_to_5_s_and_puts_it_back_even_when_it_fails(tmp_path):
    path = tmp_path / "check_interval"
    path.write_text("300\n")
    said = []
    with pytest.raises(RuntimeError), detector_mod.fast_mce_poll(said.append, path) as own:
        assert (path.read_text(), own) == ("5\n", 300)
        assert detector_mod.mce_poll_problem(path) is None
        raise RuntimeError("the run failed")
    assert path.read_text() == "300\n"
    assert said == ["machine-check poll raised from 300 s to 5 s for this run", "machine-check poll back to 300 s"]


def test_a_machine_check_poll_already_at_5_s_is_left_alone(tmp_path):
    path = tmp_path / "check_interval"
    path.write_text("5\n")
    said = []
    with detector_mod.fast_mce_poll(said.append, path) as own:
        assert (path.read_text(), own) == ("5\n", 5)
    assert (path.read_text(), said) == ("5\n", [])


def test_a_machine_check_poll_that_cannot_be_raised_is_said_and_left_to_the_preflight(tmp_path):
    path = tmp_path / "check_interval"
    path.write_text("300\n")
    path.chmod(0o444)
    said = []
    with detector_mod.fast_mce_poll(said.append, path):
        assert "every 300 s" in detector_mod.mce_poll_problem(path)
    assert path.read_text() == "300\n"
    assert len(said) == 1
    assert said[0].startswith(f"machine-check poll stays at 300 s: writing 5 to {path} failed: ")


def test_a_machine_check_poll_that_cannot_be_put_back_is_said(tmp_path):
    path = tmp_path / "check_interval"
    path.write_text("300\n")
    said = []
    with detector_mod.fast_mce_poll(said.append, path):
        path.chmod(0o444)
    assert path.read_text() == "5\n"
    assert said[-1].startswith(f"machine-check poll stays at 5 s: writing 300 back to {path} failed: ")


@pytest.mark.parametrize("text", [None, "often\n"])
def test_an_unreadable_machine_check_poll_is_said_and_not_written(tmp_path, text):
    path = tmp_path / "check_interval"
    if text is not None:
        path.write_text(text)
    said = []
    with detector_mod.fast_mce_poll(said.append, path) as own:
        assert own == 300
    assert len(said) == 1
    assert said[0].startswith(f"machine-check poll not raised: {path} unreadable: ")
    assert path.exists() is (text is not None)
