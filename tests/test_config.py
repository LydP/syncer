import os
import re

import pytest

from syncer.config import (
    Config,
    ConfigClobberError,
    ConfigError,
    ConfigStore,
    DuplicateMasterError,
    DuplicateMasterPathError,
    DuplicateRuleNameError,
    MAX_CONFIG_BACKUPS,
    Master,
    SyncRule,
    default_rule_name,
    find_name_conflict,
    load_config,
    normalize_replica_path,
    save_config,
)

CONFIG_HEADER = "version = 1\n\n[settings]\n\n"


def _rule_toml(rule_id, masters, replicas=(), name=None):
    """`masters` is a list of (path, type) pairs."""
    masters_toml = ", ".join(f"{{ path = '{path}', type = '{type_}' }}" for path, type_ in masters)
    replicas_toml = ", ".join(f"'{r}'" for r in replicas)
    return (
        "[[rule]]\n"
        f'id = "{rule_id}"\n'
        f'name = "{name or rule_id}"\n'
        f"masters = [{masters_toml}]\n"
        f"replicas = [{replicas_toml}]\n"
        "ignore = []\n"
    )


def _write_config(config_path, *rule_tomls):
    config_path.write_text(CONFIG_HEADER + "\n".join(rule_tomls))


def test_default_rule_name_strips_extension_for_a_file_master():
    assert default_rule_name(r"C:\resumes\resume.docx", "file") == "resume"


def test_default_rule_name_keeps_full_basename_for_a_dir_master():
    assert default_rule_name(r"C:\Projects\my-skills", "dir") == "my-skills"


def _rule(id, *masters, name=None):
    return SyncRule(id=id, name=name or id, masters=list(masters), replicas=[])


def test_find_name_conflict_returns_none_when_no_other_rule_uses_the_name():
    config = Config(version=1, rules=[_rule("r1", Master(r"C:\a", "dir"), name="alpha")])

    assert find_name_conflict(config, "beta") is None


def test_find_name_conflict_returns_the_conflicting_rule():
    other = _rule("r1", Master(r"C:\a", "dir"), name="shared-name")
    config = Config(version=1, rules=[other])

    assert find_name_conflict(config, "Shared-Name") is other


def test_find_name_conflict_excludes_the_given_rule_id():
    config = Config(version=1, rules=[_rule("r1", Master(r"C:\a", "dir"), name="shared-name")])

    assert find_name_conflict(config, "shared-name", exclude_rule_id="r1") is None


def test_normalize_replica_path_resolves_relative_segments_and_case():
    normalized = normalize_replica_path(r"C:\MyStuff\ProjectA\..\ProjectA\.claude\SKILLS")

    assert normalized == r"c:\mystuff\projecta\.claude\skills"


def test_load_config_parses_version_and_empty_rules(layout):
    _write_config(layout.config_path)

    config = load_config(layout.config_path)

    assert config.version == 1
    assert config.rules == []


def test_load_config_parses_a_rule(layout):
    layout.config_path.write_text(
        CONFIG_HEADER + "[[rule]]\n"
        'id = "11111111-1111-4111-8111-111111111111"\n'
        'name = "cursor-rules skill"\n'
        "masters = [{ path = 'C:\\MyStuff\\skills\\cursor-rules', type = 'dir' }]\n"
        r"replicas = ['C:\ProjectA\.claude\skills\cursor-rules']" "\n"
        "ignore = []\n"
    )

    config = load_config(layout.config_path)

    assert len(config.rules) == 1
    rule = config.rules[0]
    assert rule.id == "11111111-1111-4111-8111-111111111111"
    assert rule.name == "cursor-rules skill"
    assert rule.masters == [Master(path=r"C:\MyStuff\skills\cursor-rules", type="dir")]
    assert rule.replicas == [r"C:\ProjectA\.claude\skills\cursor-rules"]
    assert rule.ignore == []


def test_load_config_parses_a_rule_with_several_masters(layout):
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(r"C:\MyStuff\skills\wayfinder", "dir"), (r"C:\MyStuff\resume.docx", "file")],
        ),
    )

    config = load_config(layout.config_path)

    assert config.rules[0].masters == [
        Master(path=r"C:\MyStuff\skills\wayfinder", type="dir"),
        Master(path=r"C:\MyStuff\resume.docx", type="file"),
    ]


def test_load_config_allows_the_same_master_path_across_rules(layout):
    shared_master = r"C:\MyStuff\skills\cursor-rules"
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(shared_master, "dir")],
            [r"C:\ProjectA\.claude\skills\cursor-rules"],
            name="rule-a",
        ),
        _rule_toml(
            "22222222-2222-4222-8222-222222222222",
            [(shared_master, "dir")],
            [r"C:\ProjectB\.claude\skills\cursor-rules"],
            name="rule-b",
        ),
    )

    config = load_config(layout.config_path)

    assert len(config.rules) == 2


def test_load_config_allows_the_same_replica_path_across_rules(layout):
    shared_replica = r"C:\ProjectA\.claude\skills\cursor-rules"
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(r"C:\MyStuff\skills\cursor-rules", "dir")],
            [shared_replica],
            name="rule-a",
        ),
        _rule_toml(
            "22222222-2222-4222-8222-222222222222",
            [(r"C:\MyStuff\skills\other-skill", "dir")],
            [shared_replica],
            name="rule-b",
        ),
    )

    config = load_config(layout.config_path)

    assert len(config.rules) == 2


@pytest.mark.parametrize(
    "second_name",
    ["shared-name", "SHARED-NAME"],
    ids=["exact", "different-case"],
)
def test_load_config_rejects_duplicate_rule_name_across_rules(layout, second_name):
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(r"C:\MyStuff\skills\cursor-rules", "dir")],
            name="shared-name",
        ),
        _rule_toml(
            "22222222-2222-4222-8222-222222222222",
            [(r"C:\MyStuff\skills\other-skill", "dir")],
            name=second_name,
        ),
    )

    with pytest.raises(DuplicateRuleNameError, match=re.escape(second_name)):
        load_config(layout.config_path)


@pytest.mark.parametrize(
    "second_master",
    [r"C:\MyStuff\skills\cursor-rules", "c:\\mystuff\\SKILLS\\cursor-rules\\"],
    ids=["exact", "different-case-and-trailing-slash"],
)
def test_load_config_rejects_duplicate_master_path_within_a_rule(layout, second_master):
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(r"C:\MyStuff\skills\cursor-rules", "dir"), (second_master, "dir")],
        ),
    )

    with pytest.raises(DuplicateMasterPathError, match=re.escape(second_master)):
        load_config(layout.config_path)


def test_load_config_rejects_duplicate_master_basename_within_a_rule(layout):
    _write_config(
        layout.config_path,
        _rule_toml(
            "11111111-1111-4111-8111-111111111111",
            [(r"C:\MyStuff\skills\cursor-rules", "dir"), (r"C:\Elsewhere\cursor-rules", "dir")],
        ),
    )

    with pytest.raises(DuplicateMasterError, match="cursor-rules"):
        load_config(layout.config_path)


def test_load_config_rejects_a_rule_with_no_masters(layout):
    _write_config(layout.config_path, _rule_toml("11111111-1111-4111-8111-111111111111", []))

    with pytest.raises(ConfigError):
        load_config(layout.config_path)


def test_save_config_round_trips_through_load_config(layout):
    config = Config(
        version=1,
        rules=[
            SyncRule(
                id="11111111-1111-4111-8111-111111111111",
                name="cursor-rules skill",
                masters=[Master(path=r"C:\MyStuff\skills\cursor-rules", type="dir")],
                replicas=[r"C:\ProjectA\.claude\skills\cursor-rules"],
                ignore=[],
            )
        ],
    )

    save_config(layout.config_path, config, layout.backups_dir)

    assert layout.config_path.is_file()
    assert load_config(layout.config_path) == config


def test_save_config_round_trips_a_rule_with_several_masters(layout):
    config = Config(
        version=1,
        rules=[
            _rule(
                "11111111-1111-4111-8111-111111111111",
                Master(path=r"C:\MyStuff\skills\wayfinder", type="dir"),
                Master(path=r"C:\MyStuff\resume.docx", type="file"),
            )
        ],
    )

    save_config(layout.config_path, config, layout.backups_dir)

    assert load_config(layout.config_path) == config


@pytest.mark.parametrize(
    ("rules", "error_cls"),
    [
        ([_rule("r1")], ConfigError),
        ([_rule("r1", Master(r"C:\a", "dir"), Master(r"C:\a", "dir"))], DuplicateMasterPathError),
        (
            [
                _rule("r1", Master(r"C:\a", "dir"), name="shared"),
                _rule("r2", Master(r"C:\b", "dir"), name="shared"),
            ],
            DuplicateRuleNameError,
        ),
    ],
    ids=["no-masters", "duplicate-master-path", "duplicate-rule-name"],
)
def test_save_config_rejects_an_invalid_config_without_writing(layout, rules, error_cls):
    with pytest.raises(error_cls):
        save_config(layout.config_path, Config(version=1, rules=rules), layout.backups_dir)

    assert not layout.config_path.exists()


def test_save_config_writes_a_backup(layout):
    config = Config(version=1, rules=[])

    save_config(layout.config_path, config, layout.backups_dir)

    backups = list(layout.backups_dir.glob("config-*.toml"))
    assert len(backups) == 1
    assert load_config(backups[0]) == config


def test_save_config_backs_up_the_content_it_is_about_to_overwrite(layout):
    save_config(layout.config_path, Config(version=1, rules=[]), layout.backups_dir)
    # simulate a hand-edit made directly on disk, outside the app
    layout.config_path.write_text("version = 2\n\n[settings]\n")

    save_config(layout.config_path, Config(version=3, rules=[]), layout.backups_dir)

    backed_up_versions = {
        load_config(b).version for b in layout.backups_dir.glob("config-*.toml")
    }
    assert 2 in backed_up_versions


def test_save_config_keeps_only_the_last_n_backups(layout):
    for version in range(1, MAX_CONFIG_BACKUPS + 3):
        save_config(layout.config_path, Config(version=version, rules=[]), layout.backups_dir)

    backups = sorted(layout.backups_dir.glob("config-*.toml"))
    assert len(backups) == MAX_CONFIG_BACKUPS
    kept_versions = {load_config(b).version for b in backups}
    assert kept_versions == set(range(2, MAX_CONFIG_BACKUPS + 2))


def test_config_store_save_then_load_round_trips(layout):
    store = ConfigStore(layout.config_path, layout.backups_dir)
    config = Config(version=1, rules=[])

    store.save(config)

    assert store.load() == config


def test_config_store_load_returns_an_empty_config_when_no_file_exists(layout):
    store = ConfigStore(layout.config_path, layout.backups_dir)

    assert store.load() == Config(version=1, rules=[])


def test_config_store_load_raises_when_a_previously_loaded_file_has_disappeared(layout):
    # Not a first run: treating it as an empty config would purge all of
    # state.json on the reconcile that follows every load.
    store = ConfigStore(layout.config_path, layout.backups_dir)
    store.save(Config(version=1, rules=[]))
    store.load()
    layout.config_path.unlink()

    with pytest.raises(ConfigError):
        store.load()


def test_config_store_load_still_raises_for_a_vanished_file_after_a_rejected_save(layout):
    store = ConfigStore(layout.config_path, layout.backups_dir)
    store.save(Config(version=1, rules=[]))
    store.load()
    layout.config_path.unlink()
    with pytest.raises(ConfigError):
        store.save(Config(version=1, rules=[SyncRule(id="r1", name="r1", masters=[])]))

    with pytest.raises(ConfigError):
        store.load()


def test_config_store_save_raises_when_file_changed_externally_since_load(layout):
    store = ConfigStore(layout.config_path, layout.backups_dir)
    store.save(Config(version=1, rules=[]))
    store.load()
    external_mtime = layout.config_path.stat().st_mtime + 5
    os.utime(layout.config_path, (external_mtime, external_mtime))

    with pytest.raises(ConfigClobberError):
        store.save(Config(version=2, rules=[]))

    assert load_config(layout.config_path).version == 1


@pytest.mark.parametrize(
    "body",
    [
        "version = 1\n[[rule\n",
        "[settings]\n",
        CONFIG_HEADER + "[[rule]]\nid = 'x'\nname = 'x'\n"
        "masters = [{ path = 'C:\\A', type = 'dir' }]\nreplicas = 'C:\\B'\n",
        CONFIG_HEADER + "[[rule]]\nid = 'x'\nname = 'x'\n"
        "masters = [{ path = 'C:\\A', type = 'folder' }]\n",
        CONFIG_HEADER + "[[rule]]\nid = 'x'\nname = 'x'\n",
    ],
    ids=["bad-toml", "no-version", "replicas-not-list", "bad-master-type", "no-masters-key"],
)
def test_load_config_raises_config_error_for_malformed_hand_edits(layout, body):
    layout.config_path.write_text(body)

    with pytest.raises(ConfigError):
        load_config(layout.config_path)


def test_config_store_save_without_load_refuses_to_clobber_existing_file(layout):
    _write_config(layout.config_path)
    store = ConfigStore(layout.config_path, layout.backups_dir)

    with pytest.raises(ConfigClobberError):
        store.save(Config(version=2, rules=[]))


def _remove_backups_dir(layout):
    for backup in list(layout.backups_dir.glob("*")):
        backup.unlink()
    layout.backups_dir.rmdir()


def test_save_config_does_not_back_up_identical_content_twice(layout):
    save_config(layout.config_path, Config(version=1, rules=[]), layout.backups_dir)

    save_config(layout.config_path, Config(version=2, rules=[]), layout.backups_dir)

    backups = list(layout.backups_dir.glob("config-*.toml"))
    assert len(backups) == 1
    assert load_config(backups[0]).version == 1


def test_save_config_failed_backup_leaves_config_untouched_and_no_temp_file(layout):
    save_config(layout.config_path, Config(version=1, rules=[]), layout.backups_dir)
    _remove_backups_dir(layout)

    with pytest.raises(OSError):
        save_config(layout.config_path, Config(version=2, rules=[]), layout.backups_dir)

    assert load_config(layout.config_path).version == 1
    assert not layout.config_path.with_name(layout.config_path.name + ".tmp").exists()


def test_config_store_save_after_failed_first_save_backup_is_not_flagged_as_clobber(layout):
    # The first save is the one place the backup runs after os.replace.
    store = ConfigStore(layout.config_path, layout.backups_dir)
    _remove_backups_dir(layout)

    with pytest.raises(OSError):
        store.save(Config(version=1, rules=[]))
    assert load_config(layout.config_path).version == 1
    layout.backups_dir.mkdir()

    store.save(Config(version=2, rules=[]))

    assert load_config(layout.config_path).version == 2
