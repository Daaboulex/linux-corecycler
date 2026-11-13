"""The shared heavy-test lock against another tool that takes it by the same protocol in its own process."""

from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecycler.config import paths
from corecycler.engine import heavy_test_lock
from corecycler.engine.heavy_test_lock import HeavyTestRefused, take_heavy_test_lock
from tests._heavy_lock import GPU_LINE, held_by_another_tool, holder_seen_by_another_tool, said_by


@pytest.mark.req("HEAVY.take")
def test_a_taken_lock_names_its_tool_pid_and_time_in_a_private_file_and_holds_until_closed(heavy_lock):
    before = datetime.now().astimezone().replace(microsecond=0)
    lock = take_heavy_test_lock("corecycler-tuner")
    try:
        seen = holder_seen_by_another_tool(heavy_lock)
    finally:
        lock.close()
    tool, pid, at = said_by(seen)
    assert (tool, pid) == ("corecycler-tuner", os.getpid())
    assert at.tzinfo is not None and before <= at <= datetime.now().astimezone()
    assert heavy_lock.read_text() == seen and seen.count("\n") == 1 and seen.endswith("\n")
    assert heavy_lock.stat().st_mode & 0o777 == 0o600
    assert holder_seen_by_another_tool(heavy_lock) is None


@pytest.mark.req("HEAVY.take")
def test_a_new_holder_replaces_what_an_earlier_one_left_with_its_one_line(heavy_lock):
    heavy_lock.write_text(f"{GPU_LINE} and a much longer tail than any line written now\nand a second line\n")
    with take_heavy_test_lock("corecycler-gui"):
        left = heavy_lock.read_text()
    assert left.count("\n") == 1
    assert said_by(left)[:2] == ("corecycler-gui", os.getpid())


@pytest.mark.req("HEAVY.take")
@pytest.mark.parametrize(
    ("written", "named"),
    [
        (f"{GPU_LINE}\n".encode(), GPU_LINE),
        (f"{GPU_LINE}\nsomething after it\n".encode(), GPU_LINE),
        (b"", "its holder line is empty"),
        (b" \n", "its holder line is empty"),
    ],
    ids=["a-line", "a-first-line", "nothing", "blank"],
)
def test_a_lock_another_tool_holds_is_refused_naming_its_line_as_written(heavy_lock, written, named):
    with held_by_another_tool(heavy_lock, written), pytest.raises(HeavyTestRefused) as refused:
        take_heavy_test_lock("corecycler-tuner")
    assert str(refused.value) == f"another heavy test holds the shared lock: {named}"
    assert heavy_lock.read_bytes() == written


@pytest.mark.req("HEAVY.take")
def test_a_holder_line_that_is_not_text_is_named_unreadable_never_guessed(heavy_lock):
    with held_by_another_tool(heavy_lock, b"\xff\xfe 4242\n"), pytest.raises(HeavyTestRefused) as refused:
        take_heavy_test_lock("corecycler-tuner")
    said = str(refused.value)
    assert said.startswith("another heavy test holds the shared lock: its holder line cannot be read (")
    assert "can't decode byte 0xff" in said


@pytest.mark.req("HEAVY.fail_closed")
def test_a_missing_runtime_directory_refuses_by_name_and_creates_nothing(heavy_lock, monkeypatch):
    missing = heavy_lock.parent / "no-runtime" / "heavy-test.lock"
    monkeypatch.setattr(heavy_test_lock, "heavy_test_lock_path", lambda uid: missing)
    with pytest.raises(HeavyTestRefused) as refused:
        take_heavy_test_lock("corecycler-tuner")
    assert str(refused.value) == f"the shared heavy-test lock {missing} cannot be taken: No such file or directory"
    assert not missing.parent.exists()


@pytest.mark.req("HEAVY.fail_closed")
def test_a_runtime_directory_the_user_cannot_write_refuses_by_name(heavy_lock):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    heavy_lock.parent.chmod(0o500)
    try:
        with pytest.raises(HeavyTestRefused) as refused:
            take_heavy_test_lock("corecycler-tuner")
    finally:
        heavy_lock.parent.chmod(0o700)
    assert str(refused.value) == f"the shared heavy-test lock {heavy_lock} cannot be taken: Permission denied"
    assert not heavy_lock.exists()


@pytest.mark.req("HEAVY.fail_closed")
def test_a_link_at_the_lock_s_path_is_refused_and_what_it_points_at_is_left_alone(heavy_lock, tmp_path):
    kept = tmp_path / "kept"
    kept.write_text("kept\n")
    heavy_lock.symlink_to(kept)
    with pytest.raises(HeavyTestRefused) as refused:
        take_heavy_test_lock("corecycler-tuner")
    assert (
        str(refused.value)
        == f"the shared heavy-test lock {heavy_lock} cannot be taken: Too many levels of symbolic links"
    )
    assert kept.read_text() == "kept\n"


def no_locks(*_args):
    raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))


def read_only(path: str, flags: int) -> int:
    return os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)


@pytest.mark.req("HEAVY.fail_closed")
@pytest.mark.parametrize(
    ("broken", "error"),
    [
        ((heavy_test_lock.fcntl, "flock", no_locks), "No locks available"),
        ((heavy_test_lock, "_private", read_only), "Invalid argument"),
    ],
    ids=["the-flock", "the-line"],
)
def test_a_lock_that_cannot_be_taken_or_named_refuses_by_name_and_is_left_free(heavy_lock, monkeypatch, broken, error):
    with monkeypatch.context() as breaking, pytest.raises(HeavyTestRefused) as refused:
        breaking.setattr(*broken)
        take_heavy_test_lock("corecycler-tuner")
    assert str(refused.value) == f"the shared heavy-test lock {heavy_lock} cannot be taken: {error}"
    assert holder_seen_by_another_tool(heavy_lock) is None


KILLED_WITH_A_TEST_RUNNING = """
import subprocess, sys, time
from pathlib import Path
from corecycler.engine import heavy_test_lock
heavy_test_lock.heavy_test_lock_path = lambda uid: Path(sys.argv[1])
lock = heavy_test_lock.take_heavy_test_lock("corecycler-tuner")
test = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], close_fds=False)
print(test.pid, flush=True)
time.sleep(60)
"""


@pytest.mark.req("HEAVY.take")
def test_a_killed_holder_lets_go_and_no_program_it_started_keeps_the_lock(heavy_lock):
    holder = subprocess.Popen(
        [sys.executable, "-c", KILLED_WITH_A_TEST_RUNNING, str(heavy_lock)],
        stdout=subprocess.PIPE,
        env=paths.child_environment(),
    )
    started = holder.stdout.readline()
    try:
        test = int(started)
        assert said_by(holder_seen_by_another_tool(heavy_lock))[:2] == ("corecycler-tuner", holder.pid)
        holder.kill()
        holder.wait(timeout=30)
        os.kill(test, 0)
        assert holder_seen_by_another_tool(heavy_lock) is None
    finally:
        holder.kill()
        holder.wait(timeout=30)
        if started.strip().isdigit():
            os.kill(int(started), signal.SIGKILL)


@pytest.mark.req("HEAVY.sudo")
def test_under_sudo_a_lock_file_root_made_is_handed_to_the_invoking_user_and_no_link_followed(heavy_lock, monkeypatch):
    user, group = os.geteuid(), os.getegid()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", str(user))
    monkeypatch.setenv("SUDO_GID", str(group))
    lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_uid=0) if path == heavy_lock else lstat(path))
    handed = []
    monkeypatch.setattr(
        os, "chown", lambda path, uid, gid, *, follow_symlinks: handed.append((path, uid, gid, follow_symlinks))
    )
    take_heavy_test_lock("corecycler-tuner").close()
    assert handed == [(heavy_lock, user, group, False)]


@pytest.mark.req("HEAVY.path")
@pytest.mark.parametrize("sudo_uid", [None, 1234], ids=["as-the-user", "as-root-under-sudo"])
def test_the_lock_is_the_invoking_user_s_in_the_runtime_tmpfs_whatever_home_and_xdg_say(
    heavy_lock, tmp_path, monkeypatch, sudo_uid
):
    monkeypatch.setenv("HOME", str(tmp_path / "moved-home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    uid = os.geteuid() if sudo_uid is None else sudo_uid
    if sudo_uid is not None:
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setenv("SUDO_UID", str(sudo_uid))
        monkeypatch.delenv("SUDO_GID", raising=False)
    asked = []
    monkeypatch.setattr(
        heavy_test_lock, "heavy_test_lock_path", lambda uid: asked.append(paths.heavy_test_lock_path(uid)) or heavy_lock
    )
    take_heavy_test_lock("corecycler-tuner").close()
    assert asked == [Path(f"/run/user/{uid}/heavy-test.lock")]
