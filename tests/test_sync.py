import os
import shutil

from syncer.check import BaselineEntry, FileChange
from syncer.config import SyncRule, normalize_replica_path
from syncer.state import ReplicaState, State, load_state
from syncer.sync import FileError, sync

EMPTY_STATE = State(version=1, hash_algo="sha256", rules={})


def _rule(master, replicas, master_type="dir"):
    return SyncRule(
        id="r1",
        name="Rule 1",
        master=str(master),
        master_type=master_type,
        replicas=[str(r) for r in replicas],
    )


def _change(rel_path, category, master_present=True, replica_present=True, baseline_present=True):
    return FileChange(
        rel_path=rel_path,
        category=category,
        master_present=master_present,
        replica_present=replica_present,
        baseline_present=baseline_present,
    )


def _applied(replica, changes):
    return {normalize_replica_path(str(replica)): changes}


def test_new_file_is_copied_to_replica(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    rule = _rule(master, [replica])
    change = _change("a.txt", "new", replica_present=False, baseline_present=False)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "a.txt").read_text() == "hello"
    assert result.copied == 1
    assert result.deleted == 0
    assert result.errors == []


def test_new_file_creates_missing_parent_directories(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "sub" / "deep").mkdir(parents=True)
    (master / "sub" / "deep" / "a.txt").write_text("hello")
    rule = _rule(master, [replica])
    change = _change("sub/deep/a.txt", "new", replica_present=False, baseline_present=False)

    sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "sub" / "deep" / "a.txt").read_text() == "hello"


def test_overwrite_uses_temp_file_then_atomic_replace_leaving_no_tmp_behind(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("new content")
    (replica / "a.txt").write_text("old content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "changed")

    sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "a.txt").read_text() == "new content"
    assert list(replica.glob("*.syncer-tmp-*")) == []


def test_overwrite_failure_cleans_up_orphaned_temp_file_and_leaves_target_untouched(
    master_and_replica, layout, monkeypatch
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("new content")
    (replica / "a.txt").write_text("old content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "changed")

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "a.txt").read_text() == "old content"
    assert list(replica.glob("*.syncer-tmp-*")) == []
    assert result.errors == [FileError("a.txt", "simulated replace failure")]


def test_master_deleted_file_is_removed_and_empty_parents_pruned_up_to_replica_root(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (replica / "sub" / "deep").mkdir(parents=True)
    (replica / "sub" / "deep" / "old.txt").write_text("gone")
    rule = _rule(master, [replica])
    change = _change("sub/deep/old.txt", "master_deleted", master_present=False)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert not (replica / "sub").exists()
    assert replica.exists()  # replica root itself is never pruned
    assert result.deleted == 1


def test_pruning_stops_when_sibling_file_remains(master_and_replica, layout):
    master, replica = master_and_replica
    (replica / "sub").mkdir()
    (replica / "sub" / "old.txt").write_text("gone")
    (replica / "sub" / "keep.txt").write_text("stays")
    rule = _rule(master, [replica])
    change = _change("sub/old.txt", "master_deleted", master_present=False)

    sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "sub").exists()
    assert (replica / "sub" / "keep.txt").exists()


def test_copies_are_applied_before_deletes_within_a_replica(master_and_replica, layout, monkeypatch):
    master, replica = master_and_replica
    (master / "new.txt").write_text("new")
    (replica / "old.txt").write_text("old")
    rule = _rule(master, [replica])
    # Deliberately ordered delete-before-copy in the input to prove the
    # executor reorders, rather than trusting caller order.
    changes = [
        _change("old.txt", "master_deleted", master_present=False),
        _change("new.txt", "new", replica_present=False, baseline_present=False),
    ]

    calls = []
    real_copy2 = shutil.copy2
    real_remove = os.remove

    def rec_copy2(src, dst, *a, **k):
        calls.append("copy")
        return real_copy2(src, dst, *a, **k)

    def rec_remove(path, *a, **k):
        calls.append("delete")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(shutil, "copy2", rec_copy2)
    monkeypatch.setattr(os, "remove", rec_remove)

    sync(rule, _applied(replica, changes), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert calls == ["copy", "delete"]


def test_partial_failure_continues_and_collects_errors(master_and_replica, layout, monkeypatch):
    master, replica = master_and_replica
    (master / "good.txt").write_text("good")
    (master / "bad.txt").write_text("bad")
    rule = _rule(master, [replica])
    changes = [
        _change("good.txt", "new", replica_present=False, baseline_present=False),
        _change("bad.txt", "new", replica_present=False, baseline_present=False),
    ]

    real_copy2 = shutil.copy2

    def flaky_copy2(src, dst, *a, **k):
        if os.path.basename(src) == "bad.txt":
            raise OSError("simulated copy failure")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copy2", flaky_copy2)

    result = sync(rule, _applied(replica, changes), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "good.txt").read_text() == "good"
    assert not (replica / "bad.txt").exists()
    assert result.copied == 1
    assert result.errors == [FileError("bad.txt", "simulated copy failure")]


def test_state_is_committed_per_replica_immediately_not_buffered_for_whole_run(
    tmp_path, master_and_replica, layout, monkeypatch
):
    master, replica_a = master_and_replica
    replica_b = tmp_path / "replica_b"
    replica_b.mkdir()
    (master / "a.txt").write_text("a")
    (master / "b.txt").write_text("b")
    rule = _rule(master, [replica_a, replica_b])

    changes_a = [_change("a.txt", "new", replica_present=False, baseline_present=False)]
    changes_b = [_change("b.txt", "new", replica_present=False, baseline_present=False)]
    applied = {
        **_applied(replica_a, changes_a),
        **_applied(replica_b, changes_b),
    }

    real_copy2 = shutil.copy2

    def failing_for_b(src, dst, *a, **k):
        if "replica_b" in str(dst):
            raise OSError("simulated failure for replica_b")
        return real_copy2(src, dst, *a, **k)

    monkeypatch.setattr(shutil, "copy2", failing_for_b)

    sync(rule, applied, EMPTY_STATE, layout.state_path, layout.logs_dir)

    on_disk = load_state(layout.state_path).state
    replica_a_key = normalize_replica_path(str(replica_a))
    assert replica_a_key in on_disk.rules["r1"]
    assert "a.txt" in on_disk.rules["r1"][replica_a_key].files


def test_master_deleted_baseline_entry_is_dropped_from_state(master_and_replica, layout):
    master, replica = master_and_replica
    (replica / "old.txt").write_text("gone")
    rule = _rule(master, [replica])
    replica_key = normalize_replica_path(str(replica))
    state = State(
        version=1,
        hash_algo="sha256",
        rules={
            "r1": {
                replica_key: ReplicaState(
                    last_sync="2026-01-01T00:00:00Z",
                    files={"old.txt": BaselineEntry(hash="h", size=4, mtime=1.0)},
                )
            }
        },
    )
    change = _change("old.txt", "master_deleted", master_present=False)

    result = sync(rule, _applied(replica, [change]), state, layout.state_path, layout.logs_dir)

    assert "old.txt" not in result.state.rules["r1"][replica_key].files


def test_log_file_records_operations_and_summary(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    rule = _rule(master, [replica])
    change = _change("a.txt", "new", replica_present=False, baseline_present=False)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert result.log_path.exists()
    text = result.log_path.read_text()
    assert "copy\tok\ta.txt" in text
    assert "summary\tcopied=1 deleted=0 errors=0" in text


def test_progress_and_cancel_are_polled_between_files(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    (master / "b.txt").write_text("b")
    rule = _rule(master, [replica])
    changes = [
        _change("a.txt", "new", replica_present=False, baseline_present=False),
        _change("b.txt", "new", replica_present=False, baseline_present=False),
    ]

    seen = []

    def progress(done, total, current_path):
        seen.append((done, total, current_path))

    def cancel():
        return len(seen) >= 2

    result = sync(
        rule,
        _applied(replica, changes),
        EMPTY_STATE,
        layout.state_path,
        layout.logs_dir,
        progress=progress,
        cancel=cancel,
    )

    # cancel() is polled right after progress announces a file (mirrors
    # check()'s convention) — a.txt is already copied by then; b.txt's
    # announcement fires but the trip skips its actual copy.
    assert seen == [(1, 2, "a.txt"), (2, 2, "b.txt")]
    assert (replica / "a.txt").exists()
    assert not (replica / "b.txt").exists()
    assert result.copied == 1


def test_overwrite_and_delete_succeed_on_read_only_replica_files(master_and_replica, layout):
    import stat

    master, replica = master_and_replica
    (master / "a.txt").write_text("new content")
    (replica / "a.txt").write_text("old content")
    (replica / "gone.txt").write_text("gone")
    os.chmod(master / "a.txt", stat.S_IREAD)  # copy2 carries this onto the temp file
    os.chmod(replica / "a.txt", stat.S_IREAD)
    os.chmod(replica / "gone.txt", stat.S_IREAD)
    rule = _rule(master, [replica])
    changes = [
        _change("a.txt", "changed"),
        _change("gone.txt", "master_deleted", master_present=False),
    ]

    try:
        result = sync(rule, _applied(replica, changes), EMPTY_STATE, layout.state_path, layout.logs_dir)
    finally:
        os.chmod(master / "a.txt", stat.S_IWRITE | stat.S_IREAD)

    assert result.errors == []
    assert (replica / "a.txt").read_text() == "new content"
    assert not (replica / "gone.txt").exists()
    assert list(replica.glob("*.syncer-tmp-*")) == []


def test_overwrite_failure_on_read_only_temp_reports_original_error_and_cleans_up(
    master_and_replica, layout, monkeypatch
):
    import stat

    master, replica = master_and_replica
    (master / "a.txt").write_text("new content")
    (replica / "a.txt").write_text("old content")
    os.chmod(master / "a.txt", stat.S_IREAD)
    rule = _rule(master, [replica])

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    try:
        result = sync(
            rule, _applied(replica, [_change("a.txt", "changed")]), EMPTY_STATE,
            layout.state_path, layout.logs_dir,
        )
    finally:
        os.chmod(master / "a.txt", stat.S_IWRITE | stat.S_IREAD)

    assert result.errors == [FileError("a.txt", "simulated replace failure")]
    assert list(replica.glob("*.syncer-tmp-*")) == []


def test_both_changed_with_master_absent_is_applied_as_a_deletion(master_and_replica, layout):
    master, replica = master_and_replica
    (replica / "x.txt").write_text("locally edited")
    rule = _rule(master, [replica])
    change = _change("x.txt", "both_changed", master_present=False)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert result.errors == []
    assert not (replica / "x.txt").exists()
    assert result.deleted == 1


def test_state_save_failure_is_collected_and_log_still_written(
    master_and_replica, layout, monkeypatch
):
    import syncer.sync as sync_module

    master, replica = master_and_replica
    (master / "a.txt").write_text("hello")
    rule = _rule(master, [replica])
    change = _change("a.txt", "new", replica_present=False, baseline_present=False)

    def failing_save(path, state):
        raise PermissionError("state.json is locked")

    monkeypatch.setattr(sync_module, "save_state", failing_save)

    result = sync(rule, _applied(replica, [change]), EMPTY_STATE, layout.state_path, layout.logs_dir)

    assert (replica / "a.txt").read_text() == "hello"
    assert result.errors == [FileError(str(layout.state_path), "state.json is locked")]
    assert "copy\tok\ta.txt" in result.log_path.read_text()
