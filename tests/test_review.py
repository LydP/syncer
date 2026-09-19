import pytest

from syncer.check import CheckResult, FileChange, MasterStatus, NamespaceCollision, ReplicaCheckResult
from syncer.config import Master
from syncer.review import (
    CATEGORY_BUCKET,
    CATEGORY_LABEL,
    LeafKey,
    ReviewFolder,
    ReviewMaster,
    blocked_master_count,
    build_review_rule,
    is_master_blocked,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    tally,
    toggle,
    visible_replica,
)

REP = "c:\\rep"

DIR_MASTER = Master(path="c:\\src\\skills", type="dir")
FILE_MASTER = Master(path="c:\\src\\resume.docx", type="file")


def _change(rel_path, category):
    return FileChange(
        rel_path=rel_path,
        category=category,
        master_present=True,
        replica_present=True,
        baseline_present=True,
    )


def _replica_result(replica_path, files):
    return ReplicaCheckResult(
        replica_path=replica_path, replica_exists=True, has_baseline=True, files=files
    )


def _rule_for(replicas, masters=(MasterStatus(master=DIR_MASTER, missing=False),), collisions=()):
    return build_review_rule(
        CheckResult(
            rule_id="r1",
            rule_name="Rule 1",
            masters=list(masters),
            replicas=replicas,
        ),
        collisions=collisions,
    )


def _rule(*changes, missing=False, collisions=()):
    """A one-replica (`REP`) rule over one dir master `skills`, with
    `(rel_path, category)` pairs already namespaced under it."""
    return _rule_for(
        [_replica_result(REP, [_change(*c) for c in changes])],
        masters=(MasterStatus(master=DIR_MASTER, missing=missing),),
        collisions=collisions,
    )


def _keys(*rel_paths, replica=REP):
    return frozenset(LeafKey(replica, p) for p in rel_paths)


def _master_node(rule):
    [rep] = rule.replicas
    [master] = rep.children
    return master


def test_file_missing_from_replica_is_namespaced_under_its_master_and_checkable():
    rule = _rule(("skills/a.txt", "new"))

    master = _master_node(rule)
    assert isinstance(master, ReviewMaster)
    assert master.landing_path == "skills"
    [leaf] = master.children
    assert leaf.rel_path == "skills/a.txt"
    assert leaf.category == "new"
    assert leaf.bucket == "safe"


def test_file_under_a_subfolder_is_nested_under_a_folder_node_below_the_master():
    rule = _rule(("skills/sub/a.txt", "new"))

    master = _master_node(rule)
    [folder] = master.children
    assert isinstance(folder, ReviewFolder)
    assert folder.name == "sub"
    [leaf] = folder.children
    assert leaf.rel_path == "skills/sub/a.txt"
    assert leaf.name == "a.txt"


def test_folder_paths_differing_only_by_case_share_one_folder_node():
    rule = _rule(("skills/Docs/a.txt", "new"), ("skills/docs/notes.txt", "replica_only"))

    master = _master_node(rule)
    [folder] = master.children
    assert isinstance(folder, ReviewFolder)
    assert {leaf.rel_path for leaf in folder.children} == {
        "skills/Docs/a.txt",
        "skills/docs/notes.txt",
    }


def test_a_file_type_master_lands_its_one_file_directly_under_the_master_node():
    rule = _rule_for(
        [_replica_result(REP, [_change("resume.docx", "changed")])],
        masters=(MasterStatus(master=FILE_MASTER, missing=False),),
    )

    master = _master_node(rule)
    assert master.landing_path == "resume.docx"
    [leaf] = master.children
    assert leaf.rel_path == "resume.docx"


def test_each_master_gets_its_own_top_level_branch_under_the_replica():
    rep = _replica_result(REP, [_change("skills/a.txt", "new"), _change("resume.docx", "changed")])
    rule = _rule_for(
        [rep],
        masters=(
            MasterStatus(master=DIR_MASTER, missing=False),
            MasterStatus(master=FILE_MASTER, missing=False),
        ),
    )

    [replica] = rule.replicas
    assert [m.landing_path for m in replica.children] == ["skills", "resume.docx"]
    [dir_master, file_master] = replica.children
    assert [leaf.rel_path for leaf in dir_master.children] == ["skills/a.txt"]
    assert [leaf.rel_path for leaf in file_master.children] == ["resume.docx"]


@pytest.mark.parametrize(
    "category, expected_bucket",
    [
        ("new", "safe"),
        ("changed", "safe"),
        ("master_deleted", "delete"),
        ("diverged", "conflict"),
        ("both_changed", "conflict"),
        ("no_baseline", "conflict"),
        ("in_sync", "context"),
        ("replica_only", "context"),
        ("unreadable", "context"),
        ("kept", "context"),
    ],
)
def test_each_category_maps_to_its_spec_action_bucket(category, expected_bucket):
    rule = _rule(("skills/a.txt", category))

    [leaf] = _master_node(rule).children
    assert leaf.bucket == expected_bucket


def test_every_bucketed_category_has_a_display_label():
    assert CATEGORY_LABEL.keys() == CATEGORY_BUCKET.keys()


def test_no_baseline_leaf_is_labelled_as_differing_with_no_sync_history():
    rule = _rule(("skills/a.txt", "no_baseline"))

    [leaf] = _master_node(rule).children

    assert leaf.label == "Differs from master (no sync history)"


def test_tally_counts_leaves_by_bucket_including_those_nested_in_folders_and_masters():
    rule = _rule(
        ("skills/a.txt", "new"),
        ("skills/sub/b.txt", "master_deleted"),
        ("skills/sub/c.txt", "diverged"),
        ("skills/d.txt", "in_sync"),
    )

    counts = tally(rule.replicas)

    assert counts == {"safe": 1, "delete": 1, "conflict": 1, "context": 1}


def test_leaf_state_is_checked_only_when_its_key_is_in_the_selection():
    rule = _rule(("skills/a.txt", "new"))
    [leaf] = _master_node(rule).children

    assert node_state(leaf, frozenset()) == "unchecked"
    assert node_state(leaf, _keys("skills/a.txt")) == "checked"


def test_folder_state_is_partial_when_some_but_not_all_checkable_leaves_are_selected():
    rule = _rule(("skills/sub/a.txt", "new"), ("skills/sub/b.txt", "changed"))
    [folder] = _master_node(rule).children

    assert node_state(folder, frozenset()) == "unchecked"
    assert node_state(folder, _keys("skills/sub/a.txt")) == "partial"
    assert node_state(folder, _keys("skills/sub/a.txt", "skills/sub/b.txt")) == "checked"


def test_folder_state_ignores_conflict_and_context_leaves():
    rule = _rule(("skills/sub/a.txt", "diverged"), ("skills/sub/b.txt", "in_sync"))
    [folder] = _master_node(rule).children

    assert node_state(folder, frozenset()) == "unchecked"


def test_master_node_itself_can_be_toggled_like_a_folder():
    rule = _rule(("skills/a.txt", "new"), ("skills/b.txt", "changed"))
    master = _master_node(rule)

    selected = toggle(master, frozenset())

    assert selected == _keys("skills/a.txt", "skills/b.txt")


def test_toggling_an_unchecked_folder_ticks_every_checkable_descendant():
    rule = _rule(
        ("skills/sub/a.txt", "new"),
        ("skills/sub/b.txt", "changed"),
        ("skills/sub/c.txt", "diverged"),  # conflict: never ticked
    )
    [folder] = _master_node(rule).children

    selected = toggle(folder, frozenset())

    assert selected == _keys("skills/sub/a.txt", "skills/sub/b.txt")


@pytest.mark.parametrize(
    "before, after",
    [
        pytest.param(("skills/sub/a.txt", "skills/sub/b.txt"), (), id="fully-checked-clears"),
        pytest.param(
            ("skills/sub/a.txt",), ("skills/sub/a.txt", "skills/sub/b.txt"), id="partial-ticks-rest"
        ),
    ],
)
def test_toggling_a_checked_or_partial_folder(before, after):
    rule = _rule(("skills/sub/a.txt", "new"), ("skills/sub/b.txt", "changed"))
    [folder] = _master_node(rule).children

    assert toggle(folder, _keys(*before)) == _keys(*after)


def test_resolve_selection_groups_ticked_file_changes_by_replica_for_the_executor():
    rep1 = _replica_result("c:\\rep1", [_change("skills/a.txt", "new"), _change("skills/b.txt", "changed")])
    rep2 = _replica_result("c:\\rep2", [_change("skills/a.txt", "new")])
    rule = _rule_for([rep1, rep2])
    selected = _keys("skills/a.txt", replica="c:\\rep1") | _keys("skills/a.txt", replica="c:\\rep2")

    applied = resolve_selection(rule, selected)

    assert set(applied.keys()) == {"c:\\rep1", "c:\\rep2"}
    [change1] = applied["c:\\rep1"]
    assert change1.rel_path == "skills/a.txt"
    assert change1.category == "new"
    [change2] = applied["c:\\rep2"]
    assert change2.rel_path == "skills/a.txt"


def test_sync_all_safe_changes_ignores_ticks_and_takes_every_safe_leaf():
    rule = _rule(
        ("skills/a.txt", "new"),
        ("skills/b.txt", "changed"),
        ("skills/c.txt", "master_deleted"),
        ("skills/d.txt", "diverged"),
    )

    applied = sync_all_safe_changes(rule)

    categories = {c.rel_path: c.category for c in applied[REP]}
    assert categories == {"skills/a.txt": "new", "skills/b.txt": "changed"}


def test_selection_by_bucket_narrows_the_ticked_selection_to_master_deleted_files_only():
    rule = _rule(("skills/a.txt", "new"), ("skills/b.txt", "master_deleted"))

    deletes = selection_by_bucket(rule, _keys("skills/a.txt", "skills/b.txt"), "delete")

    [change] = deletes[REP]
    assert change.rel_path == "skills/b.txt"
    assert change.category == "master_deleted"


def test_rule_tally_aggregates_bucket_counts_across_all_the_rules_replicas():
    rep1 = _replica_result("c:\\rep1", [_change("skills/a.txt", "new")])
    rep2 = _replica_result(
        "c:\\rep2", [_change("skills/a.txt", "new"), _change("skills/b.txt", "diverged")]
    )
    rule = _rule_for([rep1, rep2])

    counts = rule_tally(rule)

    assert counts == {"safe": 2, "delete": 0, "conflict": 1, "context": 0}


# -- per-master blocking (issue #24) ----------------------------------------


def _two_master_rule(dir_missing):
    """One replica holding a new file under dir master `skills` and a changed
    file master `resume.docx`, with only the dir master's presence varying."""
    rep = _replica_result(REP, [_change("skills/a.txt", "new"), _change("resume.docx", "changed")])
    return _rule_for(
        [rep],
        masters=(
            MasterStatus(master=DIR_MASTER, missing=dir_missing),
            MasterStatus(master=FILE_MASTER, missing=False),
        ),
    )


def test_missing_master_blocks_its_own_namespace_until_explicitly_unlocked():
    rule = _rule(("skills/a.txt", "new"), missing=True)
    master = _master_node(rule)

    assert master.unlockable is True
    assert is_master_blocked(master, unlocked=frozenset()) is True
    assert is_master_blocked(master, unlocked=frozenset({"skills"})) is False


def test_master_is_never_blocked_when_present_and_not_collided():
    rule = _rule(("skills/a.txt", "new"), missing=False)
    master = _master_node(rule)

    assert master.unlockable is False
    assert is_master_blocked(master, unlocked=frozenset()) is False


def test_a_missing_masters_files_are_still_built_so_unlocking_reveals_them():
    rule = _rule(("skills/a.txt", "new"), missing=True)
    master = _master_node(rule)

    assert master.missing is True
    [leaf] = master.children
    assert leaf.rel_path == "skills/a.txt"


def test_one_missing_master_does_not_block_a_sibling_master_in_the_same_replica():
    rule = _two_master_rule(dir_missing=True)

    [dir_master, file_master] = rule.replicas[0].children
    assert is_master_blocked(dir_master, unlocked=frozenset()) is True
    assert is_master_blocked(file_master, unlocked=frozenset()) is False


def test_visible_replica_clears_a_blocked_masters_children_and_leaves_others_alone():
    rule = _two_master_rule(dir_missing=True)

    visible = visible_replica(rule.replicas[0], unlocked=frozenset())

    [dir_master, file_master] = visible.children
    assert dir_master.children == []
    assert [leaf.rel_path for leaf in file_master.children] == ["resume.docx"]


def test_rule_tally_excludes_a_blocked_masters_leaves():
    rule = _two_master_rule(dir_missing=True)

    assert rule_tally(rule, unlocked=frozenset()) == {"safe": 1, "delete": 0, "conflict": 0, "context": 0}
    assert rule_tally(rule, unlocked=frozenset({"skills"})) == {
        "safe": 2,
        "delete": 0,
        "conflict": 0,
        "context": 0,
    }


def test_blocked_master_count_counts_blocked_namespaces_until_unlocked():
    rule = _two_master_rule(dir_missing=True)

    assert blocked_master_count(rule, unlocked=frozenset()) == 1
    assert blocked_master_count(rule, unlocked=frozenset({"skills"})) == 0


def test_sync_all_safe_changes_skips_a_blocked_masters_files():
    rule = _two_master_rule(dir_missing=True)

    applied = sync_all_safe_changes(rule, unlocked=frozenset())

    assert [c.rel_path for c in applied[REP]] == ["resume.docx"]


def test_resolve_selection_drops_a_stale_tick_whose_master_became_blocked():
    rule = _rule(("skills/a.txt", "new"), missing=True)
    selected = _keys("skills/a.txt")

    applied = resolve_selection(rule, selected, unlocked=frozenset())

    assert applied == {}


# -- cross-rule namespace collision (issue #24) ------------------------------


def _collision(replica_path=REP, rule_ids=("r1", "r2")):
    return NamespaceCollision(replica_path=replica_path, landing_path="skills", rule_ids=list(rule_ids))


def test_a_collided_masters_subtree_has_no_children_and_carries_the_collision():
    collision = _collision()
    rule = _rule(("skills/a.txt", "new"), collisions=[collision])

    master = _master_node(rule)
    assert master.collision == collision
    assert master.children == []


def test_a_collision_blocks_regardless_of_any_unlock():
    rule = _rule(("skills/a.txt", "new"), collisions=[_collision()])
    master = _master_node(rule)

    assert master.unlockable is False
    assert is_master_blocked(master, unlocked=frozenset({"skills"})) is True


def test_a_collision_naming_a_different_rule_does_not_apply():
    rule = _rule(
        ("skills/a.txt", "new"), collisions=[_collision(rule_ids=("other-rule", "yet-another"))]
    )

    master = _master_node(rule)
    assert master.collision is None
    assert [leaf.rel_path for leaf in master.children] == ["skills/a.txt"]


def test_a_collision_in_a_different_replica_does_not_apply_here():
    rule = _rule(("skills/a.txt", "new"), collisions=[_collision(replica_path="c:\\other-rep")])

    master = _master_node(rule)
    assert master.collision is None
