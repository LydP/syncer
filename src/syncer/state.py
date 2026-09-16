import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from syncer.check import HASH_ALGO, BaselineEntry
from syncer.config import Config, normalize_replica_path
from syncer.storage import atomic_write_bytes, utc_file_stamp

STATE_VERSION = 1


@dataclass(frozen=True)
class ReplicaState:
    last_sync: str
    files: dict[str, BaselineEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class State:
    version: int
    hash_algo: str
    rules: dict[str, dict[str, ReplicaState]] = field(default_factory=dict)


@dataclass(frozen=True)
class StateLoadResult:
    state: State
    warning: str | None = None


def _empty_state() -> State:
    return State(version=STATE_VERSION, hash_algo=HASH_ALGO)


def _parse_file_entry(raw: dict) -> BaselineEntry:
    return BaselineEntry(
        hash=raw["hash"],
        size=raw["size"],
        mtime=raw["mtime"],
        kept=raw.get("kept", False),
        kept_master_hash=raw.get("kept_master_hash"),
    )


def _parse_replica(raw: dict) -> ReplicaState:
    return ReplicaState(
        last_sync=raw["last_sync"],
        files={name: _parse_file_entry(entry) for name, entry in raw["files"].items()},
    )


def _parse_state(raw: dict) -> State:
    return State(
        version=raw["version"],
        hash_algo=raw["hash_algo"],
        rules={
            rule_id: {
                replica_path: _parse_replica(replica_raw)
                for replica_path, replica_raw in rule_raw.get("replicas", {}).items()
            }
            for rule_id, rule_raw in raw.get("rules", {}).items()
        },
    )


def _quarantine(state_path: Path) -> Path:
    quarantine_path = state_path.with_name(f"{state_path.name}.corrupt-{utc_file_stamp()}")
    state_path.replace(quarantine_path)
    return quarantine_path


def load_state(state_path: Path) -> StateLoadResult:
    if not state_path.exists():
        return StateLoadResult(state=_empty_state())
    try:
        # Bytes, not read_text(): save_state writes UTF-8, and a locale-codec
        # decode error would otherwise escape the quarantine path.
        raw = json.loads(state_path.read_bytes())
        if not isinstance(raw, dict):
            raise ValueError(f"top level must be an object, got {type(raw).__name__}")
        if raw.get("hash_algo") != HASH_ALGO:
            return StateLoadResult(state=_empty_state())
        state = _parse_state(raw)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # ValueError covers JSONDecodeError and UnicodeDecodeError; the rest are
        # valid JSON of the wrong shape — equally corrupt.
        quarantine_path = _quarantine(state_path)
        return StateLoadResult(
            state=_empty_state(),
            warning=(
                f"{state_path} was corrupt ({exc!r}); quarantined to "
                f"{quarantine_path}, starting from an empty state"
            ),
        )
    return StateLoadResult(state=state)


def _file_entry_to_dict(entry: BaselineEntry) -> dict:
    raw = {"hash": entry.hash, "size": entry.size, "mtime": entry.mtime}
    if entry.kept:
        raw["kept"] = True
        if entry.kept_master_hash is not None:
            raw["kept_master_hash"] = entry.kept_master_hash
    return raw


def _replica_to_dict(replica: ReplicaState) -> dict:
    return {
        "last_sync": replica.last_sync,
        "files": {
            rel_path: _file_entry_to_dict(entry) for rel_path, entry in replica.files.items()
        },
    }


def _state_to_dict(state: State) -> dict:
    return {
        "version": state.version,
        "hash_algo": state.hash_algo,
        "rules": {
            rule_id: {
                "replicas": {
                    replica_path: _replica_to_dict(replica)
                    for replica_path, replica in replicas.items()
                }
            }
            for rule_id, replicas in state.rules.items()
        },
    }


def save_state(state_path: Path, state: State) -> None:
    atomic_write_bytes(state_path, json.dumps(_state_to_dict(state), indent=2).encode("utf-8"))


def merge_replica_entries(
    state: State,
    rule_id: str,
    replica_path: str,
    updates: dict[str, BaselineEntry],
    *,
    now: datetime,
    removed: Iterable[str] = (),
) -> State:
    """Merge `updates` into one replica's `files` map and drop the `removed`
    rel_paths (files an applied `master_deleted` took off both sides, so a
    stale baseline doesn't linger), leaving every other entry (and every
    other rule/replica) untouched, and bump `last_sync`.

    A rel_path already present in the replica is fully overwritten by the
    incoming BaselineEntry, so an ordinary drift-sync (which never sets
    `kept`) clears any stale `kept=True` on that path simply by omitting it.
    rel_paths match case-insensitively (as `check()` looks them up), so an
    update or removal also hits an existing entry differing only by case.
    """
    rule = state.rules.get(rule_id, {})
    existing = rule.get(replica_path)
    dropped_keys = {os.path.normcase(rel_path) for rel_path in (*updates, *removed)}
    kept_files = {
        rel_path: entry
        for rel_path, entry in (existing.files if existing else {}).items()
        if os.path.normcase(rel_path) not in dropped_keys
    }
    new_replica = ReplicaState(
        last_sync=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        files={**kept_files, **updates},
    )
    return replace(state, rules={**state.rules, rule_id: {**rule, replica_path: new_replica}})


def reconcile_with_config(state: State, config: Config) -> State:
    """Drop any rule_id/replica-path entry no longer present in `config` —
    a deleted rule, or a replica removed from a surviving rule.
    """
    configured = {
        rule.id: {normalize_replica_path(replica) for replica in rule.replicas}
        for rule in config.rules
    }
    return replace(
        state,
        rules={
            rule_id: {p: r for p, r in replicas.items() if p in configured[rule_id]}
            for rule_id, replicas in state.rules.items()
            if rule_id in configured
        },
    )


def reconcile_and_save(state_path: Path, state: State, config: Config) -> State:
    """`reconcile_with_config`, persisted right away when it purged anything
    (spec.md §10 — purges apply immediately, not at the next sync). The one
    path for startup, reload and every GUI rule change alike.
    """
    reconciled = reconcile_with_config(state, config)
    if reconciled != state:
        save_state(state_path, reconciled)
    return reconciled


def baseline_for_rule(state: State, rule_id: str) -> dict[str, dict[str, BaselineEntry]]:
    return {
        replica_path: replica.files
        for replica_path, replica in state.rules.get(rule_id, {}).items()
    }
