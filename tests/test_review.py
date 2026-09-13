import pytest

from syncer.check import CheckResult, FileChange, ReplicaCheckResult
from syncer.review import (
    CATEGORY_BUCKET,
    CATEGORY_LABEL,
    LeafKey,
    ReviewFolder,
    build_review_rule,
    is_sync_blocked,
    node_state,
    resolve_selection,
    rule_tally,
    selection_by_bucket,
    sync_all_safe_changes,
    tally,
    toggle,
)

REP = "c:\\rep"


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


def _rule_for(replicas, master_missing=False):
    return build_review_rule(
        CheckResult(
            rule_id="r1",
            rule_name="Rule 1",
            master_type="dir",
            master_missing=master_missing,
            replicas=replicas,
        )
    )


def _rule(*changes, master_missing=False):
    """A one-replica (`REP`) rule over `(rel_path, category)` pairs."""
    return _rule_for(
        [_replica_result(REP, [_change(*c) for c in changes])], master_missing=master_missing
    )


def _keys(*rel_paths, replica=REP):
    return frozenset(LeafKey(replica, p) for p in rel_paths)


def test_file_missing_from_replica_is_safe_drift_and_checkable():
    rule = _rule(("a.txt", "new"))

    [rep] = rule.replicas
    [leaf] = rep.children
    assert leaf.rel_path == "a.txt"
    assert leaf.category == "new"
    assert leaf.bucket == "safe"


def test_file_under_a_subfolder_is_nested_under_a_folder_node():
    rule = _rule(("sub/a.txt", "new"))

    [rep] = rule.replicas
    [folder] = rep.children
    assert isinstance(folder, ReviewFolder)
    assert folder.name == "sub"
    [leaf] = folder.children
    assert leaf.rel_path == "sub/a.txt"
    assert leaf.name == "a.txt"


def test_folder_paths_differing_only_by_case_share_one_folder_node():
    rule = _rule(("Docs/a.txt", "new"), ("docs/notes.txt", "replica_only"))

    [rep] = rule.replicas
    [folder] = rep.children
    assert isinstance(folder, ReviewFolder)
    assert {leaf.rel_path for leaf in folder.children} == {"Docs/a.txt", "docs/notes.txt"}


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
    ],
)
def test_each_category_maps_to_its_spec_action_bucket(category, expected_bucket):
    rule = _rule(("a.txt", category))

    [leaf] = rule.replicas[0].children
    assert leaf.bucket == expected_bucket


def test_every_bucketed_category_has_a_display_label():
    assert CATEGORY_LABEL.keys() == CATEGORY_BUCKET.keys()


def test_tally_counts_leaves_by_bucket_including_those_nested_in_folders():
    rule = _rule(
        ("a.txt", "new"),
        ("sub/b.txt", "master_deleted"),
        ("sub/c.txt", "diverged"),
        ("d.txt", "in_sync"),
    )

    counts = tally(rule.replicas[0].children)

    assert counts == {"safe": 1, "delete": 1, "conflict": 1, "context": 1}


def test_leaf_state_is_checked_only_when_its_key_is_in_the_selection():
    rule = _rule(("a.txt", "new"))
    [leaf] = rule.replicas[0].children

    assert node_state(leaf, frozenset()) == "unchecked"
    assert node_state(leaf, _keys("a.txt")) == "checked"


def test_folder_state_is_partial_when_some_but_not_all_checkable_leaves_are_selected():
    rule = _rule(("sub/a.txt", "new"), ("sub/b.txt", "changed"))
    [folder] = rule.replicas[0].children

    assert node_state(folder, frozenset()) == "unchecked"
    assert node_state(folder, _keys("sub/a.txt")) == "partial"
    assert node_state(folder, _keys("sub/a.txt", "sub/b.txt")) == "checked"


def test_folder_state_ignores_conflict_and_context_leaves():
    rule = _rule(("sub/a.txt", "diverged"), ("sub/b.txt", "in_sync"))
    [folder] = rule.replicas[0].children

    assert node_state(folder, frozenset()) == "unchecked"


def test_toggling_an_unchecked_folder_ticks_every_checkable_descendant():
    rule = _rule(
        ("sub/a.txt", "new"),
        ("sub/b.txt", "changed"),
        ("sub/c.txt", "diverged"),  # conflict: never ticked
    )
    [folder] = rule.replicas[0].children

    selected = toggle(folder, frozenset())

    assert selected == _keys("sub/a.txt", "sub/b.txt")


@pytest.mark.parametrize(
    "before, after",
    [
        pytest.param(("sub/a.txt", "sub/b.txt"), (), id="fully-checked-clears"),
        pytest.param(("sub/a.txt",), ("sub/a.txt", "sub/b.txt"), id="partial-ticks-rest"),
    ],
)
def test_toggling_a_checked_or_partial_folder(before, after):
    rule = _rule(("sub/a.txt", "new"), ("sub/b.txt", "changed"))
    [folder] = rule.replicas[0].children

    assert toggle(folder, _keys(*before)) == _keys(*after)


def test_resolve_selection_groups_ticked_file_changes_by_replica_for_the_executor():
    rep1 = _replica_result("c:\\rep1", [_change("a.txt", "new"), _change("b.txt", "changed")])
    rep2 = _replica_result("c:\\rep2", [_change("a.txt", "new")])
    rule = _rule_for([rep1, rep2])
    selected = _keys("a.txt", replica="c:\\rep1") | _keys("a.txt", replica="c:\\rep2")

    applied = resolve_selection(rule, selected)

    assert set(applied.keys()) == {"c:\\rep1", "c:\\rep2"}
    [change1] = applied["c:\\rep1"]
    assert change1.rel_path == "a.txt"
    assert change1.category == "new"
    [change2] = applied["c:\\rep2"]
    assert change2.rel_path == "a.txt"


def test_sync_all_safe_changes_ignores_ticks_and_takes_every_safe_leaf():
    rule = _rule(
        ("a.txt", "new"),
        ("b.txt", "changed"),
        ("c.txt", "master_deleted"),
        ("d.txt", "diverged"),
    )

    applied = sync_all_safe_changes(rule)

    categories = {c.rel_path: c.category for c in applied[REP]}
    assert categories == {"a.txt": "new", "b.txt": "changed"}


def test_selection_by_bucket_narrows_the_ticked_selection_to_master_deleted_files_only():
    rule = _rule(("a.txt", "new"), ("b.txt", "master_deleted"))

    deletes = selection_by_bucket(rule, _keys("a.txt", "b.txt"), "delete")

    [change] = deletes[REP]
    assert change.rel_path == "b.txt"
    assert change.category == "master_deleted"


def test_master_missing_blocks_sync_until_explicitly_unlocked():
    rule = _rule(master_missing=True)

    assert is_sync_blocked(rule, unlocked=False) is True
    assert is_sync_blocked(rule, unlocked=True) is False


def test_sync_is_never_blocked_when_master_is_present():
    rule = _rule(master_missing=False)

    assert is_sync_blocked(rule, unlocked=False) is False


def test_rule_tally_aggregates_bucket_counts_across_all_the_rules_replicas():
    rep1 = _replica_result("c:\\rep1", [_change("a.txt", "new")])
    rep2 = _replica_result("c:\\rep2", [_change("a.txt", "new"), _change("b.txt", "diverged")])
    rule = _rule_for([rep1, rep2])

    counts = rule_tally(rule)

    assert counts == {"safe": 2, "delete": 0, "conflict": 1, "context": 0}
