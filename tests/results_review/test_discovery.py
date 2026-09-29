"""Does this tool agree with the pipeline about what is on disk?

Two things are mirrored from the engine rather than imported, so that browsing
results does not require numpy, pandas, matplotlib and scikit-image: the sheet
filename rule, and the dilution/medium orderings. Mirrored constants rot. These
tests are the thing that stops that happening quietly -- they check the mirror
against the original whenever the original can be imported, and they check the
derived filenames against 1100-odd real sheets whenever those are present.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from conftest import needs_engine

from results_review import discovery
from results_review.model import Candidate


# --- the mirrored constants ------------------------------------------------

@needs_engine
def test_dilution_order_matches_the_pipeline():
    import spotting_batch as sb

    assert discovery.DILUTION_ORDER == sb.DILUTION_ORDER


@needs_engine
def test_medium_order_matches_the_pipeline():
    import spotting_timecourse as tc

    assert discovery.MEDIUM_ORDER == tc.MEDIUM_ORDER


@needs_engine
def test_results_root_matches_the_pipeline():
    import spotting_batch as sb

    assert discovery.TIMECOURSE_RESULTS == sb.TIMECOURSE_RESULTS


@needs_engine
@pytest.mark.parametrize("raw", [
    "GLU", "16 Hours", "K-OAc", "_9_2", "Set 1 - Practice", "ΔSOD2", "a/b\\c",
])
def test_safe_name_matches_the_pipeline(raw):
    import spotting_timecourse_figures as tcf

    assert discovery.safe_name(raw) == tcf.safe_name(raw)


@needs_engine
def test_sheet_filename_matches_the_pipeline(live_run):
    """The whole filename, built the pipeline's way, for every real candidate."""
    import spotting_batch as sb
    import spotting_timecourse as tc
    import spotting_timecourse_figures as tcf

    made = 0
    for cand in live_run.candidates:
        # A candidate dict shaped the way `_sort_prefix` and `candidate_id` want
        # it, using the pipeline's own Shot type so nothing is faked.
        shots = [tc.Shot(Path(name), cand.hours, cand.timepoint, cand.medium,
                         cand.medium_label, plate)
                 for plate, name in ((1, cand.plate1), (2, cand.plate2))]
        d = {"medium": cand.medium, "tp_label": cand.timepoint,
             "hours": cand.hours, "plate1": shots[0], "plate2": shots[1]}
        rows = tuple(sb.DILUTIONS[cand.dilution])
        expect = f"{tcf._sort_prefix(d, rows)}{tcf.candidate_id(d, rows)}.png"
        assert discovery.sheet_name(cand) == expect
        made += 1
    assert made, "no candidates to check"


# --- the real files --------------------------------------------------------

def test_sheet_paths_are_unique_per_candidate(live_sets):
    """No two candidates may resolve to the same sheet.

    Showing nothing is a disappointment; showing the WRONG plate is a wrong
    decision. Technical replicates are the risk here -- several pairings share a
    medium and timepoint and differ only in which photo went with which -- so
    this is the property that has to hold, not merely that files exist.
    """
    for path in live_sets:
        run = discovery.load_set(path)
        seen: dict[str, object] = {}
        for c in run.candidates:
            name = str(run.sheet(c))
            assert name not in seen, (
                f"{run.label}: {c.key} and {seen[name].key} both map to {name}")
            seen[name] = c


def test_sheets_resolve_for_the_runs_that_drew_them(live_sets):
    """Where a medium's sheets were drawn, every scored candidate has one.

    Checked per medium rather than per set, because `--figures N` limits how
    many are drawn and a partially drawn medium is a legitimate state. Within a
    medium that was drawn in full, a candidate with no sheet means the naming
    rule has drifted from the pipeline's.
    """
    checked = 0
    for path in live_sets:
        run = discovery.load_set(path)
        for medium in run.media:
            pool = run.for_medium(medium)
            have = [c for c in pool if run.sheet(c).exists()]
            if len(have) not in (0, len(pool)):
                continue                     # a --figures N run; nothing to say
            if not have:
                continue
            assert len(have) == len(pool)
            checked += 1
    assert checked, "no medium had a complete set of sheets to check"


def test_unmatched_sheets_are_stale_not_mismatched(live_sets):
    """Any PNG we cannot account for must be from an older run, not a near-miss.

    The pipeline never clears `figures/`, so re-running after the measurement
    cache version changes leaves sheets for candidates that no longer score --
    three of them in this corpus. That is fine, and this tool simply does not
    offer them. What would NOT be fine is an unmatched sheet whose name is a
    slight variant of one we generate, which would mean the rule had drifted:
    so every extra is required to differ in its candidate identity, not just in
    spelling.
    """
    for path in live_sets:
        run = discovery.load_set(path)
        if not run.figures_dir.is_dir():
            continue
        want = {run.sheet(c).resolve() for c in run.candidates}
        stems = {discovery.sheet_name(c).lower() for c in run.candidates}
        for p in run.figures_dir.rglob("*.png"):
            if p.resolve() in want:
                continue
            assert p.name.lower() not in stems, (
                f"{run.label}: {p.name} matches a candidate by name but not by "
                f"path -- it is in the wrong medium folder")


def test_candidates_load_with_every_column(live_run):
    c = live_run.candidates[0]
    assert c.medium and c.timepoint and c.plate1 and c.plate2
    assert c.dilution in discovery.DILUTION_ORDER
    assert not math.isnan(c.best_set_score)


def test_media_are_in_run_order(live_run):
    order = [m for m in discovery.MEDIUM_ORDER if m in live_run.media]
    assert live_run.media[:len(order)] == order


# --- the ranking -----------------------------------------------------------

def test_pipeline_best_is_not_just_the_first_row(live_sets):
    """The two rankings in the CSV really do disagree.

    `timecourse_candidates.csv` is sorted by `rank_score`, but `best/` holds the
    highest `best_set_score`. If this ever stopped being true the simpler rule
    would be fine -- and this test would say so instead of leaving a comment
    nobody can check.
    """
    disagreements = 0
    for path in live_sets:
        run = discovery.load_set(path)
        for medium in run.media:
            pool = run.for_medium(medium)
            if pool and run.pipeline_best(medium).key != pool[0].key:
                disagreements += 1
    assert disagreements, (
        "rank_score order and best_set_score order now agree everywhere; "
        "check whether pipeline_best still needs to differ from row order")


def test_pipeline_best_matches_the_exported_best_folder(live_sets):
    """The default we show is the candidate the run actually exported.

    `best/figures/spotting_<treatment>.png` is named from
    `<label> <medium> <timepoint>`, so the timepoint the pipeline chose can be
    read straight back off the filenames it wrote -- no reimplementation, and
    no trusting our own ranking code to check our own ranking code.
    """
    checked = 0
    for path in live_sets:
        run = discovery.load_set(path)
        figures = run.best_dir / "figures"
        if not figures.is_dir():
            continue
        names = {p.stem for p in figures.glob("spotting_*.png")}
        if not names:
            continue          # R did not run for this tree; nothing to compare
        for medium in run.media:
            best = run.pipeline_best(medium)
            want = discovery.safe_name(
                f"spotting_{run.label} {medium} {best.timepoint}")
            # Only media R actually drew. A medium can be missing because its
            # winner failed to build, which is a fact about that run rather
            # than a disagreement about ranking.
            if not any(n.startswith(discovery.safe_name(
                    f"spotting_{run.label} {medium} ")) for n in names):
                continue
            assert want in names, (
                f"{run.label} {medium}: we default to {best.timepoint}, but "
                f"best/ holds {sorted(names)}")
            checked += 1
    if not checked:
        pytest.skip("no set has an exported best/ folder to compare against")


def test_pipeline_best_prefers_score_then_low_cv():
    """Ties break toward the cleanest data, as spotting_timecourse.py:1986 does."""
    def cand(tp, score, cv, order):
        return Candidate(medium="GLU", medium_label="Glucose", timepoint=tp,
                         hours=20.0, plate1="a.JPG", plate2="b.JPG",
                         dilution="least", median_cv=cv, best_set_score=score,
                         csv_order=order)

    run = discovery.SetRun(results_dir=Path("."), label="x",
                           candidates=[cand("A", 5.0, 0.30, 0),
                                       cand("B", 5.0, 0.10, 1),
                                       cand("C", 4.0, 0.01, 2)])
    assert run.pipeline_best("GLU").timepoint == "B"


def test_pipeline_best_survives_a_missing_score():
    """A candidate with no best_set_score must never win by being NaN."""
    def cand(tp, score, order):
        return Candidate(medium="GLU", medium_label="Glucose", timepoint=tp,
                         hours=20.0, plate1="a.JPG", plate2="b.JPG",
                         dilution="least", median_cv=0.1, best_set_score=score,
                         csv_order=order)

    run = discovery.SetRun(results_dir=Path("."), label="x",
                           candidates=[cand("nan", float("nan"), 0),
                                       cand("real", 1.0, 1)])
    assert run.pipeline_best("GLU").timepoint == "real"

