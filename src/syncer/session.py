"""The live working set for one run of the app (issue #45, ADR 0005).

Qt-free: the in-memory `State`, every rule's most recent review tree (which
carries each master's unlock), the per-rule tick selection, and the
namespace-collision set. `gui/` keeps widgets and asks this module what to
show and what to do; it holds no decisions of its own.

A stateful object rather than an immutable value, like `ConfigStore`: its job
is sequencing I/O (saving `state.json`, copying replica files), not
transforming data. It never starts a thread — see ADR 0005 before giving it one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import NamedTuple

from syncer.check import (
    CheckResult,
    FileChange,
    MasterLayout,
    NamespaceCollision,
    collisions_for_rule,
    find_namespace_collisions,
)
from syncer.config import Config, SyncRule
from syncer.conflict import apply_keep_replica, bulk_candidates_by_category, conflict_queue
from syncer.review import (
    LeafKey,
    ReviewBranch,
    ReviewLeaf,
    ReviewNode,
    ReviewReplica,
    ReviewRule,
    blocked_master_count,
    build_preview_rule,
    build_review_rule,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    toggle,
)
from syncer.state import State, baseline_for_rule
from syncer.storage import StorageLayout
from syncer.sync import SyncResult
from syncer.sync import sync as run_sync


class ConfigAdoption(NamedTuple):
    """What `Session.adopt_config` changed under the caller, for a view to
    redraw from."""

    unchanged: frozenset[str]
    """Ids of the rules whose review, ticks and unlocks survived."""
    replica_names_changed: bool
    """A replica was renamed, so labels need redrawing even where the review
    itself is still accurate."""


class Session:
    def __init__(self, layout: StorageLayout, config: Config, state: State):
        self._layout = layout
        self._config = config
        self._state = state
        # Insertion-ordered: also the rule list's row order.
        self._rules_by_id: dict[str, SyncRule] = {rule.id: rule for rule in config.rules}
        # Recomputed only where _rules_by_id is, not per rule checked, since a
        # check-all checks every rule in turn.
        self._namespace_collisions = find_namespace_collisions(config.rules)
        self._review: dict[str, ReviewRule] = {}
        self._selected: defaultdict[str, frozenset[LeafKey]] = defaultdict(frozenset)

    @property
    def layout(self) -> StorageLayout:
        """Where `state.json` and the sync logs live — read by the conflict
        dialog until it applies its resolutions through the session (issue #48)."""
        return self._layout

    @property
    def state(self) -> State:
        """The one in-memory `State`: every sync, keep and resolve replaces it here."""
        return self._state

    def replace_state(self, state: State) -> None:
        """Adopts a `State` resolved elsewhere — until the conflict dialog
        applies its resolutions through the session (issue #48) it carries its own."""
        self._state = state

    @property
    def rules(self) -> Mapping[str, SyncRule]:
        return self._rules_by_id

    @property
    def collisions(self) -> list[NamespaceCollision]:
        return self._namespace_collisions

    @property
    def config(self) -> Config:
        return self._config

    def adopt_config(self, config: Config, state: State) -> ConfigAdoption:
        """The rule set and/or state changed outside the normal check/sync flow
        — an add/edit/delete-rule action or a config reload's reconciliation
        purge (spec.md §10). Per-rule session state (review, tick selection,
        unlock) survives only for a rule whose definition is unchanged, since an
        edited rule's old review would show stale replicas.

        Reports what the caller has to redraw: which rules survived, and
        whether any replica was renamed. Both are comparisons against the
        outgoing config, which only this object still holds once it returns —
        so neither can be asked afterwards.
        """
        old_rules_by_id = self._rules_by_id
        old_collisions = self._namespace_collisions
        replica_names_changed = config.replica_names != self._config.replica_names
        self._config = config
        self._rules_by_id = {rule.id: rule for rule in config.rules}
        self._namespace_collisions = find_namespace_collisions(config.rules)
        self._state = state
        # Another rule's edit can add or clear a collision on an otherwise
        # unchanged rule, so its cached review is stale then too.
        unchanged = frozenset(
            rule_id
            for rule_id, rule in self._rules_by_id.items()
            if old_rules_by_id.get(rule_id) == rule
            and collisions_for_rule(rule_id, old_collisions)
            == collisions_for_rule(rule_id, self._namespace_collisions)
        )
        self._review = {k: v for k, v in self._review.items() if k in unchanged}
        self._selected = defaultdict(
            frozenset, {k: v for k, v in self._selected.items() if k in unchanged}
        )
        return ConfigAdoption(unchanged, replica_names_changed)

    def baseline(self, rule_id: str):
        return baseline_for_rule(self._state, rule_id)

    def record_check(self, result: CheckResult) -> None:
        # A fresh tree carries no unlocks, so a re-check re-blocks structurally
        # (spec.md §8).
        self._review[result.rule_id] = build_review_rule(
            result, collisions=self._namespace_collisions
        )

    def tree(self, rule_id: str) -> ReviewRule | None:
        """The rule's latest review tree, or None before its first check."""
        return self._review.get(rule_id)

    def tally(self, rule_id: str) -> dict[str, int]:
        return rule_tally(self._review[rule_id])

    def blocked_count(self, rule_id: str) -> int:
        return blocked_master_count(self._review[rule_id])

    def unlock(self, rule_id: str, landing_path: str) -> None:
        """Lifts the block on the master landing at `landing_path`, in every
        replica of the rule, until the rule's next check (spec.md §8)."""
        self._review[rule_id] = self._review[rule_id].unlock(landing_path)

    def conflict_queue(self, rule_id: str, replica_path: str | None = None) -> list[ReviewLeaf]:
        """Every conflict in the rule, or just one replica's when `replica_path`
        is given — the fixed queue the resolution dialog steps through."""
        return conflict_queue(self._replicas(rule_id, replica_path))

    def bulk_candidates(
        self, rule_id: str, replica_path: str | None = None
    ) -> dict[str, list[FileChange]]:
        """`{category: [FileChange]}` for the rule's conflicts (or one replica's)
        — the pools a bulk "Keep all" / "Overwrite all" applies to."""
        return bulk_candidates_by_category(self._replicas(rule_id, replica_path))

    def _replicas(self, rule_id: str, replica_path: str | None) -> list[ReviewReplica]:
        review = self._review.get(rule_id)
        if review is None:
            return []
        return [r for r in review.replicas if replica_path is None or r.replica_path == replica_path]

    def preview(self, rule: SyncRule, master_layout: MasterLayout) -> ReviewRule:
        """What each replica should hold per the masters, before any check has
        run — built against the session's collisions so blocking matches a
        real check's tree. `master_layout` is `scan_master_layout`'s result,
        not this session's `layout` (which is the storage one)."""
        return build_preview_rule(rule, master_layout, self._namespace_collisions)

    def toggle(self, rule_id: str, node: ReviewNode | ReviewBranch) -> None:
        self._selected[rule_id] = toggle(node, self._selected[rule_id])

    def node_state(self, rule_id: str, node: ReviewNode | ReviewBranch) -> str:
        """"checked" / "unchecked" / "partial" for `node` under the rule's tick selection."""
        return node_state(node, self._selected[rule_id])

    def selected_count(self, rule_id: str) -> int:
        """How many files `sync_selected` would apply right now."""
        return sum(map(len, self._selected_changes(rule_id).values()))

    def pending_deletions(self, rule_id: str) -> dict[str, list[FileChange]]:
        """The ticked files `sync_selected` would delete, for the caller to
        confirm before it does — deletions are their own category (spec.md §8)."""
        review = self._review.get(rule_id)
        if review is None:
            return {}
        return selection_by_bucket(review, self._selected[rule_id], "delete")

    def sync_selected(self, rule_id: str) -> SyncResult | None:
        """Syncs the ticked files, or returns None when there is nothing to sync."""
        return self._apply(rule_id, self._selected_changes(rule_id))

    def sync_all_safe(self, rule_id: str) -> SyncResult | None:
        """Syncs every safe change in the rule, ignoring the tick selection."""
        review = self._review.get(rule_id)
        return None if review is None else self._apply(rule_id, sync_all_safe_changes(review))

    def _selected_changes(self, rule_id: str) -> dict[str, list[FileChange]]:
        review = self._review.get(rule_id)
        if review is None:
            return {}
        return resolve_selection(review, self._selected[rule_id])

    def overwrite(
        self, rule_id: str, changes_by_replica: dict[str, list[FileChange]]
    ) -> SyncResult:
        """"Overwrite from master" for conflicts (spec.md §9): a plain sync of
        `changes_by_replica`, the caller having confirmed it discards local edits."""
        return self._sync(rule_id, changes_by_replica)

    def keep(self, rule_id: str, changes_by_replica: dict[str, list[FileChange]]) -> None:
        """"Keep replica's version" (spec.md §9). All or nothing: an `OSError`
        propagates having merged and saved nothing."""
        self._state = apply_keep_replica(
            self._rules_by_id[rule_id], changes_by_replica, self._state, self._layout.state_path
        )

    def _sync(self, rule_id: str, changes: dict[str, list[FileChange]]) -> SyncResult:
        result = run_sync(
            self._rules_by_id[rule_id],
            changes,
            self._state,
            self._layout.state_path,
            self._layout.logs_dir,
        )
        self._state = result.state
        return result

    def _apply(self, rule_id: str, changes: dict[str, list[FileChange]]) -> SyncResult | None:
        if not changes:
            return None
        result = self._sync(rule_id, changes)
        self._selected[rule_id] = frozenset()
        return result
