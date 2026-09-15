import os

import pytest

import syncer.check as check_module
from syncer.check import BaselineEntry, baseline_from_disk, check, hash_file
from syncer.config import SyncRule, normalize_replica_path

# Any digest that can't match real content, for exercising a stale baseline.
_STALE_HASH = "0" * 64


def _symlink(target, link):
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except OSError:
        pytest.skip("symlink creation not permitted in this environment")


def _baseline_entry_for(path):
    stat = os.stat(path)
    return BaselineEntry(hash=hash_file(str(path)), size=stat.st_size, mtime=stat.st_mtime)


def _kept_entry_for(replica_path, master_path=None):
    """A `kept` baseline entry as `apply_keep_replica` would write it:
    hash/size/mtime from the replica's current bytes, `kept_master_hash`
    from the master's current bytes (None if `master_path` doesn't exist).
    """
    master_hash = hash_file(str(master_path)) if master_path and os.path.isfile(master_path) else None
    return baseline_from_disk(str(replica_path), kept=True, kept_master_hash=master_hash)


def _baseline(replica, entries):
    """The `{normalized replica path: {rel_path: entry}}` shape state.json uses."""
    return {normalize_replica_path(str(replica)): entries}


def _rule(master, replicas, master_type="dir"):
    return SyncRule(
        id="r1",
        name="Rule 1",
        master=str(master),
        master_type=master_type,
        replicas=[str(r) for r in replicas],
    )


def _only_change(result):
    [replica_result] = result.replicas
    [change] = replica_result.files
    return change


def test_identical_file_in_master_and_replica_is_in_sync(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "a.txt"
    assert change.category == "in_sync"


def test_file_only_in_master_is_new(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "a.txt"
    assert change.category == "new"
    assert change.master_present is True
    assert change.replica_present is False


def test_file_only_in_replica_is_replica_only(master_and_replica):
    master, replica = master_and_replica
    (replica / "extra.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "extra.txt"
    assert change.category == "replica_only"
    assert change.master_present is False
    assert change.replica_present is True


def test_master_edited_since_last_sync_is_changed(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(replica / "a.txt")})
    (master / "a.txt").write_text("hello world")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "changed"


def test_replica_edited_since_last_sync_is_diverged(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(master / "a.txt")})
    (replica / "a.txt").write_text("edited locally")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "diverged"


def test_both_master_and_replica_edited_since_last_sync_is_both_changed(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(master / "a.txt")})
    (master / "a.txt").write_text("edited in master")
    (replica / "a.txt").write_text("edited in replica")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "both_changed"


def test_no_baseline_entry_but_content_differs_is_no_baseline(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("different")

    change = _only_change(check(_rule(master, [replica])))

    assert change.category == "no_baseline"


def test_master_and_replica_converge_on_same_content_refreshes_stale_baseline(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": BaselineEntry(hash=_STALE_HASH, size=11, mtime=1.0)})

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "in_sync"
    assert change.baseline_stale is True


def test_file_deleted_from_master_with_untouched_replica_is_master_deleted(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(replica / "a.txt")})
    (master / "a.txt").unlink()

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "master_deleted"


def test_file_deleted_from_master_but_also_edited_in_replica_is_both_changed(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(replica / "a.txt")})
    (master / "a.txt").unlink()
    (replica / "a.txt").write_text("edited locally")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "both_changed"


def test_replica_file_locally_deleted_but_master_and_baseline_agree_is_new(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(replica / "a.txt")})
    (replica / "a.txt").unlink()

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "new"


def test_kept_entry_unchanged_on_both_sides_is_kept(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "kept"


def test_kept_entry_with_master_changed_since_keep_is_changed(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})
    (master / "a.txt").write_text("master content, edited after the keep")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "changed"


def test_kept_entry_with_replica_changed_since_keep_is_diverged(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})
    (replica / "a.txt").write_text("kept replica content, edited again")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "diverged"


def test_kept_entry_with_both_changed_since_keep_is_both_changed(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})
    (master / "a.txt").write_text("master content, edited after the keep")
    (replica / "a.txt").write_text("kept replica content, edited again")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "both_changed"


def test_kept_entry_whose_master_was_absent_and_stays_absent_is_kept(master_and_replica):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt")})

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "kept"


def test_kept_entry_whose_master_stays_absent_and_replica_edited_again_is_diverged(
    master_and_replica,
):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt")})
    (replica / "a.txt").write_text("kept replica content, edited again")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "diverged"
    # No master copy to overwrite from: "overwrite from master" must delete.
    assert change.is_deletion


def test_kept_entry_whose_absent_master_reappears_unchanged_replica_is_changed(
    master_and_replica,
):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt")})
    (master / "a.txt").write_text("master reappeared")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "changed"


def test_kept_entry_whose_present_master_is_later_deleted_with_replica_unchanged_is_master_deleted(
    master_and_replica,
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})
    (master / "a.txt").unlink()

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "master_deleted"


def test_kept_entry_whose_present_master_is_later_deleted_with_replica_edited_is_both_changed(
    master_and_replica,
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    baseline = _baseline(replica, {"a.txt": _kept_entry_for(replica / "a.txt", master / "a.txt")})
    (master / "a.txt").unlink()
    (replica / "a.txt").write_text("kept replica content, edited again")

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "both_changed"


def test_kept_entry_masters_stat_coincidentally_matching_baseline_still_detects_change(
    master_and_replica,
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("kept replica content")
    kept_entry = _kept_entry_for(replica / "a.txt", master / "a.txt")
    # Same byte size as the replica's kept content but different bytes, so a
    # stat-only shortcut (reusing the kept entry's replica-derived size for
    # the master) would wrongly call this unchanged.
    (master / "a.txt").write_text("x" * kept_entry.size)
    # Force the master's current mtime to coincidentally match the kept
    # entry's stored mtime too, which belongs to the *replica's* file.
    os.utime(master / "a.txt", (kept_entry.mtime, kept_entry.mtime))
    assert (master / "a.txt").stat().st_size == kept_entry.size
    baseline = _baseline(replica, {"a.txt": kept_entry})

    change = _only_change(check(_rule(master, [replica]), baseline=baseline))

    assert change.category == "changed"


def test_missing_replica_reports_every_master_file_as_new(tmp_path):
    master = tmp_path / "master"
    replica = tmp_path / "replica"  # never created
    master.mkdir()
    (master / "a.txt").write_text("hello")

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    assert replica_result.replica_exists is False
    change = _only_change(result)
    assert change.rel_path == "a.txt"
    assert change.category == "new"


def test_single_file_master_compares_the_one_file(tmp_path):
    master = tmp_path / "resume.docx"
    replica = tmp_path / "resume_copy.docx"
    master.write_text("v1")
    replica.write_text("v2")

    change = _only_change(check(_rule(master, [replica], master_type="file")))

    assert change.rel_path == "resume.docx"
    assert change.category == "no_baseline"


def test_git_directory_is_ignored(master_and_replica):
    master, replica = master_and_replica
    (master / ".git").mkdir()
    (master / ".git" / "config").write_text("stuff")

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    assert replica_result.files == []


def test_nested_file_rel_path_uses_posix_separators(master_and_replica):
    master, replica = master_and_replica
    (master / "sub").mkdir()
    (master / "sub" / "nested.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "sub/nested.txt"


def test_matches_by_rel_path_case_insensitively_preserving_master_casing(master_and_replica):
    master, replica = master_and_replica
    (master / "README.txt").write_text("hello")
    (replica / "readme.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "README.txt"
    assert change.category == "in_sync"


def test_file_vs_directory_type_mismatch_is_unreadable(master_and_replica):
    master, replica = master_and_replica
    (master / "thing").write_text("a file")
    (replica / "thing").mkdir()

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "thing"
    assert change.category == "unreadable"


def test_file_symlink_in_master_is_followed_and_compared_by_content(master_and_replica, tmp_path):
    master, replica = master_and_replica
    real_target = tmp_path / "real.txt"
    real_target.write_text("hello")
    _symlink(str(real_target), str(master / "link.txt"))
    (replica / "link.txt").write_text("hello")

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "link.txt"
    assert change.category == "in_sync"


def test_directory_symlink_in_master_is_unreadable(master_and_replica, tmp_path):
    master, replica = master_and_replica
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_text("hello")
    _symlink(str(real_dir), str(master / "linked_dir"))

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "linked_dir"
    assert change.category == "unreadable"


def test_unreadable_file_is_reported_without_aborting_the_check(master_and_replica, monkeypatch):
    master, replica = master_and_replica
    (master / "broken.txt").write_text("hello")
    (replica / "broken.txt").write_text("hello")
    (master / "fine.txt").write_text("world")
    (replica / "fine.txt").write_text("world")

    real_open = open

    def flaky_open(path, *args, **kwargs):
        if os.path.basename(path) == "broken.txt" and "master" in str(path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", flaky_open)

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    changes_by_path = {c.rel_path: c for c in replica_result.files}
    assert changes_by_path["broken.txt"].category == "unreadable"
    assert "Permission denied" in changes_by_path["broken.txt"].detail
    assert changes_by_path["fine.txt"].category == "in_sync"


def test_directory_listing_error_is_collected_as_walk_error(master_and_replica, monkeypatch):
    master, replica = master_and_replica
    (master / "locked").mkdir()
    (master / "ok").mkdir()
    (master / "ok" / "a.txt").write_text("hello")
    (replica / "ok").mkdir()
    (replica / "ok" / "a.txt").write_text("hello")

    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if os.path.basename(str(path)) == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    assert len(replica_result.walk_errors) == 1
    assert "locked" in replica_result.walk_errors[0]["path"]
    assert "Permission denied" in replica_result.walk_errors[0]["message"]
    change = _only_change(result)
    assert change.rel_path == "ok/a.txt"
    assert change.category == "in_sync"


def _fail_listing_of(monkeypatch, dir_name):
    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if os.path.basename(str(path)) == dir_name:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)


def test_unlistable_master_root_sets_master_missing_flag(master_and_replica, monkeypatch):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("hello")
    baseline = _baseline(replica, {"a.txt": _baseline_entry_for(replica / "a.txt")})
    _fail_listing_of(monkeypatch, "master")

    result = check(_rule(master, [replica]), baseline=baseline)

    assert result.master_missing is True


def test_file_under_unlistable_master_folder_is_unreadable_not_master_deleted(
    master_and_replica, monkeypatch
):
    master, replica = master_and_replica
    (master / "locked").mkdir()
    (master / "locked" / "a.txt").write_text("hello")
    (replica / "locked").mkdir()
    (replica / "locked" / "a.txt").write_text("hello")
    baseline = _baseline(
        replica, {"locked/a.txt": _baseline_entry_for(replica / "locked" / "a.txt")}
    )
    _fail_listing_of(monkeypatch, "locked")

    result = check(_rule(master, [replica]), baseline=baseline)

    [replica_result] = result.replicas
    [change] = [c for c in replica_result.files if c.rel_path == "locked/a.txt"]
    assert change.category == "unreadable"


def test_single_file_rule_with_folder_at_replica_path_is_unreadable(tmp_path):
    master = tmp_path / "resume.docx"
    replica = tmp_path / "replica_dir"
    master.write_text("v1")
    replica.mkdir()

    change = _only_change(check(_rule(master, [replica], master_type="file")))

    assert change.category == "unreadable"


def test_single_file_rule_with_folder_at_master_path_is_not_master_deleted(tmp_path):
    master = tmp_path / "resume.docx"
    replica = tmp_path / "resume_copy.docx"
    master.mkdir()
    replica.write_text("v1")
    baseline = _baseline(replica, {"resume.docx": _baseline_entry_for(replica)})

    change = _only_change(check(_rule(master, [replica], master_type="file"), baseline=baseline))

    assert change.category == "unreadable"


def test_directory_junction_in_master_is_unreadable_and_not_recursed(master_and_replica, tmp_path):
    winapi = pytest.importorskip("_winapi")
    master, replica = master_and_replica
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "inner.txt").write_text("hello")
    winapi.CreateJunction(str(real_dir), str(master / "junction"))

    change = _only_change(check(_rule(master, [replica])))

    assert change.rel_path == "junction"
    assert change.category == "unreadable"


def test_progress_callback_is_invoked_between_files(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (master / "b.txt").write_text("world")
    (replica / "a.txt").write_text("hello")
    (replica / "b.txt").write_text("world")

    calls = []
    check(_rule(master, [replica]), progress=lambda done, total, path: calls.append((done, total, path)))

    assert len(calls) == 2
    assert {c[2] for c in calls} == {"a.txt", "b.txt"}
    assert all(total == 2 for _, total, _ in calls)
    assert sorted(c[0] for c in calls) == [1, 2]


def test_progress_total_spans_every_replica(master_and_replica, tmp_path):
    master, replica = master_and_replica
    second_replica = tmp_path / "replica2"
    second_replica.mkdir()
    (master / "a.txt").write_text("hello")
    (replica / "a.txt").write_text("hello")
    (second_replica / "a.txt").write_text("hello")

    calls = []
    check(
        _rule(master, [replica, second_replica]),
        progress=lambda done, total, path: calls.append((done, total, path)),
    )

    assert [c[0] for c in calls] == [1, 2]
    assert all(total == 2 for _, total, _ in calls)


def test_cancel_callback_stops_check_early_with_partial_result(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    (master / "b.txt").write_text("world")
    (master / "c.txt").write_text("!!!")
    (replica / "a.txt").write_text("hello")
    (replica / "b.txt").write_text("world")
    (replica / "c.txt").write_text("!!!")

    seen = []

    def cancel():
        return len(seen) >= 1

    result = check(
        _rule(master, [replica]),
        progress=lambda done, total, path: seen.append(path),
        cancel=cancel,
    )

    [replica_result] = result.replicas
    assert len(replica_result.files) < 3


def test_file_change_reports_master_and_replica_sizes(master_and_replica):
    master, replica = master_and_replica
    (master / "both.txt").write_text("hello world")  # 11 bytes
    (replica / "both.txt").write_text("hi")  # 2 bytes
    (master / "only_master.txt").write_text("xy")  # 2 bytes

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    changes_by_path = {c.rel_path: c for c in replica_result.files}
    assert changes_by_path["both.txt"].master_size == 11
    assert changes_by_path["both.txt"].replica_size == 2
    assert changes_by_path["only_master.txt"].master_size == 2
    assert changes_by_path["only_master.txt"].replica_size is None


def test_file_change_reports_baseline_size_and_mtime(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("replica content")
    (master / "b.txt").write_text("new")
    baseline = _baseline(replica, {"A.TXT": BaselineEntry(hash="deadbeef", size=1234, mtime=5678.0)})

    result = check(_rule(master, [replica]), baseline=baseline)

    [replica_result] = result.replicas
    changes_by_path = {c.rel_path: c for c in replica_result.files}
    assert changes_by_path["a.txt"].baseline_size == 1234
    assert changes_by_path["a.txt"].baseline_mtime == 5678.0
    assert changes_by_path["b.txt"].baseline_size is None
    assert changes_by_path["b.txt"].baseline_mtime is None


def test_missing_master_directory_sets_master_missing_flag(tmp_path):
    master = tmp_path / "master"  # never created
    replica = tmp_path / "replica"
    replica.mkdir()

    result = check(_rule(master, [replica]))

    assert result.master_missing is True


def test_missing_master_file_sets_master_missing_flag(tmp_path):
    master = tmp_path / "resume.docx"  # never created
    replica = tmp_path / "resume_copy.docx"

    result = check(_rule(master, [replica], master_type="file"))

    assert result.master_missing is True


def test_present_master_does_not_set_master_missing_flag(master_and_replica):
    master, replica = master_and_replica

    result = check(_rule(master, [replica]))

    assert result.master_missing is False


def test_pyc_files_are_ignored(master_and_replica):
    master, replica = master_and_replica
    (master / "module.pyc").write_text("bytecode")

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    assert replica_result.files == []


def test_replica_path_is_reported_normalized(tmp_path):
    master = tmp_path / "master"
    replica = tmp_path / "sub" / ".." / "replica"
    master.mkdir()
    (tmp_path / "replica").mkdir()
    (tmp_path / "sub").mkdir()

    result = check(_rule(master, [replica]))

    [replica_result] = result.replicas
    assert replica_result.replica_path == normalize_replica_path(str(replica))
    assert ".." not in replica_result.replica_path


def test_each_replica_is_categorized_independently(tmp_path):
    master = tmp_path / "master"
    matching = tmp_path / "matching"
    empty = tmp_path / "empty"
    for path in (master, matching, empty):
        path.mkdir()
    (master / "a.txt").write_text("hello")
    (matching / "a.txt").write_text("hello")

    result = check(_rule(master, [matching, empty]))

    matching_result, empty_result = result.replicas
    assert [c.category for c in matching_result.files] == ["in_sync"]
    assert [c.category for c in empty_result.files] == ["new"]


def test_master_file_is_hashed_once_across_replicas(tmp_path, monkeypatch):
    master = tmp_path / "master"
    master.mkdir()
    (master / "a.txt").write_text("hello")
    replicas = [tmp_path / "r1", tmp_path / "r2", tmp_path / "r3"]
    for replica in replicas:
        replica.mkdir()
        (replica / "a.txt").write_text("hello")

    hashed = []
    real_hash_file = check_module.hash_file

    def counting_hash_file(path):
        hashed.append(path)
        return real_hash_file(path)

    monkeypatch.setattr(check_module, "hash_file", counting_hash_file)

    result = check(_rule(master, replicas))

    assert len(result.replicas) == 3
    assert all(r.files[0].category == "in_sync" for r in result.replicas)
    assert hashed.count(str(master / "a.txt")) == 1
