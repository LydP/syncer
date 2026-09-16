"""Qt adapter for the add/edit-rule modal (issue #8, spec.md §10).

Thin wiring only: master/replica uniqueness and the default-name derivation
live in `syncer.config` (pure, unit-tested); this module only wires the
dialog and renders the resulting conflicts as inline row errors rather than
a blocking message box. Not covered by the TDD loop, verified by running
the app (matching `syncer.gui.review_pane`'s own convention).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from syncer.config import (
    Config,
    SyncRule,
    default_rule_name,
    find_master_conflict,
    find_replica_conflict,
    normalize_replica_path,
)
from syncer.storage import BaseDirNotWritableError, ensure_base_dir_writable

_ERROR_COLOR = "#b3261e"
_ERROR_STYLE = f"color: {_ERROR_COLOR};"
_ERROR_BRUSH = QBrush(QColor(_ERROR_COLOR))
_PATH_MATCHES_TYPE = {"file": os.path.isfile, "dir": os.path.isdir}


class RuleDialog(QDialog):
    """`existing_rule=None` adds a rule; otherwise edits it in place (master
    path immutable, name and replicas editable) — spec.md §10.
    """

    def __init__(self, config: Config, existing_rule: SyncRule | None, parent=None):
        super().__init__(parent)
        self._config = config
        self._existing_rule = existing_rule
        # A fresh id matches no configured rule, so it doubles as the
        # conflict lookups' exclude_rule_id for an add.
        self._rule_id = existing_rule.id if existing_rule else str(uuid.uuid4())
        self._master: str | None = existing_rule.master if existing_rule else None
        self._master_type: str | None = existing_rule.master_type if existing_rule else None
        self._replicas: list[str] = list(existing_rule.replicas) if existing_rule else []
        self._has_replica_conflict = False

        self.result_rule: SyncRule | None = None

        self.setWindowTitle("Edit sync rule" if existing_rule else "Add sync rule")
        self.setMinimumWidth(480)

        self.master_label = QLabel(self._master or "(no master chosen)")
        self.master_error = QLabel()
        self.master_error.setStyleSheet(_ERROR_STYLE)
        self.master_error.hide()
        self.btn_choose_file = QPushButton("Choose file…")
        self.btn_choose_dir = QPushButton("Choose folder…")
        self.btn_choose_file.clicked.connect(lambda: self._choose_master("file"))
        self.btn_choose_dir.clicked.connect(lambda: self._choose_master("dir"))
        if existing_rule is not None:
            for button in (self.btn_choose_file, self.btn_choose_dir):
                button.setEnabled(False)
                button.setToolTip("Master path is immutable after creation.")
        master_buttons = QHBoxLayout()
        master_buttons.addWidget(self.btn_choose_file)
        master_buttons.addWidget(self.btn_choose_dir)

        self.name_edit = QLineEdit(existing_rule.name if existing_rule else "")

        self.replica_list = QListWidget()
        self.replica_list.setAcceptDrops(True)
        self.replica_list.dragEnterEvent = self._replica_drag_enter
        self.replica_list.dragMoveEvent = self._replica_drag_enter
        self.replica_list.dropEvent = self._replica_drop

        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse_replica)
        self.btn_remove = QPushButton("Remove selected")
        self.btn_remove.clicked.connect(self._remove_selected_replica)
        replica_buttons = QHBoxLayout()
        replica_buttons.addWidget(self.btn_browse)
        replica_buttons.addWidget(self.btn_remove)
        replica_buttons.addStretch(1)

        self.typed_path_edit = QLineEdit()
        self.typed_path_edit.setPlaceholderText(r"Or type/paste a path, e.g. C:\path\to\replica")
        self.btn_add_typed = QPushButton("Add")
        self.btn_add_typed.clicked.connect(self._add_typed_replica)
        typed_row = QHBoxLayout()
        typed_row.addWidget(self.typed_path_edit, 1)
        typed_row.addWidget(self.btn_add_typed)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Master:"))
        layout.addWidget(self.master_label)
        layout.addLayout(master_buttons)
        layout.addWidget(self.master_error)
        layout.addWidget(QLabel("Name:"))
        layout.addWidget(self.name_edit)
        layout.addWidget(QLabel("Replicas (drag-and-drop, Browse, or type a path):"))
        layout.addWidget(self.replica_list, 1)
        layout.addLayout(typed_row)
        layout.addLayout(replica_buttons)
        layout.addWidget(self.buttons)

        self._refresh_replica_list()

    def _pick_path(self, master_type: str | None, role: str) -> str:
        if master_type == "file":
            return QFileDialog.getOpenFileName(self, f"Choose {role} file")[0]
        return QFileDialog.getExistingDirectory(self, f"Choose {role} folder")

    # -- master --------------------------------------------------------

    def _choose_master(self, master_type: str) -> None:
        path = self._pick_path(master_type, "master")
        if not path:
            return
        self._master = path
        self._master_type = master_type
        if not self.name_edit.isModified():  # the user hasn't typed a name of their own
            self.name_edit.setText(default_rule_name(path, master_type))
        self.master_label.setText(path)

    # -- replicas --------------------------------------------------------

    def _replica_drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _replica_drop(self, event) -> None:
        # Existing on-disk items only (unlike a typed path) so the drop's type
        # can be checked against the master's; a mismatch is silently skipped
        # rather than producing a nonsensical rule.
        matches_type = _PATH_MATCHES_TYPE.get(self._master_type)
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and (matches_type is None or matches_type(path)):
                self._add_replica(path)
        event.acceptProposedAction()

    def _browse_replica(self) -> None:
        path = self._pick_path(self._master_type, "replica")
        if path:
            self._add_replica(path)

    def _add_typed_replica(self) -> None:
        path = self.typed_path_edit.text().strip()
        if not path:
            return
        if not Path(path).is_absolute():
            # A relative path would pass the parent check against the process
            # cwd and then resolve differently wherever Syncer is launched from.
            self.typed_path_edit.setStyleSheet(_ERROR_STYLE)
            self.typed_path_edit.setToolTip("Enter a full path, e.g. C:\\path\\to\\replica.")
            return
        parent = Path(path).parent
        try:
            ensure_base_dir_writable(parent)
        except BaseDirNotWritableError:
            self.typed_path_edit.setStyleSheet(_ERROR_STYLE)
            self.typed_path_edit.setToolTip(f"{parent} doesn't exist or isn't writable.")
            return
        self.typed_path_edit.setStyleSheet("")
        self.typed_path_edit.setToolTip("")
        self.typed_path_edit.clear()
        self._add_replica(path)

    def _add_replica(self, path: str) -> None:
        normalized = normalize_replica_path(path)
        if any(normalize_replica_path(r) == normalized for r in self._replicas):
            return  # already in the list
        self._replicas.append(path)
        self._refresh_replica_list()

    def _remove_selected_replica(self) -> None:
        row = self.replica_list.currentRow()
        if row >= 0:
            del self._replicas[row]
            self._refresh_replica_list()

    def _refresh_replica_list(self) -> None:
        self.replica_list.clear()
        self._has_replica_conflict = False
        for replica in self._replicas:
            conflict = find_replica_conflict(self._config, replica, exclude_rule_id=self._rule_id)
            item = QListWidgetItem(replica)
            if conflict is not None:
                self._has_replica_conflict = True
                item.setText(f"{replica}  —  already used by '{conflict.name}'")
                item.setForeground(_ERROR_BRUSH)
            self.replica_list.addItem(item)

    # -- accept ------------------------------------------------------------

    def _on_accept(self) -> None:
        if self._master is None:
            self.master_error.setText("Choose a master file or folder.")
            self.master_error.show()
            return
        master_conflict = find_master_conflict(
            self._config, self._master, exclude_rule_id=self._rule_id
        )
        if master_conflict is not None:
            self.master_error.setText(f"Already used by rule '{master_conflict.name}'.")
            self.master_error.show()
            return
        self.master_error.hide()
        if self._has_replica_conflict:
            return  # inline errors already drawn on the offending rows

        name = self.name_edit.text().strip() or default_rule_name(self._master, self._master_type)
        replicas = list(self._replicas)
        if self._existing_rule is not None:
            # replace(), not a field-by-field rebuild, so fields the dialog
            # doesn't edit (e.g. `ignore`) carry over untouched.
            self.result_rule = replace(self._existing_rule, name=name, replicas=replicas)
        else:
            self.result_rule = SyncRule(
                id=self._rule_id,
                name=name,
                master=self._master,
                master_type=self._master_type,
                replicas=replicas,
            )
        self.accept()
