"""Qt adapter for the main window (issue #8, spec.md §10): rule add/edit/
delete, the first-run empty state, config-reload reconciliation, and the
Help -> Check for updates... entry point (issue #37).

Thin wiring only — matches review_pane.py/conflict_dialog.py's convention:
save/reconcile decisions call straight into syncer.config/syncer.state (pure,
unit-tested), this module only wires widgets and dialogs to them. Not
covered by the TDD loop, verified by running the app.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from syncer import app_version
from syncer.config import (
    Config,
    ConfigClobberError,
    ConfigStore,
    SyncRule,
    with_rule,
    without_rule,
)
from syncer.gui.review_pane import ReviewPane
from syncer.gui.rule_dialog import RuleDialog
from syncer.gui.update_dialog import UpdateDialog
from syncer.state import State, reconcile_and_save
from syncer.storage import StorageLayout, SyncerError
from syncer.update_apply import PreparedUpdate, install_update

_EMPTY_STATE_TEXT = "No sync rules yet — click + Add rule to get started."


class _EmptyStateWidget(QWidget):
    def __init__(self, on_add, parent=None):
        super().__init__(parent)
        label = QLabel(_EMPTY_STATE_TEXT)
        label.setAlignment(Qt.AlignCenter)
        add_button = QPushButton("+ Add rule")
        add_button.clicked.connect(on_add)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(label, alignment=Qt.AlignCenter)
        layout.addWidget(add_button, alignment=Qt.AlignCenter)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    """`state` must already be reconciled against `config` (app.py does so via
    `reconcile_and_save`). From then on `review_pane` owns the in-memory state.
    """

    def __init__(
        self,
        layout: StorageLayout,
        config_store: ConfigStore,
        config: Config,
        state: State,
        parent=None,
    ):
        super().__init__(parent)
        version = app_version()
        self.setWindowTitle(f"Syncer v{version}" if version else "Syncer")
        self.resize(1100, 720)
        self._storage = layout
        self._config_store = config_store
        self._config = config

        self.review_pane = ReviewPane(
            config.rules, state, layout.state_path, layout.logs_dir, self
        )
        self._empty_state = _EmptyStateWidget(self._add_rule, self)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty_state)
        self._stack.addWidget(self.review_pane)
        self.setCentralWidget(self._stack)

        self.review_pane.rule_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.review_pane.rule_list.customContextMenuRequested.connect(
            self._show_rule_context_menu
        )
        self.review_pane.rule_list.currentRowChanged.connect(self._refresh_toolbar_state)

        self._build_toolbar()
        self._build_menu()
        self._refresh_view()

    # -- chrome --------------------------------------------------------

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("Rules")
        toolbar.setMovable(False)
        self.action_add = QAction("+ Add rule", self)
        self.action_edit = QAction("Edit rule", self)
        self.action_delete = QAction("Delete rule", self)
        self.action_check = QAction("Check", self)
        self.action_reload = QAction("Reload config", self)
        self.action_add.triggered.connect(self._add_rule)
        self.action_edit.triggered.connect(self._edit_selected_rule)
        self.action_delete.triggered.connect(self._delete_selected_rule)
        self.action_check.triggered.connect(self._check_selected_rule)
        self.action_reload.triggered.connect(self._reload_config)
        for action in (self.action_add, self.action_edit, self.action_delete, self.action_check):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.action_reload)

    def _build_menu(self) -> None:
        self.action_check_updates = QAction("Check for &updates…", self)
        self.action_check_updates.triggered.connect(self._check_for_updates)
        self.menuBar().addMenu("&Help").addAction(self.action_check_updates)
        # A sync runs synchronously on this thread, so it can't overlap a click
        # here; only a check, which runs in a worker, needs gating on.
        self.review_pane.checkingChanged.connect(
            lambda checking: self.action_check_updates.setEnabled(not checking)
        )

    def _refresh_toolbar_state(self, _row: int | None = None) -> None:
        has_selection = self.review_pane.current_rule_id is not None
        self.action_edit.setEnabled(has_selection)
        self.action_delete.setEnabled(has_selection)
        self.action_check.setEnabled(has_selection)

    def _refresh_view(self) -> None:
        self._stack.setCurrentWidget(self._empty_state if not self._config.rules else self.review_pane)
        self._refresh_toolbar_state()

    def _selected_rule(self) -> SyncRule | None:
        rule_id = self.review_pane.current_rule_id
        return next((rule for rule in self._config.rules if rule.id == rule_id), None)

    def _show_rule_context_menu(self, pos) -> None:
        item = self.review_pane.rule_list.itemAt(pos)
        if item is None:
            return
        self.review_pane.rule_list.setCurrentItem(item)
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        menu.addAction(self.action_edit)
        menu.addAction(self.action_delete)
        menu.addAction(self.action_check)
        menu.exec(self.review_pane.rule_list.viewport().mapToGlobal(pos))

    # -- add / edit / delete ---------------------------------------------

    def _run_rule_dialog(self, existing_rule: SyncRule | None) -> SyncRule | None:
        dialog = RuleDialog(self._config, existing_rule, self)
        try:
            return dialog.result_rule if dialog.exec() == RuleDialog.Accepted else None
        finally:
            dialog.deleteLater()  # parented to the window, so it would otherwise live until exit

    def _add_rule(self) -> None:
        new_rule = self._run_rule_dialog(None)
        if new_rule is not None:
            self._save_and_adopt(with_rule(self._config, new_rule))

    def _edit_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        edited = self._run_rule_dialog(rule)
        if edited is not None:
            self._save_and_adopt(with_rule(self._config, edited))

    def _delete_selected_rule(self) -> None:
        rule = self._selected_rule()
        if rule is None:
            return
        confirm = QMessageBox.question(
            self,
            "Delete rule",
            f"Delete '{rule.name}'? This stops syncing it — replica files are "
            "left as-is. This can't be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self._save_and_adopt(without_rule(self._config, rule.id))

    def _check_selected_rule(self) -> None:
        rule_id = self.review_pane.current_rule_id
        if rule_id is not None:
            self.review_pane.check_rule(rule_id)

    def _reload_config(self) -> None:
        try:
            config = self._config_store.load()
        except SyncerError as exc:
            QMessageBox.warning(self, "Reload config", str(exc))
            return
        self._adopt_config(config)

    # -- app update ------------------------------------------------------

    def _check_for_updates(self) -> None:
        dialog = UpdateDialog(self._storage, self)
        try:
            dialog.exec()
            prepared = dialog.prepared
        finally:
            dialog.deleteLater()
        if prepared is not None:
            self._install_update(prepared)

    def _install_update(self, prepared: PreparedUpdate) -> None:
        """Swap the downloaded build in and supervise it starting (ADR 0004).

        Runs on the GUI thread with the window hidden, so nothing can lazy-load
        a file the swap has moved while it happens. Blocks until the new build
        reports it launched (then this one exits) or fails to (then the old
        build is back in place and the window reappears with the reason).
        """
        self.hide()
        try:
            install_update(prepared, self._storage).wait()
        except SyncerError as exc:
            # UpdateApplyError, or e.g. AlreadyRunningError from a rollback's
            # lock retake while a hung new build still holds the lock.
            self.show()
            QMessageBox.critical(self, "App update", str(exc))
            return
        except Exception as exc:
            # A bug, but a frozen build has no console to print it to, and the
            # install may be half-undone: say so before re-raising.
            self.show()
            QMessageBox.critical(
                self,
                "App update",
                f"The app update failed unexpectedly ({exc!r}). Restart Syncer "
                "before doing anything else.",
            )
            raise
        except BaseException:
            self.show()
            raise
        QApplication.quit()

    # -- persistence -----------------------------------------------------

    def _save_and_adopt(self, config: Config) -> None:
        try:
            self._config_store.save(config)
        except ConfigClobberError as exc:
            QMessageBox.warning(self, "Save failed", f"{exc}\n\nUse Reload config, then try again.")
            return
        self._adopt_config(config)

    def _adopt_config(self, config: Config) -> None:
        """The one path for every config change — GUI add/edit/delete and a
        reload alike (spec.md §10): purge state.json of anything no longer
        configured, then hand the new rules and state to the review pane.
        """
        self._config = config
        state = reconcile_and_save(self._storage.state_path, self.review_pane.state, config)
        self.review_pane.apply_config(config.rules, state)
        self._refresh_view()
