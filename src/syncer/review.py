"""Review tree model for the review-and-sync UI (issue #6).

Pure, Qt-free: turns a `check.CheckResult` into a `rule > replica > folder >
file` tree, buckets each file into the three action categories from
spec.md §7 (plus a non-actionable "context" bucket for in-sync/foreign/
unreadable rows), and provides selection roll-up over that tree. The GUI
layer wires `QTreeWidget`/`QListWidget` to this model; it owns no widgets.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import NamedTuple

from syncer.check import CheckResult, FileChange, ReplicaCheckResult

# category -> action bucket, spec.md §5's taxonomy table / §7's three
# categories. "context" rows are shown (greyed) but never checkable.
CATEGORY_BUCKET: dict[str, str] = {
    "new": "safe",
    "changed": "safe",
    "master_deleted": "delete",
    "diverged": "conflict",
    "both_changed": "conflict",
    "no_baseline": "conflict",
    "in_sync": "context",
    "replica_only": "context",
    "unreadable": "context",
    "kept": "context",
}

# Plain-text labels only — no glyphs (spec.md §5/§7: "the user does not want
# symbols to learn"). Verbatim from spec.md §5's taxonomy table.
CATEGORY_LABEL: dict[str, str] = {
    "in_sync": "In sync",
    "new": "Missing from replica",
    "changed": "Updated in master",
    "diverged": "Edited in this replica since last sync",
    "both_changed": "Changed in master and replica",
    "no_baseline": "No record of a previous sync — can't compare",
    "master_deleted": "Deleted from master",
    "replica_only": "Only in the replica",
    "unreadable": "Couldn't read this file",
    "kept": "Kept replica's version",
}

BUCKETS = ("safe", "delete", "conflict", "context")
CHECKABLE_BUCKETS = frozenset({"safe", "delete"})

# Derived from CATEGORY_BUCKET so the taxonomy keeps one owner. Tuples, not
# sets, because both drive user-visible ordering (see BUCKETS).
CONFLICT_CATEGORIES = tuple(c for c, bucket in CATEGORY_BUCKET.items() if bucket == "conflict")
# no_baseline is always resolved per-file (spec.md §9) — never in bulk.
BULK_CATEGORIES = tuple(c for c in CONFLICT_CATEGORIES if c != "no_baseline")


class LeafKey(NamedTuple):
    """Identifies one leaf for selection purposes: a replica's normalised
    path plus the file's rel_path within it."""

    replica_path: str
    rel_path: str


@dataclass(frozen=True)
class ReviewLeaf:
    replica_path: str
    file_change: FileChange

    @property
    def rel_path(self) -> str:
        return self.file_change.rel_path

    @property
    def name(self) -> str:
        return self.rel_path.rsplit("/", 1)[-1]

    @property
    def category(self) -> str:
        return self.file_change.category

    @property
    def bucket(self) -> str:
        return CATEGORY_BUCKET[self.category]

    @property
    def label(self) -> str:
        return CATEGORY_LABEL[self.category]

    @property
    def key(self) -> LeafKey:
        return LeafKey(self.replica_path, self.rel_path)

    @property
    def checkable(self) -> bool:
        return self.bucket in CHECKABLE_BUCKETS

    @property
    def resolvable(self) -> bool:
        """Offers a per-file "Resolve" affordance instead of a tick box —
        the other half of `checkable`, so the view never re-derives either
        from `bucket` itself."""
        return self.bucket == "conflict"


@dataclass(frozen=True)
class ReviewFolder:
    name: str
    children: list[ReviewNode] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewReplica:
    replica_path: str
    replica_exists: bool
    children: list[ReviewNode] = field(default_factory=list)


ReviewNode = ReviewFolder | ReviewLeaf
# Anything with `children` — the tree functions below accept a replica too.
ReviewBranch = ReviewReplica | ReviewFolder


@dataclass(frozen=True)
class ReviewRule:
    rule_id: str
    rule_name: str
    master_missing: bool
    replicas: list[ReviewReplica] = field(default_factory=list)


def _build_replica(replica_result: ReplicaCheckResult) -> ReviewReplica:
    root: list[ReviewNode] = []
    folders: dict[str, ReviewFolder] = {}
    for change in sorted(replica_result.files, key=lambda c: c.rel_path):
        siblings = root
        acc = ""
        for segment in change.rel_path.split("/")[:-1]:
            # Case-insensitive like check()'s own matching: rel_paths arrive in
            # master/baseline/replica casing, which can differ for one folder.
            acc = os.path.normcase(f"{acc}/{segment}" if acc else segment)
            folder = folders.get(acc)
            if folder is None:
                folder = folders[acc] = ReviewFolder(name=segment)
                siblings.append(folder)
            siblings = folder.children
        siblings.append(ReviewLeaf(replica_result.replica_path, change))
    return ReviewReplica(
        replica_path=replica_result.replica_path,
        replica_exists=replica_result.replica_exists,
        children=root,
    )


def iter_leaves(nodes: Iterable[ReviewNode | ReviewReplica]) -> Iterator[ReviewLeaf]:
    """Every leaf under `nodes`, recursing into folders/replicas."""
    for node in nodes:
        if isinstance(node, ReviewLeaf):
            yield node
        else:
            yield from iter_leaves(node.children)


def tally(nodes: Iterable[ReviewNode | ReviewReplica]) -> dict[str, int]:
    """Rolled-up leaf counts by bucket ("safe"/"delete"/"conflict"/"context")
    across `nodes`, recursing into folders."""
    counts = dict.fromkeys(BUCKETS, 0)
    for leaf in iter_leaves(nodes):
        counts[leaf.bucket] += 1
    return counts


def has_drift(node: ReviewBranch) -> bool:
    """True when any leaf under `node` is safe/delete/conflict — the
    default-expansion rule (a branch of only context rows collapses)."""
    return any(leaf.bucket != "context" for leaf in iter_leaves([node]))


def _checkable_keys(node: ReviewNode | ReviewReplica) -> frozenset[LeafKey]:
    return frozenset(leaf.key for leaf in iter_leaves([node]) if leaf.checkable)


def node_state(node: ReviewNode | ReviewReplica, selected: frozenset[LeafKey]) -> str:
    """"checked" / "unchecked" / "partial", computed bottom-up from `selected`
    — never stored on the node itself. A leaf that isn't checkable (context,
    conflict) is always "unchecked" and never contributes to a folder's
    partial state (spec.md §7: conflicts are display-only)."""
    keys = _checkable_keys(node)
    hit = keys & selected
    if not hit:
        return "unchecked"
    return "checked" if hit == keys else "partial"


def toggle(
    node: ReviewNode | ReviewReplica, selected: frozenset[LeafKey]
) -> frozenset[LeafKey]:
    """One click = full tick + roll-down (spec.md §7): a "checked" node clears
    every checkable descendant; anything else ("unchecked" or the computed
    "partial") ticks every checkable descendant. Never touches keys outside
    this subtree.
    """
    keys = _checkable_keys(node)
    if keys and keys <= selected:
        return selected - keys
    return selected | keys


def _grouped_changes(
    rule: ReviewRule, include: Callable[[ReviewLeaf], bool]
) -> dict[str, list[FileChange]]:
    """`{replica_path: [FileChange]}` (the shape `sync()` consumes, spec.md
    §11) for every leaf where `include(leaf)` is true."""
    applied: dict[str, list[FileChange]] = {}
    for replica in rule.replicas:
        changes = [leaf.file_change for leaf in iter_leaves([replica]) if include(leaf)]
        if changes:
            applied[replica.replica_path] = changes
    return applied


def resolve_selection(
    rule: ReviewRule, selected: frozenset[LeafKey]
) -> dict[str, list[FileChange]]:
    """Ticked leaf keys -> the `{replica_path: [FileChange]}` shape
    `sync()` consumes. Only checkable leaves in `selected` are included, so a
    stale key whose file has since become a conflict never appears here.
    """
    return _grouped_changes(rule, lambda leaf: leaf.checkable and leaf.key in selected)


def selection_by_bucket(
    rule: ReviewRule, selected: frozenset[LeafKey], bucket: str
) -> dict[str, list[FileChange]]:
    """The ticked selection narrowed to one bucket — e.g. the master-deleted
    files a "Review deletes" batch-confirm dialog itemises separately from
    an ordinary safe-drift sync (spec.md §8)."""
    return _grouped_changes(rule, lambda leaf: leaf.bucket == bucket and leaf.key in selected)


def sync_all_safe_changes(rule: ReviewRule) -> dict[str, list[FileChange]]:
    """"Sync all safe changes" (spec.md §7): ignores the current tick
    selection entirely and applies every `new` + `changed` file across the
    rule's replicas."""
    return _grouped_changes(rule, lambda leaf: leaf.bucket == "safe")


def is_sync_blocked(rule: ReviewRule, unlocked: bool) -> bool:
    """Master missing blocks ordinary tick-and-sync for the rule until the
    user's in-session, non-persisted "I know the master is missing" unlock
    (spec.md §8) — re-blocks on the next check/relaunch since `unlocked` is
    never stored on the model itself."""
    return rule.master_missing and not unlocked


def rule_tally(rule: ReviewRule) -> dict[str, int]:
    """Bucket counts aggregated across every replica in the rule — the left
    pane's one-line status ("N to sync, M to delete, K conflict")."""
    return tally(rule.replicas)


def build_review_rule(check_result: CheckResult) -> ReviewRule:
    return ReviewRule(
        rule_id=check_result.rule_id,
        rule_name=check_result.rule_name,
        master_missing=check_result.master_missing,
        replicas=[_build_replica(r) for r in check_result.replicas],
    )
