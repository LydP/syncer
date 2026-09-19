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
    MasterLayout,
    NamespaceCollision,
    check,
    collisions_for_rule,
    find_namespace_collisions,
    other_rule_names,
    scan_master_layout,
)
from syncer.config import Config, SyncRule, master_basename, replica_label
from syncer.conflict import apply_keep_replica, bulk_candidates_by_category, conflict_queue
from syncer.gui.conflict_dialog import ConflictDialog, bulk_overwrite
from syncer.review import (
    CATEGORY_LABEL,
    CONFLICT_CATEGORIES,
    LeafKey,
    ReviewLeaf,
    ReviewMaster,
    ReviewReplica,
    ReviewRule,
    blocked_master_count,
    build_preview_rule,
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

# The tree's first column: opening width, and the floor it can't be dragged
# below (collapsed, the names vanish while the tick boxes stay).
_NAME_COLUMN_WIDTH = 320
_MIN_NAME_COLUMN_WIDTH = 160


def _tally_text(counts: dict[str, int]) -> str:
    bits = [f"{counts[bucket]} {words}" for bucket, words in _TALLY_WORDING if counts[bucket]]
    return ", ".join(bits) if bits else "in sync"


def _make_inert(item: QTreeWidgetItem) -> None:
    """Greyed, no tick box. Greying is the brush's job, not ItemIsEnabled's:
    disabled items don't receive itemClicked, which a resolvable or unlockable
    row needs. _apply_selection_to_tree skips non-checkable items, so no inert
    check indicator is painted either."""
    item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
    for column in (0, 1):
        item.setForeground(column, QBrush(_GREY))


def _inert_item(parent, name: str, change: str = "", action: str = "") -> QTreeWidgetItem:
    item = QTreeWidgetItem(parent, [name, change, action])
    _make_inert(item)
    return item


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


class PreviewWorker(QThread):
    """Walks one rule's masters by name off the UI thread, so selecting a rule
    with a large master can't freeze the window (no hashing, no replica read)."""

    # (SyncRule, MasterLayout) — the rule rides along so the receiver can tell
    # whether it was edited or deselected while the walk ran.
    preview_ready = Signal(object, object)
    # (SyncRule, message)
    preview_failed = Signal(object, str)

    def __init__(self, rule: SyncRule, parent=None):
        super().__init__(parent)
        self.rule = rule

    def run(self) -> None:
        try:
            layout = scan_master_layout(self.rule.masters)
        except Exception as exc:
            # A bug, not a user-facing failure — but an exception escaping a
            # thread is only printed, and a frozen build has no console, so the
            # tree would sit on "Scanning masters…" forever.
            self.preview_failed.emit(self.rule, f"Couldn't scan the masters: {exc!r}")
        else:
            self.preview_ready.emit(self.rule, layout)


class ReviewPane(QWidget):
    """The two-pane review screen: rule list on the left (variant D of the
    ticket-06 prototype), `replica > master > folder > file` tree on the right for
    whichever rule is selected. Embeddable — owns no top-level window and no
    app-level concerns (config/state persistence stays with the host).
    """

    conflictsRequested = Signal(str, str)  # rule_id, replica_path — issue #7's hook
    # True while any rule is being checked or queued to be; the host gates
    # actions that mustn't overlap a check (e.g. an app update) on it.
    checkingChanged = Signal(bool)

    def __init__(
        self,
        config: Config,
        state: State,
        state_path: Path,
        logs_dir: Path,
        parent=None,
    ):
        super().__init__(parent)
        self._config = config
        # Insertion-ordered: also the left pane's row order.
        self._rules_by_id: dict[str, SyncRule] = {rule.id: rule for rule in config.rules}
        # Recomputed only where _rules_by_id is (here and apply_config) — not
        # per rule checked, since check_all() checks every rule in turn.
        self._namespace_collisions: list[NamespaceCollision] = find_namespace_collisions(config.rules)
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
            app.aboutToQuit.connect(self._stop_workers)

        self.rule_list = QListWidget()
        self.rule_list.setMaximumWidth(360)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Replica / folder / file", "Change", ""])
        self.tree.setColumnWidth(0, _NAME_COLUMN_WIDTH)
        self.tree.header().sectionResized.connect(self._clamp_name_column)
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
        # Populate from the initial rules: a launch with an existing config.toml
        # never goes through apply_config, so the pane seeds itself here.
        self._repopulate_and_select(None, set())

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

    def apply_config(self, config: Config, state: State) -> None:
        """The rule set and/or state changed outside the normal check/sync
        flow — an add/edit/delete-rule action, or a config-reload's
        reconciliation purge (spec.md §10). Per-rule session state (review,
        selection, unlock) survives only for a rule whose definition is
        unchanged — an edited rule's old review would show stale replicas.
        Rebuilds the left pane's rows, keeping the current selection when the
        selected rule survives. A changed replica name relabels the tree
        without invalidating any review.
        """
        old_rules_by_id = self._rules_by_id
        old_collisions = self._namespace_collisions
        names_changed = config.replica_names != self._config.replica_names
        self._config = config
        self._rules_by_id = {rule.id: rule for rule in config.rules}
        self._namespace_collisions = find_namespace_collisions(config.rules)
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

        self._repopulate_and_select(self._current_rule_id, unchanged)
        if names_changed:
            # The tree shows names, so a rename relabels it even for a rule
            # whose cached review (and so its tree) is still accurate.
            self._relabel_replica_items()

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

    def _stop_workers(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait()
        # Preview walks can't be cancelled, only waited out.
        for worker in self.findChildren(PreviewWorker):
            worker.wait()

    def _clamp_name_column(self, index: int, _old: int, new: int) -> None:
        if index == 0 and new < _MIN_NAME_COLUMN_WIDTH:
            self.tree.setColumnWidth(0, _MIN_NAME_COLUMN_WIDTH)

    def _set_checking(self, checking: bool) -> None:
        """The one place the Check/Cancel buttons and `checkingChanged` change,
        so they can't drift apart; emits only on an actual transition."""
        if self.btn_check.isEnabled() != checking:
            return
        self.btn_check.setEnabled(not checking)
        self.btn_cancel_check.setVisible(checking)
        self.checkingChanged.emit(checking)

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
            self._set_checking(False)
            return
        rule_id = self._pending_check_ids.pop(0)
        rule = self._rules_by_id[rule_id]
        self.status_label.setText(f"Checking {rule.name}…")
        self._set_checking(True)
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
        if result.rule_id == self._current_rule_id:
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

    def _repopulate_and_select(self, preferred_rule_id: str | None, unchanged: set[str]) -> None:
        """Rebuilds the left pane's rows and lands on a row: `preferred_rule_id`
        if it survived the rule set changing, else the first one. The single
        place that decides which row is current afterwards — `__init__` and
        `apply_config` both come through here, so a pane is never left
        unpopulated or without a selection. `unchanged` names the rules whose
        cached review is still accurate, so reselecting one can skip the
        rebuild of the right pane.
        """
        with QSignalBlocker(self.rule_list):
            self.rule_list.clear()  # empties the list, so the refresh recreates rows
            self._refresh_rule_list()
        ids = list(self._rules_by_id)  # also the row order
        if not ids:
            self._on_rule_row_changed(-1)
            return
        rule_id = preferred_rule_id if preferred_rule_id in self._rules_by_id else ids[0]
        if rule_id == preferred_rule_id and rule_id in unchanged:
            with QSignalBlocker(self.rule_list):
                self.rule_list.setCurrentRow(ids.index(rule_id))  # tree is still accurate
        else:
            self.rule_list.setCurrentRow(ids.index(rule_id))  # fires _on_rule_row_changed -> _show_rule

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
        if not self._rules_by_id[rule_id].replicas:
            self._show_note("No replicas yet")
            return
        review_rule = self._review.get(rule_id)
        if review_rule is None:
            self._show_unchecked(rule_id)
            return
        self._populate_tree(review_rule, self._unlocked[rule_id])
        self._apply_selection_to_tree(self._selected[rule_id])
        self._expand_drifted()
        self._refresh_bar()

    def _populate_tree(
        self, review_rule: ReviewRule, unlocked: frozenset[str], preview: bool = False
    ) -> None:
        with QSignalBlocker(self.tree):
            self.tree.clear()
            # Built from the visible replicas, so a blocked master's hidden
            # files never feed a replica's toggle/tick state or expansion.
            for replica in review_rule.replicas:
                self._build_item(self.tree, visible_replica(replica, unlocked), unlocked, preview)

    def _show_note(self, text: str) -> None:
        with QSignalBlocker(self.tree):
            self.tree.clear()
            _inert_item(self.tree, text)
        self._refresh_bar()

    def _show_unchecked(self, rule_id: str) -> None:
        """A rule with no check result yet (or whose result an edit or reload
        discarded): shows what each replica should hold per the masters, from a
        names-only walk on a worker thread. Checks stay on-demand."""
        self._show_note("Scanning masters…")
        # Parented to the pane, which keeps it alive until deleteLater.
        worker = PreviewWorker(self._rules_by_id[rule_id], self)
        worker.preview_ready.connect(self._on_preview_ready)
        worker.preview_failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _preview_is_stale(self, rule: SyncRule) -> bool:
        # The user moved on, or the rule changed, while it walked — or a check
        # landed first, and its tree is the truth.
        return self._rules_by_id.get(self._current_rule_id) != rule or rule.id in self._review

    def _on_preview_failed(self, rule: SyncRule, message: str) -> None:
        if not self._preview_is_stale(rule):
            self._show_note(message)

    def _on_preview_ready(self, rule: SyncRule, layout: MasterLayout) -> None:
        if self._preview_is_stale(rule):
            return
        preview = build_preview_rule(rule, layout, self._namespace_collisions)
        self._populate_tree(preview, frozenset(), preview=True)
        self.tree.expandToDepth(0)

    def _collision_banner(self, rule_id: str, collision: NamespaceCollision) -> str:
        others = other_rule_names(collision, rule_id, self._rules_by_id)
        return (
            f"Also claimed by: {', '.join(others) or 'another rule'} — rename a master or "
            "stop sharing this replica to fix."
        )

    def _build_item(
        self, parent, node, unlocked: frozenset[str], preview: bool = False
    ) -> QTreeWidgetItem:
        """`preview` draws the pre-check tree: replicas read "Not checked", and
        nothing is tickable or unlockable until a check runs."""
        if isinstance(node, ReviewMaster) and is_master_blocked(node, unlocked):
            # Banner-only: visible_replica already dropped its children.
            if node.collision is not None:
                change, action = self._collision_banner(self._current_rule_id, node.collision), ""
            else:
                change = "Master is missing — ordinary sync is blocked for this namespace."
                action = "" if preview else "Unlock →"
            item = _inert_item(parent, node.landing_path, change, action)
        elif isinstance(node, ReviewMaster) and node.is_file:
            # A file master has no folder in the replica, so its one file
            # stands in for it, like any other leaf.
            if node.file_leaf is not None:
                return self._build_item(parent, node.file_leaf, unlocked, preview)
            item = _inert_item(parent, node.landing_path)
        elif isinstance(node, ReviewLeaf):
            item = QTreeWidgetItem(parent, [node.name, node.label, ""])
            if node.checkable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            else:
                _make_inert(item)
                if node.resolvable:
                    item.setText(2, "Resolve →")
        else:  # ReviewReplica, unblocked dir ReviewMaster, or ReviewFolder
            if isinstance(node, ReviewReplica):
                item = QTreeWidgetItem(
                    parent, [self._replica_item_label(node), "Not checked" if preview else "", ""]
                )
                item.setToolTip(0, node.replica_path)
            else:
                label = node.landing_path if isinstance(node, ReviewMaster) else node.name
                item = QTreeWidgetItem(parent, [label, "", ""])
            if preview:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            else:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            bold = item.font(0)
            bold.setWeight(QFont.Bold)
            item.setFont(0, bold)
            for child in node.children:
                self._build_item(item, child, unlocked, preview)
        item.setData(0, ROLE_NODE, node)
        return item

    def _replica_item_label(self, node: ReviewReplica) -> str:
        label = replica_label(self._config, node.replica_path)
        if not node.replica_exists:
            label += "  (missing — will be created)"
        return label

    def _relabel_replica_items(self) -> None:
        # Blocked: setText fires itemChanged, which would toggle the tick.
        with QSignalBlocker(self.tree):
            for i in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(i)
                node = item.data(0, ROLE_NODE)
                if isinstance(node, ReviewReplica):
                    item.setText(0, self._replica_item_label(node))

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
        # A pre-check preview offers no actions, however its rows look.
        if column != 2 or self._current_review() is None:
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
        dialog = ConflictDialog(
            rule,
            queue,
            self._state,
            self._state_path,
            self._logs_dir,
            self._config,
            self,
        )
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
        for category in CONFLICT_CATEGORIES:
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
                rule, {replica_path: changes}, self._state, self._state_path
            )
        except OSError as exc:
            # Nothing is merged or saved unless every file could be read.
            QMessageBox.warning(self, "Couldn't keep replica versions", str(exc))
            return
        self._queue_check(rule_id)

    def _bulk_overwrite(self, rule_id: str, replica_path: str, changes) -> None:
        rule = self._rules_by_id[rule_id]
        result = bulk_overwrite(
            self, rule, {replica_path: changes}, self._state, self._state_path, self._logs_dir
        )
        if result is None:
            return  # cancelled at the confirm prompt — nothing applied
        self._state = result.state
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
