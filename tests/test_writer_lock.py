from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from corecycler.smu import writer_lock
from corecycler.smu.writer_lock import WriterBusy, co_writer, running_driver, start_time


def test_the_lock_lives_in_the_user_s_data_directory(writer_dir):
    assert writer_lock.lock_path() == writer_dir / "tuner.lock"


def test_a_second_writer_is_refused_while_the_first_holds_the_lock(writer_dir):
    with (
        co_writer(),
        pytest.raises(WriterBusy, match=f"another CO writer holds {writer_dir / 'tuner.lock'}"),
        co_writer(),
    ):
        pass
    with co_writer():
        pass


@pytest.mark.req("E6.B1")
def test_a_recording_writer_names_itself_until_it_lets_go():
    assert running_driver() is None
    with co_writer(record=True):
        assert running_driver() == os.getpid()
        with pytest.raises(WriterBusy), co_writer():
            pass
        assert running_driver() == os.getpid()
    assert running_driver() is None


def test_a_plain_writer_leaves_the_driver_s_record_alone(writer_dir):
    with co_writer(record=True):
        pass
    (writer_dir / "tuner.lock").write_text(f"{os.getpid()} {start_time(os.getpid())}\n")
    with co_writer():
        pass
    assert running_driver() == os.getpid()


@pytest.mark.parametrize("content", ["", "garbage", "12 34 56", "x 1"])
def test_a_malformed_record_names_no_driver(writer_dir, content):
    (writer_dir / "tuner.lock").write_text(content)
    assert running_driver() is None


def test_a_dead_or_reused_pid_names_no_driver(writer_dir):
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    (writer_dir / "tuner.lock").write_text(f"{done.stdout.strip()} 1\n")
    assert running_driver() is None
    (writer_dir / "tuner.lock").write_text(f"{os.getpid()} 1\n")
    assert running_driver() is None


RENAMES_ITSELF = """
import sys, time
with open("/proc/self/comm", "wb") as comm:
    comm.write(bytes.fromhex(sys.argv[1]))
print(flush=True)
time.sleep(30)
"""


def test_the_start_time_of_a_process_whose_name_is_not_utf8_is_read():
    child = subprocess.Popen([sys.executable, "-c", RENAMES_ITSELF, b"\xe2\x82x) R 1 2".hex()], stdout=subprocess.PIPE)
    try:
        child.stdout.readline()
        assert Path(f"/proc/{child.pid}/comm").read_bytes() == b"\xe2\x82x) R 1 2\n"
        started = start_time(child.pid)
    finally:
        child.kill()
        child.wait()
    assert started is not None and started.isdigit(), started


def test_the_start_time_is_when_the_process_started_since_boot():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = start_time(child.pid)
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    finally:
        child.kill()
        child.wait()
    assert abs(int(started) / os.sysconf("SC_CLK_TCK") - uptime) < 10
    assert start_time(child.pid) is None
