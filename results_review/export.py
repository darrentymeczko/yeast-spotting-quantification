"""Write the reviewed result to `Results/Timecourse/<set>/chosen/`.

Laid out exactly like the pipeline's own `best/`, and produced by the same two
calls -- `spotting_batch.run_plots` for figures and paired tests,
`spotting_timecourse.draw_montages` for the spot montages -- so a `chosen/`
figure is comparable to a `best/` figure rather than merely similar to one.

`best/` is never written to. A re-run of the pipeline must be free to overwrite
its own output without destroying a review, and a reader must be able to see
what the automatic pick was and what a person decided instead. Keeping them in
two folders is what makes that diffable.

The selected media go into one tidy CSV and one plotting call. The renderer keys
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


def deck_name(run: SetRun) -> str:
    """The deck is named for its experiment: `Set13.pptx`.

    By the rule the run named the results folder with, so the two agree, and
    decks from several experiments can be gathered into one folder.
    """
    import spotting_timecourse as tc

    return f"{tc.safe_dirname(run.label)}.pptx"


@dataclass(frozen=True)
class ExportOptions:
    data: bool = True
    summary: bool = True
    figures: bool = True
    statistics: bool = True
    montages: bool = True
    powerpoint: bool = False
    #: How each slide shows its sheet: the review's own view ("fit" or
    #: "aligned") and whether the graph is turned on its side.
    view: str = "fit"
    rotate: bool = False

    @property
    def any_selected(self) -> bool:
        return any((self.data, self.summary, self.figures, self.statistics,
                    self.montages, self.powerpoint))


#: Written into the CSV in this order: the pipeline's own columns first, so a
#: `chosen/` file opens looking like a `best/` file, then this tool's
#: annotations. The PyPrism renderer picks the columns it needs by name.
COLUMN_ORDER = [
    "experiment", "treatment", "set", "plate", "image", "replicate",
    "dilution_row", "dilution", "strain_col", "strain", "raw_growth",
    "artifact", "excluded", "is_control", "control_raw", "control_mean",
    "relative_growth", "control_ok", "outlier",
    "raw_growth_original", "manual", "excluded_source", "outlier_source",
    "edit_note", "data_review",
]


@dataclass
class ExportResult:
    csv_path: "Path | None" = None
    summary_path: "Path | None" = None
    figures: list[Path] = field(default_factory=list)
    montages: list[Path] = field(default_factory=list)
    ttests: "Path | None" = None
    statistics: "Path | None" = None
    powerpoint: "Path | None" = None
    output_dir: "Path | None" = None
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files or self.csv_path)


def named_frames(run: SetRun, frames: list[Frame]) -> list[Frame]:
    """The frames as an export names them: in the experiment's own words.

    A frame's `experiment` ("Set11 K-OACNAA 40 Hours") is built from the
    treatment's generated code. It titles the graphs and names the figures,
    the montages and the rows of every CSV, so an export renames it to what
    the treatment is called ("Set11 K-OAc NaAsO2 40 Hours"). Names that would
    come out as the same file keep their codes rather than overwrite each
    other. Copies: the review's own frames are left as they are.
    """
    from collections import Counter
    from dataclasses import replace

    names = {f.medium: f"{run.label} {run.medium_label(f.medium)} "
                       f"{f.candidate.timepoint}" for f in frames}
    taken = Counter(safe_name(n).casefold() for n in names.values())
    out = []
    for frame in frames:
        name = names[frame.medium]
        if taken[safe_name(name).casefold()] > 1:
            name = frame.experiment
        out.append(replace(frame, experiment=name,
                           tidy=frame.tidy.assign(experiment=name,
                                                  treatment=name)))
    return out


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
                    "statistical_test", "p_adjust", "posthoc",
                    "comparisons", "p_cutoff", "reason"])
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
                        review.posthoc if review.statistical_test == "anova"
                        else "not applicable",
                        review.comparisons_text(),
                        review.alpha,
                        (pick.reason if pick else "")])
    return path


def draw_figures(csv_path: Path, outdir: Path, *, statistical_test: str = "t_test",
                 **statistics) -> tuple[list[Path], "Path | None"]:
    """Run the PyPrism renderer over the exported CSV.

    The renderer honours `artifact`, `excluded` and `outlier` straight from the
    file, so every correction made here reaches the
    figure and the paired t-tests without a line of statistics code in this
    package. `statistics` is the rest of `Review.statistics_kwargs()`.
    """
    import contextlib
    import io

    import spotting_batch as sb

    outdir = Path(outdir)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sb.run_plots(Path(csv_path), outdir, statistical_test=statistical_test,
                     **statistics)
    figs = sorted((outdir / "figures").glob("spotting_*.png"))
    name = ("spotting_paired_ttests.csv" if statistical_test == "t_test"
            else "spotting_anova.csv")
    stats = outdir / "figures" / name
    return figs, (stats if stats.exists() else None)


def draw_preview(frame: Frame, workdir: Path, *,
                 original_sheet: "Path | None" = None,
                 **statistics) -> "tuple[Path | None, bool]":
    """Redraw one medium and, when possible, retain its spot-image panel.

    `statistics` is `Review.statistics_kwargs()`.
    """
    out = Path(workdir) / safe_name(frame.medium)
    try:
        graph = _draw_preview_with_pyprism(frame, workdir, **statistics)
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


def _draw_preview_with_pyprism(frame: Frame, workdir: Path,
                               **statistics) -> "Path | None":
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
                        "spotting_paired_ttests.csv", "spotting_anova.csv",
                        "horizontal/spotting_*.png"):
            for old in figures.glob(pattern):
                old.unlink()      # never show the previous edit's result
    out.mkdir(parents=True, exist_ok=True)

    csv_path = out / CSV_NAME
    _ordered(frame.tidy).to_csv(csv_path, index=False, encoding="utf-8-sig")

    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sb.run_plots(csv_path, out, **statistics)
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
    import spotting_sheet as ss

    with Image.open(original_sheet) as source, Image.open(graph) as plot:
        pieces = ss.pieces_of(source)
        if pieces is not None:
            # The sheet says where its pieces are: keep its header, montage
            # and footer pixel for pixel, in the same arrangement, and bring
            # the new graph to the edge it shares with the montage -- the
            # old graph's height side by side, its width when stacked.
            mode = ss.mode_of(source) or "side"
            new = plot.convert("RGB")
            m = pieces["montage"]
            f = (m.height / new.height if mode == "side"
                 else m.width / new.width)
            new = new.resize((max(1, round(new.width * f)),
                              max(1, round(new.height * f))), Image.LANCZOS)
            pieces["graph"] = new
            sheet, lay = ss.arrange(pieces, (10 ** 6, 10 ** 6),
                                    modes=(mode,))
            # The spot map still describes the kept montage; the tick map
            # must be the new graph's own.
            import spotting_plots as sp

            extra = dict(ss.extra_of(source),
                         graph_map=ss.png_record(plot, sp.GRAPH_KEY))
            # Carry the full-resolution montage on, beside the new graph at
            # its own full resolution -- and the new graph's sideways twin,
            # which the Rotate view shows; the old one is no longer true.
            native = ss.native_panels(source)
            native["graph"] = plot.convert("RGB")
            native.pop("graph_h", None)
            extra.pop("graph_h_map", None)
            sideways = sp.horizontal_of(graph)
            if sideways.exists():
                with Image.open(sideways) as twin:
                    extra["graph_h_map"] = ss.png_record(twin, sp.GRAPH_KEY)
                    native["graph_h"] = twin.convert("RGB")
            return ss.save(sheet, lay, output, extra=extra, panels=native)

        sheet = source.convert("RGB")
        width, height = sheet.size
        # A sheet from before piece boxes were recorded. These bounds mirror
        # the old fixed 46/54 grid: the graph below the three-line header and
        # above the footnote. A wide montage ran past that split, so the graph
        # box starts where the montage's black panel actually ends.
        box = (max(round(width * 0.475),
                   _legacy_montage_right(sheet, height) + round(width * 0.01)),
               round(height * 0.105),
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


#: The box a slide's picture is laid out in: the 13.33 x 7.5 in widescreen
#: slide at 300 dpi. Panels are never enlarged past their own resolution.
SLIDE_PX = (4000, 2250)


def slide_picture(sheet: Path, graph: Path, outdir: Path, mode: str = "fit",
                  rotate: bool = False) -> "tuple[Path, str]":
    """One slide's picture, shown the way the review shows it: (png, note).

    The candidate's comparison sheet with its graph replaced by `graph`, the
    one drawn from the review's corrections, then laid out by the same
    `spotting_sheet.view` the sheet pane uses. A sheet that cannot be shown
    that way (one from an older run) falls back to Fit, upright, and the note
    says so.
    """
    from PIL import Image

    import spotting_sheet as ss

    outdir = Path(outdir)
    combined = _combine_preview(Path(sheet), Path(graph),
                                outdir / f"sheet_{Path(graph).stem}.png")
    note = ""
    with Image.open(combined) as im:
        try:
            picture = ss.view(im, mode, SLIDE_PX, rotate)
        except ss.ViewUnavailable as exc:
            if mode == "fit" and not rotate:
                raise
            picture = ss.view(im, "fit", SLIDE_PX, False)
            note = f"shown as Fit: {exc}"
    out = outdir / f"slide_{Path(graph).stem}.png"
    picture.save(out, format="PNG")
    return out, note


def fit_sheet(sheet, box: tuple, mode: str = "fit", rotate: bool = False):
    """A sheet shown as `mode` ("fit" or "aligned"), with its graph turned on
    its side if `rotate`, and fitted to a display box.

    Here rather than in the window code because it is the engine's layout:
    the window reaches the engine only through this module and rebuild.py.
    Raises ValueError with a readable reason when this sheet cannot be shown
    that way.
    """
    import spotting_sheet as ss

    try:
        return ss.view(sheet, mode, box, rotate)
    except ss.ViewUnavailable as e:
        raise ValueError(str(e)) from None


def _legacy_montage_right(sheet, height: int) -> int:
    """Right edge of the black spot panel on an older sheet; 0 if none.

    The montage is the only large near-black region on a sheet, so a column
    that is mostly near-black between the header and the footnote is montage.
    """
    from PIL import Image

    band = sheet.crop((0, round(height * 0.105), sheet.width,
                       round(height * 0.965))).convert("L")
    dark = band.point(lambda v: 255 if v < 40 else 0)
    cols = dark.resize((dark.width, 1), Image.BOX).tobytes()
    right = [x for x, v in enumerate(cols) if v > 0.35 * 255]
    return right[-1] + 1 if right else 0


def draw_montages(root: Path, frames: list[Frame], cfg: dict, outdir: Path,
                  cache_dir: "Path | None" = None,
                  workers: "int | None" = None) -> tuple[dict, dict]:
    """Every medium's spot montage: ({medium: png}, {medium: why it failed}).

    Each montage is a fresh background subtraction of its photographs, ~13 s a
    plate, and drawn one after another they were most of an export's time.
    `spotting_timecourse.draw_montages` is the pass the run itself draws
    `best/` with: the same images, on several workers at once.
    """
    import contextlib
    import io
    import multiprocessing

    import spotting_timecourse as tc

    from . import PROJECT_ROOT
    from .rebuild import layout_for, pipeline_candidate, rows_for

    cache_dir = Path(cache_dir or PROJECT_ROOT / ".spotting_cache")
    jobs, outs, failed = [], {}, {}
    for frame in frames:
        out = Path(outdir) / "montages" / f"montage_{safe_name(frame.experiment)}.png"
        try:
            cand = pipeline_candidate(root, frame.candidate, cfg)
            jobs.append(tc.montage_job(cand, rows_for(frame.candidate, cfg),
                                       cfg["strains"], out, cache_dir,
                                       frame.experiment, layout_for(cfg)))
        except Exception as exc:
            failed[frame.medium] = f"{type(exc).__name__}: {exc}"
            continue
        outs[frame.medium] = (frame.experiment, out)
    if workers is None:
        workers = max(1, min(8, (multiprocessing.cpu_count() or 2) // 2))
    try:
        from concurrent.futures import ProcessPoolExecutor  # noqa: F401
    except (ImportError, OSError):
        # Some managed Windows machines block multiprocessing outright; the
        # same work is still valid one montage at a time.
        workers = 1
    workers = min(workers, len(jobs)) or 1
    # Its progress line is for a console; the export reports its own.
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            errors = dict(tc.draw_montages(jobs, workers))
        except Exception:
            if workers == 1:
                raise
            errors = dict(tc.draw_montages(jobs, 1))
    drawn = {}
    for medium, (experiment, out) in outs.items():
        if experiment in errors:
            failed[medium] = errors[experiment]
        elif out.exists():
            drawn[medium] = out
        else:
            failed[medium] = "no montage was generated"
    return drawn, failed


def replaced_outputs(run: SetRun, review: Review, options: ExportOptions,
                     outdir: Path) -> list[Path]:
    """The fixed-name files an export would replace in `outdir`.

    The ones somebody may well have open -- the deck in PowerPoint, a CSV in
    Excel. Figure and montage names follow the picks, and an image viewer does
    not hold its file shut, so they are left to `publish` to report.
    """
    outdir = Path(outdir)
    names = []
    if options.powerpoint:
        names.append(deck_name(run))
    if options.data:
        names.append(CSV_NAME)
    if options.summary:
        names.append(SUMMARY_NAME)
    if options.statistics:
        names.append("figures/" + ("spotting_paired_ttests.csv"
                                   if review.statistical_test == "t_test"
                                   else "spotting_anova.csv"))
    return [outdir / n for n in names]


def in_use(paths) -> list[Path]:
    """Those of `paths` that exist but cannot be written: open elsewhere.

    Windows will not replace a file PowerPoint or Excel has open. Opening it
    for writing, which changes nothing, is refused the same way.
    """
    busy = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        try:
            with open(path, "r+b"):
                pass
        except PermissionError:
            busy.append(path)
    return busy


def in_use_message(paths) -> str:
    names = ", ".join(Path(p).name for p in paths)
    return (f"{names} {'is' if len(paths) == 1 else 'are'} open in another "
            f"program (PowerPoint or Excel?), so the export cannot replace "
            f"{'it' if len(paths) == 1 else 'them'}. Close "
            f"{'it' if len(paths) == 1 else 'them'} and export again.")


def validate_destination(run: SetRun, outdir: Path) -> Path:
    """Keep reviewed exports outside the pipeline's automatic results."""
    outdir = Path(outdir).expanduser().resolve()
    best = run.best_dir.resolve()
    if outdir == best or best in outdir.parents or outdir == best.parent:
        raise ValueError("Choose chosen/ or another export folder outside best/.")
    return outdir


def export_review(run: SetRun, review: Review, media: list[str], root: Path,
                  cfg: dict, *, options: ExportOptions, outdir: Path,
                  data_flags=None, progress=None) -> ExportResult:
    """Rebuild the selected, committed picks from a review snapshot off-thread.

    Do not silently omit a requested treatment when its pick cannot be rebuilt.
    A complete deck matters more than producing a misleading partial one.
    """
    from .review import chosen_candidate
    from .rebuild import rebuild

    if not options.any_selected or not media:
        raise ValueError("Select at least one output and one treatment.")
    validate_destination(run, outdir)
    # Before any work: an open deck would otherwise fail the export at the end.
    busy = in_use(replaced_outputs(run, review, options, outdir))
    if busy:
        raise ValueError(in_use_message(busy))
    frames = []
    for medium in media:
        if medium not in run.media:
            raise ValueError(f"Unknown treatment: {medium}")
        candidate = chosen_candidate(run, review, medium)
        if candidate is None:
            raise ValueError("Choose a current candidate for "
                             f"{run.medium_label(medium)} first.")
        if progress:
            progress(f"rebuilding chosen photo set: {run.medium_label(medium)}…")
        frames.append(rebuild(root, run.label, cfg, candidate,
                              review.edits_for(medium), data_flags=data_flags))
    return export(run, review, frames, root, cfg, options=options,
                  outdir=outdir, progress=progress)


def export(run: SetRun, review: Review, frames: list[Frame], root: Path,
           cfg: dict, *, figures: bool = True, montages: bool = True,
           progress=None, cache_dir: "Path | None" = None,
           options: "ExportOptions | None" = None,
           outdir: "Path | None" = None) -> ExportResult:
    """Publish only requested outputs; generate dependencies in fresh scratch.

    Fresh scratch prevents old candidates or stale graphs from entering a deck.
    Legacy callers retain CSV, summary, and optional figure/montage exports.
    """
    import shutil
    import tempfile

    options = options or ExportOptions(figures=figures, statistics=figures,
                                       montages=montages)
    if not options.any_selected:
        raise ValueError("Select at least one output.")
    outdir = validate_destination(run, outdir or run.chosen_dir)
    busy = in_use(replaced_outputs(run, review, options, outdir))
    if busy:
        raise ValueError(in_use_message(busy))
    out = ExportResult(output_dir=outdir)
    if not frames:
        out.warnings.append("nothing to export -- no medium could be rebuilt")
        return out
    frames = named_frames(run, frames)

    def say(message):
        if progress:
            progress(message)

    outdir.mkdir(parents=True, exist_ok=True)
    # A scratch folder beside the outputs also permits atomic per-file replace.
    with tempfile.TemporaryDirectory(prefix=".review-export-", dir=outdir) as tmp:
        work = Path(tmp)

        def publish(path):
            dest = outdir / path.relative_to(work)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Copy to a temporary sibling before replacing an existing export.
            staging = work / "publish.tmp"
            shutil.copy2(path, staging)
            try:
                staging.replace(dest)
            except PermissionError:
                # Opened since the check: say so, not "[WinError 5] ...".
                raise RuntimeError(in_use_message([dest])) from None
            out.files.append(dest)
            return dest

        need_graphs = options.figures or options.statistics or options.powerpoint
        csv_path = None
        if options.data or need_graphs:
            say(f"writing {CSV_NAME}…")
            csv_path = write_csv(frames, work)
            if options.data:
                out.csv_path = publish(csv_path)
        if options.summary:
            out.summary_path = publish(write_summary(run, review, frames, work))

        graphs = {}
        if need_graphs:
            say("drawing updated graphs and statistics…")
            try:
                drawn, stats = draw_figures(csv_path, work,
                                            **review.statistics_kwargs())
                graphs = {p.name: p for p in drawn}
                if options.figures:
                    for path in sorted((work / "figures").rglob("*")):
                        if path.suffix.lower() in (".png", ".pdf"):
                            dest = publish(path)
                            if path in drawn:
                                out.figures.append(dest)
                    if not drawn:
                        out.warnings.append("No graphs were generated.")
                if options.statistics:
                    if stats is None:
                        out.warnings.append("No statistical results were generated.")
                    else:
                        out.statistics = publish(stats)
                        if review.statistical_test == "t_test":
                            out.ttests = out.statistics
            except Exception as exc:
                graphs = {}
                out.warnings.append(f"graphs/statistics failed: {exc}")

        # Each slide is the candidate's sheet, with the corrected graph, shown
        # the way the review shows it -- Fit or Aligned, rotated or not. Its
        # montage is the sheet's own, so nothing is redrawn from a photograph.
        slides = {}
        if options.powerpoint:
            say("laying out the slides as the review shows them…")
            for frame in frames:
                graph = graphs.get(f"spotting_{safe_name(frame.experiment)}.png")
                sheet = run.sheet(frame.candidate)
                if graph is None or not sheet.exists():
                    continue
                name = run.medium_label(frame.medium)
                try:
                    slides[frame.medium], note = slide_picture(
                        sheet, graph, work / "slides", options.view,
                        options.rotate)
                    if note:
                        out.warnings.append(f"{name}: {note}")
                except Exception as exc:
                    out.warnings.append(f"{name}: slide laid out as montage and "
                                        f"graph instead ({exc})")

        # The separate spot montage: a requested output, and the left half of
        # a slide whose sheet the run never drew.
        photos = {}
        wanted = [f for f in frames if options.montages
                  or (options.powerpoint and f.medium not in slides)]
        if wanted:
            say(f"drawing {len(wanted)} spot montage(s) from the photographs…")
            try:
                photos, failed = draw_montages(root, wanted, cfg, work, cache_dir)
            except Exception as exc:
                photos, failed = {}, {f.medium: str(exc) for f in wanted}
            for frame in wanted:
                if frame.medium in photos:
                    if options.montages:
                        out.montages.append(publish(photos[frame.medium]))
                else:
                    out.warnings.append(
                        f"montage for {run.medium_label(frame.medium)} failed: "
                        f"{failed.get(frame.medium, 'no montage was generated')}")

        if options.powerpoint:
            say("building PowerPoint from the chosen photo sets…")
            try:
                import spotting_pptx as sp

                pages = []
                for frame in frames:
                    name = run.medium_label(frame.medium)
                    montage = photos.get(frame.medium)
                    graph = graphs.get(f"spotting_{safe_name(frame.experiment)}.png")
                    if frame.medium in slides:
                        pictures = [slides[frame.medium]]
                    elif montage is None or graph is None:
                        raise RuntimeError(
                            f"{name} is missing its montage or updated graph; "
                            "no partial PowerPoint was written")
                    else:
                        pictures = [montage, graph]
                    c = frame.candidate
                    note = (f"{frame.experiment}\nTreatment: {name}\n"
                            f"Timepoint: {c.timepoint}; dilution: {c.dilution}\n"
                            f"Chosen photos: {', '.join(c.photos)}\n"
                            f"Statistics: {review.statistics_kwargs()}\n"
                            "Graphs include the review's corrections and "
                            "outlier flags.")
                    pages.append((pictures, note))
                out.powerpoint = publish(sp.build_slides(pages,
                                                         work / deck_name(run)))
            except Exception as exc:
                out.warnings.append(f"PowerPoint failed: {exc}")
    say("done")
    return out
