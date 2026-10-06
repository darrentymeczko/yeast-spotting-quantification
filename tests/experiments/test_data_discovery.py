"""Real naming patterns from PhotoVariants.txt, without requiring lab data."""

import pytest

from experiments import intake, schema
from experiments.gui.controller import ExperimentController
from experiments.model import Experiment, QUANTIFY, TIMECOURSE
from experiments.names import treatment_label
from experiments.profiles import FacetRule, NamingProfile, apply_profile, profile_from_dict, profile_to_dict


def discover(*paths, mode=TIMECOURSE):
    files = [intake.ImageFile(p, tuple(p.split("/"))) for p in paths]
    profile, _ = intake.infer_profile(files, mode=mode)
    experiment = Experiment(mode=mode, profile=profile)
    return experiment, files, intake.resolve(experiment, files)


def test_temperature_medium_and_stress_are_separate_combinations():
    e, files, result = discover(*[
        f"{temp}/glucose/{stress} - 17h.JPG"
        for temp in (30, 37) for stress in ("H2O2", "NaAsO", "just glucose")
    ])
    assert len({r.condition for r in result.rows}) == 6
    assert {r.timepoint for r in result.rows} == {17}
    assert all(r.plate is None for r in result.rows)
    assert all("glucose" in r.condition_label.lower() for r in result.rows)
    assert all("Hours" in r.timepoint_label and "/" not in r.timepoint_label for r in result.rows)


def test_treatments_are_available_without_plate_or_timepoint():
    e, files, result = discover("30C/Glucose/_9.JPG", "37C/Glucose/_9_1.JPG")
    ctl = ExperimentController(e)
    ctl.files, ctl.resolution = files, result
    assert result.usable() == []
    assert ctl.adopt_found_conditions()
    assert len(ctl.experiment.conditions) == 2
    assert all("plate" in r.missing and "timepoint" in r.missing for r in ctl.resolution.rows)


def test_camera_numbers_and_replicate_groups_never_assign_plates():
    _, _, result = discover("Glucose/16 hours/R1/_9.JPG", "Glucose/16 hours/R2/_9_1.JPG",
                            "Glucose/16 hours/R1/1a.JPG", "Glucose/16 hours/R2/R3+4.JPG")
    assert all(r.plate is None for r in result.rows)
    assert {r.condition for r in result.rows} == {"GLU"}


def test_explicit_plate_and_hours_can_appear_in_filenames():
    e, files, result = discover("glucose P1 17h.JPG", "glucose P2 17h.JPG")
    assert len(result.usable()) == 2
    assert {r.plate for r in result.rows} == {1, 2}
    restored = profile_from_dict(profile_to_dict(e.profile))
    assert apply_profile(files[0].parts, restored) == apply_profile(files[0].parts, e.profile)


@pytest.mark.parametrize("paths", [
    ("30/glucose/_9.JPG", "37/glucose/_9.JPG"),
    ("30/glucose/Plate 1/_9.JPG", "37/glucose/Plate 1/_9.JPG"),
    ("morning/glucose R1.JPG", "next day/glucose R2.JPG"),
    ("2026-01-28/Glucose/_9.JPG", "2026-01-29/Glucose/_9.JPG"),
])
def test_temperatures_dates_and_sessions_are_not_elapsed_hours(paths):
    _, _, result = discover(*paths)
    assert all(r.timepoint is None for r in result.rows)


def test_distinct_doses_and_truncated_labels_never_pool():
    _, _, result = discover("Glycerol 37/Plate 1/_9.JPG", "Glycerol NaAsO2/Plate 1/_9.JPG",
                            "h2o2 250uM 30 R1.JPG", "h2o2 500uM 30 R1.JPG")
    assert len({r.condition for r in result.rows}) == 4


def test_new_photo_labels_do_not_collide_with_saved_codes():
    e, files, _ = discover("Glycerol 37/Plate 1/_9.JPG")
    new = intake.ImageFile("Glycerol 30/Plate 1/_9.JPG", ("Glycerol 30", "Plate 1", "_9.JPG"))
    rows = intake.resolve(e, files + [new]).rows
    assert rows[0].condition != rows[1].condition


def test_folder_order_does_not_change_composite_suggestion():
    assert treatment_label(("30C", "Glucose", "_9.JPG")) == treatment_label(("Glucose", "30C", "_9.JPG"))


def test_flat_codes_are_suggested_even_when_timecourse_hours_are_missing():
    _, _, result = discover("1.1GLU.JPG", "1.2GLU.JPG")
    assert {r.condition for r in result.rows} == {"GLU"}
    assert {r.plate for r in result.rows} == {1, 2}
    assert all(r.missing == ("timepoint",) for r in result.rows)


def test_unknown_treatment_and_signs_are_retained():
    assert "DrugX" in treatment_label(("30", "DrugX", "_9.JPG"))
    assert treatment_label(("Glucose", "Met-.JPG")) != treatment_label(("Glucose", "Met+.JPG"))
    assert treatment_label(("_9_12.JPG",)) == ""
    assert "132" in treatment_label(("mg 132 30 1.JPG",))
    assert "Glycerol" in treatment_label(("JC7 30 May 29 Glycerol.JPG",))
    assert "May" not in treatment_label(("JC7 30 May 29 Glycerol.JPG",))


def test_conflicting_explicit_hours_stay_unresolved():
    profile = NamingProfile(rules=(FacetRule("timepoint", "path", -1, (r"(\d+)h",), "hours"),))
    assert "timepoint" not in apply_profile(("24h", "glucose 48h.JPG"), profile)


def test_rename_preserves_assignments_labels_and_survives_undo_and_save():
    e, files, result = discover("30/glucose_1.JPG", "37/glucose_2.JPG", mode=QUANTIFY)
    ctl = ExperimentController(e)
    ctl.files, ctl.resolution = files, result
    ctl.adopt_found_conditions()
    old = e.conditions[0].code
    e.conditions[0].control_slot = 2
    e.conditions[0].exclude = (3,)
    ctl.set_pick(old, "1", files[0].relpath)
    ctl.override(files[0].relpath, "condition", old)
    ctl.rename_condition(old, "GLU30", "Glucose at 30 C")
    assert e.pick("GLU30", "1") == files[0].relpath
    assert e.condition("GLU30").exclude == (3,)
    restored = schema.from_dict(schema.to_dict(e))
    assert intake.resolve(restored, files).rows[0].condition == "GLU30"
    ctl.undo()
    assert ctl.experiment.pick(old, "1") == files[0].relpath
    ctl.redo()
    assert ctl.experiment.condition("GLU30").control_slot == 2


def test_same_folder_reread_preserves_corrections_and_root_undo_rescans(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "glucose.JPG").write_bytes(b"x")
    (second / "glycerol.JPG").write_bytes(b"x")
    ctl = ExperimentController(Experiment())
    ctl.set_photo_root(first)
    ctl.override("glucose.JPG", "plate", 2)
    ctl.set_photo_root(first)
    assert ctl.experiment.overrides["glucose.JPG"]["plate"] == 2
    ctl.set_photo_root(second)
    ctl.undo()
    assert [f.relpath for f in ctl.files] == ["glucose.JPG"]
    assert ctl.resolution.rows[0].plate == 2
    ctl.redo()
    assert [f.relpath for f in ctl.files] == ["glycerol.JPG"]


def test_ignored_and_other_sets_are_not_adopted():
    e, files, _ = discover("1.1GLU.JPG", "2.1GLY.JPG", mode=QUANTIFY)
    e.set_key = "1"
    e.ignored = ["1.1GLU.JPG"]
    ctl = ExperimentController(e)
    ctl.files = files
    ctl.reresolve()
    assert not ctl.adopt_found_conditions()


def test_a_manually_disabled_rule_stays_disabled_after_reread(tmp_path):
    (tmp_path / "glucose.JPG").write_bytes(b"x")
    ctl = ExperimentController(Experiment())
    ctl.set_photo_root(tmp_path)
    ctl.set_profile(NamingProfile("Manual organization"))
    ctl.rescan()
    assert ctl.experiment.profile.rules == ()


def test_name_only_rename_keeps_identifier_and_links_after_roundtrip():
    e, files, result = discover("1.1GLU.JPG", "1.2GLU.JPG", mode=QUANTIFY)
    ctl = ExperimentController(e)
    ctl.files, ctl.resolution = files, result
    ctl.adopt_found_conditions()
    ctl.set_pick("GLU", "1", files[0].relpath)
    ctl.assign_treatment([files[1].relpath], "GLU")
    ctl.set_condition_name("GLU", "Glucose without stress")
    restored = schema.from_dict(schema.to_dict(e))
    assert restored.condition("GLU").display() == "Glucose without stress"
    assert restored.pick("GLU", "1") == files[0].relpath
    assert {r.condition for r in intake.resolve(restored, files).rows} == {"GLU"}
    assert restored.overrides[files[1].relpath]["condition"] == "GLU"
    ctl.undo()
    assert ctl.experiment.condition("GLU").display() == "GLU"


def test_generated_identifiers_are_unique_and_names_are_reused():
    ctl = ExperimentController(Experiment())
    first = ctl.add_named_condition("Glycerol 30 degrees")
    second = ctl.add_named_condition("Glycerol 37 degrees")
    assert first != second
    assert ctl.add_named_condition("  glycerol   30 degrees ") == first
    assert len(ctl.experiment.conditions) == 2
    with pytest.raises(ValueError, match="already has"):
        ctl.set_condition_name(second, "Glycerol 30 degrees")


def test_assigning_new_name_and_undoing_is_one_operation():
    e, files, result = discover("_9.JPG", mode=QUANTIFY)
    ctl = ExperimentController(e)
    ctl.files, ctl.resolution = files, result
    ctl.assign_treatment(["_9.JPG"], "Glucose + 250 µM H2O2")
    code = ctl.experiment.conditions[0].code
    assert ctl.resolution.rows[0].condition == code
    ctl.undo()
    assert ctl.experiment.conditions == []
    assert ctl.experiment.overrides == {}
    ctl.redo()
    assert ctl.condition_name(code) == "Glucose + 250 µM H2O2"
    ctl.assign_treatment(["_9.JPG"], "")
    assert ctl.experiment.overrides == {}
