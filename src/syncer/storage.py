import os
import shutil
import stat
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT_MARKER = "pyproject.toml"
PROJECT_NAME = "syncer"


class SyncerError(Exception):
    """Base class for expected, user-facing Syncer failures.

    Lets the top-level handler tell a blocking-but-expected refusal apart from a
    genuine bug, without catching bare ``Exception``.
    """


class BaseDirNotWritableError(SyncerError):
    pass


class BaseDirNotFoundError(SyncerError):
    pass


@dataclass(frozen=True)
class StorageLayout:
    base_dir: Path
    config_path: Path
    state_path: Path
    lock_path: Path
    backups_dir: Path
    logs_dir: Path


def resolve_base_dir() -> Path:
    """Locate `base_dir`: beside the executable when packaged, else the project root.

    ADR 0001 permits no fallback, so a source run that is not inside a source
    tree is a hard error rather than an arbitrary directory.
    """
    if _is_standalone_build():
        return Path(sys.executable).resolve().parent
    module_path = Path(__file__).resolve()
    for candidate in module_path.parents:
        if _is_syncer_project_root(candidate):
            return candidate
    raise BaseDirNotFoundError(
        f"no Syncer {PROJECT_ROOT_MARKER} found above {module_path}; run Syncer "
        "from a source tree or as a packaged executable"
    )


def _is_standalone_build() -> bool:
    """True only inside a Nuitka standalone build, where `sys.executable` is the
    packaged .exe sitting in the distribution folder (ADR 0003).

    Nuitka never sets ``sys.frozen`` -- that is a PyInstaller/cx_Freeze
    convention, and ADR 0003 settled on Nuitka. Its own marker is
    ``__compiled__``, injected into every compiled module's globals, but that
    marker alone is not enough: it is present in non-standalone compiles too,
    where ``sys.executable`` is still the interpreter. Keying off the marker's
    presence would make an arbitrary directory `base_dir` there, silently,
    which is exactly what ADR 0001's no-fallback rule exists to prevent.
    """
    compiled = globals().get("__compiled__")
    return compiled is not None and compiled.standalone


def _is_syncer_project_root(candidate: Path) -> bool:
    """True only for Syncer's own project root, not any unrelated Python project.

    Without the name check, a Syncer installed into a venv nested inside some
    other project would adopt that project's root as `base_dir`.
    """
    marker = candidate / PROJECT_ROOT_MARKER
    if not marker.is_file():
        return False
    try:
        with marker.open("rb") as fh:
            project = tomllib.load(fh).get("project")
    except (OSError, ValueError):
        return False
    return isinstance(project, dict) and project.get("name") == PROJECT_NAME


def ensure_base_dir_writable(base_dir: Path) -> None:
    """Prove `base_dir` is writable by writing to it.

    `os.access` only inspects the read-only attribute on Windows and ignores
    ACLs, so an actual write is the only trustworthy check. `TemporaryFile`
    picks a collision-free name and deletes on close, leaving nothing behind if
    the process dies mid-probe.
    """
    try:
        with tempfile.TemporaryFile(dir=base_dir):
            pass
    except OSError as exc:
        raise BaseDirNotWritableError(f"{base_dir} is not writable") from exc


def make_writable(path: Path | str) -> None:
    """Clear the Windows read-only attribute, if set. `os.replace` and
    `os.remove` both refuse a read-only target, and `shutil.copy2` carries a
    read-only master's attribute onto every copy of it.
    """
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if not mode & stat.S_IWRITE:
        os.chmod(path, mode | stat.S_IWRITE)


def _atomic_replace(dst: Path, tmp_path: Path, write_tmp: Callable[[Path], object]) -> None:
    """Fill `tmp_path` via `write_tmp`, then `os.replace` it onto `dst`, so a
    crash never leaves `dst` half-written; the temp file is best-effort
    removed if anything fails.
    """
    try:
        write_tmp(tmp_path)
        os.replace(tmp_path, dst)
    except BaseException:
        try:
            make_writable(tmp_path)
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort only — never mask the original failure
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    _atomic_replace(path, path.with_name(path.name + ".tmp"), lambda tmp: tmp.write_bytes(data))


def atomic_copy(src: Path | str, dst: Path) -> None:
    """Copy `src` onto `dst` atomically.

    A `.syncer-tmp-<uuid8>` suffix (rather than `atomic_write_bytes`'s plain
    `.tmp`) avoids collisions when copies into the same directory could
    overlap in time.
    """
    tmp_path = dst.with_name(f"{dst.name}.syncer-tmp-{uuid.uuid4().hex[:8]}")

    def write_tmp(tmp: Path) -> None:
        shutil.copy2(src, tmp)
        make_writable(dst)  # a read-only dst would make os.replace fail

    _atomic_replace(dst, tmp_path, write_tmp)


def utc_file_stamp() -> str:
    """Timestamp for generated file names. UTC, so name order stays
    chronological across DST/clock changes — backup pruning relies on it.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def ensure_storage_layout(base_dir: Path) -> StorageLayout:
    backups_dir = base_dir / "backups"
    logs_dir = base_dir / "logs"
    backups_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return StorageLayout(
        base_dir=base_dir,
        config_path=base_dir / "config.toml",
        state_path=base_dir / "state.json",
        lock_path=base_dir / "syncer.lock",
        backups_dir=backups_dir,
        logs_dir=logs_dir,
    )


def initialize_storage(base_dir: Path | None = None) -> StorageLayout:
    """Resolve, verify and provision `base_dir` — the supported startup path.

    Callers use this rather than the three steps by hand, so the ADR 0001
    writability gate cannot be skipped by accident. `base_dir` overrides
    resolution, for tests and for callers that already know the directory.
    """
    if base_dir is None:
        base_dir = resolve_base_dir()
    ensure_base_dir_writable(base_dir)
    return ensure_storage_layout(base_dir)
