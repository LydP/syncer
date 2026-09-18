import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile

import pytest

from syncer import APP_UPDATE_MANIFEST_FILENAME as MANIFEST
from syncer import update_apply
from syncer.update import UpdateOffer
from syncer.update_apply import (
    UpdateApplyError,
    UpdateRolledBackError,
    install_update,
    prepare_update,
    report_launched,
    settle_previous_update,
)

STAGING = ".app-update/staging"


def _write_install(base_dir, files, *, manifest=True):
    """Lay down an installed build under `base_dir`, with its manifest."""
    for rel, content in files.items():
        path = base_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if manifest:
        (base_dir / MANIFEST).write_text(json.dumps(sorted([*files, MANIFEST])))


def _release_zip(files, *, manifest=None):
    """A release asset: `files` under `app.dist/`, plus the manifest a build
    ships (listing `manifest`, or every file, when not given)."""
    listed = sorted([*files, MANIFEST]) if manifest is None else manifest
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for rel, content in files.items():
            archive.writestr(f"app.dist/{rel}", content)
        archive.writestr(f"app.dist/{MANIFEST}", json.dumps(listed))
    return buffer.getvalue()


def _offer(url, asset, *, sha256=None):
    return UpdateOffer(
        version="0.2.1",
        release_notes="",
        asset_url=url,
        sha256=hashlib.sha256(asset).hexdigest() if sha256 is None else sha256,
    )


def _serve(github, asset):
    """Have the loopback GitHub serve `asset`, and return its download URL."""
    github.reply(body=asset)
    return f"{github.base_url}/asset.zip"


def test_prepare_update_stages_the_new_build_and_leaves_the_install_alone(layout, github):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe", "lib/a.dll": b"old dll"})
    asset = _release_zip({"syncer.exe": b"new exe", "lib/a.dll": b"new dll"})

    prepared = prepare_update(_offer(_serve(github, asset), asset), layout)

    staging = layout.base_dir / STAGING
    assert (staging / "syncer.exe").read_bytes() == b"new exe"
    assert (staging / "lib" / "a.dll").read_bytes() == b"new dll"
    assert (layout.base_dir / "syncer.exe").read_bytes() == b"old exe"
    assert prepared.version == "0.2.1"


def test_prepare_update_refuses_a_download_that_does_not_match_the_digest(layout, github):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})
    asset = _release_zip({"syncer.exe": b"new exe"})
    offer = _offer(_serve(github, asset), asset, sha256="00" * 32)

    with pytest.raises(UpdateApplyError, match="SHA-256"):
        prepare_update(offer, layout)

    assert not (layout.base_dir / ".app-update").exists()
    assert (layout.base_dir / "syncer.exe").read_bytes() == b"old exe"


@pytest.mark.parametrize(
    "bad_path",
    [
        "../evil.dll",
        "sub/../../evil.dll",
        "/evil.dll",
        "C:/evil.dll",
        "sub\\evil.dll",
        "config.toml",
        "Config.TOML",
        "state.json",
        "syncer.lock",
        "backups/config-1.toml",
        "logs/sync.log",
        ".app-update/staging/x",
    ],
)
def test_prepare_update_refuses_a_manifest_that_escapes_the_install_or_names_user_data(
    layout, github, bad_path
):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})
    asset = _release_zip({"syncer.exe": b"new exe"}, manifest=["syncer.exe", MANIFEST, bad_path])

    with pytest.raises(UpdateApplyError, match="manifest"):
        prepare_update(_offer(_serve(github, asset), asset), layout)

    assert not (layout.base_dir / ".app-update").exists()


def test_prepare_update_refuses_an_archive_without_a_manifest(layout, github):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("app.dist/syncer.exe", b"new exe")
    asset = buffer.getvalue()

    with pytest.raises(UpdateApplyError, match="manifest"):
        prepare_update(_offer(_serve(github, asset), asset), layout)

    assert not (layout.base_dir / ".app-update").exists()


def test_prepare_update_refuses_an_archive_missing_a_file_its_manifest_lists(layout, github):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})
    asset = _release_zip({"syncer.exe": b"new exe"}, manifest=["syncer.exe", "lib/a.dll", MANIFEST])

    with pytest.raises(UpdateApplyError, match=r"lib/a\.dll"):
        prepare_update(_offer(_serve(github, asset), asset), layout)

    assert not (layout.base_dir / ".app-update").exists()


def test_prepare_update_only_stages_files_the_manifest_lists(layout, github):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})
    asset = _release_zip(
        {"syncer.exe": b"new exe", "extra.txt": b"unlisted"},
        manifest=["syncer.exe", MANIFEST],
    )

    prepare_update(_offer(_serve(github, asset), asset), layout)

    assert not (layout.base_dir / STAGING / "extra.txt").exists()


def test_prepare_update_refuses_an_install_with_no_manifest_of_its_own(layout, github):
    # v0.1.0 and a source checkout ship none, so there is no record of which
    # files are Syncer's own to replace.
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"}, manifest=False)
    asset = _release_zip({"syncer.exe": b"new exe"})

    with pytest.raises(UpdateApplyError, match="manifest"):
        prepare_update(_offer(_serve(github, asset), asset), layout)

    assert not (layout.base_dir / ".app-update").exists()
    assert (layout.base_dir / "syncer.exe").read_bytes() == b"old exe"


def test_prepare_update_reports_an_unreachable_host_and_leaves_nothing_behind(layout):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})

    def opener(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    with pytest.raises(UpdateApplyError, match="download"):
        prepare_update(_offer("http://github.invalid/asset.zip", b""), layout, opener=opener)

    assert not (layout.base_dir / ".app-update").exists()


def test_prepare_update_reports_a_download_rejected_by_the_server(layout):
    _write_install(layout.base_dir, {"syncer.exe": b"old exe"})

    def opener(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with pytest.raises(UpdateApplyError, match="HTTP 404"):
        prepare_update(_offer("http://github.invalid/asset.zip", b""), layout, opener=opener)

    assert not (layout.base_dir / ".app-update").exists()


OLD = ".app-update/old"


class FakeProcess:
    """Stands in for the launched new build: `exit_code` stays None while it
    is "running", like `subprocess.Popen.poll`."""

    def __init__(self):
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 1

    def wait(self, timeout=None):
        return self.exit_code


class FakeLauncher:
    def __init__(self):
        self.launches = []
        self.process = FakeProcess()

    def __call__(self, exe, args):
        self.launches.append((exe, args))
        return self.process


def _prepared(layout, github, installed, new):
    """An installed build under `layout.base_dir` and an update to `new`,
    already downloaded and staged."""
    _write_install(layout.base_dir, installed)
    asset = _release_zip(new)
    return prepare_update(_offer(_serve(github, asset), asset), layout)


def test_install_update_swaps_in_the_new_build_and_sets_the_old_one_aside(layout, github):
    prepared = _prepared(
        layout,
        github,
        {"syncer.exe": b"old exe", "lib/a.dll": b"old dll", "lib/gone.dll": b"old only"},
        {"syncer.exe": b"new exe", "lib/a.dll": b"new dll", "lib/b.dll": b"new only"},
    )

    install_update(prepared, layout, launch=FakeLauncher())

    base = layout.base_dir
    assert (base / "syncer.exe").read_bytes() == b"new exe"
    assert (base / "lib" / "a.dll").read_bytes() == b"new dll"
    assert (base / "lib" / "b.dll").read_bytes() == b"new only"
    assert not (base / "lib" / "gone.dll").exists()
    assert (base / OLD / "syncer.exe").read_bytes() == b"old exe"
    assert (base / OLD / "lib" / "a.dll").read_bytes() == b"old dll"
    assert (base / OLD / "lib" / "gone.dll").read_bytes() == b"old only"
    assert json.loads((base / MANIFEST).read_text()) == sorted(
        ["syncer.exe", "lib/a.dll", "lib/b.dll", MANIFEST]
    )


def test_install_update_never_touches_files_that_are_in_no_manifest(layout, github):
    prepared = _prepared(layout, github, {"syncer.exe": b"old exe"}, {"syncer.exe": b"new exe"})
    base = layout.base_dir
    layout.config_path.write_text("version = 1")
    layout.state_path.write_text("{}")
    (layout.backups_dir / "config-1.toml").write_text("backup")
    (layout.logs_dir / "sync.log").write_text("log")
    (base / "notes.txt").write_text("the user's own file")

    install_update(prepared, layout, launch=FakeLauncher())

    assert layout.config_path.read_text() == "version = 1"
    assert layout.state_path.read_text() == "{}"
    assert (layout.backups_dir / "config-1.toml").read_text() == "backup"
    assert (layout.logs_dir / "sync.log").read_text() == "log"
    assert (base / "notes.txt").read_text() == "the user's own file"


def test_install_update_launches_the_new_build_with_the_hand_off_flag(layout, github):
    prepared = _prepared(layout, github, {"syncer.exe": b"old exe"}, {"syncer.exe": b"new exe"})
    launcher = FakeLauncher()

    install_update(prepared, layout, launch=launcher)

    assert launcher.launches == [(layout.base_dir / "syncer.exe", ["--app-update-handoff"])]


def test_install_update_releases_our_lock_so_the_new_build_can_take_it(layout, github):
    prepared = _prepared(layout, github, {"syncer.exe": b"old exe"}, {"syncer.exe": b"new exe"})
    layout.lock_path.write_text(str(os.getpid()))
    lock_seen_at_launch = []

    def launch(exe, args):
        lock_seen_at_launch.append(layout.lock_path.exists())
        return FakeProcess()

    install_update(prepared, layout, launch=launch)

    assert lock_seen_at_launch == [False]


def test_install_update_reverses_a_swap_that_fails_partway(layout, github):
    prepared = _prepared(
        layout,
        github,
        {"syncer.exe": b"old exe", "lib/a.dll": b"old dll"},
        {"syncer.exe": b"new exe", "lib/a.dll": b"new dll", "lib/b.dll": b"new only"},
    )
    (layout.base_dir / STAGING / "lib" / "b.dll").unlink()  # a placement will fail
    launcher = FakeLauncher()

    with pytest.raises(UpdateApplyError, match="previous version"):
        install_update(prepared, layout, launch=launcher)

    base = layout.base_dir
    assert (base / "syncer.exe").read_bytes() == b"old exe"
    assert (base / "lib" / "a.dll").read_bytes() == b"old dll"
    assert not (base / "lib" / "b.dll").exists()
    assert json.loads((base / MANIFEST).read_text()) == sorted(["syncer.exe", "lib/a.dll", MANIFEST])
    assert not (base / ".app-update").exists()
    assert launcher.launches == []


def test_install_update_retries_a_move_that_is_briefly_locked(layout, github, monkeypatch):
    monkeypatch.setattr(update_apply, "_MOVE_RETRY_DELAY", 0)
    prepared = _prepared(layout, github, {"syncer.exe": b"old exe"}, {"syncer.exe": b"new exe"})
    failures = iter([PermissionError("locked by Defender")] * 2)

    def flaky_replace(src, dst):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        os.replace(src, dst)

    install_update(prepared, layout, launch=FakeLauncher(), replace=flaky_replace)

    assert (layout.base_dir / "syncer.exe").read_bytes() == b"new exe"
    assert (layout.base_dir / OLD / "syncer.exe").read_bytes() == b"old exe"


MARKER = ".app-update/launched-ok"


class FakeClock:
    """Time that only moves when `wait` sleeps; `on_sleep(n)` runs after the
    nth sleep, letting a test play the new build's part (write the marker,
    exit) at a chosen moment."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


OLD_BUILD = {"syncer.exe": b"old exe", "lib/gone.dll": b"old only"}
NEW_BUILD = {"syncer.exe": b"new exe", "lib/b.dll": b"new only"}


def _installed(layout, github, launcher=None):
    """An update from `OLD_BUILD` to `NEW_BUILD`, swapped in and launched."""
    prepared = _prepared(layout, github, OLD_BUILD, NEW_BUILD)
    return install_update(prepared, layout, launch=launcher or FakeLauncher())


def test_wait_finishes_the_update_once_the_new_build_reports_it_launched(layout, github):
    handoff = _installed(layout, github)
    base = layout.base_dir
    clock = FakeClock(lambda n: (base / MARKER).write_bytes(b"") if n == 3 else None)

    handoff.wait(clock=clock, sleep=clock.sleep)

    assert (base / "syncer.exe").read_bytes() == b"new exe"
    assert (base / "lib" / "b.dll").read_bytes() == b"new only"
    assert not (base / "lib" / "gone.dll").exists()
    assert not (base / ".app-update").exists()


def _assert_old_files_are_back(layout):
    base = layout.base_dir
    assert (base / "syncer.exe").read_bytes() == b"old exe"
    assert (base / "lib" / "gone.dll").read_bytes() == b"old only"
    assert not (base / "lib" / "b.dll").exists()
    assert json.loads((base / MANIFEST).read_text()) == sorted(
        ["syncer.exe", "lib/gone.dll", MANIFEST]
    )
    assert not (base / ".app-update").exists()


def _assert_old_build_is_back(layout):
    _assert_old_files_are_back(layout)
    assert layout.lock_path.read_text() == str(os.getpid())


def test_wait_restores_the_old_build_when_the_new_one_exits_before_launching(layout, github):
    launcher = FakeLauncher()
    handoff = _installed(layout, github, launcher)

    def crash(n):
        launcher.process.exit_code = 3221225781  # e.g. a missing DLL

    clock = FakeClock(crash)
    with pytest.raises(UpdateRolledBackError, match="3221225781"):
        handoff.wait(clock=clock, sleep=clock.sleep)

    _assert_old_build_is_back(layout)


def test_wait_restores_the_old_build_when_the_new_one_never_reports_in(layout, github):
    launcher = FakeLauncher()
    handoff = _installed(layout, github, launcher)
    clock = FakeClock()

    with pytest.raises(UpdateRolledBackError, match="60 seconds"):
        handoff.wait(timeout=60, clock=clock, sleep=clock.sleep)

    assert launcher.process.terminated
    _assert_old_build_is_back(layout)


def test_wait_counts_a_new_build_that_reported_in_and_then_exited_as_launched(layout, github):
    launcher = FakeLauncher()
    handoff = _installed(layout, github, launcher)
    base = layout.base_dir

    def report_then_exit(n):
        (base / MARKER).write_bytes(b"")
        launcher.process.exit_code = 0

    clock = FakeClock(report_then_exit)
    handoff.wait(clock=clock, sleep=clock.sleep)

    assert (base / "syncer.exe").read_bytes() == b"new exe"


def test_install_update_restores_the_old_build_when_the_new_one_cannot_be_launched(layout, github):
    prepared = _prepared(layout, github, OLD_BUILD, NEW_BUILD)
    layout.lock_path.write_text(str(os.getpid()))

    def cannot_launch(exe, args):
        raise OSError("not a valid Win32 application")

    with pytest.raises(UpdateRolledBackError, match="Win32"):
        install_update(prepared, layout, launch=cannot_launch)

    _assert_old_build_is_back(layout)


class Crash(BaseException):
    """A hard stop (power loss, killed process): not an `Exception`, so
    nothing in `install_update` gets to clean up after it."""


def _crash_on_move(nth):
    calls = []

    def replace(src, dst):
        calls.append(src)
        if len(calls) == nth:
            raise Crash
        os.replace(src, dst)

    return replace


@pytest.mark.parametrize("nth_move", [2, 5, 6])  # while setting aside; while placing
def test_settle_restores_the_old_build_after_a_crash_partway_through_the_swap(
    layout, github, nth_move
):
    prepared = _prepared(layout, github, OLD_BUILD, NEW_BUILD)
    with pytest.raises(Crash):
        install_update(prepared, layout, launch=FakeLauncher(), replace=_crash_on_move(nth_move))

    message = settle_previous_update(layout, ["syncer.exe"], running_version="0.2.0")

    assert "restored" in message
    _assert_old_files_are_back(layout)


def test_settle_accepts_a_new_build_that_was_launched_by_hand_after_the_old_one_died(
    layout, github
):
    _installed(layout, github)  # swapped in and launched, but never supervised to the end

    message = settle_previous_update(layout, ["syncer.exe"], running_version="0.2.1")

    assert message is None
    base = layout.base_dir
    assert (base / "syncer.exe").read_bytes() == b"new exe"
    assert not (base / "lib" / "gone.dll").exists()
    assert not (base / ".app-update").exists()


def test_settle_sweeps_what_a_finished_update_left_behind(layout):
    # The old build's locked files couldn't be deleted while it was still
    # running, so the next launch clears them.
    leftover = layout.base_dir / OLD / "lib" / "a.dll"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"old dll")

    message = settle_previous_update(layout, ["syncer.exe"], running_version="0.2.1")

    assert message is None
    assert not (layout.base_dir / ".app-update").exists()


def test_settle_leaves_an_update_alone_when_launched_by_it(layout, github):
    # The old build is still watching `.app-update/` and owns it until it
    # decides; settling here would sweep the journal out from under it.
    _installed(layout, github)

    message = settle_previous_update(
        layout, ["syncer.exe", "--app-update-handoff"], running_version="0.2.1"
    )

    assert message is None
    assert (layout.base_dir / ".app-update" / "journal.json").exists()


def test_settle_does_nothing_when_no_update_was_ever_started(layout):
    assert settle_previous_update(layout, ["syncer.exe"], running_version="0.2.1") is None


def test_report_launched_writes_the_marker_when_started_by_an_app_update(layout, github):
    handoff = _installed(layout, github)

    report_launched(layout, ["syncer.exe", "--app-update-handoff"])

    assert (layout.base_dir / MARKER).exists()
    handoff.wait(sleep=lambda seconds: None)  # the old build sees it and finishes


def test_report_launched_does_nothing_for_an_ordinary_launch(layout):
    report_launched(layout, ["syncer.exe"])

    assert not (layout.base_dir / ".app-update").exists()


def _launch_python(code):
    """A launcher whose "new build" is a real child process running `code`."""
    return lambda exe, args: subprocess.Popen([sys.executable, "-c", code])


def test_wait_restores_the_old_build_when_a_real_new_build_process_dies_at_startup(
    layout, github
):
    handoff = _installed(layout, github, _launch_python("import sys; sys.exit(3)"))

    with pytest.raises(UpdateRolledBackError, match=r"code 3\)"):
        handoff.wait(timeout=30)

    _assert_old_build_is_back(layout)


def test_wait_kills_a_real_new_build_process_that_hangs_and_restores_the_old_build(
    layout, github
):
    handoff = _installed(layout, github, _launch_python("import time; time.sleep(60)"))
    process = handoff.process

    with pytest.raises(UpdateRolledBackError, match="didn't finish starting"):
        handoff.wait(timeout=0.5)

    assert process.poll() is not None
    _assert_old_build_is_back(layout)
