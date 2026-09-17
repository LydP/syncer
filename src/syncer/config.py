import filecmp
import os
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

from syncer.storage import SyncerError, atomic_write_bytes, utc_file_stamp

MAX_CONFIG_BACKUPS = 10
MASTER_TYPES = ("dir", "file")


class ConfigError(SyncerError):
    """`config.toml` is malformed — bad TOML, a missing key, or a wrong type."""


class DuplicateMasterError(SyncerError):
    """Two masters in the same rule share a basename (they'd land on the same path)."""


class DuplicateMasterPathError(SyncerError):
    """Two masters in the same rule share a path."""


class DuplicateRuleNameError(SyncerError):
    """Two rules share a name (case-insensitively)."""


class ConfigClobberError(SyncerError):
    pass


def normalize_replica_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def master_basename(path: str) -> str:
    return os.path.basename(os.path.normpath(path))


def master_basename_key(path: str) -> str:
    """A master's basename identity: what must be unique within a rule, and
    what two rules' masters collide on inside a shared replica.
    """
    return os.path.normcase(master_basename(path))


def default_rule_name(master: str, master_type: str) -> str:
    """The add-rule modal's pre-filled name: the master's basename, extension
    stripped for a file (a folder's basename has no extension to strip).
    """
    basename = master_basename(master)
    if master_type == "file":
        return os.path.splitext(basename)[0]
    return basename


def abs_path(root: str, master_type: str, rel_path: str) -> str:
    """The on-disk path of `rel_path` under a master or replica `root`.

    A file-type master's one replica entry *is* the file — there's no root
    folder to join a rel_path onto (mirrors check.py's _scan_side). Shared by
    sync.py and conflict.py so this rule has a single owner.
    """
    if master_type == "file":
        return root
    return os.path.join(root, *rel_path.split("/"))


@dataclass(frozen=True)
class Master:
    path: str
    type: str


@dataclass(frozen=True)
class SyncRule:
    id: str
    name: str
    masters: list[Master]
    replicas: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    version: int
    rules: list[SyncRule] = field(default_factory=list)


def masters_by_basename_key(masters: list[Master]) -> dict[str, Master]:
    return {master_basename_key(master.path): master for master in masters}


def is_owned_landing_path(masters_by_key: dict[str, Master], rel_path: str) -> bool:
    """Whether `rel_path` — a replica-relative landing path, matched
    case-insensitively — falls in the namespace of one of `masters_by_key`
    (config data only, independent of what's on disk): a file master's bare
    filename, or a dir master's landing folder itself or anything under it.
    The landing folder's own path stays owned so a file or junction sitting
    where that folder belongs is reported as a type mismatch / unreadable,
    not silently dropped. A landing path no *currently configured* master
    claims is left over from a master since removed from the rule, which
    check() reconciles away silently (issue #21) and reconcile_with_config
    purges from the baseline (issue #22).
    """
    # normcase turns "/" into "\\" on Windows, so split on either.
    head, sep, _ = os.path.normcase(rel_path).replace("\\", "/").partition("/")
    master = masters_by_key.get(head)
    return master is not None and (master.type == "dir" or not sep)


def _rule_name_key(name: str) -> str:
    return name.casefold()


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


def _require_str(table: dict, key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"key {key!r} must be a string, got {value!r}")
    return value


def _require_str_list(table: dict, key: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"key {key!r} must be a list of strings, got {value!r}")
    return value


def _parse_master(raw_master: object) -> Master:
    if not isinstance(raw_master, dict):
        raise ConfigError(f"each rule's 'masters' entry must be a table, got {raw_master!r}")
    master_type = _require_str(raw_master, "type")
    if master_type not in MASTER_TYPES:
        raise ConfigError(
            f"master key 'type' must be one of {MASTER_TYPES}, got {master_type!r}"
        )
    return Master(path=_require_str(raw_master, "path"), type=master_type)


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
        replicas=_require_str_list(raw_rule, "replicas"),
        ignore=_require_str_list(raw_rule, "ignore"),
    )


def _reject_duplicates(
    items: list[str],
    error_cls: type[SyncerError],
    message: str,
    key: Callable[[str], str],
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
            key=normalize_replica_path,
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


def load_config(config_path: Path) -> Config:
    with open(config_path, "rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc
    version = raw.get("version")
    if not isinstance(version, int):
        raise ConfigError(f"{config_path}: 'version' must be an integer, got {version!r}")
    raw_rules = raw.get("rule", [])
    if not isinstance(raw_rules, list):
        raise ConfigError(f"{config_path}: 'rule' must be an array of [[rule]] tables")
    rules = [_parse_rule(raw_rule) for raw_rule in raw_rules]
    _validate_rules(rules)
    return Config(version=version, rules=rules)


def _config_to_dict(config: Config) -> dict:
    return {
        "version": config.version,
        "settings": {},
        "rule": [asdict(rule) for rule in config.rules],
    }


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
    _validate_rules(config.rules)
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
