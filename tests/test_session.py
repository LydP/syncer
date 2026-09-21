import shutil
from dataclasses import replace

import pytest

from syncer.check import check, scan_master_layout
from syncer.config import Config, Master, ReplicaName, SyncRule, normalize_replica_path
from syncer.review import changes_by_replica, iter_leaves, tally
from syncer.session import Session
from syncer.state import State

EMPTY_STATE = State(version=1, hash_algo="sha256", rules={})


def _rule(master_dir, replica_dir, rule_id="r1"):
    return SyncRule(
        id=rule_id,
        name=f"Rule {rule_id}",
        masters=[Master(path=str(master_dir), type="dir")],
        replicas=[str(replica_dir)],
    )


def _session(layout, *rules, state=EMPTY_STATE):
    return Session(layout, Config(version=1, rules=list(rules)), state)


def _checked(session, rule_id="r1"):
    """Runs a real check of the rule and hands the result to the session, the
    way the Qt layer does after its worker finishes."""
    result = check(
        session.rules[rule_id],
        baseline=session.baseline(rule_id),
        collisions=session.collisions,
    )
    session.record_check(result)


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

    session.adopt_config(Config(version=1, rules=[replace(rule, ignore=["*.tmp"])]), EMPTY_STATE)

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

    adoption = session.adopt_config(Config(version=1, rules=[rule, other]), EMPTY_STATE)

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

    adoption = session.adopt_config(named, EMPTY_STATE)

    assert adoption.replica_names_changed is True
    assert adoption.unchanged == {"r1"}
    assert session.tree("r1") is not None
    # Asked after the fact it would be False: the old config is gone.
    assert session.adopt_config(named, EMPTY_STATE).replica_names_changed is False


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


def _conflicted_session(layout, master, replica):
    (master / "a.txt").write_text("from master")
    (replica / "master" / "a.txt").write_text("edited in the replica")
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
    assert (replica / "master" / "a.txt").read_text() == "edited in the replica"


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
    assert (replica / "master" / "a.txt").read_text() == "from master"
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


def test_state_a_dialog_resolved_against_replaces_the_sessions(master_and_replica, layout):
    master, replica = master_and_replica
    session = _session(layout, _rule(master, replica))
    resolved = State(version=1, hash_algo="sha256", rules={"r1": {}})

    session.replace_state(resolved)

    assert session.state is resolved


def test_the_session_exposes_the_storage_layout_its_state_is_saved_under(layout):
    assert _session(layout).layout is layout
