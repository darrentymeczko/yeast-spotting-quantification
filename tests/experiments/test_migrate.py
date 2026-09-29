"""Migrating the pipelines' existing config files.

The fixtures here are copied from the real files: set 1's panel and its K-OAc
control override, set 9's null columns, and the two dilution shapes
(`mode: combo` with one choice, `mode: plate` with one per plate).
"""

import json

import pytest

from experiments import migrate, schema, validate
from experiments.migrate import MigrationError
from experiments.model import QUANTIFY, TIMECOURSE

SPOTTING_CONFIG = {
    "version": 2,
    "sets": {
        "1": {
            "strains": ["WT BY", "ΔATX1", "ΔWHI2", "ΔOXR1", "ΔPOS5", "ΔCTA1",
                        "ΔGTR1", "ΔSOD2"],
            "control_col": 1,
            "control_per_combo": False,
        },
        "9": {
            "strains": [None, "WT BY", "ΔMCR1", "ΔNQM1", "ΔCTT1", "ΔUBP2",
                        "ΔNTG1", None],
            "control_col": 2,
        },
    },
    "combo": {
        "1|K-OAc": {"exclude": [1], "control_col": 2,
                    "dilution": {"mode": "combo", "choice": "middle"}},
        "1|GLU": {"exclude": [], "control_col": 1,
                  "dilution": {"mode": "plate",
                               "choices": {"1": "least", "2": "middle"}}},
        "9|GLY": {"exclude": [], "control_col": 2,
                  "dilution": {"mode": "combo", "choice": "middle"}},
    },
    "nudge": {},
}

TIMECOURSE_CONFIG = {
    "strains": [None, "WT BY", "ΔMCR1", "ΔNQM1", "ΔCTT1", "ΔUBP2", "ΔNTG1", None],
    "control_col": 2,
    "exclude": [],
    "from_set": "9",
    "media": {
        "GLU": {"control_col": 2, "exclude": []},
        "GLY": {"control_col": 2, "exclude": []},
        "K-OAc": {"control_col": 3, "exclude": [5]},
    },
}


@pytest.fixture
def main_config(tmp_path):
    path = tmp_path / "Spotting Assays" / "spotting_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(SPOTTING_CONFIG, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "Set09"
    root.mkdir()
    (root / "timecourse_config.json").write_text(
        json.dumps(TIMECOURSE_CONFIG, ensure_ascii=False), encoding="utf-8"
    )
    return root


# --- the main config --------------------------------------------------------


def test_each_set_becomes_one_experiment(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    assert [e.name for e in experiments] == ["Set01 handpicked", "Set09 handpicked"]
    assert all(e.mode == QUANTIFY for e in experiments)


def test_the_panel_survives_including_its_empty_columns(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    nine = next(e for e in experiments if e.set_key == "9")
    assert nine.strains == TIMECOURSE_CONFIG["strains"]
    assert nine.filled_slots() == [2, 3, 4, 5, 6, 7]
    assert nine.control_slot == 2


def test_each_combination_becomes_a_condition(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    one = next(e for e in experiments if e.set_key == "1")
    assert sorted(one.condition_codes()) == ["GLU", "K-OAc"]


def test_the_per_combination_control_and_exclusions_survive(main_config):
    """Set 1 normalises K-OAc to slot 2 and drops slot 1 -- the WT BY case."""
    experiments, _ = migrate.from_spotting_config(main_config)
    one = next(e for e in experiments if e.set_key == "1")
    assert one.control_slot == 1
    assert one.control_for("GLU") == 1
    assert one.control_for("K-OAc") == 2
    assert one.exclude_for("K-OAc") == (1,)
    assert 1 not in one.scored_slots("K-OAc")


def test_the_dilution_mode_combo_is_renamed_condition(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    one = next(e for e in experiments if e.set_key == "1")
    assert one.condition("K-OAc").dilution == {"mode": "condition", "choice": "middle"}


def test_a_per_plate_dilution_choice_survives(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    one = next(e for e in experiments if e.set_key == "1")
    assert one.condition("GLU").dilution == {
        "mode": "plate",
        "choices": {"1": "least", "2": "middle"},
    }


def test_set_key_keeps_the_panels_apart_in_a_shared_folder(main_config):
    experiments, _ = migrate.from_spotting_config(main_config)
    assert {e.set_key for e in experiments} == {"1", "9"}
    assert len({e.photo_root for e in experiments}) == 1


def test_a_set_with_no_strains_is_skipped_with_a_note(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"sets": {"1": {"strains": [None, None]}}}),
                    encoding="utf-8")
    experiments, notes = migrate.from_spotting_config(path)
    assert experiments == []
    assert any("no strain names" in n for n in notes)


def test_a_migrated_experiment_round_trips(main_config, tmp_path):
    experiments, _ = migrate.from_spotting_config(main_config)
    out = tmp_path / "one.json"
    schema.save(experiments[0], out, bump_revision=False)
    again = schema.load(out)
    assert again.strains == experiments[0].strains
    assert again.control_for("K-OAc") == experiments[0].control_for("K-OAc")


# --- a capture tree ---------------------------------------------------------


def test_a_capture_tree_becomes_a_timecourse_experiment(tree):
    e, _ = migrate.from_capture_tree(tree)
    assert e.name == "Set09"
    assert e.mode == TIMECOURSE
    assert e.photo_root == str(tree)
    assert e.set_key is None  # the folder is the set
    assert sorted(e.condition_codes()) == ["GLU", "GLY", "K-OAc"]


def test_the_per_medium_control_wins_and_says_so(tree):
    """The more specific statement wins; the disagreement is never silent."""
    e, notes = migrate.from_capture_tree(tree)
    assert e.control_slot == 2
    assert e.control_for("GLU") == 2
    assert e.control_for("K-OAc") == 3
    assert any("K-OAc" in n and "slot 3" in n for n in notes)


def test_per_medium_exclusions_survive(tree):
    e, _ = migrate.from_capture_tree(tree)
    assert e.exclude_for("K-OAc") == (5,)
    assert e.exclude_for("GLU") == ()


def test_the_panel_is_fetched_from_the_main_config_when_absent(tree, main_config):
    (tree / "timecourse_config.json").write_text(
        json.dumps({"from_set": "9", "media": {"GLU": {}}}), encoding="utf-8"
    )
    e, notes = migrate.from_capture_tree(tree, main_config=main_config)
    assert e.strains == SPOTTING_CONFIG["sets"]["9"]["strains"]
    assert e.control_slot == 2
    assert any("spotting_config.json" in n for n in notes)


def test_no_panel_anywhere_is_a_clear_error(tmp_path):
    root = tmp_path / "Set42"
    root.mkdir()
    (root / "timecourse_config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MigrationError, match="no strain panel"):
        migrate.from_capture_tree(root)


def test_finding_capture_trees_one_level_down(tmp_path, tree):
    assert migrate.find_capture_trees(tree) == [tree]
    assert migrate.find_capture_trees(tree.parent) == [tree]
    assert migrate.find_capture_trees(tmp_path / "nothing") == []


def test_set_ids_are_read_the_way_the_pipeline_reads_them():
    assert migrate.set_id_from_name("Set01") == "1"
    assert migrate.set_id_from_name("Set 1 - Practice") == "1"
    assert migrate.set_id_from_name("set_10") == "10"
    assert migrate.set_id_from_name("Take02") is None


def test_broken_json_names_the_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(MigrationError, match="bad.json"):
        migrate.from_spotting_config(path)


# --- the two migrations together -------------------------------------------


def test_the_two_migrations_do_not_collide(main_config, tree):
    """Same panel, two experiments: they must not overwrite each other."""
    handpicked, _ = migrate.from_spotting_config(main_config)
    timecourse, _ = migrate.from_capture_tree(tree)
    names = {e.name for e in handpicked} | {timecourse.name}
    assert len(names) == 3
    assert timecourse.name not in {e.name for e in handpicked}


def test_a_migrated_timecourse_experiment_validates_against_the_lab_template(tree):
    from plate_template import presets

    e, _ = migrate.from_capture_tree(tree, template_id="")
    errors = [i for i in validate.validate(e, presets.lab_standard_8x6()) if i.is_error]
    assert [i.code for i in errors] == []
