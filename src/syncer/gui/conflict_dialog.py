"""Qt adapter for the conflict-resolution dialog (issue #7, spec.md §9).

Thin wiring only: every diff-panel, queue, and bulk-selection decision lives
in `syncer.conflict` (pure, Qt-free, unit-tested). This module owns widgets
and Qt signals and nothing else — it is not covered by the TDD loop, and is
verified by running the app rather than by pytest (matching
`syncer.gui.review_pane`'s own convention).
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from syncer.check import FileChange
from syncer.config import Config, SyncRule, replica_label
from syncer.conflict import (
    DiffOp,
    DiffPanel,
    FileMeta,
    apply_keep_replica,
    build_conflict_view,
    summarize_overwrite,
)
from syncer.review import CATEGORY_LABEL, ReviewLeaf, changes_by_replica
from syncer.state import State
from syncer.sync import SyncResult
from syncer.sync import sync as run_sync

_CALLOUT_STYLE = "color: #b35900; font-weight: bold;"
_META_STYLE = "color: #9a9a9a;"
_DIFF_STYLE = "font-family: Consolas, monospace; white-space: pre-wrap;"


def _rows(lines: tuple[str, ...], colour: str, prefix: str) -> list[str]:
    return [
        f'<span style="color:{colour};">{prefix}{html.escape(line, quote=False)}</span>'
        for line in lines
    ]


def _diff_html(ops: tuple[DiffOp, ...]) -> str:
    rows = []
    for op in ops:
        if op.tag == "equal":
            rows += _rows(op.left, "#888", "  ")
            continue
        if op.tag in ("delete", "replace"):
            rows += _rows(op.left, "#c0392b", "- ")
        if op.tag in ("insert", "replace"):
            rows += _rows(op.right, "#27ae60", "+ ")
    return f'<div style="{_DIFF_STYLE}">' + "<br>".join(rows) + "</div>"


def _meta_text(meta: FileMeta) -> str:
    if not meta.exists:
        return "does not exist"
    mtime = datetime.fromtimestamp(meta.mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"{meta.size:,} bytes, modified {mtime}"


def _panel_widget(panel: DiffPanel) -> QGroupBox:
    box = QGroupBox(panel.title)
    layout = QVBoxLayout(box)
    if panel.ops is not None:
        view = QTextEdit()
        view.setReadOnly(True)
        view.setHtml(_diff_html(panel.ops))
        layout.addWidget(view)
    else:
        reason = QLabel(panel.unavailable_reason)
        reason.setWordWrap(True)
        layout.addWidget(reason)
        meta = QLabel(
            f"{panel.left_label}: {_meta_text(panel.left_meta)}\n"
            f"{panel.right_label}: {_meta_text(panel.right_meta)}"
        )
        meta.setStyleSheet(_META_STYLE)
        layout.addWidget(meta)
    return box


class ConflictDialog(QDialog):
    """Resolves a queue of conflicts one at a time (spec.md §9). A
    single-item queue is the per-file "Resolve" entry point; a longer one is
    the per-replica/per-rule "Resolve conflicts" entry point — resolving one
    file immediately advances to the next, closing when the queue empties or
    the user dismisses it.

    `.state` holds the (possibly updated) `State` after the dialog closes —
    callers should adopt it in place of what they passed in. `.resolved_any`
    says whether any file was actually resolved, so a caller can skip
    re-checking after a dialog that was only closed or skipped through.
    """

    def __init__(
        self,
        rule: SyncRule,
        queue: list[ReviewLeaf],
        state: State,
        state_path: Path,
        logs_dir: Path,
        config: Config,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Resolve conflict")
        self.resize(720, 520)
        self._rule = rule
        self._queue = queue
        self._index = 0
        self._state_path = state_path
        self._logs_dir = logs_dir
        self._config = config
        self.state = state
        self.resolved_any = False

        self._position_label = QLabel()
        self._file_label = QLabel()
        self._file_label.setWordWrap(True)
        bold = self._file_label.font()
        bold.setBold(True)
        self._file_label.setFont(bold)
        self._callout_label = QLabel()
        self._callout_label.setWordWrap(True)
        self._callout_label.setStyleSheet(_CALLOUT_STYLE)
        self._callout_label.hide()

        self._panels_layout = QVBoxLayout()

        self.btn_overwrite = QPushButton("Overwrite from master")
        self.btn_skip = QPushButton("Skip for now")
        self.btn_keep = QPushButton("Keep replica's version")
        self.btn_overwrite.clicked.connect(self._overwrite)
        self.btn_skip.clicked.connect(self._skip)
        self.btn_keep.clicked.connect(self._keep)
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.reject)

        # Act on the current file and everything after it (issue #43); the
        # counts are refreshed by _show_current as the queue advances.
        self.btn_overwrite_all = QPushButton()
        self.btn_keep_all = QPushButton()
        self.btn_overwrite_all.clicked.connect(self._overwrite_all_remaining)
        self.btn_keep_all.clicked.connect(self._keep_all_remaining)

        button_row = QHBoxLayout()
        for button in (self.btn_overwrite, self.btn_skip, self.btn_keep):
            button_row.addWidget(button)
        button_row.addStretch(1)
        button_row.addWidget(self.btn_close)

        bulk_row = QHBoxLayout()
        bulk_row.addWidget(self.btn_overwrite_all)
        bulk_row.addWidget(self.btn_keep_all)
        bulk_row.addStretch(1)
        # Redundant on the per-file "Resolve" entry point's one-item queue.
        for button in (self.btn_overwrite_all, self.btn_keep_all):
            button.setVisible(len(queue) > 1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._position_label)
        layout.addWidget(self._file_label)
        layout.addWidget(self._callout_label)
        layout.addLayout(self._panels_layout, 1)
        layout.addLayout(bulk_row)
        layout.addLayout(button_row)

        self._show_current()

    def _current_leaf(self) -> ReviewLeaf | None:
        return self._queue[self._index] if self._index < len(self._queue) else None

    def _remaining(self) -> list[ReviewLeaf]:
        # What the "…all remaining" buttons act on: the current file onward,
        # across categories. Files skipped earlier are left alone (issue #43).
        return self._queue[self._index :]

    def _clear_panels(self) -> None:
        while self._panels_layout.count():
            item = self._panels_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # setParent(None) detaches immediately so the old panel
                # can't still be visible when deleteLater()'s deferred
                # cleanup eventually runs.
                widget.setParent(None)
                widget.deleteLater()

    def _show_current(self) -> None:
        leaf = self._current_leaf()
        if leaf is None:
            self.accept()
            return
        self._position_label.setText(f"{self._index + 1} of {len(self._queue)}")
        n_remaining = len(self._queue) - self._index
        self.btn_overwrite_all.setText(f"Overwrite all remaining from master ({n_remaining})")
        self.btn_keep_all.setText(f"Keep all remaining as-is ({n_remaining})")
        self._file_label.setText(
            f"{replica_label(self._config, leaf.replica_path)}\n{leaf.rel_path} — {leaf.label}"
        )
        self._file_label.setToolTip(leaf.replica_path)
        view = build_conflict_view(self._rule, leaf.replica_path, leaf.file_change)
        self._callout_label.setVisible(view.callout is not None)
        if view.callout is not None:
            self._callout_label.setText(view.callout)
        self._clear_panels()
        for panel in view.panels:
            self._panels_layout.addWidget(_panel_widget(panel))

    def _advance(self) -> None:
        self._index += 1
        self._show_current()

    def _overwrite(self) -> None:
        leaf = self._current_leaf()
        if leaf is None:
            return
        if leaf.file_change.is_deletion and not _confirm_deletion(self, leaf.rel_path):
            return
        result = run_sync(
            self._rule,
            {leaf.replica_path: [leaf.file_change]},
            self.state,
            self._state_path,
            self._logs_dir,
        )
        self.state = result.state
        if result.errors:
            QMessageBox.warning(self, "Couldn't overwrite", result.errors[0].message)
            return
        self.resolved_any = True
        self._advance()

    def _skip(self) -> None:
        # Pure no-op (spec.md §9): the baseline is never written, so this
        # file is re-flagged as a conflict on every future check.
        self._advance()

    def _keep(self) -> None:
        leaf = self._current_leaf()
        if leaf is None:
            return
        try:
            self.state = apply_keep_replica(
                self._rule,
                {leaf.replica_path: [leaf.file_change]},
                self.state,
                self._state_path,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't keep replica's version", str(exc))
            return
        self.resolved_any = True
        self._advance()

    def _keep_all_remaining(self) -> None:
        # Records the replica's version as kept (unlike Skip, which leaves no
        # record). Changes no files, so it needs no confirm.
        try:
            self.state = apply_keep_replica(
                self._rule, changes_by_replica(self._remaining()), self.state, self._state_path
            )
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't keep replica's version", str(exc))
            return
        self.resolved_any = True
        self.accept()

    def _overwrite_all_remaining(self) -> None:
        result = bulk_overwrite(
            self,
            self._rule,
            changes_by_replica(self._remaining()),
            self.state,
            self._state_path,
            self._logs_dir,
        )
        if result is None:
            return
        self.state = result.state
        if result.copied or result.deleted:
            self.resolved_any = True
            self.accept()


def _confirm_deletion(parent, rel_path: str) -> bool:
    """An "overwrite" whose master copy is gone is a deletion — confirmed as
    its own category, never applied silently (spec.md §8)."""
    box = QMessageBox(QMessageBox.Question, "Confirm deletion", "", parent=parent)
    box.setText(
        f"{rel_path} was deleted from the master, so overwriting from master "
        "deletes the replica's edited copy. Delete it?"
    )
    confirm = box.addButton("Yes, delete 1 file", QMessageBox.AcceptRole)
    box.setDefaultButton(box.addButton(QMessageBox.Cancel))
    box.exec()
    return box.clickedButton() is confirm


def confirm_bulk_overwrite(parent, by_replica: dict[str, list[FileChange]]) -> bool:
    """Takes the same `{replica_path: [FileChange]}` shape the apply half does
    (`sync()`, `Session.overwrite`), and derives the file and replica counts
    itself so no caller has to restate them. Named `by_replica` rather than
    `changes_by_replica` so it can't shadow the helper of that name this
    module imports."""
    changes = [change for group in by_replica.values() for change in group]
    if not changes:
        return False  # nothing to confirm, and nothing to apply
    replica_count = sum(1 for group in by_replica.values() if group)
    n = len(changes)
    summary = summarize_overwrite(changes)
    box = QMessageBox(QMessageBox.Question, "Overwrite from master", "", parent=parent)
    breakdown = "\n".join(
        f"  {CATEGORY_LABEL[category]}: {count}" for category, count in summary.by_category.items()
    )
    where = "the replica" if replica_count == 1 else f"{replica_count} replicas"
    text = (
        f"Overwrite {n} file(s) in {where} with master's version?\n\n{breakdown}\n\n"
        "This can't be undone."
    )
    if summary.deletions:
        text += (
            f"\n\n{summary.deletions} of these were deleted from the master, so those "
            "replica files will be deleted."
        )
    box.setText(text)
    confirm = box.addButton(f"Yes, overwrite {n} file(s)", QMessageBox.AcceptRole)
    box.setDefaultButton(box.addButton(QMessageBox.Cancel))
    box.exec()
    return box.clickedButton() is confirm


def error_detail(errors) -> str:
    """The per-file failure list every sync-result box itemises. One home, so
    the wording can't drift between the review pane and this dialog."""
    return "\n".join(f"{e.rel_path}: {e.message}" for e in errors)


def bulk_overwrite(
    parent,
    rule: SyncRule,
    by_replica: dict[str, list[FileChange]],
    state: State,
    state_path: Path,
    logs_dir: Path,
) -> SyncResult | None:
    """"Overwrite all from master" (spec.md §9): gated behind its own
    confirm, since it discards local edits; a plain `sync.sync()` call once
    confirmed — identical to the safe-drift path.

    Returns `None` when the user cancels, so "cancelled" stays distinct from
    "applied, and the state happens to be unchanged".
    """
    if not confirm_bulk_overwrite(parent, by_replica):
        return None
    result = run_sync(rule, by_replica, state, state_path, logs_dir)
    if result.errors:
        QMessageBox.warning(parent, "Finished with errors", error_detail(result.errors))
    return result
