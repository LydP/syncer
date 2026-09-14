"""Qt adapter for the review-and-sync screen (issue #6, spec.md §7).

Thin wiring only: every tree-shape, bucketing, and selection-roll-up decision
lives in `syncer.review` (pure, Qt-free, unit-tested). This module owns
widgets and Qt signals and nothing else — it is not covered by the TDD loop,
and is verified by running the app rather than by pytest.

Conflict resolution (issue #7) is wired in via `syncer.gui.conflict_dialog`:
the "Resolve conflicts" button and each conflict leaf's "Resolve" cell open
`ConflictDialog`; the replica branch's context menu offers the per-category
bulk actions. Rule add/edit/delete (issue #8) stays out of scope.
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

from syncer.check import CheckResult, check
from syncer.config import SyncRule
from syncer.conflict import apply_keep_replica, bulk_candidates_by_category, conflict_queue
from syncer.gui.conflict_dialog import ConflictDialog, bulk_overwrite
from syncer.review import (
    BULK_CATEGORIES,
    CATEGORY_LABEL,
    LeafKey,
    ReviewLeaf,
    ReviewReplica,
    ReviewRule,
    build_review_rule,
    has_drift,
    is_sync_blocked,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    toggle,
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

ROLE_NODE = Qt.UserRole + 1  # the ReviewReplica/ReviewFolder/ReviewLeaf a tree item mirrors

# Minimum gap between cross-thread progress signals; check() reports per file.
_PROGRESS_INTERVAL_S = 0.05

# Paths shown inline in the delete confirm before deferring to its details pane.
_DELETE_PREVIEW_LINES = 15


def _tally_text(counts: dict[str, int]) -> str:
    bits = [f"{counts[bucket]} {words}" for bucket, words in _TALLY_WORDING if counts[bucket]]
    return ", ".join(bits) if bits else "in sync"


class CheckWorker(QThread):
    """Runs `check()` for one rule off the UI thread — spec.md §7: "wires
    check engine's (issue #3) progress/cancel to a QThread"."""

    progress = Signal(int, int, str)
    # (CheckResult, cancelled) — a cancelled check's result is truncated.
    check_finished = Signal(object, bool)

    def __init__(self, rule: SyncRule, baseline: dict, parent=None):
        super().__init__(parent)
        self._rule = rule
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
            self._rule,
            baseline=self._baseline,
            progress=self._report,
            cancel=lambda: self._cancelled,
        )
        self.check_finished.emit(result, self._cancelled)


class ReviewPane(QWidget):
    """The two-pane review screen: rule list on the left (variant D of the
    ticket-06 prototype), `replica > folder > file` tree on the right for
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
        self._state = state
        self._state_path = state_path
        self._logs_dir = logs_dir

        self._review: dict[str, ReviewRule] = {}
        self._selected: defaultdict[str, frozenset[LeafKey]] = defaultdict(frozenset)
        self._unlocked: set[str] = set()
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
        self.unlock_button = QPushButton("I know the master is missing — unlock")
        self.unlock_button.hide()
        self.unlock_button.clicked.connect(self._unlock_current_rule)

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
        action_lay.addWidget(self.unlock_button)
        action_lay.addLayout(action_row)

        split = QSplitter()
        split.addWidget(self.rule_list)
        split.addWidget(self.tree)
        split.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addWidget(actions)

        self.rule_list.currentRowChanged.connect(self._on_rule_row_changed)

    def _is_blocked(self, rule_id: str, review_rule: ReviewRule) -> bool:
        return is_sync_blocked(review_rule, rule_id in self._unlocked)

    def _current_review(self) -> tuple[str, ReviewRule] | None:
        rule_id = self._current_rule_id
        review_rule = self._review.get(rule_id) if rule_id else None
        return None if review_rule is None else (rule_id, review_rule)

    # -- checking ------------------------------------------------------

    def check_all(self) -> None:
        """(Re-)check every rule, one at a time. Each completed re-check
        re-blocks its rule if the master is still missing (spec.md §8: the
        unlock is in-session only)."""
        self._pending_check_ids = list(self._rules_by_id)
        self._run_next_check()

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
        self._worker = CheckWorker(rule, baseline_for_rule(self._state, rule_id), self)
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
        self._unlocked.discard(result.rule_id)  # a re-check re-blocks (spec.md §8)
        self._review[result.rule_id] = build_review_rule(result)
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
        if review_rule is None:
            status = "not checked yet"
        else:
            blocked = self._is_blocked(rule_id, review_rule)
            drift = "Master missing" if blocked else _tally_text(rule_tally(review_rule))
            n_rep = len(sync_rule.replicas)
            status = f"{n_rep} replica{'s' if n_rep != 1 else ''}  ·  {drift}"
        return f"{sync_rule.name}\n   {sync_rule.master}\n   {status}"

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
        blocked = review_rule is not None and self._is_blocked(rule_id, review_rule)
        self.unlock_button.setVisible(blocked)
        with QSignalBlocker(self.tree):
            self.tree.clear()
            if review_rule is not None:
                if blocked:
                    QTreeWidgetItem(
                        self.tree,
                        ["Master is missing for this rule — ordinary sync is blocked.", "", ""],
                    )
                else:
                    for replica in review_rule.replicas:
                        self._build_item(self.tree, replica)
        if review_rule is not None and not blocked:
            self._apply_selection_to_tree(self._selected[rule_id])
            self._expand_drifted()
        self._refresh_bar()

    def _build_item(self, parent, node) -> QTreeWidgetItem:
        if isinstance(node, ReviewLeaf):
            item = QTreeWidgetItem(parent, [node.name, node.label, ""])
            if node.checkable:
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            else:
                # Greying is the brush's job, not ItemIsEnabled's: disabled
                # items don't receive itemClicked, which a resolvable row
                # needs. _apply_selection_to_tree leaves these alone, so no
                # inert check indicator is painted either.
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                for column in (0, 1):
                    item.setForeground(column, QBrush(_GREY))
                if node.resolvable:
                    item.setText(2, "Resolve →")
        else:  # ReviewReplica or ReviewFolder
            if isinstance(node, ReviewReplica):
                label = node.replica_path
                if not node.replica_exists:
                    label += "  (missing — will be created)"
            else:
                label = node.name
            item = QTreeWidgetItem(parent, [label, "", ""])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            bold = item.font(0)
            bold.setWeight(QFont.Bold)
            item.setFont(0, bold)
            for child in node.children:
                self._build_item(item, child)
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
                # A non-checkable leaf has no tick to set — calling
                # setCheckState on one paints an indicator that can't be used.
                if node is None or (isinstance(node, ReviewLeaf) and not node.checkable):
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
        blocked = self._is_blocked(rule_id, review_rule)
        counts = rule_tally(review_rule)
        selection = resolve_selection(review_rule, self._selected[rule_id])
        n_selected = sum(map(len, selection.values()))
        self.btn_sync_selected.setText(f"Sync selected ({n_selected})")
        self.btn_sync_selected.setEnabled(not blocked and n_selected > 0)
        self.btn_sync_all_safe.setText(f"Sync all safe changes ({counts['safe']})")
        self.btn_sync_all_safe.setEnabled(not blocked and counts["safe"] > 0)
        self.btn_resolve.setText(f"Resolve conflicts → ({counts['conflict']})")
        # Blocked too: with the master missing every conflict is a both_changed
        # whose "overwrite" is a deletion (spec.md §8's one-click-wipe guard).
        self.btn_resolve.setEnabled(not blocked and counts["conflict"] > 0)

    def _unlock_current_rule(self) -> None:
        if self._current_rule_id is not None:
            self._unlocked.add(self._current_rule_id)
            self._refresh_rule_list()
            self._show_rule(self._current_rule_id)

    def _request_conflict_resolution(self) -> None:
        current = self._current_review()
        if current is not None and not self._is_blocked(*current):
            self._open_conflict_dialog(current[0])

    # -- conflict resolution (issue #7) -------------------------------------

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 2 or self._current_rule_id is None:
            return
        node = item.data(0, ROLE_NODE)
        if isinstance(node, ReviewLeaf) and node.resolvable:
            self._run_conflict_dialog(self._current_rule_id, [node])

    def _open_conflict_dialog(self, rule_id: str, replica_path: str | None = None) -> None:
        """Queues every conflict in the rule, or just one replica's when
        `replica_path` is given."""
        review_rule = self._review.get(rule_id)
        if review_rule is None:
            return
        nodes = [
            r
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
            self._apply_changes(rule_id, sync_all_safe_changes(review_rule))

    def _sync_selected(self) -> None:
        current = self._current_review()
        if current is None:
            return
        rule_id, review_rule = current
        selected = self._selected[rule_id]
        delete_changes = selection_by_bucket(review_rule, selected, "delete")
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
        self._apply_changes(rule_id, resolve_selection(review_rule, selected))

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
