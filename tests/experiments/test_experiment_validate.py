"""What validation must catch.

The tests worth reading are the ones about controls. A missing control does not
crash anything -- it silently divides every strain by nothing, or by a strain
that was never meant to be the reference, and the result still looks like a
result. Those are errors, and they are asserted to be errors.
"""

import pytest

from experiments import profiles, validate
from experiments.model import QUANTIFY, TIMECOURSE, Condition, Experiment
from experiments.validate import Severity, blocking
from plate_template import presets


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def error_codes(issues) -> set[str]:
    return {i.code for i in issues if i.is_error}


def good() -> Experiment:
    return Experiment(
        name="Set01",
        template_id="",
        strains=[f"s{i}" for i in range(1, 9)],
        control_slot=1,
        conditions=[Condition("GLU", "Glucose"), Condition("K-OAc", "Potassium Acetate")],
        mode=TIMECOURSE,
        photo_root="/data/Set01",
        profile=profiles.capture_tree(),
    )


def template():
    return presets.lab_standard_8x6()


def test_a_complete_experiment_has_no_errors():
    assert blocking(validate.validate(good(), template())) == []


def test_the_summary_is_always_present():
    issues = validate.validate(good(), template())
    assert "summary" in codes(issues)
    assert issues[-1].severity is Severity.INFO


# --- controls ---------------------------------------------------------------


def test_no_control_designated_is_an_error():
    e = good()
    e.control_slot = None
    assert "no_control" in error_codes(validate.validate(e, template()))


def test_a_control_slot_with_no_strain_is_an_error():
    e = good()
    e.strains[0] = None
    assert "control_slot_empty" in error_codes(validate.validate(e, template()))


def test_a_condition_excluding_its_own_control_is_an_error():
    """The quiet one: there would be nothing left to normalise to."""
    e = good()
    e.conditions[1].control_slot = 2
    e.conditions[1].exclude = (2,)
    issues = validate.validate(e, template())
    assert "control_excluded" in error_codes(issues)
    bad = next(i for i in issues if i.code == "control_excluded")
    assert bad.condition == "K-OAc" and bad.slots == (2,)


def test_a_per_condition_control_pointing_at_an_empty_slot_is_an_error():
    e = good()
    e.conditions[1].control_slot = 4
    e.strains[3] = None
    issues = [i for i in validate.validate(e, template()) if i.code == "control_slot_empty"]
    assert any(i.condition == "K-OAc" for i in issues)


def test_differing_controls_are_reported_but_not_blocking():
    """Intended when a strain does not grow on one medium -- the K-OAc case."""
    e = good()
    e.conditions[1].control_slot = 2
    issues = validate.validate(e, template())
    assert "control_differs_between_conditions" in codes(issues)
    assert blocking(issues) == []


def test_a_control_outside_the_template_is_an_error():
    e = good()
    e.strains.append("s9")
    e.control_slot = 9
    assert "control_not_in_template" in error_codes(validate.validate(e, template()))


# --- panel ------------------------------------------------------------------


def test_an_empty_panel_is_an_error():
    e = good()
    e.strains = [None] * 8
    assert "no_strains" in error_codes(validate.validate(e, template()))


def test_empty_slots_are_a_warning_with_their_positions():
    e = good()
    e.strains[3] = None
    issue = next(i for i in validate.validate(e, template()) if i.code == "empty_slots")
    assert issue.severity is Severity.WARNING
    assert issue.slots == (4,)


def test_a_repeated_strain_name_is_flagged():
    e = good()
    e.strains[4] = e.strains[3]
    issue = next(
        i for i in validate.validate(e, template()) if i.code == "duplicate_strain"
    )
    assert issue.slots == (4, 5)
    assert "not pooled" in issue.message


# --- conditions -------------------------------------------------------------


def test_no_conditions_is_an_error():
    e = good()
    e.conditions = []
    assert "no_conditions" in error_codes(validate.validate(e, template()))


def test_excluding_a_slot_outside_the_panel_is_an_error():
    e = good()
    e.conditions[0].exclude = (99,)
    assert "exclude_out_of_range" in error_codes(validate.validate(e, template()))


def test_excluding_everything_but_the_control_leaves_nothing_to_test():
    e = good()
    e.conditions[0].exclude = tuple(range(2, 9))
    issue = next(
        i for i in validate.validate(e, template()) if i.code == "nothing_to_compare"
    )
    assert issue.condition == "GLU"


def test_quantify_mode_needs_a_photograph_for_every_plate():
    e = good()
    e.mode = QUANTIFY
    issues = [i for i in validate.validate(e, template())
              if i.code == "no_photo_for_plate"]
    # Two conditions, two plates in the lab standard template.
    assert len(issues) == 4
    assert {i.condition for i in issues} == {"GLU", "K-OAc"}


def test_the_dilution_is_asked_for_once_a_photograph_is_chosen(tmp_path):
    """Not before: the level is chosen while looking at the plate."""
    e = good()
    e.mode = QUANTIFY
    e.photo_root = ""          # so the file-exists check stays out of the way
    for c in e.conditions:
        for plate in ("1", "2"):
            e.set_pick(c.code, plate, f"{c.code}-{plate}.JPG")

    codes = error_codes(validate.validate(e, template()))
    assert "no_photo_for_plate" not in codes
    assert "no_dilution_choice" in codes


def _handpicked(tmp_path, levels=("middle", "middle")) -> Experiment:
    """A complete handpicked experiment, with its photographs actually on disk."""
    e = good()
    e.mode = QUANTIFY
    e.photo_root = str(tmp_path)
    for c in e.conditions:
        for plate, level in zip(("1", "2"), levels):
            name = f"{c.code}-{plate}.JPG"
            (tmp_path / name).write_bytes(b"x")
            e.set_pick(c.code, plate, name)
            e.set_dilution_for(c.code, plate, level)
    return e


def test_quantify_mode_is_satisfied_once_each_plate_has_a_level(tmp_path):
    assert blocking(validate.validate(_handpicked(tmp_path), template())) == []


def test_two_plates_of_one_condition_may_differ(tmp_path):
    """The point of per-plate levels: each is normalised to its own control."""
    e = _handpicked(tmp_path, levels=("least", "most"))
    assert blocking(validate.validate(e, template())) == []
    assert e.dilution_for("GLU", "1") == "least"
    assert e.dilution_for("GLU", "2") == "most"


def test_a_picked_photograph_that_has_gone_missing_is_an_error(tmp_path):
    e = good()
    e.mode = QUANTIFY
    e.photo_root = str(tmp_path)
    for c in e.conditions:
        for plate in ("1", "2"):
            e.set_pick(c.code, plate, "gone.JPG")
            e.set_dilution_for(c.code, plate, "middle")
    assert "picked_photo_missing" in error_codes(validate.validate(e, template()))


def test_a_pick_left_behind_by_a_removed_condition_is_reported():
    e = good()
    e.mode = QUANTIFY
    e.photo_root = ""
    e.set_pick("GONE", "1", "a.JPG")
    issues = validate.validate(e, template())
    assert "pick_for_unknown_condition" in codes(issues)


def test_handpicked_mode_asks_nothing_of_the_naming_profile():
    """Its photos are chosen by hand, so their names may encode nothing."""
    e = good()
    e.mode = QUANTIFY
    e.profile = profiles.NamingProfile(name="empty")
    assert "profile_incomplete" not in codes(validate.validate(e, template()))


def test_timecourse_mode_does_not_ask_for_a_dilution():
    """The time course scores every dilution and ranks them; choosing is its job."""
    assert "no_dilution_choice" not in codes(validate.validate(good(), template()))


# --- template binding -------------------------------------------------------


def test_a_missing_template_is_an_error():
    e = good()
    e.template_id = "some-id"
    assert "template_not_found" in error_codes(validate.validate(e, None))


def test_no_template_at_all_is_an_error():
    assert "no_template" in error_codes(validate.validate(good(), None))


def test_a_template_of_a_different_id_is_an_error():
    e = good()
    e.template_id = "not-the-one"
    t = template()
    t.id = "the-other-one"
    assert "template_mismatch" in error_codes(validate.validate(e, t))


def test_a_panel_that_does_not_fit_the_template_is_an_error():
    """Strains would be attached to the wrong spots."""
    e = good()
    e.strains.append("s9")
    assert "template_slot_mismatch" in error_codes(validate.validate(e, template()))


def test_the_lab_standard_template_is_scorable():
    assert "plate_not_scorable" not in codes(validate.validate(good(), template()))


# --- photos -----------------------------------------------------------------


def test_no_photo_folder_is_an_error():
    e = good()
    e.photo_root = ""
    assert "no_photo_root" in error_codes(validate.validate(e, template()))


def test_a_profile_that_cannot_read_a_timepoint_blocks_a_timecourse():
    e = good()
    e.profile = profiles.flat_lab()
    issue = next(
        i for i in validate.validate(e, template()) if i.code == "profile_incomplete"
    )
    assert issue.is_error and "timepoint" in issue.message


def test_the_same_profile_is_fine_for_handpicked_quantification():
    e = good()
    e.mode = QUANTIFY
    e.profile = profiles.flat_lab()
    for c in e.conditions:
        c.dilution = {"mode": "condition", "choice": "middle"}
    assert "profile_incomplete" not in codes(validate.validate(e, template()))


def test_a_broken_profile_is_reported_not_raised():
    e = good()
    e.profile = profiles.NamingProfile(
        name="bad", rules=(profiles.FacetRule("plate", "segment", -2, ("no group",)),)
    )
    issues = validate.validate(e, template())
    assert "profile_invalid" in error_codes(issues)


def test_issues_come_back_worst_first():
    e = good()
    e.control_slot = None
    e.strains[3] = None
    issues = validate.validate(e, template())
    ranks = [i.severity for i in issues]
    assert ranks == sorted(ranks, key=lambda s: [Severity.ERROR, Severity.WARNING,
                                                 Severity.INFO].index(s))
