"""Purpose 1: what the data review flags is left out of the multi-step analysis.

The endpoint analysis is NOT changed by a flag; the run only records the flags
beside its results, for the results review to mark (purpose 2, tested in
test_data_review_marks.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from data_review.flags import DataFlags
from experiments import intake, multistep, run
from experiments.model import Condition, Experiment, MultiStepAnalysis
from plate_template.presets import lab_standard_8x6
from results_review import discovery


def experiment():
    return Experiment(name="Synthetic", strains=["WT", "mutant"], control_slot=1,
                      conditions=[Condition("GLU")],
                      multi_step=MultiStepAnalysis(enabled=True, dilution=0,
                                                   hours=(24.,)))


def resolution():
    return intake.Resolution([intake.PhotoRow(
        relpath=f"24/p{p}-{t}.jpg", condition="GLU", timepoint=24., plate=p,
        status="ok") for p in (1, 2) for t in ("A", "B")])


def label(e, res, skip=()):
    e.multi_step.plate_ids = {r.relpath: Path(r.relpath).stem.split("-")[-1]
                              for r in res.rows if r.relpath not in skip}


def test_exclusions_merge_the_dialog_and_the_data_review():
    e = experiment()
    e.multi_step.excluded_photos = {"a.jpg": "duplicate", "b.jpg": "blurred"}
    got = multistep.exclusions(e, {"b.jpg": "smeared", "c.jpg": "cracked"})
    assert got == {"a.jpg": "duplicate", "b.jpg": "blurred; data review: smeared",
                   "c.jpg": "data review: cracked"}
    assert multistep.exclusions(e) == e.multi_step.excluded_photos


def test_a_flagged_plate_needs_no_technical_label_and_is_no_duplicate():
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    flagged = {"24/p1-B.jpg": "smeared"}
    label(e, res, skip=flagged)
    assert any("Assign technical" in m for m in multistep.design_errors(e, template, res))
    assert not multistep.design_errors(e, template, res, flagged)
    # Two photos labelled the same plate: the flagged one is not counted.
    e.multi_step.plate_ids["24/p1-B.jpg"] = "A"
    assert any("duplicate" in m for m in multistep.design_errors(e, template, res))
    assert not multistep.design_errors(e, template, res, flagged)
    # The designer's own checks see the same thing.
    assert not [i for i in intake.check_resolution(e, res, template, flagged)
                if i.code == "multi_step_photos"]


def _measure(monkeypatch, calls):
    _, tc = run._pipeline()

    def measure(path, plate, rows, cache, layout):
        calls.append(Path(path).name)
        net = np.full((6, 8), 20.)
        net[:, 1] = 10.
        return SimpleNamespace(net=net, rim=np.zeros((6, 8), dtype=bool),
                               bg_samples=np.zeros(20))

    monkeypatch.setattr(tc, "_cached_measure", measure)


def test_flagged_plates_and_spots_carry_a_reason_into_the_analysis(monkeypatch):
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    label(e, res)
    review = DataFlags()
    review.set_plate("24/p2-B.jpg", "smeared")
    # Row 1, column 2 of plate 1 photo A: the mutant (slot 2), replicate 1.
    review.set_spot("24/p1-A.jpg", 1, 2, "contamination")
    calls = []
    _measure(monkeypatch, calls)
    data = run.measure_multi_step(e, res, template, review)
    # The flagged plate is not even measured; it is still audited.
    assert "p2-B.jpg" not in calls and len(calls) == 3
    plate = data[data.image == "24/p2-B.jpg"]
    assert len(plate) and plate.qc_reason.str.contains("data review: smeared").all()
    spot = data[(data.image == "24/p1-A.jpg") & (data.row == 1) & (data.column == 2)]
    assert spot.qc_reason.tolist() == ["data review: contamination"]
    clean = data[(data.image == "24/p1-A.jpg") & ~((data.row == 1) & (data.column == 2))]
    assert clean.qc_reason.eq("").all()
    # Without a review nothing is left out: the flags are the only change.
    calls.clear()
    plain = run.measure_multi_step(e, res, template)
    assert plain.qc_reason.eq("").all() and len(calls) == 4


def test_a_flagged_control_spot_invalidates_only_its_own_pairing(monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import spotting_multistep as analysis

    e, res, template = experiment(), resolution(), lab_standard_8x6()
    label(e, res)
    review = DataFlags()
    review.set_spot("24/p1-A.jpg", 1, 1, "bubble")       # WT, replicate 1
    _measure(monkeypatch, [])
    data = run.measure_multi_step(e, res, template, review)
    _, pairs = analysis.prepare_contrasts(data)
    bad = pairs[~pairs.included]
    assert len(bad) == 1
    assert "matched control: data review: bubble" in bad.qc_reason.iloc[0]


def test_the_run_hands_the_review_on_and_records_it_beside_the_results(
        monkeypatch, tmp_path):
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    label(e, res)
    review = DataFlags()
    review.set_plate("24/p1-B.jpg", "smeared")
    seen = {}
    monkeypatch.setattr(run, "_run_timecourse",
                        lambda *a, **k: seen.update(current=k) or 0)
    monkeypatch.setattr(run, "run_multi_step",
                        lambda *a, **k: seen.update(additional=k))
    side = tmp_path / "Synthetic.datareview.json"
    assert run.run(e, res=res, template=template, outdir=tmp_path,
                   review=review, review_path=side) == 0
    assert seen["additional"]["review"] is review
    assert seen["current"]["review"] is review
    assert seen["current"]["review_path"] == side

    # What the results review will read back.
    run.write_handoff(e, res, tmp_path, review=review, review_path=side)
    info = discovery.load_run_info(tmp_path)
    assert info.data_review_file == str(side)
    assert DataFlags.from_dict(info.data_review_snapshot).plate_reason(
        "24/p1-B.jpg") == "smeared"


def test_a_handoff_without_a_review_still_reads(tmp_path):
    e, res = experiment(), resolution()
    run.write_handoff(e, res, tmp_path)
    raw = json.loads((tmp_path / "experiment.json").read_text(encoding="utf-8"))
    assert raw["data_review"] == {"file": "", "flags": None}
    info = discovery.load_run_info(tmp_path)
    assert info.data_review_file == "" and info.data_review_snapshot == {}
