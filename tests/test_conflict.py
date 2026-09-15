import time

from syncer.check import BaselineEntry, CheckResult, FileChange, ReplicaCheckResult, hash_file
from syncer.config import SyncRule, normalize_replica_path
from syncer.conflict import (
    MAX_DIFF_BYTES,
    apply_keep_replica,
    build_conflict_view,
    bulk_candidates_by_category,
    conflict_queue,
)
from syncer.review import BULK_CATEGORIES, build_review_rule
from syncer.state import ReplicaState, State, load_state

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


def _replica_result(replica_path, categories):
    return ReplicaCheckResult(
        replica_path=replica_path,
        replica_exists=True,
        has_baseline=True,
        files=[_change(rel_path, category) for rel_path, category in categories],
    )


def _review_rule(*replicas):
    """`*replicas` are `(replica_path, [(rel_path, category), ...])` pairs."""
    return build_review_rule(
        CheckResult(
            rule_id="r1",
            rule_name="Rule 1",
            master_type="dir",
            master_missing=False,
            replicas=[_replica_result(path, cats) for path, cats in replicas],
        )
    )


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
    (replica / "a.txt").write_text("line1\nCHANGED\n")
    rule = _rule(master, [replica])
    change = _change("a.txt", "diverged")

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.title == "Replica vs. baseline"
    assert panel.ops is not None
    tags = [op.tag for op in panel.ops]
    assert "replace" in tags or "delete" in tags or "insert" in tags


def test_both_changed_falls_back_to_metadata_only_baseline_panels(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "both_changed")

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


def test_both_changed_with_master_deleted_shows_master_absent(master_and_replica):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "both_changed", master_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [master_panel, _] = view.panels
    assert master_panel.right_meta.exists is False


def test_no_baseline_diffs_master_directly_against_replica_with_callout(master_and_replica):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master line\n")
    (replica / "a.txt").write_text("replica line\n")
    rule = _rule(master, [replica])
    change = _change("a.txt", "no_baseline", baseline_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.title == "Master vs. replica"
    assert panel.ops is not None
    assert view.callout is not None
    assert "no record of a previous sync" in view.callout.lower()


# -- diff fidelity fallback ---------------------------------------------------


def test_diverged_with_binary_replica_falls_back_to_metadata_only(master_and_replica):
    master, replica = master_and_replica
    (master / "a.bin").write_bytes(b"binary master content")
    (replica / "a.bin").write_bytes(b"\x00\x01\x02binary replica\xffcontent")
    rule = _rule(master, [replica])
    change = _change("a.bin", "diverged")

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    assert panel.ops is None
    assert panel.unavailable_reason == "Binary file — no content diff available."
    assert panel.left_meta.exists
    assert panel.right_meta.exists


def test_no_baseline_with_oversized_text_falls_back_to_metadata_only(master_and_replica, monkeypatch):
    master, replica = master_and_replica
    (master / "a.txt").write_text("x" * 100)
    (replica / "a.txt").write_text("y" * 100)
    rule = _rule(master, [replica])
    change = _change("a.txt", "no_baseline", baseline_present=False)

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
    (replica / "a.md").write_text(replica_text)
    rule = _rule(master, [replica])
    change = _change("a.md", "diverged")

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
    (replica / "a.csv").write_text("\n".join(lines) + "\n")
    rule = _rule(master, [replica])
    change = _change("a.csv", "no_baseline", baseline_present=False)

    view = build_conflict_view(rule, str(replica), change)

    [panel] = view.panels
    changed = [op for op in panel.ops if op.tag != "equal"]
    assert [(len(op.left), len(op.right)) for op in changed] == [(1, 1)]


# -- queue and bulk selection --------------------------------------------------


def test_conflict_queue_holds_only_conflict_leaves_in_rel_path_order():
    rule = _review_rule(
        ("rep1", [("z.txt", "diverged"), ("a.txt", "both_changed"), ("m.txt", "changed")])
    )

    queue = conflict_queue(rule.replicas)

    assert [leaf.rel_path for leaf in queue] == ["a.txt", "z.txt"]


def test_conflict_queue_spans_every_replica_passed_in():
    rule = _review_rule(("rep1", [("a.txt", "diverged")]), ("rep2", [("b.txt", "no_baseline")]))

    queue = conflict_queue(rule.replicas)

    assert [leaf.rel_path for leaf in queue] == ["a.txt", "b.txt"]


def test_bulk_candidates_narrows_to_the_requested_category():
    rule = _review_rule(
        ("rep1", [("a.txt", "diverged"), ("b.txt", "diverged"), ("c.txt", "both_changed")])
    )

    by_category = bulk_candidates_by_category(rule.replicas)

    assert sorted(c.rel_path for c in by_category["diverged"]) == ["a.txt", "b.txt"]
    assert [c.rel_path for c in by_category["both_changed"]] == ["c.txt"]


def test_no_baseline_is_never_a_bulk_category():
    assert "no_baseline" not in BULK_CATEGORIES


# -- keep replica's version ----------------------------------------------------


def test_keep_replica_version_sets_kept_flag_and_baseline_to_replicas_content(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "diverged")

    new_state = apply_keep_replica(rule, str(replica), [change], EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["a.txt"]
    assert entry.kept is True
    assert entry.hash == hash_file(str(replica / "a.txt"))

    reloaded = load_state(layout.state_path).state
    assert reloaded.rules["r1"][normalize_replica_path(str(replica))].files["a.txt"].kept is True


def test_keep_replica_version_leaves_other_files_and_replicas_untouched(master_and_replica, layout):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("replica content")
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
    change = _change("a.txt", "diverged")

    new_state = apply_keep_replica(rule, str(replica), [change], starting_state, layout.state_path)

    assert new_state.rules["r1"][replica_key].files["b.txt"] == untouched_entry
    assert new_state.rules["r1"][other_replica_key].files["c.txt"] == untouched_entry


def test_keep_replica_version_records_masters_hash_at_keep_time(master_and_replica, layout):
    master, replica = master_and_replica
    (master / "a.txt").write_text("master content")
    (replica / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "diverged")

    new_state = apply_keep_replica(rule, str(replica), [change], EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["a.txt"]
    assert entry.kept_master_hash == hash_file(str(master / "a.txt"))


def test_keep_replica_version_with_master_absent_records_none_master_hash(
    master_and_replica, layout
):
    master, replica = master_and_replica
    (replica / "a.txt").write_text("replica content")
    rule = _rule(master, [replica])
    change = _change("a.txt", "both_changed", master_present=False)

    new_state = apply_keep_replica(rule, str(replica), [change], EMPTY_STATE, layout.state_path)

    entry = new_state.rules["r1"][normalize_replica_path(str(replica))].files["a.txt"]
    assert entry.kept_master_hash is None
