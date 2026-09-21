import time

import pytest

from syncer.check import BaselineEntry, FileChange, hash_file
from syncer.config import Master, SyncRule, normalize_replica_path
from syncer.conflict import (
    MAX_DIFF_BYTES,
    apply_keep_replica,
    build_conflict_view,
    bulk_candidates_by_category,
    conflict_queue,
    summarize_overwrite,
)
from syncer.review import ReviewLeaf, ReviewReplica, changes_by_replica
from syncer.state import ReplicaState, State, load_state

EMPTY_STATE = State(version=1, hash_algo="sha256", rules={})


def _rule(master, replicas):
    return SyncRule(
        id="r1",
        name="Rule 1",
        masters=[Master(path=str(master), type="dir")],
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


def _review_replicas(*replicas):
    """`*replicas` are `(replica_path, [(rel_path, category), ...])` pairs.

    Builds `ReviewReplica`s directly rather than going through
    `check.CheckResult`/`review.build_review_rule` — that conversion is
    review.py's own territory (issue #24), not conflict.py's; these tests
    only exercise conflict.py's queue/bulk-selection logic over an already
    built tree.
    """
    return [
        ReviewReplica(
            replica_path=path,
            replica_exists=True,
            # Sorted like review.py's own tree-building, since conflict_queue's
            # ordering guarantee rides on the tree already arriving this way.
            children=[
                ReviewLeaf(path, _change(rel_path, category))
                for rel_path, category in sorted(categories)
            ],
        )
        for path, categories in replicas
    ]


def _heavily_rewritten_markdown(prefix):
    """~90 KB of prose paragraphs where `prefix` ("master"/"replica") makes
    every non-blank line unique to that side, leaving blank lines as the only
    lines the two sides share — issue #10's pathological case."""
    paragraphs = [" ".join(f"{prefix}{i}-{w}" for w in range(15)) for i in range(500)]
    return "\n\n".join(paragraphs) + "\n"


# -- diff panels per category ------------------------------------------------


def test_diverged_diff_is_replica_vs_masters_current_content(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("line1\nline2\n")
    (replica / "master" / "a.txt").write_text("line1\nCHANGED\n")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "diverged")

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.title == "Replica vs. baseline"
    assert panel.ops is not None
    tags = [op.tag for op in panel.ops]
    assert "replace" in tags or "delete" in tags or "insert" in tags


def test_both_changed_falls_back_to_metadata_only_baseline_panels(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = FileChange(
        rel_path="master/a.txt",
        category="both_changed",
        master_present=True,
        replica_present=True,
        baseline_present=True,
        baseline_size=1234,
        baseline_mtime=5678.0,
    )

    view = build_conflict_view(rule, str(replica), change)

    [master_panel, replica_panel] = view.panels
    assert master_panel.title == "Master vs. baseline"
    assert master_panel.ops is None
    assert master_panel.unavailable_reason is not None
    assert master_panel.right_meta.exists
    assert master_panel.right_meta.size == len("master content")

    assert replica_panel.title == "Replica vs. baseline"
    assert replica_panel.ops is None
    assert replica_panel.unavailable_reason == master_panel.unavailable_reason
    assert replica_panel.right_meta.exists
    assert replica_panel.right_meta.size == len("replica content")

    # The baseline side shows the stat stored with the baseline entry.
    for panel in (master_panel, replica_panel):
        assert panel.left_meta.exists is True
        assert panel.left_meta.size == 1234
        assert panel.left_meta.mtime == 5678.0


def test_both_changed_with_master_deleted_shows_master_absent(master_and_replica):
    master, replica = master_and_replica
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "both_changed", master_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [master_panel, _] = view.panels
    assert master_panel.right_meta.exists is False


def test_no_baseline_diffs_master_directly_against_replica_with_callout(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master line\n")
    (replica / "master" / "a.txt").write_text("replica line\n")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "no_baseline", baseline_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.title == "Master vs. replica"
    assert panel.ops is not None
    assert view.callout is not None
    assert "no sync history" in view.callout.lower()


# -- diff fidelity fallback ---------------------------------------------------


def test_diverged_with_binary_replica_falls_back_to_metadata_only(master_and_replica):
    master, replica = master_and_replica
    (master / "a.bin").write_bytes(b"binary master content")
    (replica / "master" / "a.bin").write_bytes(b"\x00\x01\x02binary replica\xffcontent")
    rule = _rule(master, [replica])
    change = _change("master/a.bin", "diverged")

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.ops is None
    assert panel.unavailable_reason == "Binary file — no content diff available."
    assert panel.left_meta.exists
    assert panel.right_meta.exists


def test_no_baseline_with_oversized_text_falls_back_to_metadata_only(master_and_replica, monkeypatch):
    master, replica = master_and_replica
    (master / "a.txt").write_text("x" * 100)
    (replica / "master" / "a.txt").write_text("y" * 100)
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "no_baseline", baseline_present=False)

    import syncer.conflict as conflict_mod

    monkeypatch.setattr(conflict_mod, "MAX_DIFF_BYTES", 10)

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.ops is None
    assert "too large" in panel.unavailable_reason.lower()


def test_heavily_rewritten_markdown_content_diff_stays_within_a_tight_time_budget(
    master_and_replica,
):
    master, replica = master_and_replica
    master_text = _heavily_rewritten_markdown("master")
    replica_text = _heavily_rewritten_markdown("replica")
    # Big enough to be a real workload, but under the cutoff, so this
    # exercises _diff_ops rather than the metadata-only fallback.
    assert 90_000 < len(master_text.encode("utf-8")) < MAX_DIFF_BYTES
    (master / "a.md").write_text(master_text)
    (replica / "master" / "a.md").write_text(replica_text)
    rule = _rule(master, [replica])
    change = _change("master/a.md", "diverged")

    start = time.perf_counter()
    view = build_conflict_view(rule, str(replica), change)
    elapsed = time.perf_counter() - start

    [panel] = view.panels
    assert panel.ops is not None
    # 0.25 s: ~12x over the observed runtime, but ~17x under the 4.2 s the
    # un-flipped autojunk=False costs, so a slow CI box still catches it.
    assert elapsed < 0.25
    # autojunk collapses this input to a single whole-file replace; pinning
    # it keeps the fidelity trade visible rather than only timing-dependent.
    assert [op.tag for op in panel.ops] == ["replace"]


def test_single_line_edit_in_repetitive_file_diffs_as_one_changed_line(master_and_replica):
    master, replica = master_and_replica
    # Few distinct lines, so every line is "popular" to autojunk.
    lines = [f"v{i % 20}" for i in range(1000)]
    (master / "a.csv").write_text("\n".join(lines) + "\n")
    lines[500] = "edited"
    (replica / "master" / "a.csv").write_text("\n".join(lines) + "\n")
    rule = _rule(master, [replica])
    change = _change("master/a.csv", "no_baseline", baseline_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    changed = [op for op in panel.ops if op.tag != "equal"]
    assert [(len(op.left), len(op.right)) for op in changed] == [(1, 1)]


# -- queue and bulk selection --------------------------------------------------


def test_conflict_queue_holds_only_conflict_leaves_in_rel_path_order():
    replicas = _review_replicas(
        ("rep1", [("z.txt", "diverged"), ("a.txt", "both_changed"), ("m.txt", "changed")])
    )

    queue = conflict_queue(replicas)

    assert [leaf.rel_path for leaf in queue] == ["a.txt", "z.txt"]


def test_conflict_queue_spans_every_replica_passed_in():
    replicas = _review_replicas(("rep1", [("a.txt", "diverged")]), ("rep2", [("b.txt", "no_baseline")]))

    queue = conflict_queue(replicas)

    assert [leaf.rel_path for leaf in queue] == ["a.txt", "b.txt"]


def test_changes_by_replica_groups_leaves_under_their_own_replica_in_order():
    queue = conflict_queue(
        _review_replicas(
            ("rep1", [("a.txt", "diverged"), ("b.txt", "no_baseline")]),
            ("rep2", [("c.txt", "both_changed")]),
        )
    )

    grouped = changes_by_replica(queue)

    assert list(grouped) == ["rep1", "rep2"]
    assert [c.rel_path for c in grouped["rep1"]] == ["a.txt", "b.txt"]
    assert [c.rel_path for c in grouped["rep2"]] == ["c.txt"]


def test_overwrite_summary_counts_files_per_category_and_replica_files_deleted():
    changes = [
        _change("a.txt", "diverged"),
        _change("b.txt", "diverged"),
        _change("c.txt", "no_baseline", baseline_present=False),
        # Master gone and replica edited: overwriting from master deletes it.
        _change("d.txt", "both_changed", master_present=False),
        _change("e.txt", "both_changed"),
    ]

    summary = summarize_overwrite(changes)

    assert summary.by_category == {"diverged": 2, "both_changed": 2, "no_baseline": 1}
    assert summary.deletions == 1


def test_overwrite_summary_of_nothing_is_empty():
    summary = summarize_overwrite([])

    assert summary.by_category == {}
    assert summary.deletions == 0


def test_bulk_candidates_narrows_to_the_requested_category():
    replicas = _review_replicas(
        ("rep1", [("a.txt", "diverged"), ("b.txt", "diverged"), ("c.txt", "both_changed")])
    )

    by_category = bulk_candidates_by_category(replicas)

    assert sorted(c.rel_path for c in by_category["diverged"]) == ["a.txt", "b.txt"]
    assert [c.rel_path for c in by_category["both_changed"]] == ["c.txt"]


def test_bulk_candidates_include_files_with_no_sync_history():
    replicas = _review_replicas(("rep1", [("a.txt", "no_baseline"), ("b.txt", "diverged")]))

    by_category = bulk_candidates_by_category(replicas)

    assert [c.rel_path for c in by_category["no_baseline"]] == ["a.txt"]


# -- keep replica's version ----------------------------------------------------


def test_keep_replica_version_sets_kept_flag_and_baseline_to_replicas_content(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "diverged")

    new_state = apply_keep_replica(rule, {str(replica): [change]}, EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["master/a.txt"]
    assert entry.kept is True
    assert entry.hash == hash_file(str(replica / "master" / "a.txt"))

    reloaded = load_state(layout.state_path).state
    assert reloaded.rules["r1"][normalize_replica_path(str(replica))].files["master/a.txt"].kept is True


def test_keep_replica_version_works_for_a_file_with_no_sync_history(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "no_baseline", baseline_present=False)

    new_state = apply_keep_replica(rule, {str(replica): [change]}, EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["master/a.txt"]
    assert entry.kept is True
    assert entry.hash == hash_file(str(replica / "master" / "a.txt"))
    assert entry.kept_master_hash == hash_file(str(master / "a.txt"))


def test_keep_replica_version_leaves_other_files_and_replicas_untouched(master_and_replica, layout):
    master, replica = master_and_replica
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    replica_key = normalize_replica_path(str(replica))
    other_replica_key = normalize_replica_path(str(master.parent / "other"))
    untouched_entry = BaselineEntry(hash="deadbeef", size=1, mtime=1.0)
    starting_state = State(
        version=1,
        hash_algo="sha256",
        rules={
            "r1": {
                replica_key: ReplicaState(
                    last_sync="2020-01-01T00:00:00Z", files={"b.txt": untouched_entry}
                ),
                other_replica_key: ReplicaState(
                    last_sync="2020-01-01T00:00:00Z", files={"c.txt": untouched_entry}
                ),
            }
        },
    )
    change = _change("master/a.txt", "diverged")

    new_state = apply_keep_replica(rule, {str(replica): [change]}, starting_state, layout.state_path)

    assert new_state.rules["r1"][replica_key].files["b.txt"] == untouched_entry
    assert new_state.rules["r1"][other_replica_key].files["c.txt"] == untouched_entry


def test_keep_replica_version_spans_every_replica_in_one_save(master_and_replica, layout):
    master, replica = master_and_replica
    other = master.parent / "other"
    (other / "master").mkdir(parents=True)
    (master / "a.txt").write_text("master content")
    (replica / "master" / "a.txt").write_text("replica content")
    (other / "master" / "a.txt").write_text("other content")
    rule = _rule(master, [replica, other])
    change = _change("master/a.txt", "no_baseline", baseline_present=False)

    new_state = apply_keep_replica(
        rule, {str(replica): [change], str(other): [change]}, EMPTY_STATE, layout.state_path
    )

    reloaded = load_state(layout.state_path).state
    for root in (replica, other):
        entry = reloaded.rules["r1"][normalize_replica_path(str(root))].files["master/a.txt"]
        assert entry.kept is True
        assert entry.hash == hash_file(str(root / "master" / "a.txt"))
    assert reloaded == new_state


def test_keep_replica_version_saves_nothing_if_any_replica_file_is_unreadable(
    master_and_replica, layout
):
    master, replica = master_and_replica
    other = master.parent / "other"
    (other / "master").mkdir(parents=True)
    (replica / "master" / "a.txt").write_text("replica content")
    # other/master/a.txt is missing, so reading it fails.
    rule = _rule(master, [replica, other])
    change = _change("master/a.txt", "diverged")

    with pytest.raises(OSError):
        apply_keep_replica(
            rule, {str(replica): [change], str(other): [change]}, EMPTY_STATE, layout.state_path
        )

    assert not layout.state_path.exists()


def test_keep_replica_version_records_masters_hash_at_keep_time(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "diverged")

    new_state = apply_keep_replica(rule, {str(replica): [change]}, EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["master/a.txt"]
    assert entry.kept_master_hash == hash_file(str(master / "a.txt"))


def test_keep_replica_version_with_master_absent_records_none_master_hash(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (replica / "master" / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("master/a.txt", "both_changed", master_present=False)

    new_state = apply_keep_replica(rule, {str(replica): [change]}, EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["master/a.txt"]
    assert entry.kept_master_hash is None
