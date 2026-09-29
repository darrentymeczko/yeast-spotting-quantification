"""Picked photographs and per-plate dilution levels.

Per-plate rather than per-condition is the point. Every spot is divided by the
control on ITS OWN plate, so two plates of one treatment that grew to different
densities can each be scored at whichever row is actually readable without the
two becoming incomparable. These tests pin that down at each layer: the model,
the file, validation, and the dict handed to the pipeline.
"""

import pytest

from experiments import schema, validate
from experiments.model import QUANTIFY, TIMECOURSE, Condition, Experiment


def experiment(**kw) -> Experiment:
    e = Experiment(
        name="Set04",
        # Eight, matching the lab standard template these are validated against.
        strains=["WT BY", "ΔSDH8", "ΔTDH2", "ΔHCM1",
                 "ΔGPX1", "ΔSKN7", "ΔMXR2", "ΔSAM1"],
        control_slot=1,
        conditions=[Condition("GLY", "Glycerol"), Condition("K-OAc", "K acetate")],
        mode=QUANTIFY,
        photo_root="/data",
        **kw,
    )
    return e


# --- the model --------------------------------------------------------------


def test_a_pick_is_stored_per_condition_and_plate():
    e = experiment()
    e.set_pick("GLY", "1", "a.JPG")
    e.set_pick("GLY", "2", "b.JPG")
    assert e.pick("GLY", "1") == "a.JPG"
    assert e.pick("GLY", "2") == "b.JPG"
    assert e.pick("K-OAc", "1") is None


def test_plate_ids_are_compared_as_strings():
    """The template numbers plates as strings; callers may pass either."""
    e = experiment()
    e.set_pick("GLY", 1, "a.JPG")
    assert e.pick("GLY", "1") == "a.JPG"
    assert e.pick("GLY", 1) == "a.JPG"


def test_clearing_a_pick_removes_the_condition_when_it_empties():
    e = experiment()
    e.set_pick("GLY", "1", "a.JPG")
    e.set_pick("GLY", "1", None)
    assert e.picks == {}


def test_missing_picks_lists_what_is_left():
    e = experiment()
    e.set_pick("GLY", "1", "a.JPG")
    assert e.missing_picks(["1", "2"]) == [("GLY", "2"), ("K-OAc", "1"),
                                           ("K-OAc", "2")]


# --- dilution ---------------------------------------------------------------


def test_each_plate_carries_its_own_level():
    e = experiment()
    e.set_dilution_for("GLY", "1", "least")
    e.set_dilution_for("GLY", "2", "most")
    assert e.dilution_for("GLY", "1") == "least"
    assert e.dilution_for("GLY", "2") == "most"
    assert e.condition("GLY").dilution == {
        "mode": "plate", "choices": {"1": "least", "2": "most"}}


def test_a_condition_wide_level_still_reads_for_every_plate():
    """Migrated `spotting_config.json` entries carry the old shape."""
    e = experiment()
    e.condition("GLY").dilution = {"mode": "condition", "choice": "middle"}
    assert e.dilution_for("GLY", "1") == "middle"
    assert e.dilution_for("GLY", "9") == "middle"


def test_setting_one_plate_does_not_silently_change_the_others():
    """Expanding a shared choice must keep the plates that relied on it."""
    e = experiment()
    e.set_pick("GLY", "1", "a.JPG")
    e.set_pick("GLY", "2", "b.JPG")
    e.condition("GLY").dilution = {"mode": "condition", "choice": "middle"}

    e.set_dilution_for("GLY", "2", "most")
    assert e.dilution_for("GLY", "1") == "middle"
    assert e.dilution_for("GLY", "2") == "most"


def test_clearing_the_last_level_clears_the_condition():
    e = experiment()
    e.set_dilution_for("GLY", "1", "least")
    e.set_dilution_for("GLY", "1", None)
    assert e.condition("GLY").dilution is None


def test_an_unknown_condition_has_no_level():
    assert experiment().dilution_for("NOPE", "1") is None


# --- the file ---------------------------------------------------------------


def test_picks_round_trip():
    e = experiment()
    e.set_pick("GLY", "1", "sub/a.JPG")
    e.set_pick("K-OAc", "2", "b.JPG")
    e.set_dilution_for("GLY", "1", "least")

    again = schema.loads_experiment(schema.dumps_experiment(e))
    assert again.pick("GLY", "1") == "sub/a.JPG"
    assert again.pick("K-OAc", "2") == "b.JPG"
    assert again.dilution_for("GLY", "1") == "least"


def test_an_experiment_with_no_picks_writes_no_picks_key():
    import json

    d = json.loads(schema.dumps_experiment(experiment()))
    assert "picks" not in d["photos"]


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"GLY": "a.JPG"}, "plate -> file"),
        ({"GLY": {"1": 7}}, "expected a file path"),
    ],
)
def test_malformed_picks_are_refused(bad, message):
    import json

    d = json.loads(schema.dumps_experiment(experiment()))
    d["photos"]["picks"] = bad
    with pytest.raises(schema.ExperimentError, match=message):
        schema.from_dict(d)


# --- what the pipeline is handed --------------------------------------------


def test_a_level_resolves_to_the_rows_it_occupies():
    """What the pipeline actually needs: which rows to size the ROI to."""
    from experiments import geometry, run

    t = template()
    e = experiment()
    e.set_dilution_for("GLY", "1", 0)
    e.set_dilution_for("GLY", "2", 2)
    assert run.resolved_level(e, t, "GLY", "1") == 0
    assert run.resolved_level(e, t, "GLY", "2") == 2
    assert geometry.rows_for(t, "1", 0) == (1, 4)
    assert geometry.rows_for(t, "2", 2) == (3, 6)


def test_a_level_stored_as_a_name_still_resolves():
    """Files written before levels were indexed, and spotting_config.json."""
    from experiments import run

    e = experiment()
    e.condition("GLY").dilution = {"mode": "condition", "choice": "middle"}
    assert run.resolved_level(e, template(), "GLY", "1") == 1


def test_photo_refs_come_from_the_picks_when_there_are_any():
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    from experiments import run

    e = experiment()
    e.set_pick("GLY", "1", "a.JPG")
    e.set_pick("GLY", "2", "b.JPG")
    combos = run.to_photo_refs(e)
    assert set(combos) == {"Set04|GLY"}
    assert [r.plate for r in combos["Set04|GLY"]] == [1, 2]
    assert [r.path.name for r in combos["Set04|GLY"]] == ["a.JPG", "b.JPG"]


def test_a_non_numeric_plate_id_is_refused_rather_than_renumbered():
    """Renumbering would reassign biological replicates without saying so."""
    pytest.importorskip("numpy")
    from experiments import run

    e = experiment()
    e.set_pick("GLY", "left", "a.JPG")
    with pytest.raises(run.RunError, match="not a number"):
        run.to_photo_refs(e)


# --- validation -------------------------------------------------------------


def template():
    from plate_template import presets

    return presets.lab_standard_8x6()


def test_timecourse_mode_never_asks_for_picks():
    e = experiment()
    e.mode = TIMECOURSE
    codes = {i.code for i in validate.validate(e, template())}
    assert "no_photo_for_plate" not in codes


def test_a_pick_for_a_plate_the_template_lacks_is_reported():
    e = experiment()
    e.photo_root = ""
    e.set_pick("GLY", "7", "a.JPG")
    codes = {i.code for i in validate.validate(e, template())}
    assert "pick_for_unknown_plate" in codes


# --- the filename fallback, for migrated work -------------------------------


def flat_named() -> Experiment:
    """As `migrate.from_spotting_config` leaves one: no picks, flat filenames."""
    from experiments.profiles import flat_lab

    e = experiment()
    e.profile = flat_lab()
    return e


def test_filename_matching_is_a_note_not_an_error():
    """Migrated experiments were never picked in a window; their names say it."""
    e = flat_named()
    issues = validate.validate(e, template())
    assert [i.code for i in issues if i.is_error] == []
    assert "photos_read_from_filenames" in {i.code for i in issues}


def test_a_profile_that_cannot_read_the_layout_still_demands_picks():
    e = experiment()          # the default profile reads nothing
    codes = {i.code for i in validate.validate(e, template()) if i.is_error}
    assert "no_photo_for_plate" in codes


def test_a_folder_based_layout_does_not_earn_the_fallback():
    """A capture tree maps many photographs onto one condition and plate.

    Describing the tree completely still does not identify a single plate, so
    the photographs have to be picked.
    """
    from experiments.profiles import capture_tree

    e = experiment()
    e.profile = capture_tree()
    issues = validate.validate(e, template())
    assert "photos_read_from_filenames" not in {i.code for i in issues}
    assert "no_photo_for_plate" in {i.code for i in issues if i.is_error}


def test_one_pick_hands_over_to_the_picker():
    """No silent mixing: from then on the unfilled plates are errors."""
    e = flat_named()
    e.photo_root = ""
    e.set_pick("GLY", "1", "a.JPG")
    issues = validate.validate(e, template())
    assert "photos_read_from_filenames" not in {i.code for i in issues}
    assert "no_photo_for_plate" in {i.code for i in issues if i.is_error}
