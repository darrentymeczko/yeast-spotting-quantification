"""The edit layer, and what it does to the numbers.

The claim this whole tool rests on is that a correction goes through the
PIPELINE'S normalisation rather than being painted on afterwards. The test that
matters is the control one: relative growth is a strain divided by the mean of
the control spots on its plate, so excluding a control spot has to move every
strain on that plate. If it only greyed out a dot in the figure, the tool would
be producing numbers that look corrected and are not.

These build a tidy frame directly instead of measuring photographs -- the
arithmetic is what is under test, and a fixture with known values makes the
expected answer something that can be written down rather than recomputed by
the code being checked.
"""

from __future__ import annotations

import pytest
from conftest import needs_engine

from results_review.model import SpotEdit
from results_review.rebuild import (Frame, _apply_edits, _flag_outliers,
                                    _normalize, summarize)

pytestmark = needs_engine

CONTROL_COL = 1
STRAINS = {1: "WT BY", 2: "dATX1", 3: "dSOD2"}


class FakePlate:
    """Only `bg_samples` is read, to derive the noise-aware control floor."""

    def __init__(self, noise: float = 0.0) -> None:
        self.bg_samples = [1.0, 1.0, 1.0, 1.0, 1.0] if noise == 0 else \
            [1.0 - noise, 1.0, 1.0, 1.0, 1.0 + noise]


def frame(values=None):
    """Two plates, two replicates each, three strains. Controls all read 10.0.

    A control mean of exactly 10 on both plates makes every expected ratio
    something that can be stated: 12.0 / 10.0 is 1.2, and it stays 1.2 only if
    the normalisation is doing what it says.
    """
    import pandas as pd

    values = values or {}
    rows = []
    for plate in (1, 2):
        for rep_idx in (1, 2):
            rep = f"rep{(plate - 1) * 2 + rep_idx}"
            for col, strain in STRAINS.items():
                raw = values.get((rep, col),
                                 10.0 if col == CONTROL_COL else 12.0)
                rows.append({
                    "experiment": "Set01 GLU 20 Hours",
                    "treatment": "Set01 GLU 20 Hours", "set": "TC",
                    "plate": plate, "image": f"p{plate}.JPG", "replicate": rep,
                    "dilution_row": rep_idx, "dilution": "middle",
                    "strain_col": col, "strain": strain, "raw_growth": raw,
                    "artifact": False, "excluded": False,
                    "is_control": col == CONTROL_COL,
                })
    return pd.DataFrame(rows)


def build(values=None, edits=(), noise: float = 0.0):
    tidy, unmatched = _apply_edits(frame(values), {e.key: e for e in edits},
                                   CONTROL_COL)
    tidy, _ = _normalize(tidy, [FakePlate(noise), FakePlate(noise)], CONTROL_COL)
    return tidy, unmatched


def rel(tidy, rep, col) -> float:
    hit = tidy[(tidy["replicate"] == rep) & (tidy["strain_col"] == col)]
    return float(hit["relative_growth"].iloc[0])


def raw(tidy, rep, col) -> float:
    hit = tidy[(tidy["replicate"] == rep) & (tidy["strain_col"] == col)]
    return float(hit["raw_growth"].iloc[0])


# --- the baseline ----------------------------------------------------------

def test_unedited_normalisation_is_the_plain_ratio():
    tidy, _ = build()
    assert rel(tidy, "rep1", 2) == pytest.approx(1.2)
    assert rel(tidy, "rep1", 1) == pytest.approx(1.0)


def test_an_unedited_frame_is_marked_as_unedited():
    tidy, _ = build()
    assert not tidy["manual"].any()
    assert (tidy["excluded_source"] == "").all()


# --- the central claim -----------------------------------------------------

def test_excluding_a_control_spot_moves_every_strain_on_that_plate():
    """The divisor is the mean of the plate's controls, so dropping one shifts it.

    Plate 1's controls read 10.0 and 4.0, a mean of 7.0. Excluding the 4.0
    leaves 10.0, so every strain on plate 1 must fall from 12/7 to 12/10 --
    and plate 2, whose controls were not touched, must not move at all.
    """
    values = {("rep2", 1): 4.0}
    before, _ = build(values)
    assert rel(before, "rep1", 2) == pytest.approx(12.0 / 7.0)

    after, _ = build(values, edits=[SpotEdit("rep2", 1, excluded=True)])
    assert rel(after, "rep1", 2) == pytest.approx(1.2)
    assert rel(after, "rep2", 2) == pytest.approx(1.2)
    assert rel(after, "rep3", 2) == pytest.approx(1.2), "plate 2 must not move"


def test_excluding_a_strain_spot_leaves_the_divisor_alone():
    tidy, _ = build(edits=[SpotEdit("rep1", 2, excluded=True)])
    assert rel(tidy, "rep1", 3) == pytest.approx(1.2)
    assert bool(tidy[(tidy["replicate"] == "rep1")
                     & (tidy["strain_col"] == 2)]["excluded"].iloc[0])


def test_a_manual_raw_value_normalises_like_a_measured_one():
    tidy, _ = build(edits=[SpotEdit("rep1", 2, raw_growth=8.0)])
    assert raw(tidy, "rep1", 2) == pytest.approx(8.0)
    assert rel(tidy, "rep1", 2) == pytest.approx(0.8)
    assert rel(tidy, "rep1", 3) == pytest.approx(1.2), "only that spot moves"


def test_a_manual_control_value_changes_the_divisor():
    """Re-measuring a control by hand is the other way to fix a bad divisor."""
    tidy, _ = build(edits=[SpotEdit("rep2", 1, raw_growth=30.0)])
    # plate 1 controls are now 10 and 30, mean 20
    assert rel(tidy, "rep1", 2) == pytest.approx(12.0 / 20.0)
    assert rel(tidy, "rep3", 2) == pytest.approx(1.2), "plate 2 untouched"


def test_the_measured_value_is_kept_when_one_is_typed_in():
    """The number the machine produced is never overwritten, only overridden."""
    tidy, _ = build(edits=[SpotEdit("rep1", 2, raw_growth=8.0)])
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert float(hit["raw_growth_original"].iloc[0]) == pytest.approx(12.0)
    assert bool(hit["manual"].iloc[0])


# --- provenance ------------------------------------------------------------

def test_who_excluded_a_spot_is_recorded():
    tidy, _ = build(edits=[SpotEdit("rep1", 2, excluded=True)])
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert hit["excluded_source"].iloc[0] == "manual"


def test_re_including_a_config_excluded_strain_is_recorded():
    import pandas as pd

    base = frame()
    base.loc[base["strain_col"] == 3, "excluded"] = True
    tidy, _ = _apply_edits(base, {("rep1", 3): SpotEdit("rep1", 3,
                                                        excluded=False)},
                           CONTROL_COL)
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 3)]
    assert hit["excluded_source"].iloc[0] == "manual-cleared"
    assert not bool(hit["excluded"].iloc[0])
    # ...and the ones nobody touched still say the config did it.
    other = tidy[(tidy["replicate"] == "rep2") & (tidy["strain_col"] == 3)]
    assert other["excluded_source"].iloc[0] == "config"
    assert isinstance(tidy, pd.DataFrame)


def test_a_note_alone_changes_no_numbers():
    tidy, _ = build(edits=[SpotEdit("rep1", 2, note="looks odd")])
    assert rel(tidy, "rep1", 2) == pytest.approx(1.2)
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert hit["edit_note"].iloc[0] == "looks odd"
    assert not bool(hit["manual"].iloc[0])


def test_an_edit_for_a_spot_that_is_not_here_is_reported():
    """Picking a different candidate can strand an edit; it must not be silent."""
    _, unmatched = build(edits=[SpotEdit("rep9", 7, excluded=True)])
    assert unmatched and "rep9" in unmatched[0]


# --- outliers --------------------------------------------------------------

def test_manual_outlier_flags_are_distinguished_from_automatic_ones():
    tidy, _ = build()
    tidy, _ = _flag_outliers(tidy, {("rep1", 2): SpotEdit("rep1", 2,
                                                          outlier=True)})
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert bool(hit["outlier"].iloc[0])
    assert hit["outlier_source"].iloc[0] == "manual"


#: Values that actually trip `sq.flag_outliers`, which is deliberately hard to
#: trip. All four conditions have to hold at once: n >= 4, the group's CV >= 0.30,
#: one point at robust z >= 3.5, and removing it cutting the CV by half. The
#: other three values must DIFFER from each other or the MAD is zero and the
#: robust z is undefined -- which is what a fixture of identical values gets
#: wrong, and why this one spells the numbers out.
OUTLIER_CASE = {("rep1", 2): 60.0, ("rep2", 2): 11.0,
                ("rep3", 2): 12.0, ("rep4", 2): 13.0}


def test_the_automatic_flagger_fires_on_a_dominating_point():
    tidy, _ = build(OUTLIER_CASE)
    tidy, _ = _flag_outliers(tidy, {})
    flagged = tidy[tidy["outlier"]]
    assert len(flagged) == 1, "exactly one point should dominate this group"
    assert flagged["replicate"].iloc[0] == "rep1"
    assert flagged["outlier_source"].iloc[0] == "auto"


def test_clearing_an_automatic_outlier_is_recorded_as_cleared():
    """Overruling the flagger is a claim, and is recorded as this person's."""
    tidy, _ = build(OUTLIER_CASE)
    tidy, _ = _flag_outliers(tidy, {("rep1", 2): SpotEdit("rep1", 2,
                                                          outlier=False)})
    hit = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert not bool(hit["outlier"].iloc[0])
    assert hit["outlier_source"].iloc[0] == "cleared"


def test_an_untouched_automatic_flag_stays_automatic():
    """Clearing one spot must not relabel the others as hand-made."""
    tidy, _ = build(OUTLIER_CASE)
    tidy, _ = _flag_outliers(tidy, {("rep2", 3): SpotEdit("rep2", 3,
                                                          outlier=True)})
    rep1 = tidy[(tidy["replicate"] == "rep1") & (tidy["strain_col"] == 2)]
    assert rep1["outlier_source"].iloc[0] == "auto"


# --- the summary panel -----------------------------------------------------

def _summary(edits=()):
    tidy, _ = build(edits=edits)
    tidy, _ = _flag_outliers(tidy, {e.key: e for e in edits})
    return summarize(Frame(medium="GLU", candidate=None,
                           experiment="Set01 GLU 20 Hours", tidy=tidy,
                           control_col=CONTROL_COL))


def test_the_summary_keeps_plate_column_order_while_editing():
    """The panel must not rearrange itself the moment a spot is dropped.

    Grouping in encounter order sends a strain to the bottom as soon as its
    first replicate is excluded -- which is exactly when somebody is trying to
    read a before-and-after off it.
    """
    before = [s["strain"] for s in _summary()]
    after = [s["strain"] for s in
             _summary([SpotEdit("rep1", 1, excluded=True)])]
    assert before == [STRAINS[c] for c in sorted(STRAINS)]
    assert after == before, "the control must stay in column 1's place"


def test_the_summary_counts_only_the_rows_that_count():
    got = {s["strain"]: s for s in
           _summary([SpotEdit("rep1", 2, excluded=True)])}
    assert got["dATX1"]["n"] == 3
    assert got["dSOD2"]["n"] == 4


def test_the_summary_reports_each_t_test_against_the_positive_control():
    got = {s["strain"]: s for s in _summary()}
    assert got["WT BY"]["p_value"] is None
    assert got["dATX1"]["p_value"] <= 0.05
    assert got["dSOD2"]["p_value"] <= 0.05
    assert got["dATX1"]["p_heading"] == "p vs +ctrl"


def test_anova_is_shown_once_as_an_omnibus_result():
    tidy, _ = build()
    tidy, _ = _flag_outliers(tidy, {})
    got = summarize(Frame(medium="GLU", candidate=None,
                          experiment="Set01 GLU 20 Hours", tidy=tidy,
                          control_col=CONTROL_COL), statistical_test="anova")
    values = [row["p_value"] for row in got if row["p_value"] is not None]
    assert len(values) == 1
    assert all(row["p_heading"] == "ANOVA p" for row in got)


# --- refusing to guess -----------------------------------------------------

def test_excluding_every_control_gives_nan_not_a_number():
    """No divisor means no answer. Inventing one would be the worst outcome."""
    tidy, _ = build(edits=[SpotEdit(f"rep{i}", 1, excluded=True)
                           for i in (1, 2, 3, 4)])
    assert tidy["relative_growth"].isna().all()


def test_a_control_at_the_noise_floor_is_refused():
    """A control that did not grow cannot normalise; the pipeline says NaN."""
    tidy, _ = build({("rep1", 1): 0.0, ("rep2", 1): 0.0})
    plate1 = tidy[tidy["plate"] == 1]
    assert plate1["relative_growth"].isna().all()
    assert not bool(plate1["control_ok"].iloc[0])
    # plate 2 is independent and still fine
    assert rel(tidy, "rep3", 2) == pytest.approx(1.2)
