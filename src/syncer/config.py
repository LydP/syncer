import filecmp
import os
import shutil
import tomllib
from collections.abc import Callable, Hashable
from dataclasses import asdict, dataclass, field, replace
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TypeVar

import tomli_w

from syncer.landing import master_basename, master_basename_key
from syncer.storage import SyncerError, atomic_write_bytes, utc_file_stamp

MAX_CONFIG_BACKUPS = 10
MASTER_TYPES = ("dir", "file")

# `_reject_duplicates` reports the offending item itself, so its items are
# whatever spelling the user typed, keyed on a separate identity function.
_Item = TypeVar("_Item")


class ConfigError(SyncerError):
    """`config.toml` is malformed — bad TOML, a missing key, or a wrong type."""


class DuplicateMasterError(SyncerError):
    """Two masters in the same rule share a basename (they'd land on the same path)."""


class DuplicateMasterPathError(SyncerError):
    """Two masters in the same rule share a path."""


class DuplicateRuleNameError(SyncerError):
    """Two rules share a name (case-insensitively)."""


class DuplicateReplicaNameError(SyncerError):
    """Two replicas share a name (case-insensitively)."""


class DuplicateDependencyError(SyncerError):
    """The same dependency (master and what it depends on) is declared twice."""


class DependencyCycleError(SyncerError):
    """Dependencies form a cycle, including a master depending on itself."""


class ConfigClobberError(SyncerError):
    pass


def native_path(path: str) -> str:
    """`path` with `/` turned into `\\` — the stored form, so Qt's `/`-form
    paths and typed `\\`-form ones never mix. Casing, a UNC prefix and a
    trailing separator stay as entered, unlike the lossy
    `path_key` dedupe key."""
    return path.replace("/", os.sep)


def path_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def default_rule_name(master: str, master_type: str) -> str:
    """The add-rule modal's pre-filled name: the master's basename, extension
    stripped for a file (a folder's basename has no extension to strip).
    """
    basename = master_basename(master)
    if master_type == "file":
        return os.path.splitext(basename)[0]
    return basename


@dataclass(frozen=True)
class Master:
    path: str
    type: str


@dataclass(frozen=True)
class Dependency:
    """`master` depends on `depends_on` (ADR 0006). Identity is the pair of
    `path_key`s (`dependency_key`), so there is no id."""

    master: Master
    depends_on: Master

    def __str__(self) -> str:
        return f"{self.master.path} -> {self.depends_on.path}"


@dataclass(frozen=True)
class SyncRule:
    id: str
    name: str
    masters: list[Master]
    replicas: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    ignore_dependencies: bool = False


@dataclass(frozen=True)
class ReplicaName:
    path: str
    name: str


@dataclass(frozen=True)
class Config:
    version: int
    rules: list[SyncRule] = field(default_factory=list)
    replica_names: list[ReplicaName] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)


def _rule_name_key(name: str) -> str:
    return name.casefold()


def _replica_name_key(name: str) -> str:
    return name.strip().casefold()


def dependency_key(dependency: Dependency) -> tuple[str, str]:
    """A dependency's identity: both ends' `path_key`s, so casing and `/` vs
    `\\` spelling differences still name the one edge."""
    return (path_key(dependency.master.path), path_key(dependency.depends_on.path))


def find_name_conflict(
    config: Config, name: str, *, exclude_rule_id: str | None = None
) -> SyncRule | None:
    """The other rule (if any) already using `name`, case-insensitively.

    Non-raising counterpart to load_config's DuplicateRuleNameError, for the
    add/edit-rule modal's inline validation.
    """
    target = _rule_name_key(name)
    for rule in config.rules:
        if rule.id != exclude_rule_id and _rule_name_key(rule.name) == target:
            return rule
    return None


def find_master_conflict(masters: list[Master], index: int) -> tuple[str, Master] | None:
    """The other master (if any) `masters[index]` collides with, as
    `("path" | "basename", other)` — the same two per-rule duplicate rules
    `_validate_rules` raises on, in non-raising form for the add/edit-rule
    modal's inline per-row validation.

    Path wins over basename, matching `_validate_rules`' own ordering: equal
    paths always share a basename, so reporting both would double-flag one
    pair of rows. Hence the whole list is scanned before a basename match is
    returned — a path duplicate anywhere outranks an earlier basename one.
    """
    candidate = masters[index]
    target_path = path_key(candidate.path)
    target_basename = master_basename_key(candidate.path)
    basename_match: Master | None = None
    for other_index, other in enumerate(masters):
        if other_index == index:
            continue
        if path_key(other.path) == target_path:
            return ("path", other)
        if basename_match is None and master_basename_key(other.path) == target_basename:
            basename_match = other
    return ("basename", basename_match) if basename_match is not None else None


def with_rule(config: Config, rule: SyncRule) -> Config:
    """`config` with `rule` appended, or replacing the rule sharing its id.

    The single definition of what saving an add/edit produces, so the
    rule modal's live collision preview is computed against exactly the
    config a save would write rather than its own private guess.
    """
    if any(existing.id == rule.id for existing in config.rules):
        rules = [rule if existing.id == rule.id else existing for existing in config.rules]
    else:
        rules = [*config.rules, rule]
    return replace(config, rules=rules)


def without_rule(config: Config, rule_id: str) -> Config:
    """`config` with the rule of `rule_id` removed — `with_rule`'s inverse."""
    return replace(config, rules=[r for r in config.rules if r.id != rule_id])


def find_replica_sharers(
    config: Config, path: str, *, exclude_rule_id: str | None = None
) -> list[SyncRule]:
    """The other rules (if any) already listing `path` as a replica.

    Informational only — ADR 0002 makes replicas shareable across rules — for
    the add/edit-rule modal's "also used by" badge, not an error.
    """
    target = path_key(path)
    return [
        rule
        for rule in config.rules
        if rule.id != exclude_rule_id
        and any(path_key(replica) == target for replica in rule.replicas)
    ]


def replica_name(config: Config, path: str) -> str | None:
    """The name given to the replica at `path`, or None if it has none. Matched
    on `path_key`, so `/`, `\\` and casing differences still find
    the one replica."""
    target = path_key(path)
    for replica in config.replica_names:
        if path_key(replica.path) == target:
            return replica.name
    return None


def find_replica_name_conflict(config: Config, path: str, name: str) -> ReplicaName | None:
    """The other replica (if any) already named `name`, case-insensitively.

    Non-raising counterpart to load_config's DuplicateReplicaNameError, for the
    rule modal's inline validation. Checked against exactly the names a save
    would keep (`_tidy_replica_names`), so a blank name never conflicts and a
    name whose replica no rule lists never blocks; the replica at `path` itself
    is never its own conflict.
    """
    target = _replica_name_key(name)
    own_key = path_key(path)
    for replica in _tidy_replica_names(config).replica_names:
        if (
            _replica_name_key(replica.name) == target
            and path_key(replica.path) != own_key
        ):
            return replica
    return None


def with_replica_name(config: Config, path: str, name: str) -> Config:
    """`config` with the replica at `path` named `name` — added, or renamed in
    place — or, for a blank `name`, left with no name. `with_rule`'s sibling: the
    one definition of what naming a replica produces."""
    key = path_key(path)
    name = name.strip()
    replica_names: list[ReplicaName] = []
    found = False
    for replica in config.replica_names:
        if path_key(replica.path) != key:
            replica_names.append(replica)
        elif name:
            replica_names.append(replace(replica, name=name))  # keeps its stored path spelling
            found = True
    if name and not found:
        replica_names.append(ReplicaName(path=native_path(path), name=name))
    return replace(config, replica_names=replica_names)


def dependencies_of(config: Config, master_path: str) -> list[Dependency]:
    """The dependencies declared for the master at `master_path`, in config
    order. Matched on `path_key`, like `replica_name`."""
    target = path_key(master_path)
    return [d for d in config.dependencies if path_key(d.master.path) == target]


def find_dependency_conflict(config: Config, dependency: Dependency) -> str | None:
    """Why adding `dependency` would be rejected — a duplicate or a cycle — or
    None. Non-raising counterpart to load_config's DuplicateDependencyError /
    DependencyCycleError, for the dependency popup's inline validation; runs
    the very check a save would."""
    try:
        _validate_dependencies(with_dependency(config, dependency).dependencies)
    except (DuplicateDependencyError, DependencyCycleError) as exc:
        return str(exc)
    return None


def with_dependency(config: Config, dependency: Dependency) -> Config:
    """`config` with `dependency` appended — `with_rule`'s sibling."""
    return replace(config, dependencies=[*config.dependencies, dependency])


def without_dependency(config: Config, dependency: Dependency) -> Config:
    """`config` with `dependency` removed, whatever way either end's path is
    spelled — `with_dependency`'s inverse."""
    doomed = dependency_key(dependency)
    return replace(
        config,
        dependencies=[d for d in config.dependencies if dependency_key(d) != doomed],
    )


def replica_label(config: Config, path: str) -> str:
    """What to show for the replica at `path`: its name, else the path itself.
    The one place that rule lives, for every screen that shows a replica."""
    return replica_name(config, path) or path


def _require_str(table: dict, key: str, context: str = "key") -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"{context} {key!r} must be a string, got {value!r}")
    return value


def _require_str_list(table: dict, key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"key {key!r} must be a list of strings, got {value!r}")
    return value


def _require_bool(table: dict, key: str) -> bool:
    value = table.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"key {key!r} must be true or false, got {value!r}")
    return value


def _require_table_array(raw: dict, key: str, config_path: Path) -> list:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{config_path}: {key!r} must be an array of [[{key}]] tables")
    return value


def _parse_master(raw_master: object, context: str = "each rule's 'masters' entry") -> Master:
    """`context` names which master is being parsed, and rides along into every
    error below — a hand-edited `config.toml` holds many masters, and "key 'type'
    must be a string" alone doesn't say which one to go and fix."""
    if not isinstance(raw_master, dict):
        raise ConfigError(f"{context} must be a table, got {raw_master!r}")
    master_type = _require_str(raw_master, "type", f"{context}: key")
    if master_type not in MASTER_TYPES:
        raise ConfigError(
            f"{context}: key 'type' must be one of {MASTER_TYPES}, got {master_type!r}"
        )
    path = _require_str(raw_master, "path", f"{context}: key")
    return Master(path=native_path(path), type=master_type)


def _parse_dependency(raw_dependency: object) -> Dependency:
    if not isinstance(raw_dependency, dict):
        raise ConfigError(f"each [[dependency]] must be a table, got {raw_dependency!r}")
    return Dependency(
        master=_parse_master(raw_dependency.get("master"), "a [[dependency]]'s 'master'"),
        depends_on=_parse_master(
            raw_dependency.get("depends_on"), "a [[dependency]]'s 'depends_on'"
        ),
    )


def _parse_replica_name(raw_replica: object) -> ReplicaName:
    if not isinstance(raw_replica, dict):
        raise ConfigError(f"each [[replica]] must be a table, got {raw_replica!r}")
    return ReplicaName(
        path=native_path(_require_str(raw_replica, "path")),
        name=_require_str(raw_replica, "name"),
    )


def _parse_rule(raw_rule: object) -> SyncRule:
    if not isinstance(raw_rule, dict):
        raise ConfigError(f"each [[rule]] must be a table, got {raw_rule!r}")
    raw_masters = raw_rule.get("masters")
    if not isinstance(raw_masters, list):
        raise ConfigError(f"rule key 'masters' must be a list of tables, got {raw_masters!r}")
    return SyncRule(
        id=_require_str(raw_rule, "id"),
        name=_require_str(raw_rule, "name"),
        masters=[_parse_master(raw_master) for raw_master in raw_masters],
        replicas=[native_path(r) for r in _require_str_list(raw_rule, "replicas")],
        ignore=_require_str_list(raw_rule, "ignore"),
        ignore_dependencies=_require_bool(raw_rule, "ignore_dependencies"),
    )


def _reject_duplicates(
    items: list[_Item],
    error_cls: type[SyncerError],
    message: str,
    key: Callable[[_Item], Hashable],
) -> None:
    seen = set()
    for item in items:
        identity = key(item)
        if identity in seen:
            raise error_cls(f"{message}: {item}")
        seen.add(identity)


def _validate_rules(rules: list[SyncRule]) -> None:
    for rule in rules:
        if not rule.masters:
            raise ConfigError(f"rule {rule.id!r} must have at least one master")
        master_paths = [master.path for master in rule.masters]
        # Path check first: equal paths always share a basename, so the
        # basename check would otherwise shadow DuplicateMasterPathError.
        _reject_duplicates(
            master_paths,
            DuplicateMasterPathError,
            "master path used by more than one master in the same rule",
            key=path_key,
        )
        _reject_duplicates(
            master_paths,
            DuplicateMasterError,
            "master basename used by more than one master in the same rule",
            key=master_basename_key,
        )
    _reject_duplicates(
        [rule.name for rule in rules],
        DuplicateRuleNameError,
        "rule name used by more than one rule",
        key=_rule_name_key,
    )


def _replica_keys_in_use(config: Config) -> set[str]:
    return {path_key(r) for rule in config.rules for r in rule.replicas}


def _tidy_replica_names(config: Config) -> Config:
    """`config` with each replica name trimmed, blank ones (meaning "no name")
    dropped, and any whose replica path no rule lists any more dropped too."""
    in_use = _replica_keys_in_use(config)
    return replace(
        config,
        replica_names=[
            replace(replica, name=name)
            for replica in config.replica_names
            if (name := replica.name.strip()) and path_key(replica.path) in in_use
        ],
    )


def _validate_replica_names(replica_names: list[ReplicaName]) -> None:
    _reject_duplicates(
        [replica.name for replica in replica_names],
        DuplicateReplicaNameError,
        "replica name used by more than one replica",
        key=_replica_name_key,
    )


def _validate_dependencies(dependencies: list[Dependency]) -> None:
    _reject_duplicates(
        dependencies,
        DuplicateDependencyError,
        "dependency declared more than once",
        key=dependency_key,
    )
    # prepare() is the cycle check: a topological order exists iff there is
    # none, and a self-edge is just the shortest cycle.
    sorter: TopologicalSorter[str] = TopologicalSorter()
    for dependency in dependencies:
        master, depends_on = dependency_key(dependency)
        sorter.add(master, depends_on)
    try:
        sorter.prepare()
    except CycleError as exc:
        raise DependencyCycleError(
            "dependencies form a cycle: " + " -> ".join(exc.args[1])
        ) from exc


def load_config(config_path: Path) -> Config:
    with open(config_path, "rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
    version = raw.get("version")
    if not isinstance(version, int):
        raise ConfigError(f"{config_path}: 'version' must be an integer, got {version!r}")
    rules = [_parse_rule(raw_rule) for raw_rule in _require_table_array(raw, "rule", config_path)]
    _validate_rules(rules)
    replica_names = [
        _parse_replica_name(raw_replica)
        for raw_replica in _require_table_array(raw, "replica", config_path)
    ]
    dependencies = [
        _parse_dependency(raw_dependency)
        for raw_dependency in _require_table_array(raw, "dependency", config_path)
    ]
    # Tidied as a save would be, so a hand-edit's blank or orphaned names mean
    # "no name" here too rather than tripping the duplicate check.
    config = _tidy_replica_names(
        Config(
            version=version,
            rules=rules,
            replica_names=replica_names,
            dependencies=dependencies,
        )
    )
    _validate_replica_names(config.replica_names)
    _validate_dependencies(config.dependencies)
    return config


def _config_to_dict(config: Config) -> dict:
    raw = {
        "version": config.version,
        "settings": {},
        "rule": [asdict(rule) for rule in config.rules],
    }
    if config.replica_names:
        raw["replica"] = [asdict(replica) for replica in config.replica_names]
    if config.dependencies:
        raw["dependency"] = [asdict(dependency) for dependency in config.dependencies]
    return raw


def _write_backup(config_path: Path, backups_dir: Path) -> None:
    backups = sorted(backups_dir.glob("config-*.toml"))
    if backups and filecmp.cmp(config_path, backups[-1], shallow=False):
        return  # newest backup already holds this content; don't spend a slot on it
    shutil.copy2(config_path, backups_dir / f"config-{utc_file_stamp()}.toml")
    _prune_old_backups(backups_dir)


def _prune_old_backups(backups_dir: Path) -> None:
    backups = sorted(backups_dir.glob("config-*.toml"))
    for stale in backups[:-MAX_CONFIG_BACKUPS]:
        stale.unlink()


def save_config(config_path: Path, config: Config, backups_dir: Path) -> None:
    config = _tidy_replica_names(config)
    _validate_rules(config.rules)
    _validate_replica_names(config.replica_names)
    _validate_dependencies(config.dependencies)
    # Back up whatever save is about to destroy, not what it just wrote — the
    # latter is already sitting live in config_path with nothing at risk.
    # Otherwise a hand-edit made between loads is overwritten with no backup
    # ever having held it. Done before the temp file exists, so a failed
    # backup leaves nothing behind.
    had_previous_version = config_path.exists()
    if had_previous_version:
        _write_backup(config_path, backups_dir)
    atomic_write_bytes(config_path, tomli_w.dumps(_config_to_dict(config)).encode("utf-8"))
    if not had_previous_version:
        # Nothing was overwritten, but spec §4 wants a backup on every save.
        _write_backup(config_path, backups_dir)


class ConfigStore:
    """Tracks the mtime `config.toml` had at last load, to warn before a save
    would silently clobber an external hand-edit made since.
    """

    def __init__(self, config_path: Path, backups_dir: Path):
        self._config_path = config_path
        self._backups_dir = backups_dir
        self._loaded_mtime: float | None = None

    def _current_mtime(self) -> float | None:
        try:
            return self._config_path.stat().st_mtime
        except FileNotFoundError:
            return None

    def load(self) -> Config:
        # No config.toml yet is a first run (spec.md §10), not an error —
        # mirrors load_state's handling of a missing state.json. But a file
        # this store already loaded or saved that has since vanished is not a
        # first run: an empty config would make the reconcile after this load
        # purge every rule's state.json entries.
        if not self._config_path.exists():
            if self._loaded_mtime is not None:
                raise ConfigError(
                    f"{self._config_path} is missing; restore it (backups are in "
                    f"{self._backups_dir}) and reload"
                )
            config = Config(version=1)
        else:
            config = load_config(self._config_path)
        self._loaded_mtime = self._current_mtime()
        return config

    def save(self, config: Config) -> None:
        # A missing file has nothing to clobber; an existing file whose mtime we
        # never saw (never loaded, or changed since) holds unseen content.
        current_mtime = self._current_mtime()
        if current_mtime is not None and current_mtime != self._loaded_mtime:
            raise ConfigClobberError(
                f"{self._config_path} changed externally since it was last "
                "loaded; reload before saving to avoid clobbering those changes"
            )
        try:
            save_config(self._config_path, config, self._backups_dir)
        finally:
            # Record our own write even if the first-save backup fails after
            # os.replace, so the next save isn't mistaken for a clobber. A save
            # that wrote nothing (e.g. rejected by validation) against a file
            # that has since vanished must keep the old mtime, or load() would
            # mistake the vanished file for a first run.
            current_mtime = self._current_mtime()
            if current_mtime is not None:
                self._loaded_mtime = current_mtime
