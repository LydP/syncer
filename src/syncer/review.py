"""Review tree model for the review-and-sync UI (issue #6, reworked for
multi-master rules by issue #24).

Pure, Qt-free: turns a `check.CheckResult` into a `rule > replica >
master-subfolder > folder > file` tree, buckets each file into the three
action categories from spec.md §7 (plus a non-actionable "context" bucket
for in-sync/foreign/unreadable rows), and provides selection roll-up over
that tree. The GUI layer wires `QTreeWidget`/`QListWidget` to this model; it
owns no widgets.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from syncer.check import (
    CheckResult,
    FileChange,
    MasterStatus,
    NamespaceCollision,
    ReplicaCheckResult,
    collisions_for_rule,
)
from syncer.config import (
    Master,
    master_basename,
    master_basename_key,
    masters_by_basename_key,
    split_landing_path,
)

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
class ReviewMaster:
    """One rule master's namespace inside one replica (CONTEXT.md's
    **Replica** entry: a dir master's `<basename>/` subfolder, or a file
    master's bare filename) — the new tree level issue #24 adds between a
    replica and its files, so per-master **Master missing** and cross-rule
    **Cross-rule namespace collision** each block only this one namespace,
    not the whole rule (CONTEXT.md, spec.md §8).

    `children` is always populated with the real per-file tree (like any
    other branch) even when `missing` is true — missing is a runtime,
    unlockable fact, not a structural one, so baking emptiness into the tree
    would make it stick past an unlock. `collision` has no unlock, so a
    collided master is instead built with no children at all (see
    `_build_master`); nothing to bake around.
    """

    master: Master
    missing: bool
    collision: NamespaceCollision | None = None
    children: list[ReviewNode] = field(default_factory=list)

    @property
    def landing_path(self) -> str:
        return master_basename(self.master.path)

    @property
    def unlockable(self) -> bool:
        """Offers the in-session "I know the master is missing" unlock —
        only for a missing master; a collision never has one (CONTEXT.md)."""
        return self.missing and self.collision is None


@dataclass(frozen=True)
class ReviewReplica:
    replica_path: str
    replica_exists: bool
    children: list[ReviewMaster] = field(default_factory=list)


ReviewNode = ReviewFolder | ReviewLeaf
# Anything with `children` — the tree functions below accept a replica or a
# master-subfolder too.
ReviewBranch = ReviewReplica | ReviewMaster | ReviewFolder


@dataclass(frozen=True)
class ReviewRule:
    rule_id: str
    rule_name: str
    replicas: list[ReviewReplica] = field(default_factory=list)


def is_master_blocked(master: ReviewMaster, unlocked: frozenset[str]) -> bool:
    """Ordinary sync/selection is blocked for this master-subfolder: either a
    cross-rule collision (never unlockable — CONTEXT.md) or a missing master
    the user hasn't unlocked this session (spec.md §8, `unlocked` keyed by
    `landing_path` since master basenames are unique within a rule).
    """
    return master.collision is not None or (master.missing and master.landing_path not in unlocked)


def visible_replica(replica: ReviewReplica, unlocked: frozenset[str]) -> ReviewReplica:
    """`replica` with every blocked master-subfolder's children cleared — the
    one place blocking is resolved into an ordinary tree, so every other tree
    function (tally, iter_leaves, node_state, toggle, ...) stays
    blocking-unaware and unchanged from before issue #24's per-master split.
    """
    return replace(
        replica,
        children=[
            replace(master, children=[]) if is_master_blocked(master, unlocked) else master
            for master in replica.children
        ],
    )


def _build_master(
    status: MasterStatus,
    collision: NamespaceCollision | None,
    files: list[tuple[FileChange, str]],
    replica_path: str,
) -> ReviewMaster:
    """`files` are `(change, rest)` pairs, `rest` being the rel_path below the
    landing path (`split_landing_path`'s remainder) — empty for a file
    master's one file, which lands directly under the master node."""
    root: list[ReviewNode] = []
    folders: dict[str, ReviewFolder] = {}
    if collision is not None:
        # A collided master is never walked into (CONTEXT.md: "no unlock...
        # the tool never offers to proceed anyway") — unlike `missing`,
        # there's no session state that could later reveal these files, so
        # they're simply never built rather than built-then-hidden.
        files = []
    for change, rest in sorted(files, key=lambda pair: pair[0].rel_path):
        siblings = root
        acc = ""
        for segment in rest.split("/")[:-1]:
            # Case-insensitive like check()'s own matching: rel_paths arrive in
            # master/baseline/replica casing, which can differ for one folder.
            acc = os.path.normcase(f"{acc}/{segment}" if acc else segment)
            folder = folders.get(acc)
            if folder is None:
                folder = folders[acc] = ReviewFolder(name=segment)
                siblings.append(folder)
            siblings = folder.children
        siblings.append(ReviewLeaf(replica_path, change))
    return ReviewMaster(
        master=status.master,
        missing=status.missing,
        collision=collision,
        children=root,
    )


def _group_by_master(
    files: list[FileChange], masters_by_key: dict[str, Master]
) -> dict[str, list[tuple[FileChange, str]]]:
    """`files` bucketed by the basename key of the master whose landing path
    starts each `rel_path` — always exactly one, since check() only ever
    reports rel_paths owned by a configured, non-collided master of this rule
    (config.is_owned_landing_path) — each paired with its remainder below
    that landing path. Reuses config.split_landing_path so the head-matching
    stays identical to is_owned_landing_path/master_abs_path's.
    """
    grouped: dict[str, list[tuple[FileChange, str]]] = {key: [] for key in masters_by_key}
    for change in files:
        master, _, rest = split_landing_path(masters_by_key, change.rel_path)
        if master is not None:
            grouped[master_basename_key(master.path)].append((change, rest))
    return grouped


def _build_replica(
    replica_result: ReplicaCheckResult,
    masters: list[MasterStatus],
    masters_by_key: dict[str, Master],
    collided: dict[tuple[str, str], NamespaceCollision],
) -> ReviewReplica:
    grouped = _group_by_master(replica_result.files, masters_by_key)
    master_nodes = []
    for status in masters:
        key = master_basename_key(status.master.path)
        master_nodes.append(
            _build_master(
                status,
                collided.get((replica_result.replica_path, key)),
                grouped[key],
                replica_result.replica_path,
            )
        )
    return ReviewReplica(
        replica_path=replica_result.replica_path,
        replica_exists=replica_result.replica_exists,
        children=master_nodes,
    )


def iter_leaves(nodes: Iterable[ReviewNode | ReviewBranch]) -> Iterator[ReviewLeaf]:
    """Every leaf under `nodes`, recursing into folders/master-subfolders/
    replicas alike — blocking-unaware; pass `visible_replica(...)`'d replicas
    to exclude a blocked master-subfolder's leaves (issue #24)."""
    for node in nodes:
        if isinstance(node, ReviewLeaf):
            yield node
        else:
            yield from iter_leaves(node.children)


def tally(nodes: Iterable[ReviewNode | ReviewBranch]) -> dict[str, int]:
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


def _checkable_keys(node: ReviewNode | ReviewBranch) -> frozenset[LeafKey]:
    return frozenset(leaf.key for leaf in iter_leaves([node]) if leaf.checkable)


def node_state(node: ReviewNode | ReviewBranch, selected: frozenset[LeafKey]) -> str:
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
    node: ReviewNode | ReviewBranch, selected: frozenset[LeafKey]
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
    rule: ReviewRule, include: Callable[[ReviewLeaf], bool], unlocked: frozenset[str]
) -> dict[str, list[FileChange]]:
    """`{replica_path: [FileChange]}` (the shape `sync()` consumes, spec.md
    §11) for every leaf where `include(leaf)` is true, across every replica's
    *unblocked* master-subfolders only (issue #24: a blocked namespace never
    contributes, bulk actions included)."""
    applied: dict[str, list[FileChange]] = {}
    for replica in rule.replicas:
        visible = visible_replica(replica, unlocked)
        changes = [leaf.file_change for leaf in iter_leaves([visible]) if include(leaf)]
        if changes:
            applied[replica.replica_path] = changes
    return applied


def resolve_selection(
    rule: ReviewRule, selected: frozenset[LeafKey], unlocked: frozenset[str] = frozenset()
) -> dict[str, list[FileChange]]:
    """Ticked leaf keys -> the `{replica_path: [FileChange]}` shape
    `sync()` consumes. Only checkable leaves in `selected` are included, so a
    stale key whose file has since become a conflict — or whose
    master-subfolder has since become blocked — never appears here.
    """
    return _grouped_changes(
        rule, lambda leaf: leaf.checkable and leaf.key in selected, unlocked
    )


def selection_by_bucket(
    rule: ReviewRule, selected: frozenset[LeafKey], bucket: str, unlocked: frozenset[str] = frozenset()
) -> dict[str, list[FileChange]]:
    """The ticked selection narrowed to one bucket — e.g. the master-deleted
    files a "Review deletes" batch-confirm dialog itemises separately from
    an ordinary safe-drift sync (spec.md §8)."""
    return _grouped_changes(
        rule, lambda leaf: leaf.bucket == bucket and leaf.key in selected, unlocked
    )


def sync_all_safe_changes(
    rule: ReviewRule, unlocked: frozenset[str] = frozenset()
) -> dict[str, list[FileChange]]:
    """"Sync all safe changes" (spec.md §7): ignores the current tick
    selection entirely and applies every `new` + `changed` file across the
    rule's unblocked master-subfolders."""
    return _grouped_changes(rule, lambda leaf: leaf.bucket == "safe", unlocked)


def rule_tally(rule: ReviewRule, unlocked: frozenset[str] = frozenset()) -> dict[str, int]:
    """Bucket counts aggregated across every replica's unblocked
    master-subfolders in the rule — the left pane's one-line status ("N to
    sync, M to delete, K conflict"); a blocked namespace contributes nothing
    (issue #24)."""
    return tally([visible_replica(r, unlocked) for r in rule.replicas])


def blocked_master_count(rule: ReviewRule, unlocked: frozenset[str] = frozenset()) -> int:
    """Count of (replica, master-subfolder) pairs currently blocked —
    collision blocking is scoped per replica, so this can exceed the rule's
    own master count when more than one replica is affected."""
    return sum(
        is_master_blocked(master, unlocked)
        for replica in rule.replicas
        for master in replica.children
    )


def build_review_rule(
    check_result: CheckResult, collisions: list[NamespaceCollision] | None = None
) -> ReviewRule:
    """`collisions` should be `find_namespace_collisions()`'s full result (or
    any subset) — narrowed here to the ones naming this rule and matched to
    each replica by path, so a caller can just pass the whole-config list
    through for every rule it builds.
    """
    collided = collisions_for_rule(check_result.rule_id, collisions)
    masters_by_key = masters_by_basename_key([status.master for status in check_result.masters])
    return ReviewRule(
        rule_id=check_result.rule_id,
        rule_name=check_result.rule_name,
        replicas=[
            _build_replica(r, check_result.masters, masters_by_key, collided)
            for r in check_result.replicas
        ],
    )
