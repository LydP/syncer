import filecmp
import os
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

from syncer.storage import SyncerError

MAX_CONFIG_BACKUPS = 10
MASTER_TYPES = ("dir", "file")


class ConfigError(SyncerError):
    """`config.toml` is malformed — bad TOML, a missing key, or a wrong type."""


class DuplicateMasterError(SyncerError):
    pass


class DuplicateReplicaError(SyncerError):
    pass


class ConfigClobberError(SyncerError):
    pass


def normalize_replica_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


@dataclass(frozen=True)
class SyncRule:
    id: str
    name: str
    master: str
    master_type: str
    replicas: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Config:
    version: int
    rules: list[SyncRule] = field(default_factory=list)


def _require_str(raw_rule: dict, key: str) -> str:
    value = raw_rule.get(key)
    if not isinstance(value, str):
        raise ConfigError(f"rule key {key!r} must be a string, got {value!r}")
    return value


def _require_str_list(raw_rule: dict, key: str) -> list[str]:
    value = raw_rule.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"rule key {key!r} must be a list of strings, got {value!r}")
    return value


def _parse_rule(raw_rule: object) -> SyncRule:
    if not isinstance(raw_rule, dict):
        raise ConfigError(f"each [[rule]] must be a table, got {raw_rule!r}")
    master_type = _require_str(raw_rule, "master_type")
    if master_type not in MASTER_TYPES:
        raise ConfigError(
            f"rule key 'master_type' must be one of {MASTER_TYPES}, got {master_type!r}"
        )
    return SyncRule(
        id=_require_str(raw_rule, "id"),
        name=_require_str(raw_rule, "name"),
        master=_require_str(raw_rule, "master"),
        master_type=master_type,
        replicas=_require_str_list(raw_rule, "replicas"),
        ignore=_require_str_list(raw_rule, "ignore"),
    )


def _reject_duplicate_paths(
    paths: list[str],
    error_cls: type[SyncerError],
    role: str,
    key: Callable[[str], str] = lambda path: path,
) -> None:
    seen = set()
    for path in paths:
        identity = key(path)
        if identity in seen:
            raise error_cls(f"{role} path used by more than one rule: {path}")
        seen.add(identity)


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
    _reject_duplicate_paths(
        [rule.master for rule in rules],
        DuplicateMasterError,
        "master",
        key=normalize_replica_path,
    )
    _reject_duplicate_paths(
        [replica for rule in rules for replica in rule.replicas],
        DuplicateReplicaError,
        "replica",
        key=normalize_replica_path,
    )
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
    # UTC, so name order stays chronological across DST/clock changes — pruning
    # relies on it.
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    shutil.copy2(config_path, backups_dir / f"config-{timestamp}.toml")
    _prune_old_backups(backups_dir)


def _prune_old_backups(backups_dir: Path) -> None:
    backups = sorted(backups_dir.glob("config-*.toml"))
    for stale in backups[:-MAX_CONFIG_BACKUPS]:
        stale.unlink()


def save_config(config_path: Path, config: Config, backups_dir: Path) -> None:
    # Back up whatever save is about to destroy, not what it just wrote — the
    # latter is already sitting live in config_path with nothing at risk.
    # Otherwise a hand-edit made between loads is overwritten with no backup
    # ever having held it. Done before the temp file exists, so a failed
    # backup leaves nothing behind.
    had_previous_version = config_path.exists()
    if had_previous_version:
        _write_backup(config_path, backups_dir)
    tmp_path = config_path.with_name(config_path.name + ".tmp")
    try:
        with open(tmp_path, "wb") as fh:
            tomli_w.dump(_config_to_dict(config), fh)
        os.replace(tmp_path, config_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
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
            # os.replace, so the next save isn't mistaken for a clobber.
            self._loaded_mtime = self._current_mtime()
