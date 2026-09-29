"""Write the reviewed result to `Results/Timecourse/<set>/chosen/`.

Laid out exactly like the pipeline's own `best/`, and produced by the same two
calls -- `spotting_batch.run_plots` for figures and paired tests,
`spotting_timecourse._draw_montage_job` for the spot montages -- so a `chosen/`
figure is comparable to a `best/` figure rather than merely similar to one.

`best/` is never written to. A re-run of the pipeline must be free to overwrite
its own output without destroying a review, and a reader must be able to see
what the automatic pick was and what a person decided instead. Keeping them in
two folders is what makes that diffable.

All three media go into one tidy CSV and one plotting call. The renderer keys
its figures on `treatment` and writes one paired t-test table per invocation, so
a call per medium would leave each overwriting the last one's statistics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .discovery import SetRun, safe_name
from .model import Review
from .rebuild import Frame

CSV_NAME = "spotting_results_normalized.csv"
SUMMARY_NAME = "chosen_summary.csv"

#: Written into the CSV in this order: the pipeline's own columns first, so a
#: `chosen/` file opens looking like a `best/` file, then this tool's
#: annotations. The PyPrism renderer picks the columns it needs by name.
COLUMN_ORDER = [
    "experiment", "treatment", "set", "plate", "image", "replicate",
    "dilution_row", "dilution", "strain_col", "strain", "raw_growth",
    "artifact", "excluded", "is_control", "control_raw", "control_mean",
    "relative_growth", "control_ok", "outlier",
    "raw_growth_original", "manual", "excluded_source", "outlier_source",
    "edit_note",
]


@dataclass
class ExportResult:
    csv_path: "Path | None" = None
    summary_path: "Path | None" = None
    figures: list[Path] = field(default_factory=list)
    montages: list[Path] = field(default_factory=list)
    ttests: "Path | None" = None
    statistics: "Path | None" = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.csv_path is not None


def _ordered(tidy):
    cols = [c for c in COLUMN_ORDER if c in tidy.columns]
    return tidy[cols + [c for c in tidy.columns if c not in cols]]


def write_csv(frames: list[Frame], outdir: Path) -> Path:
    """One tidy CSV covering every medium, in medium order."""
    import pandas as pd

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tidy = pd.concat([f.tidy for f in frames], ignore_index=True)
    path = outdir / CSV_NAME
    # utf-8-sig to match every other CSV the pipeline writes: Excel reads the
    # strain names (dATX1 and friends are spelled with a real delta) as mojibake
    # without the BOM.
    _ordered(tidy).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_summary(run: SetRun, review: Review, frames: list[Frame],
                  outdir: Path) -> Path:
    """What was picked for each medium, whether it was the pipeline's pick, why.

    The point of the whole tool is the decision, so the decision is written down
    next to its result instead of living only in `review.json`.
    """
    import csv as _csv

    from . import review as rv

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / SUMMARY_NAME
    # plate3, plate4, ... only when some chosen candidate has them, so a
    # two-plate summary keeps exactly the columns it always had.
    n_extra = max((len(f.candidate.extra_plates) for f in frames), default=0)
    extra_cols = [f"plate{k}" for k in range(3, 3 + n_extra)]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["medium", "experiment", "timepoint", "dilution", "plate1",
                    "plate2", *extra_cols, "is_pipeline_default",
                    "best_set_score",
                    "median_CV", "n_significant", "n_strains", "n_edited_spots",
                    "statistical_test", "p_adjust", "p_cutoff", "reason"])
        for f in frames:
            c = f.candidate
            pick = review.pick(f.medium)
            extras = list(c.extra_plates) + [""] * (n_extra - len(c.extra_plates))
            w.writerow([f.medium, f.experiment, c.timepoint, c.dilution,
                        c.plate1, c.plate2, *extras,
                        rv.is_default(run, review, f.medium),
                        f"{c.best_set_score:.4f}", f"{c.median_cv:.4f}",
                        c.n_significant, c.n_strains, f.n_edited,
                        review.statistical_test,
                        review.p_adjust if review.statistical_test == "t_test"
                        else "not applicable",
                        review.alpha,
                        (pick.reason if pick else "")])
    return path


def draw_figures(csv_path: Path, outdir: Path, *, statistical_test: str = "t_test",
                 p_adjust: str = "none", alpha: float = 0.05
                 ) -> tuple[list[Path], "Path | None"]:
    """Run the PyPrism renderer over the exported CSV.

    The renderer honours `artifact`, `excluded` and `outlier` straight from the
    file, so every correction made here reaches the
    figure and the paired t-tests without a line of statistics code in this
    package.
    """
    import contextlib
    import io

    import spotting_batch as sb

    outdir = Path(outdir)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sb.run_plots(Path(csv_path), outdir, statistical_test=statistical_test,
                     p_adjust=p_adjust, alpha=alpha)
    figs = sorted((outdir / "figures").glob("spotting_*.png"))
    name = ("spotting_paired_ttests.csv" if statistical_test == "t_test"
            else "spotting_anova.csv")
    stats = outdir / "figures" / name
    return figs, (stats if stats.exists() else None)


def draw_preview(frame: Frame, workdir: Path, *, statistical_test: str = "t_test",
                 p_adjust: str = "none",
                 alpha: float = 0.05,
                 original_sheet: "Path | None" = None) -> "tuple[Path | None, bool]":
    """Redraw one medium and, when possible, retain its spot-image panel."""
    out = Path(workdir) / safe_name(frame.medium)
    try:
        graph = _draw_preview_with_pyprism(
            frame, workdir, statistical_test=statistical_test,
            p_adjust=p_adjust, alpha=alpha)
        if original_sheet is not None and Path(original_sheet).exists():
            identity = ("_".join(str(value) for value in frame.candidate.key)
                        if frame.candidate is not None else frame.experiment)
            sheet = out / f"review_{safe_name(identity)}.png"
            _combine_preview(Path(original_sheet), graph, sheet)
            return sheet, True
        return graph, True
    except Exception as exc:
        out.mkdir(parents=True, exist_ok=True)
        (out / "plot_error.log").write_text(str(exc), encoding="utf-8")
        return None, False


def _draw_preview_with_pyprism(frame: Frame, workdir: Path, *,
                               statistical_test: str = "t_test",
                               p_adjust: str = "none",
                               alpha: float = 0.05) -> "Path | None":
    """Redraw ONE medium's graph from its corrected numbers.

    The comparison sheet in the window was drawn by the pipeline from the
    pipeline's numbers, so the moment a spot is excluded or re-measured it shows
    something that is no longer true. This regenerates the graph -- through the
    the same PyPrism renderer, so the dots, error bars, and significance
    marks are what the exported figure will be, not an approximation of it.

    Into a scratch folder, not `chosen/`: looking at the effect of an edit is not
    the same act as accepting it, and a preview must not leave anything behind
    that could be mistaken for a result.
    """
    import spotting_batch as sb

    workdir = Path(workdir)
    # One medium per call, each in its own folder: the renderer writes a
    # single paired t-test table per run, so two media sharing a folder would
    # overwrite each other's.
    out = workdir / safe_name(frame.medium)
    figures = out / "figures"
    if figures.is_dir():
        for pattern in ("spotting_*.png", "spotting_*.pdf",
                        "spotting_paired_ttests.csv", "spotting_anova.csv"):
            for old in figures.glob(pattern):
                old.unlink()      # never show the previous edit's result
    out.mkdir(parents=True, exist_ok=True)

    csv_path = out / CSV_NAME
    _ordered(frame.tidy).to_csv(csv_path, index=False, encoding="utf-8-sig")

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sb.run_plots(csv_path, out, statistical_test=statistical_test,
                     p_adjust=p_adjust, alpha=alpha)
    want = figures / f"spotting_{safe_name(frame.experiment)}.png"
    if want.exists():
        return want
    got = sorted(figures.glob("spotting_*.png"))
    if got:
        return got[0]
    raise RuntimeError("PyPrism Plot produced no graph.\n" + buf.getvalue())


def _combine_preview(original_sheet: Path, graph: Path, output: Path) -> Path:
    """Replace only the graph panel of a pipeline comparison sheet.

    Spot pixels and the amber quantified-row outlines do not change when a
    reviewer edits a value or flag.  Reusing that panel is both exact and much
    faster than repeating two full background-subtraction passes for every
    graph redraw.
    """
    from PIL import Image, ImageDraw, ImageOps

    with Image.open(original_sheet) as source, Image.open(graph) as plot:
        sheet = source.convert("RGB")
        width, height = sheet.size
        # These bounds mirror spotting_timecourse_figures._compose: the graph
        # occupies the right member of its 46/54 grid below the three-line
        # header and above the spot-display footnote.
        box = (round(width * 0.475), round(height * 0.105),
               round(width * 0.992), round(height * 0.965))
        ImageDraw.Draw(sheet).rectangle(box, fill="white")
        available = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
        fitted = ImageOps.contain(plot.convert("RGB"), available,
                                  method=Image.Resampling.LANCZOS)
        x = box[0] + (available[0] - fitted.width) // 2
        y = box[1] + (available[1] - fitted.height) // 2
        sheet.paste(fitted, (x, y))
        output.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output, format="PNG")
    return output


def draw_montage(root: Path, frame: Frame, cfg: dict, outdir: Path,
                 cache_dir: "Path | None" = None) -> "Path | None":
    """One medium's spot montage, via the pipeline's own montage worker."""
    import spotting_timecourse as tc

    from . import PROJECT_ROOT
    from .rebuild import layout_for, pipeline_candidate, rows_for

    cache_dir = Path(cache_dir or PROJECT_ROOT / ".spotting_cache")
    cand = pipeline_candidate(root, frame.candidate, cfg)
    level = rows_for(frame.candidate, cfg)
    layout = layout_for(cfg)
    out = Path(outdir) / "montages" / f"montage_{safe_name(frame.experiment)}.png"
    job = tc.montage_job(cand, level, cfg["strains"], out, cache_dir,
                         frame.experiment, layout)
    _, err = tc._draw_montage_job(job)
    if err:
        raise RuntimeError(err)
    return out if out.exists() else None


def export(run: SetRun, review: Review, frames: list[Frame], root: Path,
           cfg: dict, *, figures: bool = True, montages: bool = True,
           progress=None, cache_dir: "Path | None" = None) -> ExportResult:
    """Write `chosen/`. The CSV lands first, on purpose.

    Drawing is slow -- a montage is two Python background subtractions,
    about 40 s -- and it can fail for reasons that have nothing to do with the
    data (plotting dependency missing, photos unavailable). Writing the numbers
    before any of that starts means a failed drawing costs a picture, never the
    review.
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    out = ExportResult()
    if not frames:
        out.warnings.append("nothing to export -- no medium could be rebuilt")
        return out

    outdir = run.chosen_dir
    say(f"writing {CSV_NAME} ...")
    out.csv_path = write_csv(frames, outdir)
    out.summary_path = write_summary(run, review, frames, outdir)

    if figures:
        say("drawing figures with PyPrism Plot ...")
        try:
            out.figures, out.statistics = draw_figures(
                out.csv_path, outdir,
                statistical_test=review.statistical_test,
                p_adjust=review.p_adjust, alpha=review.alpha)
            if review.statistical_test == "t_test":
                out.ttests = out.statistics
            if not out.figures:
                out.warnings.append(
                    "PyPrism Plot drew no figures. The CSV is "
                    "written either way.")
        except Exception as e:
            out.warnings.append(f"figures skipped: {type(e).__name__}: {e}")

    if montages:
        for i, f in enumerate(frames, 1):
            say(f"montage {i}/{len(frames)}: {f.medium} ...")
            try:
                got = draw_montage(root, f, cfg, outdir, cache_dir)
                if got:
                    out.montages.append(got)
            except Exception as e:
                out.warnings.append(
                    f"montage for {f.medium} skipped: {type(e).__name__}: {e}")

    say("done")
    return out
