"""Exporting a review: what lands in chosen/, and what must not change.

Runs against a COPY of a real results folder, so the promise that `best/` is
never written to is checked by hashing it rather than by trusting a comment.
The copy keeps the set's folder name, so the capture tree is still found the
way it is in normal use.

Montages are not drawn here: each is two background subtractions, measured at
~86 s on a machine without FIJI, and what they prove -- that the pipeline's own
montage worker can be called -- is not worth three minutes on every test run.
"""

from __future__ import annotations

import hashlib
import shutil

import pytest
from conftest import needs_engine

from results_review import discovery, links, review as rv
from results_review.export import CSV_NAME, SUMMARY_NAME, export
from results_review.rebuild import RebuildError, rebuild

pytestmark = needs_engine


def _hashes(folder):
    return {p.relative_to(folder): hashlib.sha1(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """A real set, copied, with a non-default pick and two corrections applied."""
    src = None
    for path in discovery.list_sets():
        run = discovery.load_set(path)
        if (run.best_dir / CSV_NAME).exists() and len(run.media) >= 2:
            src = run
            break
    if src is None:
        pytest.skip("no set with an exported best/ to copy")

    root = links.resolve(src.label)
    if root is None:
        pytest.skip(f"{src.label}: the capture tree is not on this machine")
    cfg = links.load_config(root)

    dst = tmp_path_factory.mktemp("rr") / src.label
    dst.mkdir()
    shutil.copy2(src.candidates_csv, dst)
    shutil.copytree(src.best_dir, dst / "best")
    before = _hashes(dst / "best")

    run = discovery.load_set(dst)
    review = rv.with_defaults(run, rv.Review())

    # Disagree with the pipeline on the first medium, and correct two spots:
    # one control excluded, one strain re-measured by hand.
    medium = run.media[0]
    default = run.pipeline_best(medium)
    other = next((c for c in run.for_medium(medium) if c.key != default.key),
                 None)
    if other is None:
        pytest.skip(f"{medium} has only one candidate; nothing to disagree with")
    review.set_pick(medium, other, reason="test: a deliberate disagreement")

    try:
        first = rebuild(root, run.label, cfg,
                        rv.chosen_candidate(run, review, medium), {})
    except RebuildError as e:
        pytest.skip(str(e))
    ctrl = first.tidy[first.tidy["is_control"]].iloc[0]
    review.amend(medium, ctrl["replicate"], int(ctrl["strain_col"]),
                 excluded=True, note="test: dropped")
    review.amend(medium, ctrl["replicate"], int(ctrl["strain_col"]) + 1,
                 raw_growth=42.0, note="test: hand-measured")

    frames = []
    for m in run.media:
        cand = rv.chosen_candidate(run, review, m)
        try:
            frames.append(rebuild(root, run.label, cfg, cand,
                                  review.edits_for(m)))
        except RebuildError:
            pass
    if not frames:
        pytest.skip("nothing could be rebuilt")

    rv.save_for(run, review)
    result = export(run, review, frames, root, cfg, montages=False)
    return {"run": run, "review": review, "frames": frames, "result": result,
            "before": before, "medium": medium, "first": first,
            "ctrl": ctrl, "default": default, "other": other}


# --- what is written -------------------------------------------------------

def test_the_csv_is_written_even_without_r(exported):
    """R is optional; the numbers are not."""
    result = exported["result"]
    assert result.ok and result.csv_path.exists()
    assert result.csv_path.name == CSV_NAME
    assert result.summary_path.name == SUMMARY_NAME


def test_best_is_byte_identical_afterwards(exported):
    run = exported["run"]
    assert _hashes(run.best_dir) == exported["before"]


def test_chosen_is_a_separate_folder(exported):
    run = exported["run"]
    assert run.chosen_dir != run.best_dir
    assert run.chosen_dir.is_dir()


def test_every_rebuilt_medium_is_in_one_csv(exported):
    import pandas as pd

    from results_review.export import named_frames

    out = pd.read_csv(exported["result"].csv_path, encoding="utf-8-sig")
    got = set(out["experiment"].unique())
    named = named_frames(exported["run"], exported["frames"])
    assert got == {f.experiment for f in named}, (
        "all media must share one CSV, or R overwrites its own t-test table")
    # Named in the experiment's words, not the treatments' generated codes.
    run = exported["run"]
    for f in exported["frames"]:
        assert (f"{run.label} {run.medium_label(f.medium)} "
                f"{f.candidate.timepoint}") in got


def test_the_pipelines_columns_come_first(exported):
    import pandas as pd

    out = pd.read_csv(exported["result"].csv_path, encoding="utf-8-sig")
    assert list(out.columns)[:10] == [
        "experiment", "treatment", "set", "plate", "image", "replicate",
        "dilution_row", "dilution", "strain_col", "strain"]
    for col in ("raw_growth_original", "manual", "excluded_source",
                "outlier_source", "edit_note"):
        assert col in out.columns


# --- what it records -------------------------------------------------------

def test_the_corrections_are_visible_in_the_csv(exported):
    import pandas as pd

    out = pd.read_csv(exported["result"].csv_path, encoding="utf-8-sig")
    edited = out[out["edit_note"].notna() & (out["edit_note"] != "")]
    assert len(edited) == 2, "both corrections must be recorded"
    assert set(edited["edit_note"]) == {"test: dropped", "test: hand-measured"}

    hand = edited[edited["manual"].astype(bool)].iloc[0]
    assert float(hand["raw_growth"]) == 42.0
    assert float(hand["raw_growth_original"]) != 42.0, (
        "the measured value must survive being overridden")


def test_the_summary_names_the_decision(exported):
    import csv

    with exported["result"].summary_path.open(encoding="utf-8-sig") as fh:
        rows = {r["medium"]: r for r in csv.DictReader(fh)}
    mine = rows[exported["medium"]]
    assert mine["is_pipeline_default"] == "False"
    assert mine["reason"] == "test: a deliberate disagreement"
    assert int(mine["n_edited_spots"]) == 2
    assert mine["timepoint"] == exported["other"].timepoint
    assert mine["statistical_test"] == "t_test"
    assert mine["p_adjust"] == "none"
    assert float(mine["p_cutoff"]) == pytest.approx(0.05)


def test_choosing_a_different_candidate_changes_the_experiment(exported):
    """The figure is named from `experiment`, so a different pick must rename it."""
    frame = exported["frames"][0]
    assert exported["other"].timepoint in frame.experiment


# --- the arithmetic, on real data ------------------------------------------

def test_excluding_a_real_control_spot_moves_its_whole_plate(exported):
    """The claim, checked on measured numbers rather than a fixture."""
    ctrl = exported["ctrl"]
    plate = int(ctrl["plate"])

    def rels(frame):
        t = frame.tidy
        sel = t[(t["plate"] == plate) & (~t["is_control"])]
        return sel.set_index(["replicate", "strain_col"])["relative_growth"]

    before = rels(exported["first"])
    after = rels(exported["frames"][0])
    common = before.index.intersection(after.index)
    assert len(common), "no comparable spots"
    moved = (before[common].round(9) != after[common].round(9)).sum()
    assert moved == len(common), (
        f"only {moved} of {len(common)} strain spots on plate {plate} moved; "
        f"excluding a control must rescale all of them")


def test_the_other_plate_does_not_move(exported):
    """Normalisation is per plate: correcting one must not touch the other."""
    plate = int(exported["ctrl"]["plate"])

    def rels(frame):
        t = frame.tidy
        sel = t[(t["plate"] != plate) & (~t["is_control"])]
        return sel.set_index(["replicate", "strain_col"])["relative_growth"]

    before, after = rels(exported["first"]), rels(exported["frames"][0])
    common = before.index.intersection(after.index)
    if not len(common):
        pytest.skip("this candidate has only one plate of comparable spots")
    assert before[common].round(9).equals(after[common].round(9))


def test_the_review_round_trips_through_its_file(exported):
    run = exported["run"]
    assert run.review_path.exists()
    assert rv.load_for(run).to_dict() == exported["review"].to_dict()
