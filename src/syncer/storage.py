import sys
import tempfile
import tomllib
from dataclasses import dataclass
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
    """Locate `base_dir`: beside the executable when frozen, else the project root.

    ADR 0001 permits no fallback, so an unfrozen run that is not inside a source
    tree is a hard error rather than an arbitrary directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    module_path = Path(__file__).resolve()
    for candidate in module_path.parents:
        if _is_syncer_project_root(candidate):
            return candidate
    raise BaseDirNotFoundError(
        f"no Syncer {PROJECT_ROOT_MARKER} found above {module_path}; run Syncer "
        "from a source tree or as a frozen executable"
    )


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
