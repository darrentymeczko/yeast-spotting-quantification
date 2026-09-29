"""Round-tripping experiments, and refusing files that would mislead."""

import json

import pytest

from experiments import profiles, schema
from experiments.model import KIND, SCHEMA_VERSION, QUANTIFY, TIMECOURSE, Condition, Experiment
from experiments.schema import ExperimentError


def sample() -> Experiment:
    return Experiment(
        name="Set01",
        template_id="9f2c3a1e-7d44-4b8a-8c31-6e0f5a2b1d90",
        template_path="plate_template/templates/lab_standard_8x6.json",
        # Real strain names: the Greek deltas are the reason this file is
        # written with ensure_ascii=False.
        strains=["WT BY", "ΔATX1", "ΔWHI2", None, "ΔPOS5", "ΔCTA1", "ΔGTR1", "ΔSOD2"],
        control_slot=1,
        conditions=[
            Condition("GLU", "Glucose"),
            Condition("K-OAc", "Potassium Acetate", control_slot=2, exclude=(3,)),
        ],
        mode=TIMECOURSE,
        photo_root=r"D:\Data\Set01",
        set_key="1",
        profile=profiles.capture_tree(),
        overrides={"40 Hours/Glucose/Plate 1/_9.JPG": {"plate": 2}},
        ignored=["16 Hours/Glucose/Plate 1/blurred.JPG"],
        id="abc-123",
        created="2026-09-12",
    )


def test_round_trip_preserves_everything():
    original = sample()
    again = schema.loads_experiment(schema.dumps_experiment(original))

    assert again.name == original.name
    assert again.strains == original.strains
    assert again.control_slot == original.control_slot
    assert again.mode == original.mode
    assert again.photo_root == original.photo_root
    assert again.set_key == original.set_key
    assert again.overrides == original.overrides
    assert again.ignored == original.ignored
    assert again.template_id == original.template_id
    assert again.profile.rules == original.profile.rules
    assert again.profile.aliases == original.profile.aliases
    assert [(c.code, c.label, c.control_slot, c.exclude) for c in again.conditions] == [
        (c.code, c.label, c.control_slot, c.exclude) for c in original.conditions
    ]


def test_the_file_declares_what_it_is():
    d = json.loads(schema.dumps_experiment(sample()))
    assert d["kind"] == KIND
    assert d["schema_version"] == SCHEMA_VERSION


def test_strain_names_are_written_readably_not_escaped():
    text = schema.dumps_experiment(sample())
    assert "ΔATX1" in text
    assert "\\u0394" not in text


def test_saving_is_atomic_and_bom_free(tmp_path):
    path = tmp_path / "Set01.spotexp.json"
    e = sample()
    schema.save(e, path)
    assert path.read_bytes()[:1] != b"\xef"
    assert not list(tmp_path.glob("*.tmp"))
    assert schema.load(path).name == "Set01"


def test_saving_bumps_the_revision_unless_told_not_to(tmp_path):
    e = sample()
    schema.save(e, tmp_path / "a.json")
    assert e.revision == 2
    schema.save(e, tmp_path / "a.json", bump_revision=False)
    assert e.revision == 2


def test_a_file_with_a_bom_still_loads(tmp_path):
    path = tmp_path / "notepad.json"
    path.write_text(schema.dumps_experiment(sample()), encoding="utf-8-sig")
    assert schema.load(path).name == "Set01"


def test_a_newer_schema_is_refused_rather_than_half_read():
    d = json.loads(schema.dumps_experiment(sample()))
    d["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ExperimentError, match="newer than this tool understands"):
        schema.from_dict(d)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.pop("mode"), "missing required key 'mode'"),
        (lambda d: d.update(mode="somehow"), "expected one of"),
        (lambda d: d.update(strains=["a", 7]), r"strains\[1\]"),
        (lambda d: d.pop("schema_version"), "schema_version"),
        (lambda d: d.update(control_slot="one"), "expected int"),
    ],
)
def test_malformed_files_say_what_and_where(mutate, message):
    d = json.loads(schema.dumps_experiment(sample()))
    mutate(d)
    with pytest.raises(ExperimentError, match=message):
        schema.from_dict(d)


def test_duplicate_condition_codes_are_refused():
    """Codes name result folders; two of one would overwrite each other."""
    d = json.loads(schema.dumps_experiment(sample()))
    d["conditions"].append({"code": "GLU", "label": "Glucose again"})
    with pytest.raises(ExperimentError, match="more than once"):
        schema.from_dict(d)


def test_a_blank_condition_code_is_refused():
    d = json.loads(schema.dumps_experiment(sample()))
    d["conditions"][0]["code"] = "  "
    with pytest.raises(ExperimentError, match="must not be blank"):
        schema.from_dict(d)


def test_an_unknown_dilution_mode_is_refused():
    d = json.loads(schema.dumps_experiment(sample()))
    d["conditions"][0]["dilution"] = {"mode": "vibes", "choice": "middle"}
    with pytest.raises(ExperimentError, match="dilution.mode"):
        schema.from_dict(d)


def test_a_quantify_dilution_choice_round_trips():
    e = sample()
    e.mode = QUANTIFY
    e.conditions[0].dilution = {"mode": "plate", "choices": {"1": "least", "2": "middle"}}
    again = schema.loads_experiment(schema.dumps_experiment(e))
    assert again.conditions[0].dilution == {
        "mode": "plate",
        "choices": {"1": "least", "2": "middle"},
    }


def test_blank_strain_names_become_empty_slots():
    d = json.loads(schema.dumps_experiment(sample()))
    d["strains"][1] = "   "
    assert schema.from_dict(d).strains[1] is None


def test_reading_a_missing_file_names_it(tmp_path):
    with pytest.raises(ExperimentError, match="could not read"):
        schema.load(tmp_path / "nope.json")


def test_reading_invalid_json_names_the_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ExperimentError, match="broken.json"):
        schema.load(path)


# --- model behaviour --------------------------------------------------------


def test_the_per_condition_control_overrides_the_experiment_one():
    e = sample()
    assert e.control_for("GLU") == 1
    assert e.control_for("K-OAc") == 2
    assert e.control_for("nonesuch") == 1


def test_scored_slots_drop_excluded_but_keep_the_control():
    e = sample()
    assert 4 not in e.scored_slots("GLU")  # slot 4 has no strain
    assert e.scored_slots("GLU") == [1, 2, 3, 5, 6, 7, 8]
    assert e.scored_slots("K-OAc") == [1, 2, 5, 6, 7, 8]
    assert e.control_for("K-OAc") in e.scored_slots("K-OAc")


def test_naming_a_slot_beyond_the_panel_grows_it():
    e = Experiment()
    e.set_strain(3, "ΔSOD2")
    assert e.strains == [None, None, "ΔSOD2"]
    assert e.filled_slots() == [3]


def test_slots_are_one_based():
    e = sample()
    assert e.strain(1) == "WT BY"
    assert e.strain(0) is None
    assert e.strain(99) is None
    with pytest.raises(ValueError, match="1-based"):
        e.set_strain(0, "x")
