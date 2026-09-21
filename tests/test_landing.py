import os

import pytest

from syncer.config import Master
from syncer.landing import LandingMap, master_basename, master_basename_key, replica_abs_path


def test_a_dir_master_owns_its_landing_folder_and_everything_beneath_it():
    landing = LandingMap([Master(r"C:\skills\writing", "dir")])

    assert landing.owns("writing")
    assert landing.owns("writing/SKILL.md")
    assert landing.owns("writing/examples/deep/one.md")


def test_a_file_master_owns_its_bare_filename_and_nothing_nested_under_it():
    landing = LandingMap([Master(r"C:\docs\resume.docx", "file")])

    assert landing.owns("resume.docx")
    assert not landing.owns("resume.docx/inner.txt")


def test_matching_ignores_case():
    landing = LandingMap([Master(r"C:\skills\Writing", "dir")])

    assert landing.owns("WRITING/skill.md")


def test_a_rel_path_no_master_claims_is_not_owned():
    landing = LandingMap([Master(r"C:\skills\writing", "dir")])

    assert not landing.owns("editing/SKILL.md")
    assert not landing.owns("editing")


def test_one_map_answers_for_masters_of_both_types():
    landing = LandingMap([Master(r"C:\skills\writing", "dir"), Master(r"C:\docs\resume.docx", "file")])

    assert landing.owns("writing/SKILL.md")
    assert landing.owns("resume.docx")
    assert not landing.owns("resume.docx/inner.txt")


def test_split_names_the_master_and_keeps_the_original_casing_of_the_remainder():
    writing = Master(r"C:\skills\Writing", "dir")
    landing = LandingMap([writing])

    split = landing.split("WRITING/Examples/One.md")

    assert split.master == writing
    assert split.nested is True
    assert split.rest == "Examples/One.md"


def test_split_of_a_bare_landing_path_has_nothing_nested():
    landing = LandingMap([Master(r"C:\docs\resume.docx", "file")])

    split = landing.split("resume.docx")

    assert split.nested is False
    assert split.rest == ""


def test_split_of_an_unclaimed_rel_path_has_no_master():
    landing = LandingMap([Master(r"C:\skills\writing", "dir")])

    assert landing.split("editing/SKILL.md").master is None


def test_master_abs_maps_a_file_under_a_dir_master_back_to_the_master_side():
    landing = LandingMap([Master(r"C:\skills\writing", "dir")])

    assert landing.master_abs("writing/examples/one.md") == os.path.join(
        r"C:\skills\writing", "examples", "one.md"
    )


def test_master_abs_maps_a_file_masters_bare_filename_to_the_master_itself():
    landing = LandingMap([Master(r"C:\docs\resume.docx", "file")])

    assert landing.master_abs("resume.docx") == r"C:\docs\resume.docx"


@pytest.mark.parametrize(
    "rel_path",
    [
        "editing/SKILL.md",  # no master lands here
        "writing",  # a dir master's landing folder isn't a file
        "resume.docx/inner.txt",  # nothing nests under a file master
    ],
)
def test_master_abs_raises_for_a_rel_path_that_is_not_a_files_landing_path(rel_path):
    landing = LandingMap([Master(r"C:\skills\writing", "dir"), Master(r"C:\docs\resume.docx", "file")])

    with pytest.raises(ValueError):
        landing.master_abs(rel_path)


def test_master_basename_ignores_a_trailing_separator():
    assert master_basename("C:\skills\writing\\") == "writing"


def test_master_basename_key_ignores_case():
    assert master_basename_key(r"C:\skills\Writing") == master_basename_key(r"D:\other\WRITING")


def test_replica_abs_path_joins_a_slash_separated_rel_path_onto_the_replica_root():
    assert replica_abs_path(r"C:\replica", "writing/examples/one.md") == os.path.join(
        r"C:\replica", "writing", "examples", "one.md"
    )
