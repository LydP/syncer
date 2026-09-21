"""The live working set for one run of the app (issue #45, ADR 0005).

Qt-free: the in-memory `State`, every rule's most recent review tree (which
carries each master's unlock), the per-rule tick selection, the
namespace-collision set, and the check queue with its stale-result gate.
`gui/` keeps widgets and asks this module what to show and what to do; it
holds no decisions of its own.

A stateful object rather than an immutable value, like `ConfigStore`: its job
is sequencing I/O (saving `state.json`, copying replica files), not
transforming data. It never starts a thread — see ADR 0005 before giving it one.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import NamedTuple

from syncer.check import (
    CheckResult,
    FileChange,
    MasterLayout,
    NamespaceCollision,
    collisions_for_rule,
    find_namespace_collisions,
)
from syncer.config import Config, ConfigStore, SyncRule
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
    iter_leaves,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    toggle,
)
from syncer.state import State, baseline_for_rule, reconcile_and_save
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


class CheckJob(NamedTuple):
    """One check for the Qt layer to run off-thread, as `Session.next_check`
    hands it out. Carries the rule, baseline and collisions the check is made
    against, so `check_finished` can tell whether they have since changed."""

    rule: SyncRule
    baseline: dict
    collisions: list[NamespaceCollision]

    @property
    def rule_id(self) -> str:
        return self.rule.id


class CheckOutcome(NamedTuple):
    """What `Session.check_finished` did with a result, for a view to redraw from."""

    rule_id: str
    applied: bool
    """The result is now the rule's tree. False when it was discarded — whether
    a discarded rule is being checked again is `has_pending_checks`' answer,
    not a second field here."""


class Session:
    def __init__(
        self, layout: StorageLayout, config_store: ConfigStore, config: Config, state: State
    ):
        self._layout = layout
        self._config_store = config_store
        self._config = config
        self._state = state
        # Insertion-ordered: also the rule list's row order.
        self._rules_by_id: dict[str, SyncRule] = {rule.id: rule for rule in config.rules}
        # Recomputed only where _rules_by_id is, not per rule checked, since a
        # check-all checks every rule in turn.
        self._namespace_collisions = find_namespace_collisions(config.rules)
        self._review: dict[str, ReviewRule] = {}
        self._selected: defaultdict[str, frozenset[LeafKey]] = defaultdict(frozenset)
        self._pending_checks: list[str] = []

    @property
    def state(self) -> State:
        """The one in-memory `State`: every sync, keep and resolve replaces it here."""
        return self._state

    @property
    def rules(self) -> Mapping[str, SyncRule]:
        return self._rules_by_id

    @property
    def config(self) -> Config:
        return self._config

    def save_config(self, config: Config) -> ConfigAdoption:
        """Writes `config` to `config.toml` and adopts it. A `ConfigClobberError`
        (the file was hand-edited since it was loaded) propagates before
        anything is adopted, so the session is left as it was."""
        self._config_store.save(config)
        return self.adopt_config(config)

    def reload_config(self) -> ConfigAdoption:
        """Re-reads `config.toml` and adopts it. A config that can't be read
        raises a `SyncerError` before anything is adopted."""
        return self.adopt_config(self._config_store.load())

    def adopt_config(self, config: Config) -> ConfigAdoption:
        """The rule set changed outside the normal check/sync flow — an
        add/edit/delete-rule action or a config reload. Purges `state.json` of
        anything `config` no longer mentions (spec.md §10) before taking it on,
        so the reconciliation and the adoption can't get out of step.

        Per-rule session state (review, tick selection, unlock) survives only
        for a rule whose definition is unchanged, since an edited rule's old
        review would show stale replicas.

        Reports what the caller has to redraw: which rules survived, and
        whether any replica was renamed. Both are comparisons against the
        outgoing config, which only this object still holds once it returns —
        so neither can be asked afterwards.
        """
        old_rules_by_id = self._rules_by_id
        old_collisions = self._namespace_collisions
        replica_names_changed = config.replica_names != self._config.replica_names
        self._state = reconcile_and_save(self._layout.state_path, self._state, config)
        self._config = config
        self._rules_by_id = {rule.id: rule for rule in config.rules}
        self._namespace_collisions = find_namespace_collisions(config.rules)
        self._pending_checks = [k for k in self._pending_checks if k in self._rules_by_id]
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

    def queue_check(self, rule_id: str) -> None:
        """Adds one rule to the check queue without dropping rules already
        queued (e.g. by an in-progress check-all)."""
        if rule_id not in self._pending_checks:
            self._pending_checks.append(rule_id)

    def queue_all_checks(self) -> None:
        """(Re-)checks every rule, in rule order, replacing whatever was queued."""
        self._pending_checks = list(self._rules_by_id)

    def cancel_checks(self) -> None:
        """Drops every rule still queued — a plain "stop checking" rather than
        "skip this rule". Cancelling the check already in flight is the
        caller's, since it owns the worker; `check_finished` then discards it."""
        self._pending_checks.clear()

    def has_pending_checks(self) -> bool:
        return bool(self._pending_checks)

    def next_check(self) -> CheckJob | None:
        """Takes the next queued check, or None when the queue is empty. The
        caller runs `check()` on a worker thread and hands the result to
        `check_finished` — this object never starts a thread (ADR 0005)."""
        if not self._pending_checks:
            return None
        rule_id = self._pending_checks.pop(0)
        return CheckJob(
            self._rules_by_id[rule_id],
            baseline_for_rule(self._state, rule_id),
            self._namespace_collisions,
        )

    def check_finished(self, job: CheckJob, result: CheckResult, cancelled: bool) -> CheckOutcome:
        if cancelled:
            # Truncated: applying it would under-report drift (even "in sync").
            return CheckOutcome(job.rule_id, applied=False)
        current_rule = self._rules_by_id.get(job.rule_id)
        if current_rule != job.rule or collisions_for_rule(
            job.rule_id, job.collisions
        ) != collisions_for_rule(job.rule_id, self._namespace_collisions):
            # The rule was deleted or edited mid-check, or another rule's edit
            # changed its collisions: this result describes the old config, so
            # syncing it could write to a replica or namespace no longer valid.
            # A surviving rule is checked again; a deleted one isn't.
            if current_rule is not None:
                self.queue_check(job.rule_id)
            return CheckOutcome(job.rule_id, applied=False)
        # A fresh tree carries no unlocks, so a re-check re-blocks any master
        # still missing structurally (spec.md §8).
        self._review[job.rule_id] = build_review_rule(result, collisions=self._namespace_collisions)
        return CheckOutcome(job.rule_id, applied=True)

    def tree(self, rule_id: str) -> ReviewRule | None:
        """The rule's latest review tree, or None before its first check — the
        one accessor the rest of this class reads the cache through."""
        return self._review.get(rule_id)

    # `tally`, `blocked_count` and `unlock` describe a tree that exists: they
    # raise KeyError before the rule's first check rather than inventing a
    # reading for it. Callers gate on `tree(rule_id) is not None`. The methods
    # that produce sync candidates take the other contract — see `_changes`.

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
        — the pools a bulk "Keep all" / "Overwrite all" applies to.

        The pools partition `conflict_queue` over the same nodes: every
        conflict lands in exactly one category, so their total is the conflict
        count and a caller needn't walk the tree again to label a menu.
        """
        return bulk_candidates_by_category(self._replicas(rule_id, replica_path))

    def _replicas(self, rule_id: str, replica_path: str | None) -> list[ReviewReplica]:
        review = self.tree(rule_id)
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
        """How many files `sync_selected` would apply right now. Counts the
        same leaves `resolve_selection` would keep, without building (and
        throwing away) the grouped result — this runs on every tick click."""
        review = self.tree(rule_id)
        if review is None:
            return 0
        selected = self._selected[rule_id]
        return sum(
            1 for leaf in iter_leaves(review.replicas) if leaf.checkable and leaf.key in selected
        )

    def pending_deletions(self, rule_id: str) -> dict[str, list[FileChange]]:
        """The ticked files `sync_selected` would delete, for the caller to
        confirm before it does — deletions are their own category (spec.md §8)."""
        return self._changes(
            rule_id, lambda review: selection_by_bucket(review, self._selected[rule_id], "delete")
        )

    def sync_selected(self, rule_id: str) -> SyncResult | None:
        """Syncs the ticked files, or returns None when there is nothing to sync."""
        return self._apply(rule_id, self._selected_changes(rule_id))

    def sync_all_safe(self, rule_id: str) -> SyncResult | None:
        """Syncs every safe change in the rule, ignoring the tick selection."""
        return self._apply(rule_id, self._changes(rule_id, sync_all_safe_changes))

    def _selected_changes(self, rule_id: str) -> dict[str, list[FileChange]]:
        return self._changes(
            rule_id, lambda review: resolve_selection(review, self._selected[rule_id])
        )

    def _changes(
        self, rule_id: str, gather: Callable[[ReviewRule], dict[str, list[FileChange]]]
    ) -> dict[str, list[FileChange]]:
        """`gather` applied to the rule's tree, or `{}` before its first check
        — the one place the "no tree yet" case is spelled out for the methods
        that produce sync candidates."""
        review = self.tree(rule_id)
        return {} if review is None else gather(review)

    def overwrite(
        self, rule_id: str, changes_by_replica: dict[str, list[FileChange]]
    ) -> SyncResult:
        """"Overwrite from master" for conflicts (spec.md §9): a plain sync of
        `changes_by_replica`, the caller having confirmed it discards local
        edits. Leaves the tick selection alone — unlike `sync_selected`, the
        changes didn't come from it."""
        result = run_sync(
            self._rules_by_id[rule_id],
            changes_by_replica,
            self._state,
            self._layout.state_path,
            self._layout.logs_dir,
        )
        self._state = result.state
        return result

    def keep(self, rule_id: str, changes_by_replica: dict[str, list[FileChange]]) -> None:
        """"Keep replica's version" (spec.md §9). All or nothing: an `OSError`
        propagates having merged and saved nothing."""
        self._state = apply_keep_replica(
            self._rules_by_id[rule_id], changes_by_replica, self._state, self._layout.state_path
        )

    def _apply(self, rule_id: str, changes: dict[str, list[FileChange]]) -> SyncResult | None:
        """A sync that came from the tick selection: clears it once applied,
        and reports None rather than syncing nothing."""
        if not changes:
            return None
        result = self.overwrite(rule_id, changes)
        self._selected[rule_id] = frozenset()
        return result
