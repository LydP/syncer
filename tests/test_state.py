import json
import re
from datetime import datetime, timezone

import pytest

from syncer.check import BaselineEntry
from syncer.config import Config, Master, SyncRule
from syncer.state import (
    ReplicaState,
    State,
    baseline_for_rule,
    load_state,
    merge_replica_entries,
    reconcile_and_save,
    reconcile_with_config,
    save_state,
)

NOW = datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)
EMPTY_STATE = State(version=1, hash_algo="sha256", rules={})


def _state(rules):
    return State(version=1, hash_algo="sha256", rules=rules)


def _config(*masters):
    return Config(
        version=1,
        rules=[
            SyncRule(id="rule-1", name="kept rule", masters=list(masters), replicas=[r"C:\A\Replica"])
        ],
    )


def test_load_state_missing_file_returns_empty_state(layout):
    result = load_state(layout.state_path)

    assert result.state == EMPTY_STATE
    assert result.warning is None


def test_load_state_parses_rules_replicas_and_files(layout):
    layout.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hash_algo": "sha256",
                "rules": {
                    "rule-1": {
                        "replicas": {
                            r"c:\projecta\skills\cursor-rules": {
                                "last_sync": "2026-09-09T14:32:00Z",
                                "files": {
                                    "SKILL.md": {
                                        "hash": "a1b2",
                                        "size": 4096,
                                        "mtime": 1757423520.0,
                                    },
                                    "references/api.md": {
                                        "hash": "c3d4",
                                        "size": 812,
                                        "mtime": 1757423519.0,
                                        "kept": True,
                                    },
                                },
                            }
                        }
                    }
                },
            }
        )
    )

    result = load_state(layout.state_path)

    assert result.warning is None
    replica = result.state.rules["rule-1"][r"c:\projecta\skills\cursor-rules"]
    assert replica == ReplicaState(
        last_sync="2026-09-09T14:32:00Z",
        files={
            "SKILL.md": BaselineEntry(hash="a1b2", size=4096, mtime=1757423520.0),
            "references/api.md": BaselineEntry(
                hash="c3d4", size=812, mtime=1757423519.0, kept=True
            ),
        },
    )


def test_load_state_quarantines_corrupt_json_and_starts_empty(layout):
    layout.state_path.write_text("{not valid json")

    result = load_state(layout.state_path)

    assert result.state == EMPTY_STATE
    assert result.warning is not None
    assert not layout.state_path.exists()
    quarantined = list(layout.state_path.parent.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{not valid json"
    assert re.search(r"corrupt-\d{8}-\d{6}", quarantined[0].name)


@pytest.mark.parametrize(
    "content",
    [
        b"[]",
        b'{"version": 1, "hash_algo": "sha256", "rules": {"r": {"replicas": {"x": {}}}}}',
        b"\xff\xfe\x81garbage",
    ],
    ids=["wrong-top-level-type", "missing-replica-keys", "undecodable-bytes"],
)
def test_load_state_quarantines_malformed_content(layout, content):
    layout.state_path.write_bytes(content)

    result = load_state(layout.state_path)

    assert result.state == EMPTY_STATE
    assert result.warning is not None
    assert not layout.state_path.exists()
    assert len(list(layout.state_path.parent.glob("state.json.corrupt-*"))) == 1


def test_load_state_hash_algo_mismatch_is_treated_as_no_baseline(layout):
    layout.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hash_algo": "md5",
                "rules": {
                    "rule-1": {
                        "replicas": {
                            "replica": {
                                "last_sync": "2026-09-09T14:32:00Z",
                                "files": {
                                    "a.txt": {"hash": "x", "size": 1, "mtime": 1.0}
                                },
                            }
                        }
                    }
                },
            }
        )
    )

    result = load_state(layout.state_path)

    assert result.state == EMPTY_STATE
    assert result.warning is None
    assert layout.state_path.exists()  # left alone, not quarantined


def test_baseline_for_rule_returns_that_rules_files_keyed_by_replica():
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-09-09T14:32:00Z",
                    files={
                        "a.txt": BaselineEntry(hash="h1", size=1, mtime=1.0),
                        "b.txt": BaselineEntry(hash="h2", size=2, mtime=2.0, kept=True),
                    },
                )
            },
            "rule-2": {"replica-b": ReplicaState(last_sync="x", files={})},
        }
    )

    baseline = baseline_for_rule(state, "rule-1")

    assert baseline == {
        "replica-a": {
            "a.txt": BaselineEntry(hash="h1", size=1, mtime=1.0),
            "b.txt": BaselineEntry(hash="h2", size=2, mtime=2.0, kept=True),
        }
    }


def test_baseline_for_rule_unknown_rule_returns_empty_dict():
    assert baseline_for_rule(EMPTY_STATE, "no-such-rule") == {}


def test_save_state_then_load_state_round_trips(layout):
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-09-09T14:32:00Z",
                    files={"a.txt": BaselineEntry(hash="h1", size=1, mtime=1.0, kept=True)},
                )
            }
        }
    )

    save_state(layout.state_path, state)

    assert not layout.state_path.with_name("state.json.tmp").exists()
    result = load_state(layout.state_path)
    assert result.state == state


def test_save_state_then_load_state_round_trips_kept_master_hash(layout):
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-09-09T14:32:00Z",
                    files={
                        "a.txt": BaselineEntry(
                            hash="h1", size=1, mtime=1.0, kept=True, kept_master_hash="m1"
                        )
                    },
                )
            }
        }
    )

    save_state(layout.state_path, state)
    result = load_state(layout.state_path)

    assert result.state == state


def test_load_state_kept_entry_without_kept_master_hash_defaults_to_none(layout):
    layout.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "hash_algo": "sha256",
                "rules": {
                    "rule-1": {
                        "replicas": {
                            "replica-a": {
                                "last_sync": "2026-09-09T14:32:00Z",
                                "files": {
                                    "a.txt": {
                                        "hash": "h1",
                                        "size": 1,
                                        "mtime": 1.0,
                                        "kept": True,
                                    }
                                },
                            }
                        }
                    }
                },
            }
        )
    )

    result = load_state(layout.state_path)

    assert result.warning is None
    entry = result.state.rules["rule-1"]["replica-a"].files["a.txt"]
    assert entry == BaselineEntry(hash="h1", size=1, mtime=1.0, kept=True, kept_master_hash=None)


def test_merge_replica_entries_creates_new_rule_and_replica():
    state = _state({})

    new_state = merge_replica_entries(
        state,
        "rule-1",
        "replica-a",
        {"a.txt": BaselineEntry(hash="h1", size=1, mtime=1.0)},
        now=NOW,
    )

    assert new_state.rules["rule-1"]["replica-a"] == ReplicaState(
        last_sync="2026-09-12T10:00:00Z",
        files={"a.txt": BaselineEntry(hash="h1", size=1, mtime=1.0)},
    )
    assert state.rules == {}  # pure: original untouched


def test_merge_replica_entries_only_touches_given_paths_and_bumps_last_sync():
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-01-01T00:00:00Z",
                    files={
                        "a.txt": BaselineEntry(hash="old", size=1, mtime=1.0),
                        "untouched.txt": BaselineEntry(hash="u", size=9, mtime=9.0),
                    },
                )
            }
        }
    )

    new_state = merge_replica_entries(
        state,
        "rule-1",
        "replica-a",
        {"a.txt": BaselineEntry(hash="new", size=2, mtime=2.0)},
        now=NOW,
    )

    replica = new_state.rules["rule-1"]["replica-a"]
    assert replica.last_sync == "2026-09-12T10:00:00Z"
    assert replica.files["a.txt"] == BaselineEntry(hash="new", size=2, mtime=2.0)
    assert replica.files["untouched.txt"] == BaselineEntry(hash="u", size=9, mtime=9.0)


def test_merge_replica_entries_clears_kept_when_overwritten_without_it():
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-01-01T00:00:00Z",
                    files={"a.txt": BaselineEntry(hash="old", size=1, mtime=1.0, kept=True)},
                )
            }
        }
    )

    new_state = merge_replica_entries(
        state,
        "rule-1",
        "replica-a",
        {"a.txt": BaselineEntry(hash="new", size=2, mtime=2.0)},
        now=NOW,
    )

    assert new_state.rules["rule-1"]["replica-a"].files["a.txt"].kept is False


def test_merge_replica_entries_replaces_entry_differing_only_by_case():
    state = _state(
        {
            "rule-1": {
                "replica-a": ReplicaState(
                    last_sync="2026-01-01T00:00:00Z",
                    files={"skill.md": BaselineEntry(hash="old", size=1, mtime=1.0)},
                )
            }
        }
    )

    new_state = merge_replica_entries(
        state,
        "rule-1",
        "replica-a",
        {"SKILL.md": BaselineEntry(hash="new", size=2, mtime=2.0)},
        now=NOW,
    )

    assert new_state.rules["rule-1"]["replica-a"].files == {
        "SKILL.md": BaselineEntry(hash="new", size=2, mtime=2.0)
    }


def test_reconcile_with_config_drops_rules_and_replicas_no_longer_configured():
    config = _config(Master(path="m", type="dir"))
    state = _state(
        {
            "rule-1": {
                r"c:\a\replica": ReplicaState(last_sync="x", files={}),
                r"c:\stale\replica": ReplicaState(last_sync="x", files={}),
            },
            "rule-2-removed": {"c:\\other": ReplicaState(last_sync="x", files={})},
        }
    )

    reconciled = reconcile_with_config(state, config)

    assert reconciled == _state(
        {"rule-1": {r"c:\a\replica": ReplicaState(last_sync="x", files={})}}
    )


def test_reconcile_with_config_purges_a_removed_masters_namespaced_entries():
    config = _config(Master(path="m-kept", type="dir"))
    state = _state(
        {
            "rule-1": {
                r"c:\a\replica": ReplicaState(
                    last_sync="x",
                    files={
                        "m-kept/SKILL.md": BaselineEntry(hash="a", size=1, mtime=1.0),
                        "m-removed/notes.txt": BaselineEntry(hash="b", size=2, mtime=2.0),
                    },
                )
            }
        }
    )

    reconciled = reconcile_with_config(state, config)

    assert reconciled == _state(
        {
            "rule-1": {
                r"c:\a\replica": ReplicaState(
                    last_sync="x",
                    files={"m-kept/SKILL.md": BaselineEntry(hash="a", size=1, mtime=1.0)},
                )
            }
        }
    )


def test_reconcile_and_save_persists_a_purge(tmp_path):
    state_path = tmp_path / "state.json"
    state = _state({"rule-gone": {r"c:\a\replica": ReplicaState(last_sync="x", files={})}})

    reconciled = reconcile_and_save(state_path, state, Config(version=1))

    assert reconciled == _state({})
    assert load_state(state_path).state == reconciled


def test_reconcile_and_save_does_not_write_when_nothing_was_purged(tmp_path):
    state_path = tmp_path / "state.json"
    state = _state({})

    reconcile_and_save(state_path, state, Config(version=1))

    assert not state_path.exists()
