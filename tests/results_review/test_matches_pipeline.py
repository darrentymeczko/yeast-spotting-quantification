"""The acid test: rebuilding the pipeline's own pick reproduces its own output.

Everything else in this package rests on one claim -- that the numbers you
accept are the numbers the pipeline would have produced. `best/` is the
pipeline's unedited answer for its automatic winner, written by a separate run
of separate code. So rebuilding that same candidate here, with no edits, and
diffing against the file the run left behind is the only check that actually
tests the claim rather than testing this code against itself.

Needs the photographs. Skipped, loudly, when they are not reachable -- a pass
that happened because nothing ran would be worse than no test.
"""

from __future__ import annotations

import pytest
from conftest import needs_engine

from results_review import discovery, links
from results_review.rebuild import RebuildError, rebuild

pytestmark = needs_engine

#: Columns the pipeline itself writes. The annotations this tool adds are
#: checked separately; here the point is that the shared ones agree.
SHARED = ["experiment", "treatment", "plate", "image", "replicate",
          "dilution_row", "dilution", "strain_col", "strain", "raw_growth",
          "artifact", "excluded", "is_control", "control_raw", "control_mean",
          "relative_growth", "control_ok", "outlier"]

KEY = ["experiment", "replicate", "strain_col"]


def _reference_sets():
    """Results folders that have a best/ CSV to compare against."""
    out = []
    for path in discovery.list_sets():
        run = discovery.load_set(path)
        if (run.best_dir / "spotting_results_normalized.csv").exists():
            out.append(run)
    return out


def _ids(runs):
    return [r.label for r in runs]


REFERENCE = _reference_sets()


@pytest.mark.skipif(not REFERENCE, reason="no set has an exported best/ CSV")
@pytest.mark.parametrize("run", REFERENCE, ids=_ids(REFERENCE))
def test_rebuilding_the_pipelines_pick_reproduces_best(run):
    import pandas as pd

    root = links.resolve(run.label)
    if root is None:
        pytest.skip(f"{run.label}: the capture tree is not on this machine")
    try:
        cfg = links.load_config(root)
    except Exception as e:
        pytest.skip(f"{run.label}: {e}")

    want = pd.read_csv(run.best_dir / "spotting_results_normalized.csv",
                       encoding="utf-8-sig")

    compared = 0
    for medium in run.media:
        cand = run.pipeline_best(medium)
        try:
            frame = rebuild(root, run.label, cfg, cand, edits={})
        except RebuildError as e:
            pytest.skip(f"{run.label} {medium}: {e}")

        mine = frame.tidy
        theirs = want[want["experiment"] == frame.experiment]
        if theirs.empty:
            # best/ holds only the media whose winner built successfully.
            continue

        assert len(mine) == len(theirs), (
            f"{frame.experiment}: rebuilt {len(mine)} rows, best/ has "
            f"{len(theirs)}")

        a = mine.set_index(KEY).sort_index()
        b = theirs.set_index(KEY).sort_index()
        assert list(a.index) == list(b.index), (
            f"{frame.experiment}: different spots entirely")

        for col in SHARED:
            if col in KEY or col not in b.columns:
                continue
            if col in ("raw_growth", "control_raw", "control_mean",
                       "relative_growth"):
                pd.testing.assert_series_equal(
                    a[col].astype(float), b[col].astype(float),
                    check_names=False, rtol=1e-9, atol=1e-9,
                    obj=f"{frame.experiment}.{col}")
            else:
                assert a[col].astype(str).tolist() == \
                    b[col].astype(str).tolist(), \
                    f"{frame.experiment}: {col} differs"
        compared += 1

    assert compared, f"{run.label}: nothing could be compared"


@pytest.mark.skipif(not REFERENCE, reason="no set has an exported best/ CSV")
def test_the_annotations_are_inert_when_nothing_was_edited():
    """An unedited rebuild must add columns, not change anything."""
    run = REFERENCE[0]
    root = links.resolve(run.label)
    if root is None:
        pytest.skip("the capture tree is not on this machine")
    cfg = links.load_config(root)
    cand = run.pipeline_best(run.media[0])
    try:
        frame = rebuild(root, run.label, cfg, cand, edits={})
    except RebuildError as e:
        pytest.skip(str(e))

    t = frame.tidy
    assert not t["manual"].any()
    assert (t["edit_note"] == "").all()
    assert set(t["excluded_source"].unique()) <= {"", "config"}
    assert set(t["outlier_source"].unique()) <= {"", "auto"}
    assert (t["raw_growth_original"] == t["raw_growth"]).all()
    assert frame.n_edited == 0
