"""The time course with one plate, two, or any number.

It used to pair one photograph of plate 1 with one of plate 2 and nothing else.
That was never a requirement of the method: every strain is divided by the mean
of ALL the control replicates on its OWN plate, so a single plate carrying four
control replicates and four of every strain is a complete, valid experiment, and
so is a design spread over three plates.

The engine is stubbed wherever a photograph would be read.
"""

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pandas")

import spotting_batch as sb  # noqa: E402
import spotting_timecourse as tc  # noqa: E402
import spotting_timecourse_figures as tcf  # noqa: E402
from results_review.model import Candidate, Pick  # noqa: E402


def layout_for(plates, levels=3, reps=2, rows=6, cols=8):
    """`reps` replicate blocks per plate, replicates numbered across plates."""
    out = []
    for d in range(levels):
        per = []
        for i, p in enumerate(plates):
            cells = tuple(sb.LevelCell(b * levels + d, c, c + 1, i * reps + b + 1)
                          for b in range(reps) for c in range(cols)
                          if b * levels + d < rows)
            per.append((p, cells))
        out.append(sb.DilutionLevel(d, f"level {d + 1}", tuple(per)))
    return sb.DilutionLayout(tuple(out), rows, cols)


def shot(plate, name="_9.JPG", hours=40.0, folder="40 Hours"):
    return tc.Shot(Path(f"{folder}/GLY/Plate {plate}/{name}"), hours, folder,
                   "GLY", "Glycerol", plate)


class Plate:
    """Controls (slot 1) read `control`; every other strain reads 2 x control."""

    def __init__(self, plate, control, rows=6, cols=8):
        self.ref = sb.PhotoRef(Path(f"p{plate}.JPG"), "TC", plate, "TC")
        self.net = np.full((rows, cols), 2.0 * control)
        self.net[:, 0] = control
        self.rim = np.zeros((rows, cols), bool)
        self.bg_samples = np.ones(5)
        self.radius = 50.0
        self.centers = np.zeros((rows, cols, 2))


@pytest.fixture
def plates_by_number(monkeypatch):
    """Plate k reads a control of 10 * k, so per-plate normalisation is visible."""
    def fake(path, plate, rows, cache_dir, layout=None):
        grid = (layout.n_rows, layout.n_cols) if layout else (6, 8)
        return Plate(plate, 10.0 * plate, *grid)
    monkeypatch.setattr(tc, "_cached_measure", fake)


STRAINS = [f"s{i}" for i in range(1, 9)]
CFG = {"strains": STRAINS, "control_col": 1, "exclude": [],
       "media": {"GLY": {"control_col": 1, "exclude": []}}}


# --- building candidates --------------------------------------------------------


def test_two_plates_build_the_same_pairings_as_before():
    shots = [shot(1, "a.JPG"), shot(1, "b.JPG"), shot(2, "c.JPG")]
    cands = tc.candidates(shots)
    assert len(cands) == 2
    for c in cands:
        assert c["plate1"].plate == 1 and c["plate2"].plate == 2
        assert tc.cand_shots(c) == (c["plate1"], c["plate2"])


def test_one_plate_makes_one_candidate_per_photo():
    shots = [shot(1, "a.JPG"), shot(1, "b.JPG")]
    cands = tc.candidates(shots, plates=(1,))
    assert [tc.cand_shots(c)[0].path.name for c in cands] == ["a.JPG", "b.JPG"]
    assert all("plate2" not in c for c in cands)


def test_three_plates_multiply_out_their_reshots():
    shots = [shot(1, "a.JPG"), shot(1, "b.JPG"), shot(2, "c.JPG"),
             shot(3, "d.JPG"), shot(3, "e.JPG")]
    cands = tc.candidates(shots, plates=(1, 2, 3))
    assert len(cands) == 4
    assert all([s.plate for s in tc.cand_shots(c)] == [1, 2, 3] for c in cands)


def test_a_sitting_missing_any_plate_is_not_a_candidate():
    """A missing plate is missing replicates, not a smaller version of the result."""
    shots = [shot(1), shot(2), shot(1, hours=16.0, folder="16 Hours")]
    cands = tc.candidates(shots, plates=(1, 2))
    assert [c["tp_label"] for c in cands] == ["40 Hours"]


def test_an_old_style_candidate_dict_still_reads():
    c = {"plate1": shot(1), "plate2": shot(2)}
    assert [s.plate for s in tc.cand_shots(c)] == [1, 2]


# --- scoring: normalised per plate, whatever the plate count ---------------------


def test_one_plate_with_four_replicates_is_scored(plates_by_number):
    """The case in question: four controls and four of every strain on one plate."""
    layout = layout_for((1,), levels=2, reps=4, rows=8)
    cand = tc.candidates([shot(1)], plates=(1,))[0]
    m = tc.score_candidate(cand, layout.levels[0], Path("c"), STRAINS, 1,
                           layout=layout)
    assert m is not None
    assert m["control_n"] == 4
    assert m["_control_expected"] == 4
    assert m["n_strains"] == 7
    assert m["median_CV"] == pytest.approx(0.0)


def test_each_plate_is_divided_by_its_own_controls(plates_by_number):
    """Plates read controls of 10, 20, 30; every strain is 2x ITS plate's control."""
    layout = layout_for((1, 2, 3))
    cand = tc.candidates([shot(1), shot(2), shot(3)], plates=(1, 2, 3))[0]
    tidy = tc.build_tidy_for_candidate(cand, layout.levels[0], CFG, Path("c"),
                                       "Set01", layout)
    strains = tidy[~tidy["is_control"]]
    assert np.allclose(strains["relative_growth"], 2.0)
    assert sorted(tidy["plate"].unique()) == [1, 2, 3]
    assert sorted(tidy["replicate"].unique()) == [f"rep{i}" for i in range(1, 7)]


def test_completeness_expects_the_controls_the_design_actually_spotted(plates_by_number):
    """It was a fixed 4 -- two plates of two -- regardless of the design."""
    for plates, reps, expected in (((1,), 4, 4), ((1, 2, 3), 2, 6), ((1, 2), 2, 4)):
        layout = layout_for(plates, levels=1, reps=reps, rows=reps)
        cand = tc.candidates([shot(p) for p in plates], plates=plates)[0]
        m = tc.score_candidate(cand, layout.levels[0], Path("c"), STRAINS, 1,
                               layout=layout)
        assert m["_control_expected"] == expected


def test_a_complete_design_scores_full_completeness_at_any_size():
    core = {}
    for n, expected in ((4, 4), (6, 6), (4, 8)):
        full = tc._best_set_score({}, n, 1.0, core, 0.0, expected)
        assert full == pytest.approx(tc.W_COMPLETE * min(n / expected, 1.0)
                                     + tc.W_VARIANCE)
    assert tc._best_set_score({}, 4, 1.0, core) == tc._best_set_score(
        {}, 4, 1.0, core, 0.0, 4)


# --- names, montage jobs and the review tool ---------------------------------------


def test_sheet_names_join_every_plates_photo():
    three = tc.candidates([shot(1, "a.JPG"), shot(2, "b.JPG"), shot(3, "c.JPG")],
                          plates=(1, 2, 3))[0]
    one = tc.candidates([shot(1, "a.JPG")], plates=(1,))[0]
    level = sb.classic_layout().levels[0]
    assert tcf.candidate_id(three, level).endswith("_a-b-c_least")
    assert tcf.candidate_id(one, level).endswith("_a_least")


def test_a_scored_row_finds_its_candidate_by_every_photo():
    cands = tc.candidates([shot(1, "a.JPG"), shot(2, "b.JPG"), shot(2, "x.JPG"),
                           shot(3, "c.JPG")], plates=(1, 2, 3))
    row = {"medium": "GLY", "timepoint": "40 Hours",
           "plate1": "a.JPG", "plate2": "x.JPG", "plate3": "c.JPG"}
    got = tcf._match_candidate(cands, row)
    assert [s.path.name for s in tc.cand_shots(got)] == ["a.JPG", "x.JPG", "c.JPG"]


def test_montage_jobs_carry_every_plate_and_read_the_old_form():
    cand = tc.candidates([shot(1), shot(2), shot(3)], plates=(1, 2, 3))[0]
    job = tc.montage_job(cand, (0, 3), STRAINS, "m.png", "c", "Set01 GLY")
    pairs = tc._montage_job_parts(job)[0]
    assert [pl for _p, pl in pairs] == [1, 2, 3]
    old = ("a.JPG", 1, "b.JPG", 2, (0, 3), STRAINS, "m.png", "c", "Set01 GLY")
    assert [pl for _p, pl in tc._montage_job_parts(old)[0]] == [1, 2]


def test_the_review_reads_any_number_of_plate_columns():
    row = {"medium": "GLY", "timepoint": "40 Hours", "hours": "40",
           "plate1": "a.JPG", "plate2": "b.JPG", "plate3": "c.JPG",
           "plate4": "d.JPG", "dilution": "least"}
    cand = Candidate.from_row(row)
    assert cand.photos == ("a.JPG", "b.JPG", "c.JPG", "d.JPG")
    assert cand.detail == "a.JPG + b.JPG + c.JPG + d.JPG"

    one = Candidate.from_row({**row, "plate2": "", "plate3": "", "plate4": ""})
    assert one.photos == ("a.JPG",)


def test_two_plate_candidates_keep_their_identity():
    """Saved reviews key picks on (medium, timepoint, plate1, plate2, dilution)."""
    row = {"medium": "GLY", "timepoint": "40 Hours", "plate1": "a.JPG",
           "plate2": "b.JPG", "dilution": "least"}
    assert Candidate.from_row(row).key == ("GLY", "40 Hours", "a.JPG", "b.JPG",
                                           "least")


def test_a_pick_of_a_three_plate_candidate_round_trips():
    row = {"medium": "GLY", "timepoint": "40 Hours", "plate1": "a.JPG",
           "plate2": "b.JPG", "plate3": "c.JPG", "dilution": "least"}
    cand = Candidate.from_row(row)
    again = Pick.from_dict(Pick.of(cand, "clean").to_dict())
    assert again.matches(cand)
    other = Candidate.from_row({**row, "plate3": "z.JPG"})
    assert not again.matches(other)
