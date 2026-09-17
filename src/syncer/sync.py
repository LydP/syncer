import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from syncer.check import BaselineEntry, FileChange, baseline_from_disk
from syncer.config import (
    Master,
    SyncRule,
    master_abs_path,
    masters_by_basename_key,
    normalize_replica_path,
    replica_abs_path,
)
from syncer.state import State, merge_replica_entries, save_state
from syncer.storage import atomic_copy, make_writable, utc_file_stamp


@dataclass(frozen=True)
class FileError:
    rel_path: str
    message: str


@dataclass(frozen=True)
class SyncResult:
    state: State
    log_path: Path
    copied: int
    deleted: int
    duration: float
    errors: list[FileError]


def _copy_change(
    masters_by_key: dict[str, Master], replica_root: str, change: FileChange
) -> BaselineEntry:
    master_abs = master_abs_path(masters_by_key, change.rel_path)
    replica_abs = replica_abs_path(replica_root, change.rel_path)
    if change.category == "new":
        os.makedirs(os.path.dirname(replica_abs), exist_ok=True)
        shutil.copy2(master_abs, replica_abs)
    else:
        atomic_copy(master_abs, Path(replica_abs))
    return baseline_from_disk(replica_abs)


def _prune_empty_parents(replica_key: str, start_dir: str) -> None:
    current = os.path.normpath(start_dir)
    while normalize_replica_path(current) != replica_key:
        parent = os.path.dirname(current)
        if parent == current:
            break  # hit the filesystem root without ever matching replica_root
        try:
            os.rmdir(current)
        except OSError:
            break  # not empty (or already gone) — nothing higher to prune either
        current = parent


def _delete_change(replica_root: str, replica_key: str, rel_path: str) -> None:
    # Replicas are always dir-shaped now (a namespaced landing tree, never a
    # bare file), so pruning is unconditional; it's a no-op when rel_path's
    # parent is the replica root itself (a file master's landing path).
    replica_abs = replica_abs_path(replica_root, rel_path)
    make_writable(replica_abs)
    os.remove(replica_abs)
    _prune_empty_parents(replica_key, os.path.dirname(replica_abs))


def _log_line(action: str, outcome: str, rel_path: str, message: str | None = None) -> str:
    suffix = f": {message}" if message else ""
    return f"{action}\t{outcome}\t{rel_path}{suffix}"


def sync(
    rule: SyncRule,
    applied_changes: dict[str, list[FileChange]],
    state: State,
    state_path: Path,
    logs_dir: Path,
    progress=None,
    cancel=None,
) -> SyncResult:
    started = time.monotonic()
    masters_by_key = masters_by_basename_key(rule.masters)
    ordered = [
        (replica, key, applied_changes[key])
        for replica in rule.replicas
        if (key := normalize_replica_path(replica)) in applied_changes
    ]
    total = sum(len(changes) for _, _, changes in ordered)
    done = 0
    log_lines: list[str] = []
    copied = deleted = 0
    errors: list[FileError] = []
    cancelled = False

    for replica, replica_key, changes in ordered:
        baseline_updates: dict[str, BaselineEntry] = {}
        removed_paths: list[str] = []

        # Copies before deletes: a failure partway leaves the replica more
        # complete, never emptier.
        for change in sorted(changes, key=lambda c: c.is_deletion):
            done += 1
            if progress is not None:
                progress(done, total, change.rel_path)
            if cancel is not None and cancel():
                cancelled = True
                break
            action = "delete" if change.is_deletion else "copy"
            try:
                if change.is_deletion:
                    _delete_change(replica, replica_key, change.rel_path)
                    removed_paths.append(change.rel_path)
                else:
                    baseline_updates[change.rel_path] = _copy_change(
                        masters_by_key, replica, change
                    )
            except OSError as exc:
                errors.append(FileError(change.rel_path, str(exc)))
                log_lines.append(_log_line(action, "error", change.rel_path, str(exc)))
            else:
                log_lines.append(_log_line(action, "ok", change.rel_path))

        copied += len(baseline_updates)
        deleted += len(removed_paths)
        if baseline_updates or removed_paths:
            # Always bumps last_sync, even for a deletes-only batch — this
            # replica genuinely synced this run.
            state = merge_replica_entries(
                state,
                rule.id,
                replica_key,
                baseline_updates,
                now=datetime.now(timezone.utc),
                removed=removed_paths,
            )
            try:
                save_state(state_path, state)
            except OSError as exc:
                # Don't abort: the files above are already applied, and the
                # log must still record them. The in-memory state carries this
                # replica's baseline into the next replica's save.
                errors.append(FileError(str(state_path), str(exc)))
                log_lines.append(_log_line("save_state", "error", str(state_path), str(exc)))

        if cancelled:
            break

    duration = time.monotonic() - started
    log_lines.append(
        f"summary\tcopied={copied} deleted={deleted} errors={len(errors)} "
        f"duration={duration:.2f}s"
    )
    log_path = logs_dir / f"sync-{utc_file_stamp()}.log"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    return SyncResult(
        state=state,
        log_path=log_path,
        copied=copied,
        deleted=deleted,
        duration=duration,
        errors=errors,
    )
