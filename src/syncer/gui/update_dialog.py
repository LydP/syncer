"""Qt adapter for Help -> Check for updates... (issue #37, ADR 0004).

Thin wiring only: whether a newer release exists, and every user-facing failure
message, come from `syncer.update` / `syncer.update_apply` (pure, Qt-free,
unit-tested). This module owns widgets, and runs the two blocking network calls
(the check, the download) off the GUI thread. It stops once the new build is
downloaded and verified: swapping it in is the main window's job, since that
hides the window. Not covered by the TDD loop, verified by running the app
(matching `syncer.gui.review_pane`'s own convention).

The dialog can't be dismissed while a call is in flight: `prepare_update` has
no way to be cancelled, and abandoning a running QThread would either abort the
process or leave a download racing the next one for `.app-update/`.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from syncer import app_version
from syncer.storage import StorageLayout, SyncerError
from syncer.update import GitHubReleaseSource, UpdateOffer, check_for_update
from syncer.update_apply import PreparedUpdate, prepare_update

_NO_NOTES = "This release has no release notes."


class _Worker(QThread):
    """Runs one blocking call off the GUI thread and reports either its result
    or a message to show the user."""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work

    def run(self) -> None:
        try:
            result = self._work()
        except SyncerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            # A bug, not a user-facing failure — but an exception escaping a
            # thread is only printed, and a frozen build has no console, so the
            # dialog would wait on this worker forever.
            self.failed.emit(f"Unexpected error: {exc!r}")
        else:
            self.succeeded.emit(result)


class UpdateDialog(QDialog):
    """Checks for a newer release as soon as it's created. `exec()` returns
    with `prepared` set only if the user chose Install and the download
    verified; the caller then swaps it in."""

    def __init__(self, storage: StorageLayout, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Check for updates")
        self.setMinimumWidth(520)
        self.prepared: PreparedUpdate | None = None
        self._storage = storage
        self._offer: UpdateOffer | None = None
        self._worker: _Worker | None = None

        self._status = QLabel()
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # busy indicator: neither call reports progress
        self._notes = QTextBrowser()
        self._notes.setOpenLinks(False)  # release notes are read here, never navigated
        self._notes.setMinimumHeight(180)
        self._install_button = QPushButton("Install and restart")
        self._close_button = QPushButton()
        self._install_button.clicked.connect(self._install)
        self._close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self._install_button)
        buttons.addWidget(self._close_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._progress)
        layout.addWidget(self._notes, 1)
        layout.addLayout(buttons)

        self._start(
            "Checking for updates…",
            lambda: check_for_update(GitHubReleaseSource()),
            self._on_checked,
        )

    # -- worker plumbing -------------------------------------------------

    def _start(self, status: str, work, on_success) -> None:
        self._status.setText(status)
        self._progress.show()
        self._install_button.hide()
        self._notes.hide()
        self._close_button.setText("Cancel")
        self._close_button.setEnabled(False)
        self.adjustSize()
        self._worker = _Worker(work, self)
        self._worker.succeeded.connect(on_success)
        self._worker.failed.connect(self._settle)
        self._worker.start()

    def _settle(self, status: str, *, close_text: str = "Close") -> None:
        """Leave the busy state: the call finished, so the dialog can close."""
        self._status.setText(status)
        self._progress.hide()
        self._close_button.setText(close_text)
        self._close_button.setEnabled(True)
        self.adjustSize()

    def reject(self) -> None:
        # Close is disabled exactly while a call is in flight; Esc and the
        # title-bar X land here too, so gate them on the same state.
        if self._close_button.isEnabled():
            super().reject()

    def done(self, result: int) -> None:
        # The worker has already reported by the time the dialog can close, but
        # a QThread destroyed a moment before it finishes aborts the process.
        if self._worker is not None:
            self._worker.wait()
        super().done(result)

    # -- states ----------------------------------------------------------

    def _on_checked(self, offer: UpdateOffer | None) -> None:
        if offer is None:
            self._settle(f"You're on the latest version (v{app_version()}).")
            return
        self._offer = offer
        self._notes.setMarkdown(offer.release_notes.strip() or _NO_NOTES)
        self._notes.show()
        self._install_button.show()
        self._settle(
            f"Syncer v{offer.version} is available. You have v{app_version()}.",
            close_text="Cancel",
        )

    def _install(self) -> None:
        offer = self._offer
        self._start(
            f"Downloading Syncer v{offer.version}…",
            lambda: prepare_update(offer, self._storage),
            self._on_prepared,
        )

    def _on_prepared(self, prepared: PreparedUpdate) -> None:
        self.prepared = prepared
        self.accept()
