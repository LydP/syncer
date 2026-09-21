"""Pure logic for the conflict-resolution dialog (issue #7, spec.md §9).

Qt-free: builds diff panels and queue/bulk-selection decisions from a
CheckResult's conflict FileChanges. The GUI layer (syncer.gui.conflict_dialog)
renders this and applies the chosen action via state.py/sync.py.
"""

from __future__ import annotations

import difflib
import os
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from syncer.check import FileChange, baseline_from_disk, hash_file
from syncer.config import (
    SyncRule,
    master_abs_path,
    masters_by_basename_key,
    normalize_replica_path,
    replica_abs_path,
)
from syncer.review import (
    CONFLICT_CATEGORIES,
    ReviewLeaf,
    ReviewNode,
    ReviewReplica,
    iter_leaves,
)
from syncer.state import State, merge_replica_entries, save_state

# Above this, a text file falls back to metadata-only (spec.md §9: "a text
# file over a size threshold" — no best-effort diff attempted). Lowered from
# 1 MB alongside issue #10, but the autojunk flip in _diff_ops is what fixes
# that issue's shape (0.003 s even at 1 MB); this cap only guards shapes
# autojunk can't help, where shared lines are too rare to be junked. Its cost:
# 250 KB–1 MB text files get metadata only. Bytes are a loose proxy — matcher
# cost tracks line count, not file size, so this bounds neither tightly.
MAX_DIFF_BYTES = 250_000

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


@dataclass(frozen=True)
class OverwriteSummary:
    """What an "overwrite from master" over a batch of conflicts would do, for
    its confirm prompt: files per category (in `CONFLICT_CATEGORIES` order, empty
    categories omitted) and how many replica files it would delete."""

    by_category: dict[str, int]
    deletions: int


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
            # One read, one allocation: the size check above caps this at
            # MAX_DIFF_BYTES, so reading whole and slicing the sniff window
            # beats concatenating a head chunk onto the rest.
            raw = fh.read()
        if b"\0" in raw[:_BINARY_SNIFF_BYTES]:
            return None, _BINARY_REASON
    except OSError as exc:
        return None, f"Couldn't read file: {exc}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, _BINARY_REASON
    return text.splitlines(), None


def _diff_ops(left_lines: list[str], right_lines: list[str]) -> tuple[DiffOp, ...]:
    # Flipped from autojunk=False for issue #10: without it, a heavily
    # edited prose file whose two sides share only their blank lines diffs
    # cubically (4.2 s at 91 KB — this file's own test fixture). autojunk
    # drops lines filling >1% of the right side — blank lines in prose —
    # from the match search. Popular lines never anchor a match, so the diff
    # coarsens: on heavily edited prose the whole file comes back as one
    # "replace", and in a repetitive file (few distinct lines, e.g. data or
    # checklists) every popular line after an edit can show as changed.
    # Trimming the common prefix/suffix first keeps a localized edit's
    # untouched head and tail "equal" regardless of autojunk.
    limit = min(len(left_lines), len(right_lines))
    prefix = 0
    while prefix < limit and left_lines[prefix] == right_lines[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and left_lines[-1 - suffix] == right_lines[-1 - suffix]:
        suffix += 1
    left_mid = left_lines[prefix : len(left_lines) - suffix]
    right_mid = right_lines[prefix : len(right_lines) - suffix]

    ops: list[DiffOp] = []
    if prefix:
        ops.append(DiffOp("equal", tuple(left_lines[:prefix]), tuple(right_lines[:prefix])))
    matcher = difflib.SequenceMatcher(a=left_mid, b=right_mid, autojunk=True)
    ops.extend(
        DiffOp(tag, tuple(left_mid[i1:i2]), tuple(right_mid[j1:j2]))
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
    )
    if suffix:
        ops.append(
            DiffOp("equal", tuple(left_lines[-suffix:]), tuple(right_lines[-suffix:]))
        )
    return tuple(ops)


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
    """`{category: [FileChange]}` for the conflicts under `nodes` — the pools a
    bulk action ("Keep all as-is" / "Overwrite all from master") applies to,
    scoped to one category at a time (spec.md §9).

    Every conflict category is bulk-resolvable, no_baseline included: on the
    first check of a replica that already holds files, every conflict is
    no_baseline, so per-file-only resolution made that check unworkable
    (issue #43).

    One walk for every category, since the caller builds a whole menu at once.
    """
    grouped: dict[str, list[FileChange]] = defaultdict(list)
    for leaf in conflict_queue(nodes):
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


def summarize_overwrite(changes: list[FileChange]) -> OverwriteSummary:
    counts = Counter(change.category for change in changes)
    return OverwriteSummary(
        by_category={c: counts[c] for c in CONFLICT_CATEGORIES if counts[c]},
        deletions=sum(change.is_deletion for change in changes),
    )


def apply_keep_replica(
    rule: SyncRule,
    changes_by_replica: dict[str, list[FileChange]],
    state: State,
    state_path: Path,
) -> State:
    """"Keep replica's version" / bulk "Keep all as-is" (spec.md §9): adopts
    each file's current replica content as the new baseline and sets
    `kept=True` — no bytes are copied anywhere, unlike "Overwrite from
    master" which is just `sync.sync()` with these same changes.

    Takes the same `{replica_path: [FileChange]}` shape `sync.sync()` does.
    All or nothing: every file is read before anything is merged, and state
    is saved once. Replicas with no changes are skipped, and an all-empty
    mapping returns `state` untouched, so callers needn't guard: merging
    nothing would still bump `last_sync`.
    """
    masters_by_key = masters_by_basename_key(rule.masters)
    # Replicas of one rule share masters, so each master file is hashed once.
    master_hashes: dict[str, str | None] = {}
    updates_by_replica = {}
    for replica_root, changes in changes_by_replica.items():
        if not changes:
            continue
        updates = {}
        for change in changes:
            master_abs = master_abs_path(masters_by_key, change.rel_path)
            if master_abs not in master_hashes:
                master_hashes[master_abs] = (
                    hash_file(master_abs) if os.path.isfile(master_abs) else None
                )
            updates[change.rel_path] = baseline_from_disk(
                replica_abs_path(replica_root, change.rel_path),
                kept=True,
                kept_master_hash=master_hashes[master_abs],
            )
        updates_by_replica[replica_root] = updates
    if not updates_by_replica:
        return state
    now = datetime.now(timezone.utc)
    new_state = state
    for replica_root, updates in updates_by_replica.items():
        new_state = merge_replica_entries(
            new_state, rule.id, normalize_replica_path(replica_root), updates, now=now
        )
    save_state(state_path, new_state)
    return new_state


def build_conflict_view(rule: SyncRule, replica_root: str, change: FileChange) -> ConflictView:
    master_abs = master_abs_path(masters_by_basename_key(rule.masters), change.rel_path)
    replica_abs = replica_abs_path(replica_root, change.rel_path)

    if change.category == "both_changed":
        # Neither side can be diffed against the baseline: its content was
        # never stored, only its hash and stat — so both panels are
        # metadata-only, with the baseline side showing that stored stat.
        baseline_meta = FileMeta(
            exists=change.baseline_present,
            size=change.baseline_size,
            mtime=change.baseline_mtime,
        )
        panels = tuple(
            DiffPanel(
                f"{label} vs. baseline",
                "Baseline",
                label,
                baseline_meta,
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
            callout=(
                "This file differs from master and has no sync history, so there's "
                "no way to tell which side changed."
            ),
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
