import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncer import storage
from syncer.storage import (
    BaseDirNotFoundError,
    BaseDirNotWritableError,
    SyncerError,
    ensure_base_dir_writable,
    ensure_storage_layout,
    initialize_storage,
    resolve_base_dir,
)


def _fake_compiled_exe(tmp_path, monkeypatch, *, standalone: bool) -> Path:
    """Stand in for a Nuitka build: an exe on disk plus the `__compiled__` marker."""
    fake_exe = tmp_path / "install" / "syncer.exe"
    fake_exe.parent.mkdir()
    fake_exe.touch()
    monkeypatch.setitem(
        storage.__dict__, "__compiled__", SimpleNamespace(standalone=standalone)
    )
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    return fake_exe


def test_resolve_base_dir_uses_project_root_when_running_from_source():
    base_dir = resolve_base_dir()

    assert (base_dir / "pyproject.toml").is_file()


def test_resolve_base_dir_uses_executable_dir_when_nuitka_standalone(tmp_path, monkeypatch):
    fake_exe = _fake_compiled_exe(tmp_path, monkeypatch, standalone=True)

    base_dir = resolve_base_dir()

    assert base_dir == fake_exe.parent


def test_resolve_base_dir_ignores_executable_dir_when_compiled_but_not_standalone(
    tmp_path, monkeypatch
):
    # A non-standalone compile still defines __compiled__, but sys.executable is
    # the interpreter -- adopting its directory would silently make an arbitrary
    # directory `base_dir`, which ADR 0001 forbids.
    fake_exe = _fake_compiled_exe(tmp_path, monkeypatch, standalone=False)

    base_dir = resolve_base_dir()

    assert base_dir != fake_exe.parent
    assert (base_dir / "pyproject.toml").is_file()


def test_resolve_base_dir_raises_when_outside_a_source_tree(monkeypatch):
    monkeypatch.setattr(storage, "PROJECT_ROOT_MARKER", "not-a-real-marker.toml")

    with pytest.raises(BaseDirNotFoundError):
        resolve_base_dir()


def test_resolve_base_dir_ignores_an_unrelated_project_root(tmp_path, monkeypatch):
    other_project = tmp_path / "other"
    installed_module = (
        other_project / ".venv" / "Lib" / "site-packages" / "syncer" / "storage.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.touch()
    (other_project / "pyproject.toml").write_text('[project]\nname = "other"\n')
    monkeypatch.setattr(storage, "__file__", str(installed_module))

    with pytest.raises(BaseDirNotFoundError):
        resolve_base_dir()


def test_ensure_base_dir_writable_passes_for_writable_dir(tmp_path):
    ensure_base_dir_writable(tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_ensure_base_dir_writable_raises_when_base_dir_is_not_a_dir(tmp_path):
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.touch()

    with pytest.raises(BaseDirNotWritableError):
        ensure_base_dir_writable(not_a_dir)


def test_ensure_storage_layout_creates_dirs_and_locates_files(tmp_path):
    layout = ensure_storage_layout(tmp_path)

    assert layout.base_dir == tmp_path
    assert layout.backups_dir == tmp_path / "backups"
    assert layout.backups_dir.is_dir()
    assert layout.logs_dir == tmp_path / "logs"
    assert layout.logs_dir.is_dir()
    assert layout.config_path == tmp_path / "config.toml"
    assert layout.state_path == tmp_path / "state.json"
    assert layout.lock_path == tmp_path / "syncer.lock"
    assert not any(
        path.exists()
        for path in (layout.config_path, layout.state_path, layout.lock_path)
    )


def test_initialize_storage_provisions_the_given_base_dir(tmp_path):
    layout = initialize_storage(tmp_path)

    assert layout.base_dir == tmp_path
    assert layout.backups_dir.is_dir()
    assert layout.logs_dir.is_dir()


def test_initialize_storage_refuses_an_unwritable_base_dir(tmp_path):
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.touch()

    with pytest.raises(BaseDirNotWritableError):
        initialize_storage(not_a_dir)


def test_initialize_storage_resolves_base_dir_when_not_given(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "resolve_base_dir", lambda: tmp_path)

    layout = initialize_storage()

    assert layout.base_dir == tmp_path


def test_base_dir_errors_share_the_package_error_root():
    assert issubclass(BaseDirNotWritableError, SyncerError)
    assert issubclass(BaseDirNotFoundError, SyncerError)
