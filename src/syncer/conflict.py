"""Pure logic for the conflict-resolution dialog (issue #7, spec.md §9).

Qt-free: builds diff panels and queue/bulk-selection decisions from a
CheckResult's conflict FileChanges. The GUI layer (syncer.gui.conflict_dialog)
renders this and applies the chosen action via state.py/sync.py.
"""

from __future__ import annotations

import difflib
import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from syncer.check import FileChange, baseline_from_disk, hash_file
from syncer.config import SyncRule, abs_path, normalize_replica_path
from syncer.review import BULK_CATEGORIES, ReviewLeaf, ReviewNode, ReviewReplica, iter_leaves
from syncer.state import State, merge_replica_entries, save_state

# Above this, a text file falls back to metadata-only (spec.md §9: "a text
# file over a size threshold" — no best-effort diff attempted).
MAX_DIFF_BYTES = 1_000_000

_BINARY_SNIFF_BYTES = 8192

_BINARY_REASON = "Binary file — no content diff available."


@dataclass(frozen=True)
class FileMeta:
    exists: bool
    size: int | None = None
    mtime: float | None = None


@dataclass(frozen=True)
class DiffOp:
    tag: str  # "equal" | "insert" | "delete" | "replace"
    left: tuple[str, ...]
    right: tuple[str, ...]


@dataclass(frozen=True)
class DiffPanel:
    title: str
    left_label: str
    right_label: str
    left_meta: FileMeta
    right_meta: FileMeta
    ops: tuple[DiffOp, ...] | None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class ConflictView:
    panels: tuple[DiffPanel, ...]
    callout: str | None = None


def _stat_meta(path: str) -> FileMeta:
    try:
        st = os.stat(path)
    except OSError:
        return FileMeta(exists=False)
    return FileMeta(exists=True, size=st.st_size, mtime=st.st_mtime)


def _read_lines_or_reason(path: str, meta: FileMeta) -> tuple[list[str] | None, str | None]:
    """A file's lines for a real line diff, or the reason a diff can't be
    attempted (binary, oversized, unreadable) — never a best-effort diff."""
    if not meta.exists:
        return None, "File does not exist on this side."
    if meta.size > MAX_DIFF_BYTES:
        return None, f"File too large for a content diff (over {MAX_DIFF_BYTES:,} bytes)."
    try:
        with open(path, "rb") as fh:
            # Sniff before reading the rest: a binary file is rejected on its
            # first chunk rather than pulled wholly into memory and discarded.
            head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\0" in head:
                return None, _BINARY_REASON
            raw = head + fh.read()
    except OSError as exc:
        return None, f"Couldn't read file: {exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _BINARY_REASON
    return text.splitlines(), None


def _diff_ops(left_lines: list[str], right_lines: list[str]) -> tuple[DiffOp, ...]:
    matcher = difflib.SequenceMatcher(a=left_lines, b=right_lines, autojunk=False)
    return tuple(
        DiffOp(tag, tuple(left_lines[i1:i2]), tuple(right_lines[j1:j2]))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    )


_NO_BASELINE_CONTENT_REASON = (
    "No record of the previous version — both sides have changed since the "
    "last sync, so there's no earlier version to show a content diff against."
)


def _text_diff_or_fallback_panel(
    title: str,
    left_label: str,
    right_label: str,
    left_path: str,
    right_path: str,
) -> DiffPanel:
    left_meta = _stat_meta(left_path)
    right_meta = _stat_meta(right_path)

    def fallback(reason: str) -> DiffPanel:
        return DiffPanel(title, left_label, right_label, left_meta, right_meta, None, reason)

    # Short-circuits: once one side can't be diffed the other's bytes are
    # never needed, so an unreadable master doesn't cost a full replica read.
    left_lines, reason = _read_lines_or_reason(left_path, left_meta)
    if left_lines is None:
        return fallback(reason)
    right_lines, reason = _read_lines_or_reason(right_path, right_meta)
    if right_lines is None:
        return fallback(reason)
    return DiffPanel(
        title, left_label, right_label, left_meta, right_meta, _diff_ops(left_lines, right_lines)
    )


def bulk_candidates_by_category(
    nodes: Iterable[ReviewNode | ReviewReplica],
) -> dict[str, list[FileChange]]:
    """`{category: [FileChange]}` for the bulk-resolvable categories under
    `nodes` — the pools a bulk action ("Keep all as-is" / "Overwrite all from
    master") applies to, scoped to one category at a time (spec.md §9).

    One walk for every category, since the caller builds a whole menu at once.
    """
    grouped: dict[str, list[FileChange]] = defaultdict(list)
    for leaf in iter_leaves(nodes):
        if leaf.category in BULK_CATEGORIES:
            grouped[leaf.category].append(leaf.file_change)
    return grouped


def conflict_queue(nodes: Iterable[ReviewNode | ReviewReplica]) -> list[ReviewLeaf]:
    """Every conflict leaf under `nodes` — the fixed queue a per-replica or
    per-rule "Resolve conflicts" dialog steps through (spec.md §9).

    Ordering is the tree's own: `_build_replica` already sorts by rel_path,
    and replicas keep the rule's configured order, so the dialog's "1 of N"
    walks the same order as the tree the user is looking at.
    """
    return [leaf for leaf in iter_leaves(nodes) if leaf.bucket == "conflict"]


def apply_keep_replica(
    rule: SyncRule,
    replica_root: str,
    changes: list[FileChange],
    state: State,
    state_path: Path,
) -> State:
    """"Keep replica's version" / bulk "Keep all as-is" (spec.md §9): adopts
    each file's current replica content as the new baseline and sets
    `kept=True` — no bytes are copied anywhere, unlike "Overwrite from
    master" which is just `sync.sync()` with these same changes.

    An empty `changes` is a no-op returning `state` untouched, so callers
    needn't guard: merging nothing would still bump `last_sync`.
    """
    if not changes:
        return state
    updates = {}
    for change in changes:
        master_abs = abs_path(rule.master, rule.master_type, change.rel_path)
        updates[change.rel_path] = baseline_from_disk(
            abs_path(replica_root, rule.master_type, change.rel_path),
            kept=True,
            kept_master_hash=hash_file(master_abs) if os.path.isfile(master_abs) else None,
        )
    new_state = merge_replica_entries(
        state,
        rule.id,
        normalize_replica_path(replica_root),
        updates,
        now=datetime.now(timezone.utc),
    )
    save_state(state_path, new_state)
    return new_state


def build_conflict_view(rule: SyncRule, replica_root: str, change: FileChange) -> ConflictView:
    master_abs = abs_path(rule.master, rule.master_type, change.rel_path)
    replica_abs = abs_path(replica_root, rule.master_type, change.rel_path)

    if change.category == "both_changed":
        # Neither side can be diffed against a baseline whose content was
        # never stored, so both panels are metadata-only — _stat_meta already
        # reports a missing file as exists=False, on either side.
        empty_meta = FileMeta(exists=False)
        panels = tuple(
            DiffPanel(
                f"{label} vs. baseline",
                "Baseline",
                label,
                empty_meta,
                _stat_meta(path),
                None,
                _NO_BASELINE_CONTENT_REASON,
            )
            for label, path in (("Master", master_abs), ("Replica", replica_abs))
        )
        return ConflictView(panels)

    if change.category == "no_baseline":
        panel = _text_diff_or_fallback_panel(
            "Master vs. replica", "Master", "Replica", master_abs, replica_abs
        )
        return ConflictView(
            (panel,),
            callout="No record of a previous sync for this file — can't confirm what changed.",
        )

    # diverged: master's hash still equals baseline's, so master's current
    # bytes stand in for the (never-stored) baseline content.
    panel = _text_diff_or_fallback_panel(
        "Replica vs. baseline",
        "Baseline (= master, unchanged)",
        "Replica",
        master_abs,
        replica_abs,
    )
    return ConflictView((panel,))
