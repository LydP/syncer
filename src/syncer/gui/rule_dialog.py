"""Qt adapter for the add/edit-rule modal (issue #25, spec.md §10).

Thin wiring only: master/replica/name uniqueness and cross-rule namespace-
collision detection live in `syncer.config`/`syncer.check` (pure,
unit-tested); this module only wires the dialog and renders the resulting
conflicts as inline row errors/badges rather than a blocking message box.
Not covered by the TDD loop, verified by running the app (matching
`syncer.gui.review_pane`'s own convention).

Masters are a second list above the replica list, styled identically
(Add file…/Add folder…/Remove selected) — issue #18's resolution. Unlike
shipped v1, masters stay fully editable/removable after rule creation; the
same "Edit rule…" flow already used to add a replica to an existing rule
now also covers editing its masters.
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
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from syncer.check import (
    NamespaceCollision,
    collisions_for_rule,
    find_namespace_collisions,
    other_rule_names,
)
from syncer.config import (
    Config,
    Master,
    SyncRule,
    default_rule_name,
    find_master_conflict,
    find_name_conflict,
    find_replica_name_conflict,
    find_replica_sharers,
    native_path,
    path_key,
    replica_label,
    replica_name,
    with_replica_name,
    with_rule,
)
from syncer.landing import master_basename_key
from syncer.storage import BaseDirNotWritableError, ensure_base_dir_writable

_ERROR_COLOR = "#b3261e"
_WARNING_COLOR = "#a15c00"
_INFO_COLOR = "#1a5fb4"
_ERROR_STYLE = f"color: {_ERROR_COLOR};"
_ERROR_BRUSH = QBrush(QColor(_ERROR_COLOR))
_WARNING_BRUSH = QBrush(QColor(_WARNING_COLOR))
_INFO_BRUSH = QBrush(QColor(_INFO_COLOR))

# config.find_master_conflict's two outcomes, worded for the offending row.
_MASTER_CONFLICT_TEXT = {
    "path": lambda other: "same path as another master in this rule",
    "basename": lambda other: f"same name as '{other.path}' in this rule",
}


class RuleDialog(QDialog):
    """`existing_rule=None` adds a rule; otherwise edits it in place (name,
    masters, and replicas all editable) — spec.md §10, issue #25.
    """

    def __init__(self, config: Config, existing_rule: SyncRule | None, parent=None):
        super().__init__(parent)
        self._config = config
        self._existing_rule = existing_rule
        # A fresh id matches no configured rule, so it doubles as the
        # conflict lookups' exclude_rule_id for an add.
        self._rule_id = existing_rule.id if existing_rule else str(uuid.uuid4())
        self._masters: list[Master] = list(existing_rule.masters) if existing_rule else []
        self._replicas: list[str] = list(existing_rule.replicas) if existing_rule else []
        # What saving the dialog produces: the edited rule plus any replica-name
        # edits. Names are global (one path, one name in every rule), so they
        # accumulate on self._config and land here with the rule on accept.
        self.result_config: Config | None = None

        self.setWindowTitle("Edit sync rule" if existing_rule else "Add sync rule")
        self.setMinimumWidth(480)

        self.name_edit = QLineEdit(existing_rule.name if existing_rule else "")
        self.name_error = QLabel()
        self.name_error.setStyleSheet(_ERROR_STYLE)
        self.name_error.hide()

        self.master_list = QListWidget()
        self.btn_add_master_file = QPushButton("Add file…")
        self.btn_add_master_dir = QPushButton("Add folder…")
        self.btn_add_master_file.clicked.connect(lambda: self._add_master("file"))
        self.btn_add_master_dir.clicked.connect(lambda: self._add_master("dir"))
        self.btn_remove_master = QPushButton("Remove selected")
        self.btn_remove_master.clicked.connect(self._remove_selected_master)
        master_buttons = QHBoxLayout()
        master_buttons.addWidget(self.btn_add_master_file)
        master_buttons.addWidget(self.btn_add_master_dir)
        master_buttons.addWidget(self.btn_remove_master)
        master_buttons.addStretch(1)
        self.master_error = QLabel()
        self.master_error.setStyleSheet(_ERROR_STYLE)
        self.master_error.hide()

        self.replica_list = QListWidget()
        self.replica_list.setAcceptDrops(True)
        self.replica_list.dragEnterEvent = self._replica_drag_enter
        self.replica_list.dragMoveEvent = self._replica_drag_enter
        self.replica_list.dropEvent = self._replica_drop

        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse_replica)
        self.btn_name_replica = QPushButton("Name…")
        self.btn_name_replica.setEnabled(False)
        self.btn_name_replica.clicked.connect(self._name_selected_replica)
        self.replica_list.currentRowChanged.connect(
            lambda row: self.btn_name_replica.setEnabled(row >= 0)
        )
        self.btn_remove_replica = QPushButton("Remove selected")
        self.btn_remove_replica.clicked.connect(self._remove_selected_replica)
        replica_buttons = QHBoxLayout()
        replica_buttons.addWidget(self.btn_browse)
        replica_buttons.addWidget(self.btn_name_replica)
        replica_buttons.addWidget(self.btn_remove_replica)
        replica_buttons.addStretch(1)
        self.replica_name_error = QLabel()
        self.replica_name_error.setStyleSheet(_ERROR_STYLE)
        self.replica_name_error.hide()

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
        layout.addWidget(QLabel("Name:"))
        layout.addWidget(self.name_edit)
        layout.addWidget(self.name_error)
        layout.addWidget(QLabel("Masters:"))
        layout.addWidget(self.master_list, 1)
        layout.addLayout(master_buttons)
        layout.addWidget(self.master_error)
        layout.addWidget(QLabel("Replicas (drag-and-drop, Browse, or type a path):"))
        layout.addWidget(self.replica_list, 1)
        layout.addLayout(typed_row)
        layout.addLayout(replica_buttons)
        layout.addWidget(self.replica_name_error)
        layout.addWidget(self.buttons)

        self._refresh_master_list()
        self._refresh_replica_list()

    def _pick_path(self, master_type: str, role: str) -> str:
        if master_type == "file":
            return QFileDialog.getOpenFileName(self, f"Choose {role} file")[0]
        return QFileDialog.getExistingDirectory(self, f"Choose {role} folder")

    # -- draft rule, for live cross-rule collision preview ------------------

    def _rule_named(self, name: str) -> SyncRule:
        """The rule this dialog would save under `name`. The one place a rule
        is built, so the live preview and the accepted rule can never differ
        by a field one of them forgot. For an edit it's replace(), not a
        field-by-field rebuild, so fields the dialog doesn't touch (`ignore`,
        `ignore_dependencies`) carry over untouched.
        """
        masters = list(self._masters)
        replicas = list(self._replicas)
        if self._existing_rule is not None:
            return replace(self._existing_rule, name=name, masters=masters, replicas=replicas)
        return SyncRule(id=self._rule_id, name=name, masters=masters, replicas=replicas)

    def _draft_rule(self) -> SyncRule:
        return self._rule_named(self.name_edit.text().strip() or self._rule_id)

    def _draft_rules(self) -> list[SyncRule]:
        # with_rule(), not a hand-rolled add-or-replace, so the preview is
        # computed against exactly the config a save would write.
        return with_rule(self._config, self._draft_rule()).rules

    # -- masters -------------------------------------------------------

    def _add_master(self, master_type: str) -> None:
        path = self._pick_path(master_type, "master")
        if not path:
            return
        path = native_path(path)
        # Pre-fill only a new rule's name: an edited rule's loaded name is
        # the user's own, though isModified() reports it as untouched.
        if self._existing_rule is None and not self.name_edit.isModified() and not self._masters:
            self.name_edit.setText(default_rule_name(path, master_type))
        self._masters.append(Master(path=path, type=master_type))
        self._refresh_master_list()

    def _remove_selected_master(self) -> None:
        row = self.master_list.currentRow()
        if row >= 0:
            del self._masters[row]
            self._refresh_master_list()

    def _namespace_collision_text(
        self,
        master: Master,
        per_master: dict[tuple[str, str], NamespaceCollision],
        rules_by_id: dict[str, SyncRule],
    ) -> str | None:
        basename_key = master_basename_key(master.path)
        for replica in self._replicas:
            collision = per_master.get((path_key(replica), basename_key))
            if collision is None:
                continue
            other = ", ".join(other_rule_names(collision, self._rule_id, rules_by_id)) or (
                "another rule"
            )
            return f"would collide with {other}'s '{collision.landing_path}' in a shared replica"
        return None

    def _refresh_master_list(self) -> None:
        self.master_list.clear()
        draft_rules = self._draft_rules()
        per_master = collisions_for_rule(self._rule_id, find_namespace_collisions(draft_rules))
        rules_by_id = {rule.id: rule for rule in draft_rules}
        for index, master in enumerate(self._masters):
            item = QListWidgetItem(master.path)
            conflict = find_master_conflict(self._masters, index)
            if conflict is not None:
                kind, other = conflict
                item.setText(f"{master.path}  —  {_MASTER_CONFLICT_TEXT[kind](other)}")
                item.setForeground(_ERROR_BRUSH)
            else:
                warning = self._namespace_collision_text(master, per_master, rules_by_id)
                if warning is not None:
                    item.setText(f"{master.path}  —  {warning}")
                    item.setForeground(_WARNING_BRUSH)
            self.master_list.addItem(item)

    # -- replicas --------------------------------------------------------

    def _replica_drag_enter(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _replica_drop(self, event) -> None:
        # Existing on-disk folders only (unlike a typed path): a replica is
        # always a namespaced landing tree, never a bare file, regardless of
        # any master's type, so a dropped file is silently skipped rather
        # than producing a nonsensical rule.
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isdir(path):
                self._add_replica(path)
        event.acceptProposedAction()

    def _browse_replica(self) -> None:
        path = self._pick_path("dir", "replica")
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
        # The one funnel for typed, dropped and Browse paths.
        path = native_path(path)
        key = path_key(path)
        if any(path_key(r) == key for r in self._replicas):
            return  # already in the list
        self._replicas.append(path)
        self._refresh_replica_list()

    def _remove_selected_replica(self) -> None:
        row = self.replica_list.currentRow()
        if row >= 0:
            del self._replicas[row]
            self._refresh_replica_list()

    def _name_selected_replica(self) -> None:
        row = self.replica_list.currentRow()
        if row < 0:
            return
        path = self._replicas[row]
        text, ok = QInputDialog.getText(
            self,
            "Replica name",
            f"Name for {path}\n(shown everywhere it appears; leave blank for none):",
            text=replica_name(self._config, path) or "",
        )
        if not ok:
            return
        # Against the draft rule set, so a name whose replica this edit removes
        # from every rule doesn't block one that's still in use.
        draft = with_rule(self._config, self._draft_rule())
        conflict = find_replica_name_conflict(draft, path, text)
        if conflict is not None:
            self.replica_name_error.setText(
                f"'{text.strip()}' is already the name of {conflict.path}."
            )
            self.replica_name_error.show()
            return
        self.replica_name_error.hide()
        self._config = with_replica_name(self._config, path, text)
        self._refresh_replica_list()
        self.replica_list.setCurrentRow(row)

    def _refresh_replica_list(self) -> None:
        self.replica_list.clear()
        for replica in self._replicas:
            sharers = find_replica_sharers(self._config, replica, exclude_rule_id=self._rule_id)
            label = replica_label(self._config, replica)
            item = QListWidgetItem(label)
            item.setToolTip(replica)
            if sharers:
                names = ", ".join(f"'{rule.name}'" for rule in sharers)
                item.setText(f"{label}  —  also used by: {names}")
                item.setForeground(_INFO_BRUSH)
            self.replica_list.addItem(item)
        # A namespace-collision warning is keyed on (replica, master
        # basename), so a replica-set change can add or clear one.
        self._refresh_master_list()

    # -- accept ------------------------------------------------------------

    def _on_accept(self) -> None:
        if not self._masters:
            self.master_error.setText("Add at least one master file or folder.")
            self.master_error.show()
            return
        self.master_error.hide()
        # Recomputed here rather than cached from _refresh_master_list, so the
        # gate never depends on a repaint having happened first.
        if any(find_master_conflict(self._masters, i) for i in range(len(self._masters))):
            return  # inline row errors already drawn on the offending rows

        name = self.name_edit.text().strip() or default_rule_name(
            self._masters[0].path, self._masters[0].type
        )
        name_conflict = find_name_conflict(self._config, name, exclude_rule_id=self._rule_id)
        if name_conflict is not None:
            self.name_error.setText(f"Already used by rule '{name_conflict.name}'.")
            self.name_error.show()
            return
        self.name_error.hide()

        result_config = with_rule(self._config, self._rule_named(name))
        # Naming only checks the draft at that moment: a replica removed and
        # re-added afterwards brings its old name back, which may since have
        # been given to another replica — and save_config would reject that.
        for replica in self._replicas:
            taken = replica_name(result_config, replica)
            conflict = taken and find_replica_name_conflict(result_config, replica, taken)
            if conflict:
                self.replica_name_error.setText(
                    f"{replica} and {conflict.path} are both named '{taken}' — rename one."
                )
                self.replica_name_error.show()
                return
        self.replica_name_error.hide()
        self.result_config = result_config
        self.accept()
