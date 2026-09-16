import os
import subprocess
import sys

import pytest

from syncer.lock import AlreadyRunningError, acquire_lock, pid_is_running, release_lock


def test_acquire_lock_creates_lock_file_with_the_given_pid(tmp_path):
    lock_path = tmp_path / "syncer.lock"

    acquire_lock(lock_path, pid=4242)

    assert lock_path.read_text() == "4242"


def test_acquire_lock_raises_when_existing_pid_is_still_running(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("1111")

    with pytest.raises(AlreadyRunningError):
        acquire_lock(lock_path, pid=2222, is_running=lambda pid: pid == 1111)


def test_acquire_lock_silently_reclaims_a_stale_lock(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("1111")

    acquire_lock(lock_path, pid=2222, is_running=lambda pid: False)

    assert lock_path.read_text() == "2222"


def test_acquire_lock_reclaims_a_stale_lock_holding_our_own_recycled_pid(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("2222")

    acquire_lock(lock_path, pid=2222, is_running=lambda pid: True)

    assert lock_path.read_text() == "2222"


def test_acquire_lock_reclaims_a_lock_file_with_unparseable_contents(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("")

    acquire_lock(lock_path, pid=2222, is_running=lambda pid: True)

    assert lock_path.read_text() == "2222"


def test_acquire_lock_leaves_no_temp_files_behind(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("1111")

    with pytest.raises(AlreadyRunningError):
        acquire_lock(lock_path, pid=2222, is_running=lambda pid: True)
    release_lock(lock_path)
    acquire_lock(lock_path, pid=3333, is_running=lambda pid: False)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["syncer.lock"]


def test_pid_is_running_is_false_for_an_exited_process_whose_handle_is_still_open():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()  # Popen keeps its process handle open, so OpenProcess still succeeds

    assert pid_is_running(proc.pid) is False


def test_release_lock_removes_the_lock_file(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text("4242")

    release_lock(lock_path)

    assert not lock_path.exists()


def test_release_lock_is_a_noop_when_no_lock_file_exists(tmp_path):
    lock_path = tmp_path / "syncer.lock"

    release_lock(lock_path)

    assert not lock_path.exists()


def test_pid_is_running_is_true_for_the_current_process():
    assert pid_is_running(os.getpid()) is True


def test_acquire_lock_uses_pid_is_running_by_default(tmp_path):
    lock_path = tmp_path / "syncer.lock"
    lock_path.write_text(str(os.getpid()))

    with pytest.raises(AlreadyRunningError):
        acquire_lock(lock_path, pid=99999)
