"""Purpose 2: the results review MARKS what the data review flagged.

Nothing is excluded there: a flagged spot keeps counting until the person
reviewing the results omits it themselves.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from data_review import flags as ff
from data_review.flags import DataFlags
from results_review import datareview, theme
from results_review.discovery import RunInfo, SetRun
from results_review.export import COLUMN_ORDER
from results_review.model import Candidate

P1A = "24 Hours/GLU/Plate 1/a.jpg"
P1B = "24 Hours/GLU/Plate 1/b.jpg"
P2A = "24 Hours/GLU/Plate 2/a.jpg"


def _run(tmp_path, *, live=None, snapshot=None, layout=None) -> SetRun:
    records = [{"relpath": rel, "condition": "GLU", "timepoint": 24.0,
                "timepoint_label": "24 Hours", "plate": plate}
               for rel, plate in ((P1A, 1), (P1B, 1), (P2A, 2))]
    info = RunInfo(name="Synthetic",
                   pipeline_config={"strains": ["WT"], "resolved_photos": records},
                   dilution_layout=layout or {},
                   data_review_file=str(live) if live else "",
                   data_review_snapshot=snapshot or {})
    return SetRun(results_dir=tmp_path, label="Synthetic", experiment=info)


def _cand(plate1="a.jpg", dilution="middle") -> Candidate:
    return Candidate(medium="GLU", medium_label="GLU", timepoint="24 Hours",
                     hours=24.0, plate1=plate1, plate2="a.jpg", dilution=dilution)


def test_the_live_review_wins_over_the_runs_copy(tmp_path):
    old = DataFlags()
    old.set_plate(P1A, "old reason")
    live = tmp_path / "Synthetic.datareview.json"
    run = _run(tmp_path, live=live, snapshot=old.to_dict())
    # The file does not exist yet: the copy the run recorded is used.
    assert datareview.load_flags(run).plate_reason(P1A) == "old reason"
    new = DataFlags()
    new.set_plate(P1A, "new reason")
    ff.save(live, new)
    assert datareview.load_flags(run).plate_reason(P1A) == "new reason"
    assert datareview.load_flags(SetRun(tmp_path, "x")) is None


def test_photos_are_found_by_condition_time_plate_and_name(tmp_path):
    run = _run(tmp_path)
    assert datareview.candidate_photos(run, _cand()) == [(1, P1A), (2, P2A)]
    assert datareview.candidate_photos(run, _cand("b.jpg")) == [(1, P1B), (2, P2A)]
    assert datareview.candidate_photos(run, _cand("zz.jpg"))[0] == (1, None)


def test_only_flags_on_the_spots_a_candidate_scores_are_counted(tmp_path):
    run = _run(tmp_path)
    flags = DataFlags()
    flags.set_plate(P1B, "smeared")
    flags.set_spot(P2A, 2, 3, "bubble")      # row 2: the classic "middle" level
    flags.set_spot(P2A, 1, 3, "bubble")      # row 1: "least", not scored here
    assert datareview.candidate_flags(run, _cand(), flags) == (0, 1)
    assert datareview.candidate_flags(run, _cand("b.jpg"), flags) == (1, 1)
    assert datareview.candidate_flags(run, _cand(dilution="least"), flags) == (0, 1)
    assert datareview.candidate_flags(run, _cand(), None) == (0, 0)
    assert datareview.describe(1, 2) == "1 flagged plate and 2 flagged spots"


def test_a_recorded_layout_says_which_cells_a_level_scores(tmp_path):
    layout = {"n_rows": 6, "n_cols": 8, "levels": [
        {"name": "neat", "plates": {"1": [[5, 7, 1, 1]], "2": [[0, 0, 1, 2]]}}]}
    run = _run(tmp_path, layout=layout)
    flags = DataFlags()
    flags.set_spot(P1A, 6, 8, "bubble")
    assert datareview.candidate_flags(run, _cand(dilution="neat"), flags) == (0, 1)


def test_rebuilt_rows_carry_the_reason_and_nothing_else_changes(tmp_path):
    from results_review import rebuild
    import spotting_batch as sb
    import spotting_timecourse as tc

    root = tmp_path / "photos"
    shots = tuple(tc.Shot(path=root / rel, tp_hours=24.0, tp_label="24 Hours",
                          medium="GLU", medium_label="GLU", plate=plate)
                  for rel, plate in ((P1A, 1), (P2A, 2)))
    level = sb.classic_layout().by_name("middle")      # rows 2 and 5
    tidy = pd.DataFrame([
        {"plate": 1, "replicate": "rep1", "strain_col": 3, "outlier": False},
        {"plate": 1, "replicate": "rep2", "strain_col": 3, "outlier": False},
        {"plate": 2, "replicate": "rep3", "strain_col": 1, "outlier": False}])
    flags = DataFlags()
    flags.set_spot(P1A, 2, 3, "contamination")         # rep1 (row 2), slot 3
    flags.set_plate(P2A, "smeared")
    out = rebuild._mark_data_review(tidy, {"plates": shots}, root, level, flags)
    assert out.data_review.tolist() == ["contamination", "", "plate: smeared"]
    assert out.drop(columns="data_review").equals(tidy)
    assert rebuild._mark_data_review(tidy, {"plates": shots}, root, level,
                                     None).data_review.eq("").all()
    assert "data_review" in rebuild.ANNOTATIONS and "data_review" in COLUMN_ORDER


def test_the_table_says_it_in_words_and_a_persons_edit_still_wins():
    row = {"data_review": "contamination", "outlier": False}
    assert "⚑ data review: contamination" in theme.flag_text(row)
    assert theme.row_tags(row)[-1] == "datareview"
    omitted = dict(row, outlier=True, outlier_source="manual")
    tags = theme.row_tags(omitted)
    assert tags[-1] == "manual" and "datareview" in tags
    assert theme.row_tags({"outlier": True, "outlier_source": "auto",
                           "data_review": "x"})[-1] == "datareview"


def test_the_spot_table_points_out_flagged_spots_that_still_count(tk_root):
    import tkinter as tk

    from results_review.gui.table import SpotTable

    top = tk.Toplevel(tk_root)
    try:
        table = SpotTable(top, on_raw=None, on_outlier=None, on_note=None,
                          on_revert=None)
        base = {"strain": "A", "strain_col": 2, "raw_growth": 1.0,
                "relative_growth": 1.0}
        rows = [dict(base, replicate="rep1", data_review="bubble"),
                dict(base, replicate="rep2", data_review="")]
        table.show(rows, [])
        assert "1 still count" in table.hint_label.cget("text")
        rows[0]["outlier"] = True
        table.show(rows, [])
        assert "already omitted" in table.hint_label.cget("text")
        table.show([rows[1]], [])
        assert table.hint_label.cget("text") == table.hint
    finally:
        top.destroy()
