"""Rebuild one candidate's per-spot data, with the person's corrections applied.

Only the automatic winner's per-spot CSV survives a run: every other
candidate's tidy frame is written to a temp folder and deleted
(spotting_timecourse_figures.py:357). So disagreeing with the pipeline means
rebuilding, and rebuilding has to go through the ENGINE'S OWN functions rather
than a lookalike -- `tc.build_tidy_for_candidate`, `sq.add_relative_growth`,
`sq.flag_outliers`. A second implementation of the normalisation would be a
second thing to keep right, and the whole point of this tool is that the number
you accept is the number the pipeline would have produced.

Measurements come from `.spotting_cache`, so a rebuild is a fraction of a second
per candidate once the run has been done; the photos are still needed because
the cache is keyed on file name, size and mtime (spotting_batch.py:490).

The one thing done here and nowhere else is the edit layer: a correction is
applied to `raw_growth` BEFORE normalisation, which is what makes excluding a
control spot actually change the divisor instead of merely hiding a dot.
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from .discovery import DILUTION_ORDER
from .model import Candidate

#: Columns `sq.add_relative_growth` derives. Dropped before it is re-run, or the
#: merge it does inside would collide with the previous run's copies.
DERIVED = ("control_raw", "control_mean", "relative_growth", "control_ok")

#: Columns this module adds on top of the pipeline's own. The plot renderer
#: selects the columns it needs and ignores the rest, so the CSV stays readable
#: by the unmodified script.
ANNOTATIONS = ("raw_growth_original", "manual", "excluded_source",
               "outlier_source", "edit_note")


class RebuildError(RuntimeError):
    """The candidate could not be rebuilt -- photos missing, cache cold, etc."""


def require_engine() -> None:
    """Fail with something actionable if the measurement stack is absent.

    The engine is imported lazily so browsing works without it, which means its
    absence surfaces as a bare `No module named 'numpy'` at the moment somebody
    clicks something -- indistinguishable from a bug in this tool. Said once,
    here, so the window and the CLI give the same answer.
    """
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
        import numba  # noqa: F401
        import spotting_timecourse  # noqa: F401
    except ImportError as e:
        raise RebuildError(
            f"the measurement stack is not installed in this Python, so "
            f"nothing can be rebuilt ({e}).\n\n"
            f"    pip install -r requirements.txt\n\n"
            f"Browsing the sheets and choosing a candidate still work."
        ) from e


@dataclass
class Frame:
    """One rebuilt candidate: the tidy rows, plus what it took to make them."""

    medium: str
    candidate: Candidate
    experiment: str
    tidy: "object"                       # pandas.DataFrame
    control_col: int
    excluded_cols: list[int] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def n_edited(self) -> int:
        t = self.tidy
        if "edit_note" not in t.columns and "manual" not in t.columns:
            return 0
        touched = (t["manual"].astype(bool)
                   | t["excluded_source"].isin(["manual", "manual-cleared"])
                   | t["outlier_source"].isin(["manual", "cleared"]))
        return int(touched.sum())


# --- turning a Candidate back into the pipeline's own candidate dict --------

_TREE_CACHE: dict[tuple[str, tuple[int, ...]], list] = {}


def clear_cache() -> None:
    """Forget discovered capture trees (after photos are moved or relinked)."""
    _TREE_CACHE.clear()


def tree_candidates(root: Path, plates=(1, 2)) -> list:
    """`spotting_timecourse`'s candidate dicts for a capture tree.

    Cached per root and plate set: `discover` walks every timepoint folder,
    which is slow enough on OneDrive to be worth not repeating for each of a
    set's media. The plate set cannot be assumed to be ``(1, 2)``: templates
    may put every replicate on one large plate, as Set11 does.
    """
    import spotting_timecourse as tc

    plates = tuple(int(p) for p in plates)
    key = (str(Path(root).resolve()), plates)
    if key not in _TREE_CACHE:
        shots, bad = tc.discover(Path(root))
        if not shots:
            raise RebuildError(
                f"No photos found under {root} in the expected "
                f"timepoint/medium/plate layout."
                + (f" Problems: {'; '.join(bad[:3])}" if bad else ""))
        _TREE_CACHE[key] = tc.candidates(shots, plates=plates)
    return _TREE_CACHE[key]


def layout_for(cfg: "dict | None"):
    """The run's `DilutionLayout`: recorded in the config, else the classic one."""
    import spotting_batch as sb

    raw = (cfg or {}).get("dilution_layout")
    return sb.DilutionLayout.from_dict(raw) if raw else None


def rows_for(cand: Candidate, cfg: "dict | None" = None):
    """This candidate's dilution level, looked up by name in the run's layout.

    Returns a `spotting_batch.DilutionLevel`, which every pipeline entry point
    accepts in place of the old (lo, hi) row pair.
    """
    import spotting_batch as sb

    layout = layout_for(cfg) or sb.classic_layout()
    try:
        return layout.by_name(cand.dilution)
    except KeyError:
        names = layout.names or DILUTION_ORDER
        raise RebuildError(
            f"{str(cand.dilution)!r} is not one of {names}; the candidates CSV "
            f"and the pipeline disagree about dilution names.") from None


def pipeline_candidate(root: Path, cand: Candidate,
                       cfg: "dict | None" = None) -> dict:
    """The candidate dict (with real `Shot`s) matching this scored row.

    Matched on exactly the fields `spotting_timecourse_figures._match_candidate`
    uses first. Experiment-layer runs may preserve a declared condition name
    such as ``Glucose 37`` while the legacy folder scanner shortens that same
    directory to ``GLUCOSE3``. In that case, match the scanner's unmodified
    display label and restore the condition identity recorded in the results.
    """
    import spotting_timecourse_figures as tcf

    row = {"medium": cand.medium, "timepoint": cand.timepoint,
           **{f"plate{k}": p for k, p in enumerate(cand.photos, start=1)}}
    # Candidate CSV columns are positions in plate order, while the recorded
    # layout retains the actual plate ids. Fall back to 1..N for classic runs
    # whose handoff predates layouts. Most importantly, a one-plate large-format
    # assay remains one plate instead of being discarded while looking for #2.
    layout = layout_for(cfg)
    plates = tuple(layout.plates()) if layout and layout.plates() else \
        tuple(range(1, len(cand.photos) + 1))
    candidates = tree_candidates(root, plates=plates)
    got = tcf._match_candidate(candidates, row)
    if got is None:
        want_photos = tuple(cand.photos)

        def same_text(a, b) -> bool:
            return " ".join(str(a or "").split()).casefold() == \
                   " ".join(str(b or "").split()).casefold()

        for found in candidates:
            photos = tuple(s.path.name for s in found.get("plates", ()))
            same_condition = (
                same_text(found.get("medium_label"), cand.medium_label)
                or same_text(found.get("medium_label"), cand.medium)
            )
            if (same_condition
                    and same_text(found.get("tp_label"), cand.timepoint)
                    and photos == want_photos):
                # Downstream normalisation indexes cfg["media"] by this value.
                # Keep the real Shot paths from discovery, but use the exact
                # condition name that produced the scored CSV and its config.
                got = dict(found)
                got["medium"] = cand.medium
                got["medium_label"] = cand.medium_label or cand.medium
                break
    if got is None:
        raise RebuildError(
            f"{cand.medium} {cand.timepoint} {'+'.join(cand.photos)} is in "
            f"the results but not under {root}. The photos have moved or been "
            f"renamed since the run; link the folder those photos are in.")
    return got


# --- the rebuild -----------------------------------------------------------


def _apply_edits(tidy, edits: dict, control_col: int):
    """Overlay the person's corrections, and say what each row now is.

    Applied to `raw_growth` before anything is normalised, because that is the
    only order in which excluding or re-measuring a CONTROL spot does the right
    thing -- the divisor is the mean of the control spots, so a correction to
    one of them has to land before the mean is taken.
    """
    import pandas as pd

    tidy = tidy.copy()
    tidy["raw_growth_original"] = tidy["raw_growth"].astype(float)
    tidy["manual"] = False
    tidy["excluded_source"] = pd.Series(
        ["config" if bool(x) else "" for x in tidy["excluded"]],
        index=tidy.index, dtype=object)
    tidy["edit_note"] = ""

    if not edits:
        return tidy, []

    index = {(str(r.replicate), int(r.strain_col)): i
             for i, r in zip(tidy.index, tidy.itertuples())}
    unmatched = []
    for key, edit in edits.items():
        i = index.get((str(key[0]), int(key[1])))
        if i is None:
            unmatched.append(f"{key[0]} column {key[1]}")
            continue
        if edit.raw_growth is not None:
            tidy.at[i, "raw_growth"] = float(edit.raw_growth)
            tidy.at[i, "manual"] = True
        if edit.excluded is not None:
            was = bool(tidy.at[i, "excluded"])
            tidy.at[i, "excluded"] = bool(edit.excluded)
            if bool(edit.excluded) != was:
                tidy.at[i, "excluded_source"] = ("manual" if edit.excluded
                                                 else "manual-cleared")
        if edit.note:
            tidy.at[i, "edit_note"] = edit.note
    return tidy, unmatched


def _normalize(tidy, plates, control_col: int):
    """Re-run the pipeline's normalisation over the edited raw values.

    Byte-for-byte the same call `spotting_timecourse.build_tidy_for_candidate`
    makes, including the noise-aware control floor -- a fixed gray cutoff cannot
    work across media whose signal ranges differ by an order of magnitude.
    """
    import spotting_quant as sq

    tidy = tidy.drop(columns=[c for c in DERIVED if c in tidy.columns])
    keep = tidy[~tidy["excluded"].astype(bool)].copy()
    if keep.empty or not (keep["strain_col"] == control_col).any():
        tidy["relative_growth"] = float("nan")
        tidy["control_raw"] = float("nan")
        tidy["control_mean"] = float("nan")
        tidy["control_ok"] = False
        return tidy, ["every control spot is excluded, so nothing can be "
                      "normalised against it"]

    noise = max([sq.bg_noise(p.bg_samples) for p in plates] or [0.0])
    min_control = max(sq.MIN_CONTROL_GRAY, sq.CONTROL_NOISE_MULT * noise)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        keep = sq.add_relative_growth(keep, control_col=control_col,
                                      group_keys=["experiment"],
                                      min_control=min_control)
    key_cols = ["experiment", "replicate", "strain_col"]
    added = [c for c in keep.columns if c not in tidy.columns]
    out = tidy.merge(keep[key_cols + added], on=key_cols, how="left")
    return out, [ln.strip(" !") for ln in buf.getvalue().splitlines()
                 if ln.strip()]


def _flag_outliers(tidy, edits: dict):
    """Automatic outlier flags, then the person's overrides on top.

    Provenance is kept in `outlier_source` rather than thrown away: "the
    pipeline flagged this" and "I flagged this" are different claims, and a
    reader of the CSV is entitled to tell them apart.
    """
    import spotting_quant as sq

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        tidy = sq.flag_outliers(tidy, group_keys=["experiment", "strain"],
                                verbose=False)
    tidy["outlier_source"] = ["auto" if bool(x) else "" for x in tidy["outlier"]]

    if edits:
        index = {(str(r.replicate), int(r.strain_col)): i
                 for i, r in zip(tidy.index, tidy.itertuples())}
        for key, edit in edits.items():
            if edit.outlier is None:
                continue
            i = index.get((str(key[0]), int(key[1])))
            if i is None:
                continue
            was = bool(tidy.at[i, "outlier"])
            tidy.at[i, "outlier"] = bool(edit.outlier)
            if bool(edit.outlier) != was:
                tidy.at[i, "outlier_source"] = ("manual" if edit.outlier
                                                else "cleared")
    return tidy, [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]


def rebuild(root: Path, label: str, cfg: dict, cand: Candidate,
            edits: "dict | None" = None, cache_dir: "Path | None" = None) -> Frame:
    """Everything behind one candidate's graph, corrections included.

    One code path for every candidate, the pipeline's automatic winner
    included, so the numbers can never depend on which one was picked.
    """
    require_engine()
    import spotting_timecourse as tc

    from . import PROJECT_ROOT

    cache_dir = Path(cache_dir or PROJECT_ROOT / ".spotting_cache")
    cand_dict = pipeline_candidate(root, cand, cfg)
    level = rows_for(cand, cfg)
    layout = layout_for(cfg)
    control_col, exclude = tc.medium_cfg(cfg, cand.medium)
    experiment = tc._tc_experiment(cand_dict, label)

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            tidy = tc.build_tidy_for_candidate(cand_dict, level, cfg, cache_dir,
                                               label, layout)
            # lru_cached, so this is a dict lookup rather than a second read.
            plates = [tc._cached_measure(s.path, s.plate,
                                         level.quant_rows(s.plate), cache_dir,
                                         layout)
                      for s in tc.cand_shots(cand_dict)]
    except Exception as e:
        raise RebuildError(
            f"could not measure {cand.plate1} + {cand.plate2}: "
            f"{type(e).__name__}: {e}") from e
    if tidy is None or tidy.empty:
        raise RebuildError(
            f"{experiment} produced no rows -- the strain panel in "
            f"timecourse_config.json may not match this plate.")

    messages: list[str] = []
    tidy, unmatched = _apply_edits(tidy, edits or {}, control_col)
    for who in unmatched:
        messages.append(f"edit for {who} does not match any spot in this "
                        f"candidate; it was kept but not applied")
    tidy, notes = _normalize(tidy, plates, control_col)
    messages += notes
    tidy, notes = _flag_outliers(tidy, edits or {})
    messages += notes

    return Frame(medium=cand.medium, candidate=cand, experiment=experiment,
                 tidy=tidy, control_col=control_col,
                 excluded_cols=list(exclude), messages=messages)


def summarize(frame: Frame, *, statistical_test: str = "t_test",
              p_adjust: str = "none", alpha: float = 0.05) -> list[dict]:
    """Mean relative growth per strain, over the rows that actually count.

    The same rows `spotting_plots.py` keeps: not excluded, not an artifact, not
    flagged an outlier. Shown beside the table so the effect of a correction is
    visible before the graph is redrawn.
    """
    t = frame.tidy
    use = (~t["excluded"].astype(bool) & ~t["artifact"].astype(bool)
           & ~t["outlier"].astype(bool) & t["relative_growth"].notna())
    out = []
    # Ordered by plate column, NOT by first appearance. Grouping in encounter
    # order re-sorts the whole panel the moment a strain's first replicate is
    # dropped -- excluding the control's rep1 sends the control to the bottom of
    # the list -- and a table that rearranges itself as you edit it is one you
    # cannot read a before-and-after off.
    for col, g in t[use].groupby("strain_col", sort=True):
        v = g["relative_growth"].astype(float)
        out.append({"strain": str(g["strain"].iloc[0]), "strain_col": int(col),
                    "n": int(len(v)), "mean": float(v.mean()),
                    "sd": float(v.std()) if len(v) > 1 else 0.0})

    # Use the plotting engine's statistical routines so the number beside a
    # strain is the same number used to annotate the graph. Ratio t-tests are
    # pairwise against the positive-control baseline. ANOVA is omnibus, so its
    # one p-value appears once on the control row rather than being misleadingly
    # repeated as though it were a post-hoc pairwise result.
    for row in out:
        row.update(p_value=None, significant=False,
                   p_heading=("p vs +ctrl" if statistical_test == "t_test"
                              else "ANOVA p"))
    if not out:
        return out

    control_rows = t[use & (t["strain_col"].astype(int) == frame.control_col)]
    if control_rows.empty:
        return out
    control = str(control_rows["strain"].iloc[0])
    group = t[use].copy()
    group["value"] = group["relative_growth"].astype(float)

    import spotting_plots

    if statistical_test == "t_test":
        tests = spotting_plots._ratio_tests(group, control, p_adjust)
        p_column = "p" if p_adjust == "none" else "p_adj"
        values = {str(r.group2): float(getattr(r, p_column))
                  for r in tests.itertuples()}
        for row in out:
            p_value = values.get(row["strain"])
            row["p_value"] = p_value
            row["significant"] = (p_value is not None and p_value == p_value
                                  and p_value <= alpha)
    elif statistical_test == "anova":
        tests = spotting_plots._anova_test(group, control)
        p_value = float(tests.iloc[0]["p"]) if not tests.empty else float("nan")
        for row in out:
            if row["strain_col"] == frame.control_col:
                row["p_value"] = p_value
                row["significant"] = p_value == p_value and p_value <= alpha
                break
    else:
        raise ValueError("statistical_test must be 't_test' or 'anova'")
    return out
