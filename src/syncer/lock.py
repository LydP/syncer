import ctypes
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from syncer.storage import SyncerError

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class AlreadyRunningError(SyncerError):
    pass


def pid_is_running(pid: int) -> bool:
    """Whether a process with `pid` is currently running, via the Win32 API.

    `OpenProcess` returns a null handle both when the PID is unused and when
    it has been recycled by an unrelated process we're not privileged to
    query, so this can't distinguish "gone" from "inaccessible" — but a
    stale syncer.lock is always our own past process, never a foreign one.
    `OpenProcess` also *succeeds* on a process that has exited while some
    other handle to it is still open, so the exit code must be checked too.
    """
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # can't tell — err on the side of not stealing a live lock
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _read_lock_pid(lock_path: Path) -> int | None:
    """The PID recorded in `lock_path`, or None if it's unreadable/garbage
    (e.g. truncated by a power loss) — such a lock is treated as stale."""
    try:
        return int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return None


def acquire_lock(
    lock_path: Path,
    *,
    pid: int,
    is_running: Callable[[int], bool] = pid_is_running,
) -> None:
    # Write the PID to a unique temp file, then claim the lock with os.rename,
    # which on Windows fails if the destination exists — an atomic
    # create-if-absent. Not os.open(O_CREAT | O_EXCL): that exposes an empty
    # lock file before the PID is written, and an unparseable lock counts as
    # stale, so a second launch in that window would steal a live lock.
    tmp_path = lock_path.with_name(f"{lock_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_bytes(str(pid).encode("utf-8"))
    try:
        os.rename(tmp_path, lock_path)
    except FileExistsError:
        existing_pid = _read_lock_pid(lock_path)
        # Our own PID in the lock can only be a stale lock whose PID was
        # recycled to us (e.g. after a crash and reboot), never a live peer.
        if existing_pid is not None and existing_pid != pid and is_running(existing_pid):
            raise AlreadyRunningError(
                f"Syncer is already running (pid {existing_pid}) — check the existing window."
            ) from None
        os.replace(tmp_path, lock_path)  # stale lock: reclaim silently (spec.md §10)
    finally:
        tmp_path.unlink(missing_ok=True)


def release_lock(lock_path: Path, *, pid: int) -> None:
    """Remove `lock_path` only if it still records `pid`.

    During an app update the new build takes the lock while the old one is
    still exiting, so an unconditional unlink would delete the new build's
    lock. An unparseable lock isn't ours either — `acquire_lock` treats it as
    stale and reclaims it, so leaving it is harmless.
    """
    if _read_lock_pid(lock_path) == pid:
        lock_path.unlink(missing_ok=True)
