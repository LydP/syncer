import shutil
from dataclasses import replace

import pytest

from syncer.check import check, scan_master_layout
from syncer.config import (
    Config,
    ConfigClobberError,
    ConfigStore,
    Master,
    ReplicaName,
    SyncRule,
    load_config,
    normalize_replica_path,
    save_config,
)
from syncer.review import changes_by_replica, iter_leaves, tally
from syncer.session import CheckOutcome, Session
from syncer.state import State, load_state
from syncer.storage import SyncerError

EMPTY_STATE = State(version=1, hash_algo="sha256", rules={})


def _rule(master_dir, replica_dir, rule_id="r1"):
    return SyncRule(
        id=rule_id,
        name=f"Rule {rule_id}",
        masters=[Master(path=str(master_dir), type="dir")],
        replicas=[str(replica_dir)],
    )


def _session(layout, *rules, state=EMPTY_STATE):
    store = ConfigStore(layout.config_path, layout.backups_dir)
    return Session(layout, store, Config(version=1, rules=list(rules)), state)


def _run(job):
    """What the Qt layer's CheckWorker does off-thread with the job it was handed."""
    return check(job.rule, baseline=job.baseline, collisions=job.collisions)


def _checked(session, rule_id="r1"):
    """Runs a real check of the rule the way the Qt layer does: pull the job,
    run it, hand the result back."""
    session.queue_check(rule_id)
    job = session.next_check()
    session.check_finished(job, _run(job), cancelled=False)


def test_a_completed_check_is_applied_and_becomes_the_rules_tree(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    session = _session(layout, _rule(master, replica))
    session.queue_check("r1")

    job = session.next_check()
    outcome = session.check_finished(job, _run(job), cancelled=False)

    assert outcome == CheckOutcome(rule_id="r1", applied=True)
    assert session.tally("r1") == {"safe": 1, "delete": 0, "conflict": 0, "context": 0}
    assert session.next_check() is None


def test_a_cancelled_result_is_discarded_even_for_a_still_valid_rule(master_and_replica, layout):
    """A cancelled check is truncated, so showing it would under-report drift."""
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    session = _session(layout, _rule(master, replica))
    session.queue_check("r1")

    job = session.next_check()
    outcome = session.check_finished(job, _run(job), cancelled=True)

    assert outcome == CheckOutcome(rule_id="r1", applied=False)
    assert session.tree("r1") is None


def test_a_result_for_a_rule_edited_mid_check_is_discarded_and_the_rule_requeued(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    session.queue_check("r1")
    job = session.next_check()
    result = _run(job)
    edited = replace(rule, ignore=["*.tmp"])

    session.adopt_config(Config(version=1, rules=[edited]))
    outcome = session.check_finished(job, result, cancelled=False)

    assert outcome == CheckOutcome(rule_id="r1", applied=False)
    assert session.has_pending_checks()
    assert session.tree("r1") is None
    assert session.next_check().rule == edited


def test_a_result_for_a_rule_deleted_mid_check_is_discarded_and_not_requeued(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    session = _session(layout, _rule(master, replica))
    session.queue_check("r1")
    job = session.next_check()
    result = _run(job)

    session.adopt_config(Config(version=1, rules=[]))
    outcome = session.check_finished(job, result, cancelled=False)

    assert outcome == CheckOutcome(rule_id="r1", applied=False)
    assert session.next_check() is None


def test_a_result_whose_collisions_changed_because_another_rule_was_edited_is_requeued(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    other_master = tmp_path / "elsewhere" / "master"
    other_master.mkdir(parents=True)
    other = _rule(tmp_path / "unrelated-master", replica, rule_id="r2")
    session = _session(layout, rule, other)
    session.queue_check("r1")
    job = session.next_check()
    result = _run(job)

    # r2 now lands a "master" namespace in the replica r1 already fills.
    colliding = _rule(other_master, replica, rule_id="r2")
    session.adopt_config(Config(version=1, rules=[rule, colliding]))
    outcome = session.check_finished(job, result, cancelled=False)

    assert outcome == CheckOutcome(rule_id="r1", applied=False)
    assert session.has_pending_checks()
    assert session.tree("r1") is None


def test_queueing_an_already_queued_rule_does_not_enqueue_it_twice(master_and_replica, layout):
    master, replica = master_and_replica
    session = _session(layout, _rule(master, replica))

    session.queue_check("r1")
    session.queue_check("r1")

    assert session.next_check().rule_id == "r1"
    assert session.next_check() is None


def test_cancelling_checks_clears_the_whole_queue_not_just_the_rule_in_flight(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")
    third = _rule(tmp_path / "third-master", tmp_path / "third-replica", rule_id="r3")
    session = _session(layout, _rule(master, replica), other, third)
    for rule_id in ("r1", "r2", "r3"):
        session.queue_check(rule_id)
    in_flight = session.next_check()

    session.cancel_checks()

    assert in_flight.rule_id == "r1"
    assert not session.has_pending_checks()
    assert session.next_check() is None


def test_queueing_every_check_drains_in_rule_order(master_and_replica, tmp_path, layout):
    master, replica = master_and_replica
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")
    third = _rule(tmp_path / "third-master", tmp_path / "third-replica", rule_id="r3")
    session = _session(layout, _rule(master, replica), other, third)
    session.queue_check("r3")  # already queued: a check-all restarts from rule order

    session.queue_all_checks()

    drained = []
    while (job := session.next_check()) is not None:
        drained.append(job.rule_id)
    assert drained == ["r1", "r2", "r3"]


def test_adopting_a_config_drops_deleted_rules_from_the_queue(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    rule = _rule(master, replica)
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")
    session = _session(layout, rule, other)
    session.queue_all_checks()

    session.adopt_config(Config(version=1, rules=[rule]))

    assert session.next_check().rule_id == "r1"
    assert session.next_check() is None


def test_a_rule_has_no_tree_until_it_is_checked(master_and_replica, layout):
    master, replica = master_and_replica
    session = _session(layout, _rule(master, replica))

    assert session.tree("r1") is None


def test_a_recorded_check_becomes_the_rules_tree_and_tally(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    (master / "b.txt").write_text("b")
    session = _session(layout, _rule(master, replica))

    _checked(session)

    assert session.tree("r1").rule_id == "r1"
    assert session.tally("r1") == {"safe": 2, "delete": 0, "conflict": 0, "context": 0}


def _leaf(session, name, rule_id="r1"):
    return next(leaf for leaf in iter_leaves(session.tree(rule_id).replicas) if leaf.name == name)


def test_ticking_a_leaf_toggles_its_state(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    leaf = _leaf(session, "a.txt")

    session.toggle("r1", leaf)
    assert session.node_state("r1", leaf) == "checked"
    assert session.selected_count("r1") == 1

    session.toggle("r1", leaf)
    assert session.node_state("r1", leaf) == "unchecked"
    assert session.selected_count("r1") == 0


def test_sync_selected_applies_only_the_ticked_files_and_clears_the_selection(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    (master / "b.txt").write_text("b")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    session.toggle("r1", _leaf(session, "a.txt"))

    result = session.sync_selected("r1")

    assert result.copied == 1
    assert (replica / "master" / "a.txt").read_text() == "a"
    assert not (replica / "master" / "b.txt").exists()
    assert session.selected_count("r1") == 0
    assert session.state.rules["r1"]  # the sync's baseline update is now the session's state


def test_editing_a_rule_clears_its_tick_and_review(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    _checked(session)
    session.toggle("r1", _leaf(session, "a.txt"))

    session.adopt_config(Config(version=1, rules=[replace(rule, ignore=["*.tmp"])]))

    assert session.tree("r1") is None
    assert session.selected_count("r1") == 0


def test_an_unchanged_rule_keeps_its_tick_and_review_when_another_rule_is_added(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    _checked(session)
    leaf = _leaf(session, "a.txt")
    session.toggle("r1", leaf)
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")

    adoption = session.adopt_config(Config(version=1, rules=[rule, other]))

    assert adoption.unchanged == {"r1"}
    assert adoption.replica_names_changed is False
    assert session.tree("r1") is not None
    assert session.node_state("r1", leaf) == "checked"
    assert list(session.rules) == ["r1", "r2"]


def test_renaming_a_replica_is_reported_without_invalidating_the_review(
    master_and_replica, layout
):
    """The rename only relabels: the rule itself is untouched, so its review
    and ticks survive, but the caller still has to redraw the labels."""
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    _checked(session)
    named = Config(
        version=1,
        rules=[rule],
        replica_names=[ReplicaName(path=str(replica), name="Project A")],
    )

    adoption = session.adopt_config(named)

    assert adoption.replica_names_changed is True
    assert adoption.unchanged == {"r1"}
    assert session.tree("r1") is not None
    # Asked after the fact it would be False: the old config is gone.
    assert session.adopt_config(named).replica_names_changed is False


def test_a_tick_on_a_file_that_has_since_become_a_conflict_never_reaches_sync_selected(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("from master")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    session.toggle("r1", _leaf(session, "a.txt"))
    (replica / "master" / "a.txt").write_text("edited in the replica")
    _checked(session)  # a.txt is now a no-baseline conflict, not a safe copy

    assert session.selected_count("r1") == 0
    assert session.sync_selected("r1") is None
    assert (replica / "master" / "a.txt").read_text() == "edited in the replica"


def test_sync_all_safe_ignores_the_tick_selection(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    (master / "b.txt").write_text("b")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    session.toggle("r1", _leaf(session, "a.txt"))

    result = session.sync_all_safe("r1")

    assert result.copied == 2
    assert (replica / "master" / "b.txt").read_text() == "b"


def test_pending_deletions_reports_only_the_deletions_in_a_mixed_selection(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "gone.txt").write_text("gone")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    session.sync_all_safe("r1")  # gone.txt is now baselined in the replica
    (master / "gone.txt").unlink()
    (master / "fresh.txt").write_text("fresh")
    _checked(session)
    session.toggle("r1", _leaf(session, "gone.txt"))
    session.toggle("r1", _leaf(session, "fresh.txt"))

    deletions = session.pending_deletions("r1")

    assert {key: [c.rel_path for c in changes] for key, changes in deletions.items()} == {
        normalize_replica_path(str(replica)): ["master/gone.txt"]
    }


def _multi_master_session(layout, tmp_path, *names):
    """A rule with one dir master per name, each holding `<name>.txt`, all synced
    to one replica — so every master's file has a baseline."""
    masters = []
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / f"{name}.txt").write_text(name)
        masters.append(Master(path=str(tmp_path / name), type="dir"))
    (tmp_path / "replica").mkdir()
    rule = SyncRule(id="r1", name="Rule r1", masters=masters, replicas=[str(tmp_path / "replica")])
    session = _session(layout, rule)
    _checked(session)
    session.sync_all_safe("r1")
    return session


def test_a_missing_master_is_blocked_until_unlocked_and_contributes_nothing_while_blocked(
    tmp_path, layout
):
    session = _multi_master_session(layout, tmp_path, "a", "b")
    (tmp_path / "replica" / "b" / "b.txt").write_text("edited in the replica")
    shutil.rmtree(tmp_path / "b")
    _checked(session)

    assert session.blocked_count("r1") == 1
    assert session.tally("r1") == {"safe": 0, "delete": 0, "conflict": 0, "context": 1}
    assert session.conflict_queue("r1") == []
    assert {leaf.name for leaf in iter_leaves(session.tree("r1").replicas)} == {"a.txt"}

    session.unlock("r1", "b")

    assert session.blocked_count("r1") == 0
    assert session.tally("r1")["conflict"] == 1
    assert [leaf.name for leaf in session.conflict_queue("r1")] == ["b.txt"]


def test_unlock_reveals_exactly_one_namespace_not_the_whole_rule(tmp_path, layout):
    session = _multi_master_session(layout, tmp_path, "a", "b", "c")
    shutil.rmtree(tmp_path / "b")
    shutil.rmtree(tmp_path / "c")
    _checked(session)
    assert session.blocked_count("r1") == 2
    assert session.tally("r1")["delete"] == 0

    session.unlock("r1", "b")

    assert session.blocked_count("r1") == 1
    assert session.tally("r1")["delete"] == 1  # b's file only; c is still blocked


def test_a_recheck_reblocks_an_unlocked_master_that_is_still_missing(tmp_path, layout):
    session = _multi_master_session(layout, tmp_path, "a", "b")
    shutil.rmtree(tmp_path / "b")
    _checked(session)
    session.unlock("r1", "b")

    _checked(session)

    assert session.blocked_count("r1") == 1


def _conflicted_session(layout, master, replica, *names):
    for name in names or ("a.txt",):
        (master / name).write_text(f"{name} from master")
        (replica / "master" / name).write_text(f"{name} edited in the replica")
    session = _session(layout, _rule(master, replica))
    _checked(session)
    return session


def test_keep_records_the_replicas_version_so_the_next_check_stops_flagging_it(
    master_and_replica, layout
):
    master, replica = master_and_replica
    session = _conflicted_session(layout, master, replica)
    assert session.tally("r1")["conflict"] == 1

    session.keep("r1", changes_by_replica(session.conflict_queue("r1")))
    _checked(session)

    assert session.tally("r1")["conflict"] == 0
    assert layout.state_path.exists()
    assert (replica / "master" / "a.txt").read_text() == "a.txt edited in the replica"


def test_keep_on_an_unreadable_file_merges_and_saves_nothing(master_and_replica, layout):
    master, replica = master_and_replica
    session = _conflicted_session(layout, master, replica)
    changes = changes_by_replica(session.conflict_queue("r1"))
    (replica / "master" / "a.txt").unlink()
    before = session.state

    with pytest.raises(OSError):
        session.keep("r1", changes)

    assert session.state == before
    assert not layout.state_path.exists()


def test_overwrite_replaces_the_replicas_version_with_the_masters(master_and_replica, layout):
    master, replica = master_and_replica
    session = _conflicted_session(layout, master, replica)

    result = session.overwrite("r1", changes_by_replica(session.conflict_queue("r1")))

    assert result.copied == 1
    assert (replica / "master" / "a.txt").read_text() == "a.txt from master"
    assert session.state == result.state


def test_conflict_queue_and_bulk_candidates_can_be_narrowed_to_one_replica(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    other = tmp_path / "other"
    (other / "master").mkdir(parents=True)
    (master / "a.txt").write_text("from master")
    for root in (replica, other):
        (root / "master" / "a.txt").write_text(f"edited in {root.name}")
    rule = replace(_rule(master, replica), replicas=[str(replica), str(other)])
    session = _session(layout, rule)
    _checked(session)
    replica_key = normalize_replica_path(str(replica))

    assert len(session.conflict_queue("r1")) == 2
    assert [leaf.replica_path for leaf in session.conflict_queue("r1", replica_key)] == [
        replica_key
    ]
    candidates = session.bulk_candidates("r1", replica_key)
    assert {category: len(changes) for category, changes in candidates.items()} == {
        "no_baseline": 1
    }


def test_a_preview_shows_what_each_replica_should_hold_with_nothing_actionable(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)

    preview = session.preview(rule, scan_master_layout(rule.masters))

    assert [leaf.name for leaf in iter_leaves(preview.replicas)] == ["a.txt"]
    # Every previewed file is a not-checked context row: nothing to tick or sync.
    assert tally(preview.replicas) == {"safe": 0, "delete": 0, "conflict": 0, "context": 1}


def test_saving_a_config_writes_it_to_disk_and_adopts_it(master_and_replica, tmp_path, layout):
    master, replica = master_and_replica
    rule = _rule(master, replica)
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")
    session = _session(layout, rule)

    adoption = session.save_config(Config(version=1, rules=[rule, other]))

    assert list(session.rules) == ["r1", "r2"]
    assert load_config(layout.config_path).rules == [rule, other]
    assert adoption.unchanged == {"r1"}


def test_a_save_over_an_external_edit_raises_and_leaves_the_session_untouched(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    _checked(session)
    session.sync_all_safe("r1")
    # A hand-edit the session's store never loaded.
    save_config(layout.config_path, Config(version=1, rules=[rule]), layout.backups_dir)

    with pytest.raises(ConfigClobberError):
        session.save_config(Config(version=1, rules=[]))

    assert list(session.rules) == ["r1"]
    assert session.tree("r1") is not None
    # Adopting first would have purged r1's baseline for a rule still on disk.
    assert "r1" in session.state.rules
    assert "r1" in load_state(layout.state_path).state.rules


def test_reloading_adopts_a_config_edited_on_disk(master_and_replica, tmp_path, layout):
    master, replica = master_and_replica
    rule = _rule(master, replica)
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")
    session = _session(layout, rule)
    save_config(layout.config_path, Config(version=1, rules=[rule, other]), layout.backups_dir)

    adoption = session.reload_config()

    assert list(session.rules) == ["r1", "r2"]
    assert adoption.unchanged == {"r1"}


def test_a_reload_of_an_unreadable_config_raises_and_leaves_the_session_untouched(
    master_and_replica, layout
):
    master, replica = master_and_replica
    session = _session(layout, _rule(master, replica))
    layout.config_path.write_text("this is [not toml", encoding="utf-8")

    with pytest.raises(SyncerError):
        session.reload_config()

    assert list(session.rules) == ["r1"]


def test_adopting_a_config_without_a_rule_purges_its_state_and_saves_at_once(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule)
    _checked(session)
    session.sync_all_safe("r1")
    assert "r1" in load_state(layout.state_path).state.rules

    session.adopt_config(Config(version=1, rules=[]))

    assert "r1" not in session.state.rules
    assert "r1" not in load_state(layout.state_path).state.rules


def test_an_unchanged_rule_keeps_its_unlock_when_another_rule_is_added(tmp_path, layout):
    session = _multi_master_session(layout, tmp_path, "a", "b")
    shutil.rmtree(tmp_path / "b")
    _checked(session)
    session.unlock("r1", "b")
    other = _rule(tmp_path / "other-master", tmp_path / "other-replica", rule_id="r2")

    session.adopt_config(Config(version=1, rules=[session.rules["r1"], other]))

    assert session.blocked_count("r1") == 0


def test_an_unchanged_rule_loses_its_review_when_another_rules_edit_changes_its_collisions(
    master_and_replica, tmp_path, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("a")
    rule = _rule(master, replica)
    session = _session(layout, rule, _rule(tmp_path / "unrelated-master", replica, rule_id="r2"))
    _checked(session)
    other_master = tmp_path / "elsewhere" / "master"
    other_master.mkdir(parents=True)

    # r2 now lands a "master" namespace in the replica r1 already fills.
    adoption = session.adopt_config(
        Config(version=1, rules=[rule, _rule(other_master, replica, rule_id="r2")])
    )

    assert "r1" not in adoption.unchanged
    assert session.tree("r1") is None


def test_a_resolution_applied_before_the_dialog_closes_early_is_kept(master_and_replica, layout):
    master, replica = master_and_replica
    session = _conflicted_session(layout, master, replica, "a.txt", "b.txt")
    first, second = session.conflict_queue("r1")

    # The dialog resolves one file as the user steps through, then is closed.
    session.overwrite("r1", changes_by_replica([first]))
    _checked(session)

    assert (replica / "master" / first.name).read_text() == f"{first.name} from master"
    assert (replica / "master" / second.name).read_text() == f"{second.name} edited in the replica"
    assert [leaf.name for leaf in session.conflict_queue("r1")] == [second.name]
    assert load_state(layout.state_path).state == session.state
