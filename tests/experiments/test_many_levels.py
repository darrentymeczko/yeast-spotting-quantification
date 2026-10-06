"""A design with more than three dilution levels, all the way through.

The hardcoded table stopped at three:

    spotting_batch.DILUTIONS = {"least": (0, 3), "middle": (1, 4), "most": (2, 5)}

These tests run a six-level design and a two-level one past the picker, the
validator and the tidy builder, and check the rows and replicates come out as
the template declares.

The measurement engine is stubbed: what is being tested is the bookkeeping over
the template, not the optics.
"""

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pandas")

import numpy as np  # noqa: E402

from experiments import geometry, run, validate  # noqa: E402
from experiments.model import QUANTIFY, Condition, Experiment  # noqa: E402
from tests.experiments.test_geometry import custom  # noqa: E402


class FakePlate:
    """Stands in for `spotting_batch.PlateData`.

    Every cell reads as its own row and column so a misread row is obvious:
    `net[r, c] == r * 100 + c`.
    """

    def __init__(self, plate: int, rows=6, cols=8):
        self.ref = type("Ref", (), {
            "plate": plate, "path": type("P", (), {"name": f"p{plate}.JPG"})()})()
        self.net = np.array([[r * 100 + c for c in range(cols)]
                             for r in range(rows)], dtype=float)
        self.rim = np.zeros((rows, cols), dtype=bool)
        self.bg_samples = np.array([1.0, 1.0, 1.0, 1.0, 1.0])


def experiment(template, levels_by_plate) -> Experiment:
    e = Experiment(
        name="Many",
        strains=[f"s{i}" for i in range(1, template.cols + 1)],
        control_slot=1,
        conditions=[Condition("GLU", "Glucose")],
        mode=QUANTIFY,
        photo_root="",
    )
    for plate_id, index in levels_by_plate.items():
        e.set_pick("GLU", plate_id, f"p{plate_id}.JPG")
        e.set_dilution_for("GLU", plate_id, index)
    return e


def tidy_for(template, levels_by_plate):
    e = experiment(template, levels_by_plate)
    plates = [(pid, FakePlate(int(pid))) for pid in levels_by_plate]
    return e, run.build_tidy(e, template, "GLU", plates)


# --- six levels -------------------------------------------------------------


def test_a_six_level_design_scores_the_level_it_was_told_to():
    t = custom(6, 1)
    e, tidy = tidy_for(t, {"1": 4, "2": 4})

    # Level 4 sits on row 5 (1-based) of each plate, so raw_growth is 4*100 + c.
    assert sorted(tidy["dilution_row"].unique()) == [5]
    assert set(tidy["raw_growth"]) == {400.0 + c for c in range(t.cols)}
    assert len(tidy) == t.cols * 2          # eight strains on two plates


def test_each_of_the_six_levels_is_reachable():
    t = custom(6, 1)
    for index in range(6):
        _e, tidy = tidy_for(t, {"1": index})
        assert sorted(tidy["dilution_row"].unique()) == [index + 1]
        assert set(tidy["raw_growth"]) == {index * 100.0 + c for c in range(8)}


def test_six_levels_validate_for_handpicked_quantification():
    t = custom(6, 1)
    e = experiment(t, {"1": 5, "2": 5})
    errors = [i.code for i in validate.validate(e, t) if i.is_error]
    assert "no_dilution_choice" not in errors
    assert "dilution_out_of_range" not in errors
    assert "grid_too_small" not in errors


def test_the_two_plates_may_sit_at_different_levels():
    """Each plate is normalised to its own control, so they need not agree."""
    t = custom(6, 1)
    _e, tidy = tidy_for(t, {"1": 0, "2": 5})
    rows_on = {int(plate): sorted(group["dilution_row"].unique())
               for plate, group in tidy.groupby("plate")}
    assert rows_on == {1: [1], 2: [6]}


# --- two levels, three replicates -------------------------------------------


def test_a_level_spread_over_three_rows_scores_all_of_them():
    t = custom(2, 3)
    _e, tidy = tidy_for(t, {"1": 0})
    assert sorted(tidy["dilution_row"].unique()) == [1, 3, 5]
    assert sorted(tidy["replicate"].unique()) == ["rep1", "rep2", "rep3"]


def test_replicates_keep_counting_across_plates():
    t = custom(2, 3)
    _e, tidy = tidy_for(t, {"1": 1, "2": 1})
    assert sorted(tidy["replicate"].unique()) == [
        "rep1", "rep2", "rep3", "rep4", "rep5", "rep6"]


# --- what it refuses --------------------------------------------------------


def test_a_level_beyond_the_template_is_reported_with_both_numbers():
    t = custom(4, 1)
    e = experiment(t, {"1": 7, "2": 7})
    issue = next(i for i in validate.validate(e, t)
                 if i.code == "dilution_out_of_range")
    assert "level 8" in issue.message and "only 4" in issue.message


@pytest.mark.parametrize("levels, reps, rows", [(2, 3, 6), (3, 2, 6), (6, 2, 12),
                                                (4, 3, 12)])
def test_a_timecourse_accepts_any_number_of_levels(levels, reps, rows):
    t = custom(levels, reps, rows=rows)
    e = experiment(t, {"1": 0, "2": 0})
    e.mode = "timecourse"
    errors = {i.code for i in validate.validate(e, t) if i.is_error}
    assert not errors & {"timecourse_needs_three_levels",
                         "timecourse_too_few_replicates",
                         "timecourse_needs_two_plates"}


def test_a_timecourse_says_why_too_few_replicates_cannot_be_ranked():
    t = custom(6, 1)                     # one replicate per plate: two in all
    e = experiment(t, {"1": 0, "2": 0})
    e.mode = "timecourse"
    issue = next(i for i in validate.validate(e, t)
                 if i.code == "timecourse_too_few_replicates")
    assert issue.is_error and "has 2" in issue.message


def test_the_replicate_floor_matches_the_scorer():
    """Mirrored from `score_candidate`'s `len(v) < 3`; drift would mislead."""
    import inspect
    import re

    import spotting_timecourse as tc

    source = inspect.getsource(tc.score_candidate)
    assert re.search(rf"len\(v\) < {validate.TIMECOURSE_MIN_REPLICATES}\b", source)


@pytest.mark.parametrize("plates, reps, rows", [
    (("1",), 3, 9), (("1", "2"), 2, 6), (("1", "2", "3"), 2, 6),
    (("1", "2", "3", "4"), 1, 3),
])
def test_a_timecourse_accepts_any_number_of_plates(plates, reps, rows):
    """Any plate count, provided the replicates add up to at least three."""
    t = custom(3, reps, rows=rows, plates=plates)
    e = experiment(t, {p: 0 for p in plates})
    e.mode = "timecourse"
    errors = {i.code for i in validate.validate(e, t) if i.is_error}
    assert not {c for c in errors if c.startswith("timecourse")}


def test_one_plate_with_four_replicates_is_a_valid_timecourse():
    """The user's case: four control replicates and four of every strain, one plate.

    Each strain is divided by the mean of the controls on its own plate, so one
    plate carries a complete experiment.
    """
    t = custom(3, 4, rows=12, plates=("1",))
    e = experiment(t, {"1": 0})
    e.mode = "timecourse"
    errors = {i.code for i in validate.validate(e, t) if i.is_error}
    assert "timecourse_too_few_replicates" not in errors
    assert not {c for c in errors if c.startswith("timecourse")}
    assert len(t.replicates()) == 4


def test_the_template_becomes_the_pipelines_layout():
    """Six levels in the design -> six levels scored by the time course."""
    t = custom(6, 1)
    layout = run.to_layout(t)
    assert len(layout.levels) == 6
    assert layout.levels[4].rows(1) == (4,)
    assert layout.levels[4].rows(2) == (4,)
    assert {c.replicate for c in layout.levels[0].cells(2)} == {2}
    assert layout.block_rows() == 6


def test_the_lab_template_becomes_exactly_the_classic_layout():
    """So every cache key, filename and precompute takes the long-standing path."""
    import spotting_batch as sb
    from plate_template import presets

    layout = run.to_layout(presets.lab_standard_8x6())
    assert layout == sb.classic_layout()
    assert layout.is_classic()


def test_a_bigger_grid_is_accepted():
    """The lattice is no longer fixed at 8x6; the engine is told what to find."""
    t = custom(4, 2, rows=8, cols=12)
    e = experiment(t, {"1": 0, "2": 0})
    codes = {i.code for i in validate.validate(e, t) if i.is_error}
    assert "grid_too_small" not in codes


def test_a_grid_too_thin_to_measure_is_refused():
    t = custom(2, 1, rows=2, cols=1)
    e = experiment(t, {"1": 0, "2": 0})
    issue = next(i for i in validate.validate(e, t)
                 if i.code == "grid_too_small")
    assert "2x1" in issue.message
    assert "agar between the spots" in issue.message


def test_a_bigger_grid_quantifies_every_position():
    """A 12x16 design must yield 16 strains per row, not 8."""
    t = custom(4, 1, rows=12, cols=16)
    e = experiment(t, {"1": 2})
    plates = [("1", FakePlate(1, rows=12, cols=16))]
    tidy = run.build_tidy(e, t, "GLU", plates)
    assert len(tidy) == 16
    assert sorted(tidy["strain_col"].unique()) == list(range(1, 17))
    assert sorted(tidy["dilution_row"].unique()) == [3]


def test_running_without_a_template_says_why():
    t = custom(6, 1)
    e = experiment(t, {"1": 0})
    with pytest.raises(run.RunError, match="which rows hold which dilution"):
        run._run_quantify(e, None, "out", estimate=False, template=None)


def test_the_templates_grid_and_rows_reach_the_engine(tmp_path, monkeypatch):
    """The whole point of the wiring: a 12x16 design is measured as 12x16.

    `sb.measure` is stubbed -- this is about what the engine is ASKED for, which
    no amount of running it would show more clearly.
    """
    import spotting_batch as sb

    t = custom(4, 1, rows=12, cols=16)
    e = experiment(t, {"1": 2, "2": 2})
    e.photo_root = str(tmp_path)
    for plate in ("1", "2"):
        (tmp_path / f"p{plate}.JPG").write_bytes(b"x")

    seen = []

    def fake_measure(ref, opts, cache, **kw):
        seen.append(opts)
        return FakePlate(ref.plate, rows=12, cols=16)

    monkeypatch.setattr(sb, "measure", fake_measure)
    monkeypatch.setattr(run, "write_handoff", lambda *a, **k: None)
    monkeypatch.setattr(sb, "run_plots", lambda *a, **k: None)
    monkeypatch.setattr(sb, "write_condition_matrix", lambda *a, **k: None)

    assert run._run_quantify(e, None, tmp_path / "out", estimate=False,
                             template=t) == 0
    assert seen, "the engine was never asked to measure anything"
    for opts in seen:
        assert opts.grid_shape == (12, 16)
        # Level 2 sits on row 3 of a one-replicate-per-row design, 1-based.
        assert opts.quant_rows == (3,)
