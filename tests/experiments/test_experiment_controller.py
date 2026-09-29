"""The controller: every mutation, and undo over all of them.

The controller is where a hand correction, a re-scan and an undo can interfere
with each other, so those interactions are what is tested here rather than each
setter on its own.
"""

import pytest

from experiments import intake, schema
from experiments.gui.controller import ExperimentController, UndoStack
from experiments.model import QUANTIFY, TIMECOURSE, Condition, Experiment
from experiments.profiles import capture_tree


@pytest.fixture
def tree(tmp_path):
    """A small capture tree, two plates at two timepoints on one medium."""
    for hours in (16, 40):
        for plate in (1, 2):
            d = tmp_path / f"{hours} Hours" / "Glucose" / f"Plate {plate} (Rep 1+2)"
            d.mkdir(parents=True)
            (d / "_9.JPG").write_bytes(b"x")
            (d / "_9_1.JPG").write_bytes(b"x")
    return tmp_path


@pytest.fixture
def ctl(tree):
    e = Experiment(
        name="Set01",
        strains=["WT BY", "ΔATX1"],
        control_slot=1,
        conditions=[Condition("GLU", "Glucose")],
        mode=TIMECOURSE,
        photo_root=str(tree),
        profile=capture_tree(),
    )
    controller = ExperimentController(e)
    controller.rescan()
    return controller


# --- the undo stack ---------------------------------------------------------


def test_undo_and_redo_walk_the_stack():
    stack = UndoStack()
    stack.push({"n": 1}, "first")
    stack.push({"n": 2}, "second")
    assert stack.undo_label == "second"

    snapshot, label = stack.undo({"n": 3})
    assert snapshot == {"n": 2} and label == "second"
    assert stack.can_redo
    assert stack.redo({"n": 2})[0] == {"n": 3}


def test_a_new_edit_discards_the_redo_branch():
    stack = UndoStack()
    stack.push({"n": 1}, "a")
    stack.undo({"n": 2})
    stack.push({"n": 3}, "b")
    assert not stack.can_redo


def test_the_stack_is_bounded():
    stack = UndoStack(limit=3)
    for i in range(10):
        stack.push({"n": i}, str(i))
    assert len(stack._undo) == 3


# --- dirty tracking ---------------------------------------------------------


def test_a_fresh_controller_is_clean(ctl):
    assert not ctl.dirty


def test_an_edit_makes_it_dirty_and_saving_makes_it_clean(ctl):
    ctl.set_strain(2, "ΔSOD2")
    assert ctl.dirty
    ctl.mark_saved()
    assert not ctl.dirty


def test_setting_a_value_to_what_it_already_is_records_nothing(ctl):
    assert ctl.set_strain(1, "WT BY") is False
    assert not ctl.undo_stack.can_undo
    assert not ctl.dirty


# --- edits ------------------------------------------------------------------


def test_naming_a_slot_beyond_the_panel_grows_it(ctl):
    ctl.set_strain(5, "ΔGTR1")
    assert ctl.experiment.strains == ["WT BY", "ΔATX1", None, None, "ΔGTR1"]


def test_adding_a_duplicate_condition_is_refused(ctl):
    assert ctl.add_condition("GLU") is False
    assert len(ctl.experiment.conditions) == 1


def test_conditions_found_in_the_photos_can_be_adopted(tree):
    e = Experiment(mode=TIMECOURSE, photo_root=str(tree), profile=capture_tree())
    controller = ExperimentController(e)
    controller.rescan()
    assert controller.adopt_found_conditions()
    assert e.condition_codes() == ["GLU"]
    assert e.condition("GLU").label == "Glucose"


def test_changing_mode_changes_what_resolves(ctl):
    """A timepoint is required for a time course and not for handpicked work."""
    ctl.experiment.profile = capture_tree()
    ctl.set_mode(QUANTIFY)
    assert ctl.experiment.mode == QUANTIFY
    assert len(ctl.resolution.usable()) == 8


# --- photos -----------------------------------------------------------------


def test_scanning_reads_every_photo(ctl):
    assert len(ctl.files) == 8
    assert len(ctl.resolution.usable()) == 8
    assert not ctl.scan_error


def test_a_missing_folder_is_reported_not_raised(ctl, tmp_path):
    ctl.experiment.photo_root = str(tmp_path / "gone")
    ctl.rescan()
    assert "not found" in ctl.scan_error
    assert ctl.resolution is None


def test_an_override_survives_a_rescan(ctl):
    target = ctl.files[0].relpath
    ctl.override(target, "plate", 2)
    ctl.rescan()
    row = next(r for r in ctl.resolution.rows if r.relpath == target)
    assert row.plate == 2


def test_re_reading_the_layout_does_not_discard_corrections(ctl):
    target = ctl.files[0].relpath
    ctl.override(target, "plate", 2)
    ctl.rescan(infer=True)
    row = next(r for r in ctl.resolution.rows if r.relpath == target)
    assert row.plate == 2


def test_bulk_assignment_is_one_undo_step(ctl):
    paths = [f.relpath for f in ctl.files[:4]]
    ctl.override_many(paths, "plate", 2)
    assert ctl.undo_stack.undo_label == "Set plate on 4 photo(s)"
    ctl.undo()
    assert ctl.experiment.overrides == {}


def test_bulk_assignment_counts_a_generator_correctly(ctl):
    ctl.override_many((f.relpath for f in ctl.files[:3]), "plate", 1)
    assert "3 photo(s)" in ctl.undo_stack.undo_label


def test_clearing_an_override_removes_the_entry_entirely(ctl):
    target = ctl.files[0].relpath
    ctl.override(target, "plate", 2)
    ctl.override(target, "plate", None)
    assert ctl.experiment.overrides == {}


def test_ignoring_photos_takes_them_out_of_the_usable_set(ctl):
    paths = [f.relpath for f in ctl.files[:2]]
    ctl.set_ignored(paths, True)
    assert len(ctl.resolution.usable()) == 6
    ctl.set_ignored(paths, False)
    assert len(ctl.resolution.usable()) == 8


def test_changing_the_photo_folder_clears_stale_corrections(ctl, tmp_path):
    ctl.override(ctl.files[0].relpath, "plate", 2)
    other = tmp_path / "elsewhere"
    (other / "16 Hours" / "Glucose" / "Plate 1").mkdir(parents=True)
    (other / "16 Hours" / "Glucose" / "Plate 1" / "_9.JPG").write_bytes(b"x")
    ctl.set_photo_root(other)
    assert ctl.experiment.overrides == {}
    assert ctl.experiment.ignored == []


# --- undo across the lot ----------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        lambda c: c.set_strain(2, "changed"),
        lambda c: c.set_control(2),
        lambda c: c.add_condition("GLY", "Glycerol"),
        lambda c: c.remove_condition("GLU"),
        lambda c: c.set_condition_control("GLU", 2),
        lambda c: c.set_condition_exclude("GLU", [2]),
        lambda c: c.set_mode(QUANTIFY),
        lambda c: c.set_name("Renamed"),
        lambda c: c.set_set_key("9"),
    ],
    ids=["strain", "control", "add_condition", "remove_condition",
         "condition_control", "exclude", "mode", "name", "set_key"],
)
def test_every_edit_can_be_undone(ctl, action):
    before = schema.to_dict(ctl.experiment)
    assert action(ctl) is True
    assert schema.to_dict(ctl.experiment) != before
    ctl.undo()
    assert schema.to_dict(ctl.experiment) == before


def test_undo_then_redo_returns_to_the_edited_state(ctl):
    ctl.set_strain(2, "ΔSOD2")
    after = schema.to_dict(ctl.experiment)
    ctl.undo()
    ctl.redo()
    assert schema.to_dict(ctl.experiment) == after


def test_undo_re_resolves_the_photos(ctl):
    target = ctl.files[0].relpath
    ctl.override(target, "plate", 2)
    ctl.undo()
    row = next(r for r in ctl.resolution.rows if r.relpath == target)
    assert row.plate == 1


def test_replacing_the_experiment_drops_the_history(ctl):
    ctl.set_strain(2, "x")
    ctl.replace(Experiment(name="Other"))
    assert not ctl.undo_stack.can_undo
    assert not ctl.dirty


# --- the template binding ---------------------------------------------------


def test_binding_a_template_grows_the_panel_to_fit(ctl, tmp_path):
    from plate_template import presets, schema as tschema

    path = tmp_path / "t.json"
    tschema.save(presets.lab_standard_8x6(), path, bump_revision=False)

    assert ctl.bind_template(path)
    assert ctl.experiment.slot_count() == 8
    assert ctl.experiment.strains[:2] == ["WT BY", "ΔATX1"]
    assert ctl.template is not None


def test_binding_stores_a_path_relative_to_the_repo(ctl):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    ctl.bind_template(repo / "plate_template" / "templates" / "lab_standard_8x6.json")
    assert ctl.experiment.template_path == (
        "plate_template/templates/lab_standard_8x6.json")


def test_a_broken_template_is_reported_not_raised(ctl, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ctl.bind_template(bad) is False
    assert ctl.template_error


def test_issues_combine_the_experiment_and_its_photos(ctl):
    """One list, from both sources -- a problem is visible from any tab."""
    codes = {i.code for i in ctl.issues()}
    assert "summary" in codes        # validate: the experiment itself
    assert "no_template" in codes    # validate: nothing bound yet

    # A photo-side check must land in the same list. This tree has both plates
    # at every timepoint, so the complete-group check passes silently; drop
    # plate 2 from ONE sitting and the photo-side finding must appear here.
    ctl.set_ignored(_plate_two_at(ctl, "16 Hours"), True)
    assert "incomplete_group" in {i.code for i in ctl.issues()}


def test_photo_findings_disappear_once_fixed(ctl):
    dropped = _plate_two_at(ctl, "16 Hours")
    ctl.set_ignored(dropped, True)
    assert "incomplete_group" in {i.code for i in ctl.issues()}
    ctl.set_ignored(dropped, False)
    assert "incomplete_group" not in {i.code for i in ctl.issues()}


def test_a_plate_missing_everywhere_is_not_reported_as_incomplete(ctl):
    """Without a template there is nothing to say a plate SHOULD be there.

    The expected plate set falls back to what the photos actually hold, so a
    two-plate design photographed as one plate reads as a one-plate design
    rather than as every sitting being broken. Binding a template is what makes
    the stronger statement possible.
    """
    ctl.set_ignored([f.relpath for f in ctl.files if "Plate 2" in f.relpath], True)
    assert "incomplete_group" not in {i.code for i in ctl.issues()}


def _plate_two_at(ctl, timepoint: str) -> list[str]:
    return [f.relpath for f in ctl.files
            if "Plate 2" in f.relpath and timepoint in f.relpath]
