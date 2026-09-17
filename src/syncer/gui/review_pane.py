"""Qt adapter for the review-and-sync screen (issue #6, spec.md §7).

Thin wiring only: every tree-shape, bucketing, and selection-roll-up decision
lives in `syncer.review` (pure, Qt-free, unit-tested). This module owns
widgets and Qt signals and nothing else — it is not covered by the TDD loop,
and is verified by running the app rather than by pytest.

Conflict resolution (issue #7) is wired in via `syncer.gui.conflict_dialog`:
the "Resolve conflicts" button and each conflict leaf's "Resolve" cell open
`ConflictDialog`; the replica branch's context menu offers the per-category
bulk actions. Rule add/edit/delete itself lives in `syncer.gui.main_window`
(issue #8); `apply_config`/`check_rule` below are this pane's side of that
wiring — the rule list and its check state, not the add/edit/delete UI.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSignalBlocker, QThread, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from syncer.check import (
    CheckResult,
    NamespaceCollision,
    check,
    collisions_for_rule,
    find_namespace_collisions,
    other_rule_names,
)
from syncer.config import SyncRule, master_basename
from syncer.conflict import apply_keep_replica, bulk_candidates_by_category, conflict_queue
from syncer.gui.conflict_dialog import ConflictDialog, bulk_overwrite
from syncer.review import (
    BULK_CATEGORIES,
    CATEGORY_LABEL,
    LeafKey,
    ReviewLeaf,
    ReviewMaster,
    ReviewReplica,
    ReviewRule,
    blocked_master_count,
    build_review_rule,
    has_drift,
    is_master_blocked,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    toggle,
    visible_replica,
)
from syncer.state import State, baseline_for_rule
from syncer.sync import sync as run_sync

_CHECK_STATE = {
    "checked": Qt.Checked,
    "partial": Qt.PartiallyChecked,
    "unchecked": Qt.Unchecked,
}

_TALLY_WORDING = (("safe", "to sync"), ("delete", "to delete"), ("conflict", "conflict"))

_GREY = QColor("#9a9a9a")

# The ReviewReplica/ReviewMaster/ReviewFolder/ReviewLeaf a tree item mirrors —
# from visible_replica, so a blocked master-subfolder's hidden files are absent.
ROLE_NODE = Qt.UserRole + 1

# Minimum gap between cross-thread progress signals; check() reports per file.
_PROGRESS_INTERVAL_S = 0.05

# Paths shown inline in the delete confirm before deferring to its details pane.
_DELETE_PREVIEW_LINES = 15


def _tally_text(counts: dict[str, int]) -> str:
    bits = [f"{counts[bucket]} {words}" for bucket, words in _TALLY_WORDING if counts[bucket]]
    return ", ".join(bits) if bits else "in sync"


def _master_label(node: ReviewMaster) -> str:
    return f"{node.landing_path}  ({node.master.type})"


def _make_inert(item: QTreeWidgetItem) -> None:
    """Greyed, no tick box. Greying is the brush's job, not ItemIsEnabled's:
    disabled items don't receive itemClicked, which a resolvable or unlockable
    row needs. _apply_selection_to_tree skips non-checkable items, so no inert
    check indicator is painted either."""
    item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
    for column in (0, 1):
        item.setForeground(column, QBrush(_GREY))


class CheckWorker(QThread):
    """Runs `check()` for one rule off the UI thread — spec.md §7: "wires
    check engine's (issue #3) progress/cancel to a QThread"."""

    progress = Signal(int, int, str)
    # (CheckResult, cancelled) — a cancelled check's result is truncated.
    check_finished = Signal(object, bool)

    def __init__(
        self, rule: SyncRule, baseline: dict, collisions: list[NamespaceCollision], parent=None
    ):
        super().__init__(parent)
        self.rule = rule
        self.collisions = collisions
        self._baseline = baseline
        self._cancelled = False
        self._last_emit = 0.0

    def cancel(self) -> None:
        self._cancelled = True

    def _report(self, done: int, total: int, path: str) -> None:
        now = time.monotonic()
        if done == total or now - self._last_emit >= _PROGRESS_INTERVAL_S:
            self._last_emit = now
            self.progress.emit(done, total, path)

    def run(self) -> None:
        result = check(
            self.rule,
            baseline=self._baseline,
            progress=self._report,
            cancel=lambda: self._cancelled,
            collisions=self.collisions,
        )
        self.check_finished.emit(result, self._cancelled)


class ReviewPane(QWidget):
    """The two-pane review screen: rule list on the left (variant D of the
    ticket-06 prototype), `replica > master > folder > file` tree on the right for
    whichever rule is selected. Embeddable — owns no top-level window and no
    app-level concerns (config/state persistence stays with the host).
    """

    conflictsRequested = Signal(str, str)  # rule_id, replica_path — issue #7's hook

    def __init__(
        self,
        rules: list[SyncRule],
        state: State,
        state_path: Path,
        logs_dir: Path,
        parent=None,
    ):
        super().__init__(parent)
        # Insertion-ordered: also the left pane's row order.
        self._rules_by_id: dict[str, SyncRule] = {rule.id: rule for rule in rules}
        # Recomputed only where _rules_by_id is (here and apply_config) — not
        # per rule checked, since check_all() checks every rule in turn.
        self._namespace_collisions: list[NamespaceCollision] = find_namespace_collisions(rules)
        self._state = state
        self._state_path = state_path
        self._logs_dir = logs_dir

        self._review: dict[str, ReviewRule] = {}
        self._selected: defaultdict[str, frozenset[LeafKey]] = defaultdict(frozenset)
        # rule_id -> the landing_paths the user has unlocked this session
        # (spec.md §8: per-master-subfolder now, issue #24 — narrowed from a
        # single rule-wide unlock).
        self._unlocked: defaultdict[str, frozenset[str]] = defaultdict(frozenset)
        self._current_rule_id: str | None = None
        self._pending_check_ids: list[str] = []
        self._worker: CheckWorker | None = None
        self._check_cancelled = False

        app = QCoreApplication.instance()
        if app is not None:
            # A QThread destroyed mid-run aborts the process; stop it first.
            app.aboutToQuit.connect(self._stop_worker)

        self.rule_list = QListWidget()
        self.rule_list.setMaximumWidth(360)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Replica / folder / file", "Change", ""])
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)

        self.status_label = QLabel("Not checked yet.")
        self.status_label.setWordWrap(True)

        self.btn_check = QPushButton("Check")
        self.btn_cancel_check = QPushButton("Cancel check")
        self.btn_cancel_check.hide()
        self.btn_sync_selected = QPushButton("Sync selected")
        self.btn_sync_all_safe = QPushButton("Sync all safe changes")
        self.btn_resolve = QPushButton("Resolve conflicts →")
        self.btn_check.clicked.connect(self.check_all)
        self.btn_cancel_check.clicked.connect(self._cancel_check)
        self.btn_sync_selected.clicked.connect(self._sync_selected)
        self.btn_sync_all_safe.clicked.connect(self._sync_all_safe)
        self.btn_resolve.clicked.connect(self._request_conflict_resolution)

        actions = QFrame()
        actions.setFrameShape(QFrame.StyledPanel)
        action_row = QHBoxLayout()
        for button in (
            self.btn_check,
            self.btn_cancel_check,
            self.btn_sync_selected,
            self.btn_sync_all_safe,
            self.btn_resolve,
        ):
            action_row.addWidget(button)
        action_row.addStretch(1)
        action_lay = QVBoxLayout(actions)
        action_lay.addWidget(self.status_label)
        action_lay.addLayout(action_row)

        split = QSplitter()
        split.addWidget(self.rule_list)
        split.addWidget(self.tree)
        split.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addWidget(actions)

        self.rule_list.currentRowChanged.connect(self._on_rule_row_changed)

    def _current_review(self) -> tuple[str, ReviewRule] | None:
        rule_id = self._current_rule_id
        review_rule = self._review.get(rule_id) if rule_id else None
        return None if review_rule is None else (rule_id, review_rule)

    # -- checking ------------------------------------------------------

    def check_all(self) -> None:
        """(Re-)check every rule, one at a time. Each completed re-check
        re-blocks any of its masters still missing (spec.md §8: the unlock is
        in-session only)."""
        self._pending_check_ids = list(self._rules_by_id)
        self._run_next_check()

    def check_rule(self, rule_id: str) -> None:
        """Public entry point for main_window's toolbar/context-menu "Check"
        action (spec.md §10), which targets one rule rather than every rule."""
        self._queue_check(rule_id)

    @property
    def state(self) -> State:
        """The one in-memory `State`: every sync/keep/resolve replaces it here,
        so main_window reads it back rather than keeping its own copy."""
        return self._state

    @property
    def current_rule_id(self) -> str | None:
        return self._current_rule_id

    def apply_config(self, rules: list[SyncRule], state: State) -> None:
        """The rule set and/or state changed outside the normal check/sync
        flow — an add/edit/delete-rule action, or a config-reload's
        reconciliation purge (spec.md §10). Per-rule session state (review,
        selection, unlock) survives only for a rule whose definition is
        unchanged — an edited rule's old review would show stale replicas.
        Rebuilds the left pane's rows, keeping the current selection when the
        selected rule survives.
        """
        old_rules_by_id = self._rules_by_id
        old_collisions = self._namespace_collisions
        self._rules_by_id = {rule.id: rule for rule in rules}
        self._namespace_collisions = find_namespace_collisions(rules)
        self._state = state
        # Another rule's edit can add or clear a collision on an otherwise
        # unchanged rule, so its cached review is stale then too.
        unchanged = {
            k
            for k, rule in self._rules_by_id.items()
            if old_rules_by_id.get(k) == rule
            and collisions_for_rule(k, old_collisions)
            == collisions_for_rule(k, self._namespace_collisions)
        }
        self._review = {k: v for k, v in self._review.items() if k in unchanged}
        self._selected = defaultdict(
            frozenset, {k: v for k, v in self._selected.items() if k in unchanged}
        )
        self._unlocked = defaultdict(
            frozenset, {k: v for k, v in self._unlocked.items() if k in unchanged}
        )
        self._pending_check_ids = [k for k in self._pending_check_ids if k in self._rules_by_id]

        previous_rule_id = self._current_rule_id
        with QSignalBlocker(self.rule_list):
            self.rule_list.clear()  # empties the list, so the refresh recreates rows
            self._refresh_rule_list()
        ids = list(self._rules_by_id)  # also the row order
        if not ids:
            self._on_rule_row_changed(-1)
            return
        rule_id = previous_rule_id if previous_rule_id in self._rules_by_id else ids[0]
        if rule_id == previous_rule_id and rule_id in unchanged:
            with QSignalBlocker(self.rule_list):
                self.rule_list.setCurrentRow(ids.index(rule_id))  # tree is still accurate
        else:
            self.rule_list.setCurrentRow(ids.index(rule_id))  # fires _on_rule_row_changed -> _show_rule

    def _queue_check(self, rule_id: str) -> None:
        """Adds one rule to the check queue without dropping rules already
        queued (e.g. by an in-progress "Check")."""
        if rule_id not in self._pending_check_ids:
            self._pending_check_ids.append(rule_id)
        self._run_next_check()

    def _cancel_check(self) -> None:
        """Cancels the in-flight check and drops every rule still queued
        behind it — a plain "stop checking" rather than "skip this rule"."""
        if self._worker is not None:
            self._worker.cancel()
            self._check_cancelled = True
        self._pending_check_ids.clear()

    def _stop_worker(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait()

    def _run_next_check(self) -> None:
        if self._worker is not None:
            # Already mid-check; its `finished` drains the queue next — avoids
            # two CheckWorkers racing on the same widget state. (Cleared on
            # QThread.finished, not tested via isRunning(): check_finished can
            # be delivered while the thread is still winding down.)
            return
        if not self._pending_check_ids:
            self.status_label.setText(
                "Check cancelled." if self._check_cancelled else "Check complete."
            )
            self._check_cancelled = False
            self.btn_cancel_check.hide()
            self.btn_check.setEnabled(True)
            return
        rule_id = self._pending_check_ids.pop(0)
        rule = self._rules_by_id[rule_id]
        self.status_label.setText(f"Checking {rule.name}…")
        self.btn_check.setEnabled(False)
        self.btn_cancel_check.show()
        self._worker = CheckWorker(
            rule, baseline_for_rule(self._state, rule_id), self._namespace_collisions, self
        )
        self._worker.progress.connect(self._on_check_progress)
        self._worker.check_finished.connect(self._on_check_finished)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_check_progress(self, done: int, total: int, current_path: str) -> None:
        self.status_label.setText(f"Checking… {done}/{total}  {current_path}")

    def _on_check_finished(self, result: CheckResult, cancelled: bool) -> None:
        if cancelled:
            # Truncated: rendering it would under-report drift (even "in sync").
            return
        current_rule = self._rules_by_id.get(result.rule_id)
        # Still set here: _worker is cleared on QThread.finished, queued after this.
        worker = self._worker
        if (
            current_rule is None
            or worker.rule != current_rule
            or collisions_for_rule(result.rule_id, worker.collisions)
            != collisions_for_rule(result.rule_id, self._namespace_collisions)
        ):
            # The rule was deleted or edited (apply_config) mid-check, or
            # another rule's edit changed its collisions: this result
            # describes the old config, so syncing it could write to a replica
            # or namespace no longer valid. Re-check the rule.
            if current_rule is not None and result.rule_id not in self._pending_check_ids:
                self._pending_check_ids.append(result.rule_id)
            return
        self._unlocked.pop(result.rule_id, None)  # a re-check re-blocks (spec.md §8)
        # Equal to the pane's for this rule (checked above).
        self._review[result.rule_id] = build_review_rule(result, collisions=worker.collisions)
        self._refresh_rule_list()
        if self.rule_list.currentRow() < 0 and self.rule_list.count():
            self.rule_list.setCurrentRow(0)  # fires _on_rule_row_changed -> _show_rule
        elif result.rule_id == self._current_rule_id:
            self._show_rule(result.rule_id)

    def _on_worker_finished(self) -> None:
        # Queued after check_finished, so the result is already applied.
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._run_next_check()

    # -- left pane -------------------------------------------------------

    def _rule_list_text(self, rule_id: str) -> str:
        sync_rule = self._rules_by_id[rule_id]
        review_rule = self._review.get(rule_id)
        masters = ", ".join(master_basename(m.path) for m in sync_rule.masters)
        if review_rule is None:
            status = "not checked yet"
        else:
            unlocked = self._unlocked[rule_id]
            drift = _tally_text(rule_tally(review_rule, unlocked))
            n_blocked = blocked_master_count(review_rule, unlocked)
            if n_blocked:
                drift += f", {n_blocked} blocked"
            n_rep = len(sync_rule.replicas)
            status = f"{n_rep} replica{'s' if n_rep != 1 else ''}  ·  {drift}"
        return f"{sync_rule.name}\n   {masters}\n   {status}"

    def _refresh_rule_list(self) -> None:
        """Rewrites row texts in place (rows are created once, lazily) so the
        current row — and with it the right-pane tree — is left untouched."""
        if self.rule_list.count() == 0:
            with QSignalBlocker(self.rule_list):
                for rule_id in self._rules_by_id:
                    item = QListWidgetItem()
                    item.setData(Qt.UserRole, rule_id)
                    self.rule_list.addItem(item)
        for row, rule_id in enumerate(self._rules_by_id):
            self.rule_list.item(row).setText(self._rule_list_text(rule_id))

    def _on_rule_row_changed(self, row: int) -> None:
        if row < 0:
            self._current_rule_id = None
            self.tree.clear()
            return
        self._current_rule_id = self.rule_list.item(row).data(Qt.UserRole)
        self._show_rule(self._current_rule_id)

    # -- right pane --------------------------------------------------------

    def _show_rule(self, rule_id: str) -> None:
        review_rule = self._review.get(rule_id)
        unlocked = self._unlocked[rule_id]
        with QSignalBlocker(self.tree):
            self.tree.clear()
            if review_rule is not None:
                # Built from the visible replicas, so a blocked master's hidden
                # files never feed a replica's toggle/tick state or expansion.
                for replica in review_rule.replicas:
                    self._build_item(self.tree, visible_replica(replica, unlocked), unlocked)
        if review_rule is not None:
            self._apply_selection_to_tree(self._selected[rule_id])
            self._expand_drifted()
        self._refresh_bar()

    def _collision_banner(self, rule_id: str, collision: NamespaceCollision) -> str:
        others = other_rule_names(collision, rule_id, self._rules_by_id)
        return (
            f"Also claimed by: {', '.join(others) or 'another rule'} — rename a master or "
            "stop sharing this replica to fix."
        )

    def _build_item(self, parent, node, unlocked: frozenset[str]) -> QTreeWidgetItem:
        if isinstance(node, ReviewLeaf):
            item = QTreeWidgetItem(parent, [node.name, node.label, ""])
            if node.checkable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            else:
                _make_inert(item)
                if node.resolvable:
                    item.setText(2, "Resolve →")
        elif isinstance(node, ReviewMaster) and is_master_blocked(node, unlocked):
            # Banner-only: visible_replica already dropped its children.
            if node.collision is not None:
                change, action = self._collision_banner(self._current_rule_id, node.collision), ""
            else:
                change = "Master is missing — ordinary sync is blocked for this namespace."
                action = "Unlock →"
            item = QTreeWidgetItem(parent, [_master_label(node), change, action])
            _make_inert(item)
        else:  # ReviewReplica, unblocked ReviewMaster, or ReviewFolder
            if isinstance(node, ReviewReplica):
                label = node.replica_path
                if not node.replica_exists:
                    label += "  (missing — will be created)"
            elif isinstance(node, ReviewMaster):
                label = _master_label(node)
            else:
                label = node.name
            item = QTreeWidgetItem(parent, [label, "", ""])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            bold = item.font(0)
            bold.setWeight(QFont.Bold)
            item.setFont(0, bold)
            for child in node.children:
                self._build_item(item, child, unlocked)
        item.setData(0, ROLE_NODE, node)
        return item

    def _iter_tree_items(self) -> Iterator[QTreeWidgetItem]:
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _apply_selection_to_tree(self, selected: frozenset[LeafKey]) -> None:
        with QSignalBlocker(self.tree):
            for item in self._iter_tree_items():
                node = item.data(0, ROLE_NODE)
                # A non-checkable item (context/conflict leaf, blocked
                # master-subfolder) has no tick to set — calling setCheckState
                # on one paints an indicator that can't be used.
                if node is None or not item.flags() & Qt.ItemIsUserCheckable:
                    continue
                item.setCheckState(0, _CHECK_STATE[node_state(node, selected)])

    def _expand_drifted(self) -> None:
        for item in self._iter_tree_items():
            node = item.data(0, ROLE_NODE)
            if node is not None and not isinstance(node, ReviewLeaf):
                item.setExpanded(has_drift(node))

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0 or self._current_rule_id is None:
            return
        node = item.data(0, ROLE_NODE)
        if node is None:
            return
        rule_id = self._current_rule_id
        self._selected[rule_id] = toggle(node, self._selected[rule_id])
        self._apply_selection_to_tree(self._selected[rule_id])
        self._refresh_bar()

    # -- action bar --------------------------------------------------------

    def _refresh_bar(self) -> None:
        current = self._current_review()
        if current is None:
            for button in (self.btn_sync_selected, self.btn_sync_all_safe, self.btn_resolve):
                button.setEnabled(False)
            return
        rule_id, review_rule = current
        unlocked = self._unlocked[rule_id]
        # A blocked master-subfolder already contributes nothing to these —
        # rule_tally/resolve_selection exclude it (issue #24) — so the
        # buttons need no separate blocked gate; a still-blocked namespace
        # just never shows up in the counts they act on.
        counts = rule_tally(review_rule, unlocked)
        selection = resolve_selection(review_rule, self._selected[rule_id], unlocked)
        n_selected = sum(map(len, selection.values()))
        self.btn_sync_selected.setText(f"Sync selected ({n_selected})")
        self.btn_sync_selected.setEnabled(n_selected > 0)
        self.btn_sync_all_safe.setText(f"Sync all safe changes ({counts['safe']})")
        self.btn_sync_all_safe.setEnabled(counts["safe"] > 0)
        self.btn_resolve.setText(f"Resolve conflicts → ({counts['conflict']})")
        self.btn_resolve.setEnabled(counts["conflict"] > 0)

    def _unlock_master(self, rule_id: str, landing_path: str) -> None:
        self._unlocked[rule_id] |= {landing_path}
        self._refresh_rule_list()
        self._show_rule(rule_id)

    def _request_conflict_resolution(self) -> None:
        current = self._current_review()
        if current is not None:
            self._open_conflict_dialog(current[0])

    # -- conflict resolution (issue #7) -------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 2 or self._current_rule_id is None:
            return
        node = item.data(0, ROLE_NODE)
        if isinstance(node, ReviewLeaf) and node.resolvable:
            self._run_conflict_dialog(self._current_rule_id, [node])
        elif (
            isinstance(node, ReviewMaster)
            and node.unlockable
            and node.landing_path not in self._unlocked[self._current_rule_id]
        ):
            self._unlock_master(self._current_rule_id, node.landing_path)

    def _open_conflict_dialog(self, rule_id: str, replica_path: str | None = None) -> None:
        """Queues every conflict in the rule, or just one replica's when
        `replica_path` is given. Blocked master-subfolders are pruned first
        (visible_replica) so a still-missing or collided namespace's
        conflicts never enter the queue (issue #24)."""
        review_rule = self._review.get(rule_id)
        if review_rule is None:
            return
        unlocked = self._unlocked[rule_id]
        nodes = [
            visible_replica(r, unlocked)
            for r in review_rule.replicas
            if replica_path is None or r.replica_path == replica_path
        ]
        queue = conflict_queue(nodes)
        if queue:
            self._run_conflict_dialog(rule_id, queue)

    def _run_conflict_dialog(self, rule_id: str, queue: list[ReviewLeaf]) -> None:
        rule = self._rules_by_id[rule_id]
        dialog = ConflictDialog(rule, queue, self._state, self._state_path, self._logs_dir, self)
        dialog.exec()
        if not dialog.resolved_any:
            return  # closed or skipped through — nothing on disk changed
        self._state = dialog.state
        self._queue_check(rule_id)

    def _show_tree_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        rule_id = self._current_rule_id
        if item is None or rule_id is None:
            return
        node = item.data(0, ROLE_NODE)
        if not isinstance(node, ReviewReplica):
            return
        # Bind the path, not the node: a lambda closing over `node` would pin
        # this whole replica subtree alive for as long as the menu's actions.
        replica_path = node.replica_path
        # Already a visible_replica (_show_rule), so a blocked master-subfolder's
        # files never enter a bulk action either.
        by_category = bulk_candidates_by_category([node])
        menu = QMenu(self)
        # Deleted with the menu rather than living on as a child of the pane.
        menu.setAttribute(Qt.WA_DeleteOnClose)
        # Per-replica queue entry point (spec.md §9).
        n_conflicts = len(conflict_queue([node]))
        if n_conflicts:
            action = menu.addAction(f"Resolve conflicts ({n_conflicts})")
            action.triggered.connect(
                lambda checked=False: self._open_conflict_dialog(rule_id, replica_path)
            )
            menu.addSeparator()
        for category in BULK_CATEGORIES:
            candidates = by_category.get(category)
            if not candidates:
                continue
            label = CATEGORY_LABEL[category]
            for verb, handler in (
                ("Keep all as-is", self._bulk_keep),
                ("Overwrite all from master", self._bulk_overwrite),
            ):
                action = menu.addAction(f"{verb} — {label} ({len(candidates)})")
                action.triggered.connect(
                    lambda checked=False, h=handler, c=candidates: h(rule_id, replica_path, c)
                )
        if menu.isEmpty():
            menu.close()
            return
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _bulk_keep(self, rule_id: str, replica_path: str, changes) -> None:
        rule = self._rules_by_id[rule_id]
        try:
            self._state = apply_keep_replica(
                rule, replica_path, changes, self._state, self._state_path
            )
        except OSError as exc:
            # Nothing is merged or saved unless every file could be read.
            QMessageBox.warning(self, "Couldn't keep replica versions", str(exc))
            return
        self._queue_check(rule_id)

    def _bulk_overwrite(self, rule_id: str, replica_path: str, changes) -> None:
        rule = self._rules_by_id[rule_id]
        new_state = bulk_overwrite(
            self, rule, replica_path, changes, self._state, self._state_path, self._logs_dir
        )
        if new_state is None:
            return  # cancelled at the confirm prompt — nothing applied
        self._state = new_state
        self._queue_check(rule_id)

    def _sync_all_safe(self) -> None:
        current = self._current_review()
        if current is not None:
            rule_id, review_rule = current
            self._apply_changes(
                rule_id, sync_all_safe_changes(review_rule, self._unlocked[rule_id])
            )

    def _sync_selected(self) -> None:
        current = self._current_review()
        if current is None:
            return
        rule_id, review_rule = current
        unlocked = self._unlocked[rule_id]
        selected = self._selected[rule_id]
        delete_changes = selection_by_bucket(review_rule, selected, "delete", unlocked)
        if delete_changes:
            lines = [
                f"{path}: {change.rel_path}"
                for path, changes in delete_changes.items()
                for change in changes
            ]
            n_files = len(lines)
            # The body is capped so a huge batch can't push the buttons off
            # screen; the full itemised list stays in the scrollable details.
            preview = "\n".join(lines[:_DELETE_PREVIEW_LINES])
            if n_files > _DELETE_PREVIEW_LINES:
                preview += f"\n…and {n_files - _DELETE_PREVIEW_LINES} more (Show Details)"
            box = QMessageBox(QMessageBox.Question, "Confirm deletions", "", parent=self)
            box.setText(
                f"This deletes {n_files} file(s) that were removed from the master:\n\n{preview}"
            )
            box.setDetailedText("\n".join(lines))
            confirm = box.addButton(f"Yes, delete {n_files} files", QMessageBox.AcceptRole)
            box.setDefaultButton(box.addButton(QMessageBox.Cancel))
            box.exec()
            if box.clickedButton() is not confirm:
                return
        # sync() orders copies before deletions itself (sync.py), so the
        # safe and delete picks need no separate batching here.
        self._apply_changes(rule_id, resolve_selection(review_rule, selected, unlocked))

    def _apply_changes(self, rule_id: str, applied_changes: dict) -> None:
        if not applied_changes:
            return
        sync_rule = self._rules_by_id[rule_id]
        result = run_sync(
            sync_rule, applied_changes, self._state, self._state_path, self._logs_dir
        )
        self._state = result.state
        self._selected[rule_id] = frozenset()
        if result.errors:
            detail = "\n".join(f"{e.rel_path}: {e.message}" for e in result.errors)
            QMessageBox.warning(
                self,
                "Sync finished with errors",
                f"Copied {result.copied}, deleted {result.deleted}, "
                f"{len(result.errors)} error(s):\n\n{detail}",
            )
        else:
            QMessageBox.information(
                self, "Sync finished", f"Copied {result.copied}, deleted {result.deleted}."
            )
        # Re-check so the tree reflects the post-sync reality.
        self._queue_check(rule_id)
