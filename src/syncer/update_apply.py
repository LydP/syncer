import hashlib
import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn

from syncer import APP_UPDATE_MANIFEST_FILENAME, PROJECT_NAME
from syncer.lock import acquire_lock, release_lock
from syncer.storage import StorageLayout, SyncerError, atomic_write_bytes
from syncer.update import UpdateOffer

HANDOFF_FLAG = "--app-update-handoff"
_JOURNAL_FILENAME = "journal.json"
_LAUNCHED_MARKER = "launched-ok"
# `layout.update_dir`'s subfolders: the downloaded build, the running build
# set aside, and a new build moved back out of the way by a restore.
_STAGING = "staging"
_OLD = "old"
_NEW = "new"
_CHUNK_SIZE = 64 * 1024
# Defender can briefly lock a freshly written file, failing a rename that
# would succeed a moment later.
_MOVE_ATTEMPTS = 5
_MOVE_RETRY_DELAY = 0.05
# Per blocking socket operation, not the whole download: a stalled connection
# fails instead of hanging the download forever.
_DOWNLOAD_TIMEOUT = 30.0

Replace = Callable[[Path, Path], object]


class UpdateApplyError(SyncerError):
    pass


class UpdateRolledBackError(UpdateApplyError):
    """The new build didn't start, and the old one was put back."""


@dataclass(frozen=True)
class UpdateHandoff:
    """The swapped-in new build, running, and the old build waiting to learn
    whether it started (or to restore itself if it didn't)."""

    process: subprocess.Popen
    layout: StorageLayout
    replace: Replace

    def wait(
        self, *, timeout=60.0, poll_interval=0.1, clock=time.monotonic, sleep=time.sleep
    ) -> None:
        """Block until the new build reports it launched (then finish the
        update), or fail because it exited or timed out: restore the old
        build, retake the lock, and raise `UpdateRolledBackError`."""
        marker = self.layout.update_dir / _LAUNCHED_MARKER
        deadline = clock() + timeout
        while not marker.exists():
            exit_code = self.process.poll()
            # Re-check: it may have reported in and exited since the check above.
            if exit_code is not None and not marker.exists():
                _roll_back(
                    self.layout,
                    self.replace,
                    f"exited (code {exit_code}) before it finished starting",
                )
            # Re-check: it may have reported in since the loop's check, and
            # then it may already be writing user data, so it must be kept.
            if clock() >= deadline and not marker.exists():
                self._stop_new_build()
                _roll_back(
                    self.layout,
                    self.replace,
                    f"didn't finish starting within {timeout:g} seconds",
                )
            sleep(poll_interval)
        _clear_update_dir(self.layout)

    def _stop_new_build(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # restore still moves its files aside; a locked one is swept later


@dataclass(frozen=True)
class PreparedUpdate:
    version: str
    old_manifest: tuple[str, ...]
    new_manifest: tuple[str, ...]


def prepare_update(
    offer: UpdateOffer, layout: StorageLayout, *, opener=urllib.request.urlopen
) -> PreparedUpdate:
    old_manifest = _read_installed_manifest(layout)
    zip_path = layout.update_dir / "download.zip"
    layout.update_dir.mkdir(exist_ok=True)
    try:
        _download(offer, zip_path, opener)
        new_manifest = _extract(zip_path, layout)
    except BaseException:
        _discard(layout, zip_path)
        raise
    return PreparedUpdate(
        version=offer.version,
        old_manifest=tuple(old_manifest),
        new_manifest=tuple(new_manifest),
    )


def _read_installed_manifest(layout: StorageLayout) -> list[str]:
    """The running build's own file list. Without one there's no record of
    which files are Syncer's to replace — v0.1.0 and a source checkout ship
    none — so an app update is refused rather than guessed at."""
    try:
        raw = (layout.base_dir / APP_UPDATE_MANIFEST_FILENAME).read_bytes()
    except FileNotFoundError:
        raise UpdateApplyError(
            "This copy of Syncer has no file manifest, so an app update can't "
            "tell which files are its own. Install the new version manually."
        ) from None
    return _parse_manifest(raw, "this build's", layout.generated_names())


def _download(offer: UpdateOffer, zip_path: Path, opener) -> None:
    """Stream the asset to `zip_path`, hashing as it arrives, and refuse it
    unless it matches the digest GitHub reported for it."""
    digest = hashlib.sha256()
    try:
        with opener(offer.asset_url, timeout=_DOWNLOAD_TIMEOUT) as response, zip_path.open("wb") as out:
            while chunk := response.read(_CHUNK_SIZE):
                digest.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise UpdateApplyError(
            f"GitHub answered the update download with HTTP {exc.code}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        raise UpdateApplyError(
            f"Couldn't download the update ({reason}). Check your internet connection."
        ) from exc
    if digest.hexdigest() != offer.sha256:
        raise UpdateApplyError(
            "The downloaded update doesn't match the SHA-256 GitHub published "
            "for it, so it was discarded."
        )


def _extract(zip_path: Path, layout: StorageLayout) -> list[str]:
    """Stage exactly the files the archive's own manifest lists, with the
    archive's top-level folder stripped. Anything else in the archive is
    ignored, so an entry can't land outside the manifest's validated paths."""
    staging = layout.update_dir / _STAGING
    with zipfile.ZipFile(zip_path) as archive:
        entries = {
            PurePosixPath(*PurePosixPath(info.filename).parts[1:]).as_posix(): info
            for info in archive.infolist()
            if not info.is_dir()
        }
        if APP_UPDATE_MANIFEST_FILENAME not in entries:
            raise UpdateApplyError("The update has no file manifest, so it can't be installed.")
        manifest = _parse_manifest(
            archive.read(entries[APP_UPDATE_MANIFEST_FILENAME]),
            "the update's",
            layout.generated_names(),
        )
        for rel in manifest:
            if rel not in entries:
                raise UpdateApplyError(
                    f"The update's file manifest lists {rel}, which the download lacks."
                )
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entries[rel]) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, _CHUNK_SIZE)
    return manifest


def _parse_manifest(raw: bytes, whose: str, generated_names: frozenset[str]) -> list[str]:
    """The file paths a manifest lists, refusing any that could reach outside
    the build's own files: absolute or drive-qualified, using `..`, or naming
    something Syncer generates (compared case-insensitively, as Windows does)."""
    try:
        paths = json.loads(raw)
    except ValueError:
        paths = None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise UpdateApplyError(f"{whose} file manifest isn't a JSON list of paths.")
    for path in paths:
        parts = PurePosixPath(path).parts
        unsafe = (
            not parts
            or "\\" in path
            or ":" in path
            or path.startswith("/")
            or ".." in parts
            or parts[0].lower() in generated_names
        )
        if unsafe:
            raise UpdateApplyError(
                f"{whose} file manifest lists {path!r}, which isn't one of the "
                "build's own files."
            )
    return paths


def _discard(layout: StorageLayout, zip_path: Path) -> None:
    """Remove what a failed `prepare_update` left, and the working folder
    itself if that leaves it empty. Best-effort: never masks the failure."""
    shutil.rmtree(layout.update_dir / _STAGING, ignore_errors=True)
    zip_path.unlink(missing_ok=True)
    try:
        layout.update_dir.rmdir()
    except OSError:
        pass


def _launch(exe: Path, args: list[str]) -> subprocess.Popen:
    return subprocess.Popen([str(exe), *args], cwd=exe.parent)


def install_update(
    prepared: PreparedUpdate,
    layout: StorageLayout,
    *,
    launch: Callable[[Path, list[str]], subprocess.Popen] = _launch,
    replace: Replace = os.replace,
) -> UpdateHandoff:
    """Swap the staged build in over the running one and launch it.

    The running app renames its own files aside into `.app-update/old/` — a
    running exe or DLL can't be overwritten or deleted on Windows, but it can
    be renamed within a volume — then moves the staged files into place.
    """
    base_dir = layout.base_dir
    staging = layout.update_dir / _STAGING
    # Written before the first move: while it exists, the install may be
    # half-swapped, and `_restore` uses it to tell which files are new.
    atomic_write_bytes(
        layout.update_dir / _JOURNAL_FILENAME,
        json.dumps(
            {"to_version": prepared.version, "new_manifest": list(prepared.new_manifest)}
        ).encode("utf-8"),
    )
    try:
        for rel in dict.fromkeys([*prepared.old_manifest, *prepared.new_manifest]):
            if (base_dir / rel).exists():
                _move(replace, base_dir / rel, layout.update_dir / _OLD / rel)
        for rel in prepared.new_manifest:
            _move(replace, staging / rel, base_dir / rel)
    except Exception as exc:
        _restore(layout, replace)
        raise UpdateApplyError(
            f"The update couldn't be installed ({exc}). The previous version was restored."
        ) from exc
    release_lock(layout.lock_path, pid=os.getpid())
    try:
        process = launch(base_dir / f"{PROJECT_NAME}.exe", [HANDOFF_FLAG])
    except OSError as exc:
        _roll_back(layout, replace, f"couldn't be launched ({exc})")
    return UpdateHandoff(process=process, layout=layout, replace=replace)


def _roll_back(layout: StorageLayout, replace: Replace, reason: str) -> NoReturn:
    """Restore the old build, retake the lock, and raise for the caller."""
    _restore(layout, replace)
    acquire_lock(layout.lock_path, pid=os.getpid())
    raise UpdateRolledBackError(
        f"The new version {reason}, so the previous version was restored."
    )


def _launched_by_update(argv: list[str]) -> bool:
    """Whether an app update's old build started this process."""
    return HANDOFF_FLAG in argv[1:]


def report_launched(layout: StorageLayout, argv: list[str]) -> None:
    """Tell the old build, if an app update started this one, that startup got
    far enough to keep. Called at startup's success point: after Qt and the
    imports have loaded, before anything is written to user data, since
    rollback is only safe while the new build hasn't touched that data."""
    if not _launched_by_update(argv):
        return
    layout.update_dir.mkdir(exist_ok=True)
    atomic_write_bytes(layout.update_dir / _LAUNCHED_MARKER, b"")


def settle_previous_update(
    layout: StorageLayout, argv: list[str], *, running_version: str | None
) -> str | None:
    """Startup housekeeping for an app update that didn't run to its end.

    Skipped when an app update started this process: its old build is still
    watching `.app-update/` and owns it until it decides.

    A journal means the previous update never reached a verdict (the old
    build died mid-swap or while supervising). If this process is the new
    version, someone launched it by hand after that: accept it. Otherwise
    it's the old build, so put it back and return a message saying so.
    Without a journal, whatever `.app-update/` still holds is just leftovers
    (a finished update's locked files, a failed download) and is swept.
    """
    if _launched_by_update(argv):
        return None
    journal_path = layout.update_dir / _JOURNAL_FILENAME
    if journal_path.exists():
        to_version = json.loads(journal_path.read_bytes())["to_version"]
        if running_version == to_version:
            _clear_update_dir(layout)
            return None
        _restore(layout, os.replace)
        return (
            "A previous app update was interrupted, so the previous version "
            "of Syncer was restored."
        )
    shutil.rmtree(layout.update_dir, ignore_errors=True)
    return None


def _clear_update_dir(layout: StorageLayout) -> None:
    """End the update: drop the journal first, so a crash mid-sweep can't be
    mistaken for a half-done swap, then sweep `.app-update/`. The old build's
    running files can't be deleted until it exits, so whatever survives is
    swept by the next launch."""
    (layout.update_dir / _JOURNAL_FILENAME).unlink(missing_ok=True)
    shutil.rmtree(layout.update_dir, ignore_errors=True)


def _restore(layout: StorageLayout, replace: Replace) -> None:
    """Put the old build back: move whatever new files were placed out of the
    way, move the set-aside files home, and clear `.app-update/`.

    A staged file that's still in staging was never placed, so the file at
    its live path (if any) is still the old one and must stay. Likewise one
    already in `new/` was moved aside by an earlier, interrupted restore, so
    the file at its live path is the old one that restore moved back home.
    """
    base_dir = layout.base_dir
    work_dir = layout.update_dir
    journal = json.loads((work_dir / _JOURNAL_FILENAME).read_bytes())
    for rel in journal["new_manifest"]:
        placed = (
            (base_dir / rel).exists()
            and not (work_dir / _STAGING / rel).exists()
            and not (work_dir / _NEW / rel).exists()
        )
        if placed:
            _move(replace, base_dir / rel, work_dir / _NEW / rel)
    old_dir = work_dir / _OLD
    for old_file in [p for p in old_dir.rglob("*") if p.is_file()]:
        _move(replace, old_file, base_dir / old_file.relative_to(old_dir))
    _clear_update_dir(layout)


def _move(replace: Replace, src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, _MOVE_ATTEMPTS + 1):
        try:
            replace(src, dst)
            return
        except PermissionError:
            if attempt == _MOVE_ATTEMPTS:
                raise
            time.sleep(_MOVE_RETRY_DELAY)
