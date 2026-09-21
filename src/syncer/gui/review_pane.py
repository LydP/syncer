"""Qt adapter for the review-and-sync screen (issue #6, spec.md §7).

Thin wiring only: every tree-shape, bucketing, and selection-roll-up decision
lives in `syncer.review` (pure, Qt-free, unit-tested), and the working set they
act on — state, each rule's review tree, unlocks and ticks — lives in
`syncer.session.Session` (ADR 0005). This module owns widgets and Qt signals
and nothing else — it is not covered by the TDD loop, and is verified by
running the app rather than by pytest.

Conflict resolution (issue #7) is wired in via `syncer.gui.conflict_dialog`:
the "Resolve conflicts" button and each conflict leaf's "Resolve" cell open
`ConflictDialog`; the replica branch's context menu offers the per-category
bulk actions. Rule add/edit/delete itself lives in `syncer.gui.main_window`
(issue #8); `apply_config`/`check_rule` below are this pane's side of that
wiring — the rule list and its check state, not the add/edit/delete UI.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

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
    other_rule_names,
    scan_master_layout,
)
from syncer.config import Config, SyncRule, master_basename, replica_label
from syncer.gui.conflict_dialog import ConflictDialog, confirm_bulk_overwrite, error_detail
from syncer.review import (
    CATEGORY_LABEL,
    CONFLICT_CATEGORIES,
    ReviewLeaf,
    ReviewMaster,
    ReviewReplica,
    ReviewRule,
    has_drift,
)
from syncer.session import CheckJob, Session

_CHECK_STATE = {
    "checked": Qt.Checked,
    "partial": Qt.PartiallyChecked,
    "unchecked": Qt.Unchecked,
}

_TALLY_WORDING = (("safe", "to sync"), ("delete", "to delete"), ("conflict", "conflict"))

_GREY = QColor("#9a9a9a")

# The ReviewReplica/ReviewMaster/ReviewFolder/ReviewLeaf a tree item mirrors.
#
# Blocking is the model's job throughout this file: a blocked master-subfolder
# hides its own leaves behind `visible_children` (review.py), so nothing here —
# tree building, tallies, selection, bulk actions or the conflict queue — needs
# a blocked gate of its own.
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

    def __init__(self, job: CheckJob, parent=None):
        super().__init__(parent)
        # Handed back to `Session.check_finished`, which compares it against the
        # config as it stands by then.
        self.job = job
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
            self.job.rule,
            baseline=self.job.baseline,
            progress=self._report,
            cancel=lambda: self._cancelled,
            collisions=self.job.collisions,
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

    # True while any rule is being checked or queued to be; the host gates
    # actions that mustn't overlap a check (e.g. an app update) on it.
    checkingChanged = Signal(bool)

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self._session = session
        self._current_rule_id: str | None = None
        self._worker: CheckWorker | None = None
        self._check_cancelled = False
        self._checking = False

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

    def _checked_rule_id(self) -> str | None:
        """The current rule's id, unless it has no check result yet."""
        rule_id = self._current_rule_id
        return rule_id if rule_id and self._session.tree(rule_id) is not None else None

    # -- checking ------------------------------------------------------

    def check_all(self) -> None:
        """(Re-)check every rule, one at a time. Each completed re-check
        re-blocks any of its masters still missing (spec.md §8: the unlock is
        in-session only)."""
        self._session.queue_all_checks()
        self._run_next_check()

    def check_rule(self, rule_id: str) -> None:
        """Public entry point for main_window's toolbar/context-menu "Check"
        action (spec.md §10), which targets one rule rather than every rule."""
        self._queue_check(rule_id)

    @property
    def current_rule_id(self) -> str | None:
        return self._current_rule_id

    def apply_config(self, config: Config) -> None:
        """The rule set changed outside the normal check/sync flow — an
        add/edit/delete-rule action or a config reload. The session adopts it
        (purging `state.json` as it goes, spec.md §10); this redraws from what
        it reports. Per-rule session state (review, selection, unlock)
        survives only for a rule whose definition is unchanged — an edited
        rule's old review would show stale replicas.
        Rebuilds the left pane's rows, keeping the current selection when the
        selected rule survives. A changed replica name relabels the tree
        without invalidating any review.
        """
        adoption = self._session.adopt_config(config)
        self._repopulate_and_select(self._current_rule_id, adoption.unchanged)
        if adoption.replica_names_changed:
            # The tree shows names, so a rename relabels it even for a rule
            # whose cached review (and so its tree) is still accurate.
            self._relabel_replica_items()

    def _queue_check(self, rule_id: str) -> None:
        self._session.queue_check(rule_id)
        self._run_next_check()

    def _cancel_check(self) -> None:
        """Cancels the in-flight check and drops every rule still queued
        behind it — a plain "stop checking" rather than "skip this rule"."""
        if self._worker is not None:
            self._worker.cancel()
            self._check_cancelled = True
        self._session.cancel_checks()

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

    def _refresh_checking(self) -> None:
        """The one place the Check/Cancel buttons and `checkingChanged` change,
        so they can't drift apart. A check is running while one is queued or
        in flight; emits only on an actual transition, tracked in `_checking`
        rather than read back out of an inverted button property."""
        checking = self._session.has_pending_checks() or self._worker is not None
        if checking == self._checking:
            return
        self._checking = checking
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
        job = self._session.next_check()
        if job is None:
            self.status_label.setText(
                "Check cancelled." if self._check_cancelled else "Check complete."
            )
            self._check_cancelled = False
            self._refresh_checking()
            return
        self.status_label.setText(f"Checking {job.rule.name}…")
        self._worker = CheckWorker(job, self)
        self._refresh_checking()
        self._worker.progress.connect(self._on_check_progress)
        self._worker.check_finished.connect(self._on_check_finished)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

    def _on_check_progress(self, done: int, total: int, current_path: str) -> None:
        self.status_label.setText(f"Checking… {done}/{total}  {current_path}")

    def _on_check_finished(self, result: CheckResult, cancelled: bool) -> None:
        # Still set here: _worker is cleared on QThread.finished, queued after this.
        outcome = self._session.check_finished(self._worker.job, result, cancelled)
        if not outcome.applied:
            return
        self._refresh_rule_list()
        if outcome.rule_id == self._current_rule_id:
            self._show_rule(outcome.rule_id)

    def _on_worker_finished(self) -> None:
        # Queued after check_finished, so the result is already applied.
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._run_next_check()

    # -- left pane -------------------------------------------------------

    def _rule_list_text(self, rule_id: str) -> str:
        sync_rule = self._session.rules[rule_id]
        masters = ", ".join(master_basename(m.path) for m in sync_rule.masters)
        if self._session.tree(rule_id) is None:
            status = "not checked yet"
        else:
            drift = _tally_text(self._session.tally(rule_id))
            n_blocked = self._session.blocked_count(rule_id)
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
        ids = list(self._session.rules)  # also the row order
        if not ids:
            self._on_rule_row_changed(-1)
            return
        rule_id = preferred_rule_id if preferred_rule_id in self._session.rules else ids[0]
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
                for rule_id in self._session.rules:
                    item = QListWidgetItem()
                    item.setData(Qt.UserRole, rule_id)
                    self.rule_list.addItem(item)
        for row, rule_id in enumerate(self._session.rules):
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
        if not self._session.rules[rule_id].replicas:
            self._show_note("No replicas yet")
            return
        review_rule = self._session.tree(rule_id)
        if review_rule is None:
            self._show_unchecked(rule_id)
            return
        self._populate_tree(review_rule)
        self._apply_selection_to_tree(rule_id)
        self._expand_drifted()
        self._refresh_bar()

    def _populate_tree(self, review_rule: ReviewRule, preview: bool = False) -> None:
        with QSignalBlocker(self.tree):
            self.tree.clear()
            for replica in review_rule.replicas:
                self._build_item(self.tree, replica, preview)

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
        worker = PreviewWorker(self._session.rules[rule_id], self)
        worker.preview_ready.connect(self._on_preview_ready)
        worker.preview_failed.connect(self._on_preview_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _preview_is_stale(self, rule: SyncRule) -> bool:
        # The user moved on, or the rule changed, while it walked — or a check
        # landed first, and its tree is the truth.
        return (
            self._session.rules.get(self._current_rule_id) != rule
            or self._session.tree(rule.id) is not None
        )

    def _on_preview_failed(self, rule: SyncRule, message: str) -> None:
        if not self._preview_is_stale(rule):
            self._show_note(message)

    def _on_preview_ready(self, rule: SyncRule, layout: MasterLayout) -> None:
        if self._preview_is_stale(rule):
            return
        preview = self._session.preview(rule, layout)
        self._populate_tree(preview, preview=True)
        self.tree.expandToDepth(0)

    def _collision_banner(self, rule_id: str, collision: NamespaceCollision) -> str:
        others = other_rule_names(collision, rule_id, self._session.rules)
        return (
            f"Also claimed by: {', '.join(others) or 'another rule'} — rename a master or "
            "stop sharing this replica to fix."
        )

    def _build_item(self, parent, node, preview: bool = False) -> QTreeWidgetItem:
        """`preview` draws the pre-check tree: replicas read "Not checked", and
        nothing is tickable or unlockable until a check runs."""
        if isinstance(node, ReviewMaster) and node.blocked:
            # Banner-only: a blocked master's `visible_children` is empty.
            if node.collision is not None:
                change, action = self._collision_banner(self._current_rule_id, node.collision), ""
            else:
                change = "Master is missing — ordinary sync is blocked for this namespace."
                # The same predicate the click handler acts on, so the offer
                # and the action it promises can't drift apart.
                action = "Unlock →" if node.unlock_offered and not preview else ""
            item = _inert_item(parent, node.landing_path, change, action)
        elif isinstance(node, ReviewMaster) and node.is_file:
            # A file master has no folder in the replica, so its one file
            # stands in for it, like any other leaf.
            if node.file_leaf is not None:
                return self._build_item(parent, node.file_leaf, preview)
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
            for child in node.visible_children:
                self._build_item(item, child, preview)
        item.setData(0, ROLE_NODE, node)
        return item

    def _replica_item_label(self, node: ReviewReplica) -> str:
        label = replica_label(self._session.config, node.replica_path)
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

    def _apply_selection_to_tree(self, rule_id: str) -> None:
        with QSignalBlocker(self.tree):
            for item in self._iter_tree_items():
                node = item.data(0, ROLE_NODE)
                # A non-checkable item (context/conflict leaf, blocked
                # master-subfolder) has no tick to set — calling setCheckState
                # on one paints an indicator that can't be used.
                if node is None or not item.flags() & Qt.ItemIsUserCheckable:
                    continue
                item.setCheckState(0, _CHECK_STATE[self._session.node_state(rule_id, node)])

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
        self._session.toggle(rule_id, node)
        self._apply_selection_to_tree(rule_id)
        self._refresh_bar()

    # -- action bar --------------------------------------------------------

    def _refresh_bar(self) -> None:
        rule_id = self._checked_rule_id()
        if rule_id is None:
            for button in (self.btn_sync_selected, self.btn_sync_all_safe, self.btn_resolve):
                button.setEnabled(False)
            return
        counts = self._session.tally(rule_id)
        n_selected = self._session.selected_count(rule_id)
        self.btn_sync_selected.setText(f"Sync selected ({n_selected})")
        self.btn_sync_selected.setEnabled(n_selected > 0)
        self.btn_sync_all_safe.setText(f"Sync all safe changes ({counts['safe']})")
        self.btn_sync_all_safe.setEnabled(counts["safe"] > 0)
        self.btn_resolve.setText(f"Resolve conflicts → ({counts['conflict']})")
        self.btn_resolve.setEnabled(counts["conflict"] > 0)

    def _unlock_master(self, rule_id: str, landing_path: str) -> None:
        self._session.unlock(rule_id, landing_path)
        self._refresh_rule_list()
        self._show_rule(rule_id)

    def _request_conflict_resolution(self) -> None:
        rule_id = self._checked_rule_id()
        if rule_id is not None:
            self._open_conflict_dialog(rule_id)

    # -- conflict resolution (issue #7) -------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        # A pre-check preview offers no actions, however its rows look.
        if column != 2 or self._checked_rule_id() is None:
            return
        node = item.data(0, ROLE_NODE)
        if isinstance(node, ReviewLeaf) and node.resolvable:
            self._run_conflict_dialog(self._current_rule_id, [node])
        elif isinstance(node, ReviewMaster) and node.unlock_offered:
            self._unlock_master(self._current_rule_id, node.landing_path)

    def _open_conflict_dialog(self, rule_id: str, replica_path: str | None = None) -> None:
        """Queues every conflict in the rule, or just one replica's when
        `replica_path` is given."""
        queue = self._session.conflict_queue(rule_id, replica_path)
        if queue:
            self._run_conflict_dialog(rule_id, queue)

    def _run_conflict_dialog(self, rule_id: str, queue: list[ReviewLeaf]) -> None:
        dialog = ConflictDialog(
            self._session.rules[rule_id],
            queue,
            self._session.state,
            self._session.layout.state_path,
            self._session.layout.logs_dir,
            self._session.config,
            self,
        )
        dialog.exec()
        if not dialog.resolved_any:
            return  # closed or skipped through — nothing on disk changed
        self._session.replace_state(dialog.state)
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
        by_category = self._session.bulk_candidates(rule_id, replica_path)
        menu = QMenu(self)
        # Deleted with the menu rather than living on as a child of the pane.
        menu.setAttribute(Qt.WA_DeleteOnClose)
        # Per-replica queue entry point (spec.md §9). Counted from the pools
        # already in hand — they are the same conflict queue, grouped.
        n_conflicts = sum(map(len, by_category.values()))
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
        try:
            self._session.keep(rule_id, {replica_path: changes})
        except OSError as exc:
            # Nothing is merged or saved unless every file could be read.
            QMessageBox.warning(self, "Couldn't keep replica versions", str(exc))
            return
        self._queue_check(rule_id)

    def _bulk_overwrite(self, rule_id: str, replica_path: str, changes) -> None:
        changes_by_replica = {replica_path: changes}
        if not confirm_bulk_overwrite(self, changes_by_replica):
            return  # cancelled at the confirm prompt — nothing applied
        result = self._session.overwrite(rule_id, changes_by_replica)
        if result.errors:
            QMessageBox.warning(self, "Finished with errors", error_detail(result.errors))
        self._queue_check(rule_id)

    def _sync_all_safe(self) -> None:
        rule_id = self._checked_rule_id()
        if rule_id is not None:
            self._report_sync(rule_id, self._session.sync_all_safe(rule_id))

    def _sync_selected(self) -> None:
        rule_id = self._checked_rule_id()
        if rule_id is None:
            return
        delete_changes = self._session.pending_deletions(rule_id)
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
        self._report_sync(rule_id, self._session.sync_selected(rule_id))

    def _report_sync(self, rule_id: str, result: SyncResult | None) -> None:
        """Tells the user how a sync went, then re-checks. `result` is None
        when the session had nothing to apply, which needs neither."""
        if result is None:
            return
        if result.errors:
            QMessageBox.warning(
                self,
                "Sync finished with errors",
                f"Copied {result.copied}, deleted {result.deleted}, "
                f"{len(result.errors)} error(s):\n\n{error_detail(result.errors)}",
            )
        else:
            QMessageBox.information(
                self, "Sync finished", f"Copied {result.copied}, deleted {result.deleted}."
            )
        # Re-check so the tree reflects the post-sync reality.
        self._queue_check(rule_id)
