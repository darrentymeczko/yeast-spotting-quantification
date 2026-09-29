"""The edit layer: what it records, and what it refuses to record.

A review is the only thing in this package that is not regenerable, so the
round-trip is the thing worth pinning down. The distinction these tests exist to
protect is the three-state one: an edit can say "exclude this", "keep this", or
say nothing at all and defer to the pipeline. Collapsing the third state into
the second is the easy mistake, and it silently turns "I have no opinion" into
"I checked this and it is fine".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from results_review import review as rv
from results_review.discovery import SetRun
from results_review.model import Candidate, Pick, Review, SpotEdit


def cand(medium="GLU", tp="20 Hours", dil="least", score=5.0, cv=0.1, order=0):
    return Candidate(medium=medium, medium_label="Glucose", timepoint=tp,
                     hours=20.0, plate1="a.JPG", plate2="b.JPG", dilution=dil,
                     median_cv=cv, best_set_score=score, csv_order=order)


def run_with(*candidates) -> SetRun:
    return SetRun(results_dir=Path("."), label="Set01",
                  candidates=list(candidates))


# --- three-state edits -----------------------------------------------------

def test_an_edit_that_says_nothing_is_not_stored():
    r = Review()
    r.amend("GLU", "rep1", 3, excluded=True)
    assert r.n_edits == 1
    r.amend("GLU", "rep1", 3, excluded=None)
    assert r.n_edits == 0, "an opinion withdrawn should leave no trace"
    assert "GLU" not in r.edits


def test_clearing_one_field_keeps_the_others():
    r = Review()
    r.amend("GLU", "rep1", 3, excluded=True, note="glare")
    r.amend("GLU", "rep1", 3, excluded=None)
    edit = r.edit("GLU", ("rep1", 3))
    assert edit is not None and edit.excluded is None and edit.note == "glare"


def test_excluded_false_is_a_real_claim():
    """'Keep this spot' must survive the round-trip as False, not vanish."""
    r = Review()
    r.amend("GLU", "rep2", 5, excluded=False)
    back = Review.from_dict(json.loads(json.dumps(r.to_dict())))
    assert back.edit("GLU", ("rep2", 5)).excluded is False


def test_zero_is_a_real_raw_value():
    """A spot measured at 0.0 is data, not a missing value."""
    r = Review()
    r.amend("GLU", "rep2", 5, raw_growth=0.0)
    back = Review.from_dict(json.loads(json.dumps(r.to_dict())))
    assert back.edit("GLU", ("rep2", 5)).raw_growth == 0.0


def test_a_blank_note_does_not_create_an_edit():
    r = Review()
    r.amend("GLU", "rep1", 1, note="   ")
    assert r.n_edits == 0


# --- round trip ------------------------------------------------------------

def test_round_trip_is_exact():
    r = Review(capture_root="C:/photos/Set01", statistical_test="t_test",
               p_adjust="holm", alpha=0.01)
    r.set_pick("GLU", cand(), reason="20 Hours control is glared")
    r.set_pick("GLY", cand(medium="GLY", tp="46 Hours", dil="most"))
    r.amend("GLU", "rep3", 8, excluded=True, note="ran into its neighbour")
    r.amend("GLU", "rep1", 5, raw_growth=8.41)
    r.amend("GLY", "rep2", 3, outlier=False)

    back = Review.from_dict(json.loads(json.dumps(r.to_dict())))
    assert back.to_dict() == r.to_dict()
    assert back.capture_root == "C:/photos/Set01"
    assert back.picks["GLU"].reason.startswith("20 Hours")
    assert back.edit("GLU", ("rep1", 5)).raw_growth == 8.41
    assert back.statistical_test == "t_test" and back.p_adjust == "holm"
    assert back.alpha == pytest.approx(0.01)


def test_old_review_defaults_to_uncorrected_t_tests():
    back = Review.from_dict({"version": 1, "chosen": {}, "edits": {}})
    assert back.statistical_test == "t_test"
    assert back.p_adjust == "none"
    assert back.alpha == pytest.approx(0.05)


def test_statistical_choice_is_an_undoable_review_decision():
    from results_review.gui.controller import ReviewController

    ctl = ReviewController(run_with(cand()), Review())
    assert ctl.set_statistics("t_test", "holm", 0.01)
    assert ctl.dirty and ctl.review.p_adjust == "holm"
    assert ctl.review.alpha == pytest.approx(0.01)
    ctl.set_statistics("anova", "holm")
    assert ctl.review.statistical_test == "anova"
    assert ctl.undo()
    assert ctl.review.statistical_test == "t_test"
    assert ctl.review.p_adjust == "holm"
    assert ctl.review.alpha == pytest.approx(0.01)


def test_invalid_p_cutoffs_are_rejected():
    from results_review.gui.controller import ReviewController

    ctl = ReviewController(run_with(cand()), Review())
    for value in (0, 1, -0.1, 1.1):
        with pytest.raises(ValueError, match="alpha"):
            ctl.set_statistics("t_test", "none", value)


def test_save_and_load(tmp_path):
    r = Review(capture_root=str(tmp_path))
    r.amend("GLU", "rep1", 2, raw_growth=1.25, note="re-measured")
    path = rv.save(tmp_path / "review.json", r)
    assert path.exists()
    assert rv.load(path).to_dict() == r.to_dict()


def test_load_of_a_missing_file_is_an_empty_review(tmp_path):
    assert rv.load(tmp_path / "nope.json").n_edits == 0


def test_a_corrupt_review_is_reported_not_ignored(tmp_path):
    """Silently starting over would throw away somebody's afternoon."""
    bad = tmp_path / "review.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        rv.load(bad)


def test_save_does_not_leave_a_partial_file_behind(tmp_path):
    r = Review()
    r.amend("GLU", "rep1", 1, excluded=True)
    rv.save(tmp_path / "review.json", r)
    assert [p.name for p in tmp_path.iterdir()] == ["review.json"]


# --- picks -----------------------------------------------------------------

def test_defaults_come_from_the_pipeline():
    run = run_with(cand(score=1.0, order=0), cand(tp="22 Hours", score=9.0,
                                                  order=1))
    r = rv.with_defaults(run, Review())
    assert r.pick("GLU").timepoint == "22 Hours"
    assert rv.is_default(run, r, "GLU")


def test_defaults_do_not_overwrite_a_decision():
    run = run_with(cand(score=1.0, order=0), cand(tp="22 Hours", score=9.0,
                                                  order=1))
    r = Review()
    r.set_pick("GLU", run.candidates[0], reason="mine")
    rv.with_defaults(run, r)
    assert r.pick("GLU").timepoint == "20 Hours"
    assert not rv.is_default(run, r, "GLU")


def test_a_pick_that_no_longer_exists_is_reported():
    """Re-running the pipeline with different photos must not silently re-default."""
    run = run_with(cand())
    r = Review()
    r.picks["GLU"] = Pick("99 Hours", 99.0, "gone.JPG", "gone2.JPG", "least")
    assert rv.chosen_candidate(run, r, "GLU") is None
    assert rv.stale_picks(run, r) == ["GLU"]


def test_pick_matching_ignores_the_reason():
    c = cand()
    assert Pick.of(c, "because").matches(c)


# --- keys ------------------------------------------------------------------

def test_edits_key_on_the_spot_not_the_photo():
    """The same edit has to survive choosing a different pairing for that medium.

    'Replicate 3, column 8 is unusable' is a judgement about a spot; it does not
    stop being held because the timepoint changed. Anything that then fails to
    match is reported by the rebuild, not dropped here.
    """
    e = SpotEdit("rep3", 8, excluded=True)
    assert e.key == ("rep3", 8)
    r = Review()
    r.set_edit("GLU", e)
    r.set_pick("GLU", cand(tp="16 Hours"))
    assert r.edit("GLU", ("rep3", 8)) is not None


def test_strain_col_is_normalised_to_an_int():
    r = Review()
    r.amend("GLU", "rep1", "4", excluded=True)
    assert r.edit("GLU", ("rep1", 4)) is not None
