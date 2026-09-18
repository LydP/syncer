"""Application entry point (issue #8, spec.md §10): acquires the
single-instance lock, loads config/state (reconciling state.json against
config, same as every later "Reload config"), and shows the main window.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from syncer import app_version
from syncer.config import ConfigStore
from syncer.gui.main_window import MainWindow
from syncer.lock import AlreadyRunningError, acquire_lock, release_lock
from syncer.state import load_state, reconcile_and_save
from syncer.storage import SyncerError, initialize_storage
from syncer.update_apply import report_launched, settle_previous_update


def main() -> int:
    app = QApplication(sys.argv)

    try:
        layout = initialize_storage()
    except SyncerError as exc:
        QMessageBox.critical(None, "Syncer", str(exc))
        return 1

    try:
        acquire_lock(layout.lock_path, pid=os.getpid())
    except AlreadyRunningError as exc:
        QMessageBox.warning(None, "Syncer", str(exc))
        return 1

    try:
        interrupted = settle_previous_update(layout, sys.argv, running_version=app_version())
        if interrupted:
            QMessageBox.warning(None, "Syncer", interrupted)

        config_store = ConfigStore(layout.config_path, layout.backups_dir)
        try:
            config = config_store.load()  # an empty Config on first run
        except SyncerError as exc:
            QMessageBox.critical(None, "Syncer", str(exc))
            return 1

        # Startup's success point for an app update (ADR 0004): everything so
        # far only read user data, and everything after may write it
        # (load_state can quarantine a corrupt state.json), so the old build
        # can only safely be rolled back to before this line.
        report_launched(layout, sys.argv)

        load_result = load_state(layout.state_path)
        state = reconcile_and_save(layout.state_path, load_result.state, config)
        if load_result.warning:
            QMessageBox.warning(None, "Syncer", load_result.warning)

        window = MainWindow(layout, config_store, config, state)
        window.show()
        return app.exec()
    finally:
        release_lock(layout.lock_path, pid=os.getpid())


if __name__ == "__main__":
    sys.exit(main())
