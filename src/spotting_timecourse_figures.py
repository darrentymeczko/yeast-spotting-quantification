#!/usr/bin/env python3
"""
spotting_timecourse_figures.py -- turn time-course candidates into figures.

`timecourse_candidates.csv` answers "which photos should I quantify?" as a table
of coefficients of variation and significant-strain counts. That is the right
thing to rank on but the wrong thing to read: deciding between two candidates
means knowing whether the spots actually look quantifiable, and no column of the
CSV can tell you that. So for the best candidates this draws the two things you
would otherwise assemble by hand, side by side in one sheet:

    left  -- the spots themselves, all four replicate blocks, with the dilution
             row that was quantified outlined in amber
    right -- the relative-growth graph for exactly that candidate, drawn by the
             same PyPrism renderer the real pipeline uses

The graph is not an approximation of the real figure. The tidy frame behind it
goes through `spotting_batch.build_tidy` and then the same Python renderer, with the
same per-plate control averaging, the same rim-flag exclusions and the same
paired test. What you see is what quantifying that candidate would give you --
which is the only honest basis for choosing between candidates.

WHAT THESE FIGURES ARE FOR, AND WHAT THEY ARE NOT

They are triage. The candidate list is ranked, and ranking on any outcome-facing
quantity -- as --rank-by significance explicitly does -- means the p-values on
these sheets are not the p-values of a pre-registered comparison. Use them to
choose which photos to quantify properly; re-run the main pipeline on the chosen
photos to get a result worth reporting. The header of every sheet says so.

No brightness or contrast adjustment is applied to any spot image: the montage
is drawn over the fixed 0-255 range, exactly as spotting_montage does for the
publication figures. The amber outline is an overlay and alters no pixel.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from spotting_paths import PROJECT_ROOT   # noqa: E402
import spotting_quant as sq          # noqa: E402
import spotting_batch as sb          # noqa: E402
import spotting_estimate as est      # noqa: E402
import spotting_montage as sm        # noqa: E402

# Sheet geometry lives in spotting_sheet: the panels are arranged side by side
# or stacked, whichever suits their shapes. The montage for a sheet is drawn at
# this resolution -- 400 dpi puts one montage pixel on each pixel of the
# resampled plate (CELL_PX per 0.42 in cell), the same resolution the graph is
# written at, so neither panel is enlarged to match the other.
SHEET_MONTAGE_DPI = 400


def safe_name(s: str) -> str:
    """Filesystem-safe token shared by every figure-producing path."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_")


def candidate_id(cand: dict, rows, layout=None) -> str:
    """A stable, readable name for one (pairing, dilution) candidate.

    Both photo stems are in the name because technical replicates mean several
    pairings share a timepoint and medium, and a sheet that could not be told
    apart from its neighbour would be useless.
    """
    import spotting_timecourse as tc

    dil = _dil_choice(rows, layout)
    # Every plate's photo stem, joined -- for two plates exactly the old name.
    stems = "-".join(s.path.stem for s in tc.cand_shots(cand))
    return safe_name(f"{cand['medium']}_{cand['tp_label']}_{stems}_{dil}")


def _shots(cand) -> tuple:
    """The candidate's photos, one per plate. See `spotting_timecourse.cand_shots`."""
    import spotting_timecourse as tc

    return tc.cand_shots(cand)


def _photo_names(row) -> list:
    """plate1, plate2, ... photo names from a scored candidates-CSV row, in order."""
    out, k = [], 1
    while True:
        v = row.get(f"plate{k}")
        if v is None or (isinstance(v, float) and v != v) or v == "":
            return out
        out.append(str(v))
        k += 1


def _level(rows, layout=None) -> "sb.DilutionLevel":
    """The candidate's dilution level, from a level or a legacy row tuple."""
    if isinstance(rows, sb.DilutionLevel):
        return rows
    try:
        return (layout or sb.classic_layout()).by_rows(rows)
    except KeyError:
        raise ValueError(f"{tuple(rows)} is not one of the dilution row sets "
                         f"of this design") from None


def _dil_choice(rows, layout=None) -> str:
    """The dilution level's name ('least', 'middle', 'most', or 'level 4')."""
    return _level(rows, layout).name


def _single_level(layout=None) -> bool:
    """True when the design spots only one dilution level, so there is no
    dilution choice for a sheet to show."""
    return layout is not None and len(layout.populated_levels()) <= 1


def _mark_row(rows, layout=None, plate: int = 1):
    """Which rows, within a montage replicate block, to outline for this level.

    The montage stacks each plate as replicate blocks. For a layout of regular
    blocks the row's position within its block IS the level index -- that is
    how the lab's (lo, hi) pairs work, row % 3. A design that is not regular
    blocks is drawn a whole plate per block, and every row the level occupies
    on each plate is outlined, as {plate: rows}.

    None when the design has a single dilution level: outlining the only thing
    there is to quantify says nothing.
    """
    level = _level(rows, layout)
    if layout is None or layout.is_classic():
        return int(level.rows(plate)[0]) % (sq.N_ROWS // 2)
    if _single_level(layout):
        return None
    k = layout.block_rows()
    if k:
        return level.index
    return {int(no): level.rows(no) for no, _ in level.plates}


def build_tidy_for_candidate(cand: dict, rows, cache_dir: Path,
                             strains: list, control_col: int,
                             exclude=None, resolve=None, layout=None):
    """The tidy frame for one candidate, normalized exactly as the pipeline does.

    Returns (tidy, plates), or None if either plate is missing from the cache or
    the frame came back empty -- both mean there is nothing to draw. Returning a
    tuple whose first element could itself be None invited the caller to test
    only the tuple and then hand an empty frame to to_csv.

    `resolve` optionally maps this candidate's medium to its own
    (control_col, exclude), overriding the defaults passed in. The main
    pipeline picks the control per medium -- WT BY does not grow on K-OAc --
    and a sheet drawn against the wrong control is not comparable to the figure
    the run finally produces.
    """
    import spotting_timecourse as tc

    if resolve is not None:
        control_col, exclude = resolve(cand["medium"])

    level = _level(rows, layout)
    plates = []
    for shot in tc.cand_shots(cand):
        try:
            plates.append(tc._cached_measure(shot.path, shot.plate,
                                             level.quant_rows(shot.plate),
                                             cache_dir, layout))
        except Exception:
            return None

    # Named exactly as `sb.build_tidy` named it from the combo "<set>|<cid>",
    # so a sheet's graph keys and labels are unchanged.
    set_id = str(cand.get("set_id", "TC"))
    cid = candidate_id(cand, level, layout)
    try:
        tidy = sb.build_tidy_level(plates, strains, control_col, level, exclude,
                                   experiment=f"Set {set_id} {cid}",
                                   treatment=cid, set_label=set_id)
    except KeyError:
        return None
    if tidy is None or tidy.empty:
        return None
    return tidy, plates


@lru_cache(maxsize=None)
def _display_radius(path: Path, plate: int, cache_dir: Path,
                    layout=None) -> "float | None":
    """The largest ROI radius this photo takes across the three dilution choices.

    Used as ONE display background-subtraction radius for all of a photo's
    sheets, so the plate looks identical across them and only the marked row
    changes -- which is the comparison these sheets exist for. Three separate
    subtractions of the same photo would otherwise differ visibly: on Set09 16h
    GLU the radius runs 103.5 / 96.4 / 91.6 px, i.e. ball radii 250 / 234 / 223.

    The largest is the least-dilute row's, which is also the closest to the
    protocol's own "ball diameter = largest spot diameter + 20 px". The trade is
    that a sheet's picture is no longer subtracted with the exact radius that
    candidate's numbers used; `_compose` states the radius on every sheet.
    """
    import spotting_timecourse as tc

    radii = []
    for level in (layout or sb.classic_layout()).levels:
        try:
            pd_ = tc._cached_measure(path, plate, level.quant_rows(plate),
                                     cache_dir, layout)
        except Exception:
            continue
        radii.append(float(pd_.radius))
    return max(radii) if radii else None


PHOTOS_PER_BACKGROUND_BATCH = 4


def _background_batch_worker(job):
    """Worker: Python subtraction for several (path, ball_radius) pairs."""
    import spotting_quant as _sq
    jobs, out_dir = job
    try:
        got = _sq.subtract_background_batch(
            [(Path(p), r) for p, r in jobs], Path(out_dir))
        return {k: str(v) for k, v in got.items()}
    except Exception:
        return {}


def prepare_subtractions(keep, cache_dir: Path, opts: "sq.MeasureOptions",
                         work: Path, workers: int = 1, layout=None) -> dict:
    """Background-subtract the photos needed by comparison sheets in Python.

    Resolve each display radius from the measurement cache, then process each
    unique photo/radius once across a process pool. Returns keys mapped to TIFF
    paths. Missing entries are processed by the per-photo composition path.
    """
    import spotting_quant as _sq

    wanted = {}
    for _medium, cand, _rows, _metrics, _plates in keep:
        for s in _shots(cand):
            r_disp = _display_radius(s.path, s.plate, cache_dir, layout)
            if r_disp is None:
                continue
            ball = opts.resolve_ball_radius(2 * float(r_disp)
                                            / sq.MEASURE_RADIUS_FRAC)
            wanted[_sq.background_batch_key(s.path, ball)] = (str(s.path), ball)
    if not wanted or opts.bg_mode not in ("python", "fiji", "paraboloid"):
        return {}

    jobs = list(wanted.values())
    groups = [jobs[i:i + PHOTOS_PER_BACKGROUND_BATCH]
              for i in range(0, len(jobs), PHOTOS_PER_BACKGROUND_BATCH)]
    print(f"    subtracting {len(jobs)} photo(s) in {len(groups)} Python "
          f"batch(es) on {min(workers, len(groups))} worker(s) ...")
    t0 = time.perf_counter()
    out = {}
    if workers <= 1 or len(groups) == 1:
        for gi, g in enumerate(groups):
            out.update(_background_batch_worker((g, str(work / f"bg{gi}"))))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=min(workers, len(groups))) as pool:
            futs = [pool.submit(_background_batch_worker,
                                (g, str(work / f"bg{gi}")))
                    for gi, g in enumerate(groups)]
            for fut in as_completed(futs):
                out.update(fut.result())
    print(f"    {len(out)} subtraction(s) in {time.perf_counter() - t0:.0f}s")
    return out


def _plot_chunk_worker(job):
    """Worker: draw one slice of candidates with PyPrism Plot."""
    import pandas as _pd
    import spotting_batch as _sb
    frames, out_dir = job[:2]
    statistics = job[2] if len(job) > 2 else None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "candidates.csv"
    _pd.concat(frames, ignore_index=True).to_csv(
        csv_path, index=False, encoding="utf-8-sig")
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            _sb.run_plots(csv_path, out_dir, **(statistics or {}))
    except Exception:
        return {}
    return {p.stem[len("spotting_"):]: str(p)
            for p in (out_dir / "figures").glob("spotting_*.png")}


def draw_graphs(frames, work: Path, workers: int = 1,
                statistics: "dict | None" = None) -> dict:
    """Draw every candidate's graph across Python worker processes.

    `statistics` is passed to `spotting_batch.run_plots` as keyword arguments;
    None draws with its defaults.

    The renderer loops over distinct `treatment` values and writes a figure per
    value, so one call already serves every candidate. Each candidate is independent (its own
    `experiment`, its own normalisation, its own paired test), so the frames
    split cleanly across several calls.

    Every chunk writes into its own directory, which is what keeps this safe:
    Each worker also emits spotting_paired_ttests.csv, and concurrent calls
    sharing an outdir would overwrite each other's copy. That file is not used
    here -- only the PNGs are -- and `work` is discarded at the end.

    Returns {candidate id: graph path} merged across chunks.
    """
    if not frames:
        return {}
    n_chunks = max(1, min(workers, len(frames)))
    chunks = [frames[i::n_chunks] for i in range(n_chunks)]
    chunks = [c for c in chunks if c]
    print(f"    drawing {len(frames)} graph(s) on {len(chunks)} worker(s) "
          f"...")
    t0 = time.perf_counter()
    graphs = {}
    if len(chunks) == 1:
        graphs.update(_plot_chunk_worker(
            (chunks[0], str(work / "plot0"), statistics)))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
            futs = [pool.submit(_plot_chunk_worker,
                                (c, str(work / f"plot{i}"), statistics))
                    for i, c in enumerate(chunks)]
            for fut in as_completed(futs):
                graphs.update(fut.result())
    print(f"    plotting finished in {time.perf_counter() - t0:.0f}s "
          f"({len(graphs)} graph(s))")
    return {k: Path(v) for k, v in graphs.items()}


def _compose_chunk_worker(job):
    """Worker: draw the montage and compose the sheet for several candidates.

    Takes whole PAIRINGS, so the two subtracted images are loaded once and
    reused across that pairing's three dilutions -- the same locality the
    serial loop relied on, preserved across the split.
    """
    import tifffile
    import spotting_montage as _sm
    import spotting_quant as _sq

    items, strains, opts, outdir, rank_note, work = job[:6]
    layout = job[6] if len(job) > 6 else None
    made, errs = [], []
    cur_key, cur_proc = None, [None, None]
    for (medium, cand, rows, metrics, plates, bg_r, sub_keys) in items:
        cid = candidate_id(cand, rows, layout)
        try:
            pair_key = tuple(str(s.path) for s in _shots(cand))
            if sub_keys and pair_key != cur_key:
                cur_proc = [tifffile.imread(t).astype(np.float64) if t else None
                            for t in sub_keys]
                cur_key = pair_key
            montage_png = Path(work) / f"{cid}_montage.png"
            mark = _mark_row(rows, layout)
            _sm.build_montage(f"TC|{cid}", plates, strains, opts, montage_png,
                              rep_label="Replicate",
                              mark_row=mark,
                              mark_label="quantified" if mark is not None else None,
                              bg_radius=bg_r,
                              proc=(cur_proc if sub_keys else None),
                              layout=layout, dpi=SHEET_MONTAGE_DPI,
                              clean_path=_clean_of(montage_png),
                              rotated_path=_rot_of(montage_png))
            sheet = (Path(outdir) / safe_name(medium)
                     / f"{_sort_prefix(cand, rows, layout)}{cid}.png")
            gp = metrics.pop("_graph", None)
            _compose(sheet, montage_png, Path(gp) if gp else None,
                     cand, rows, metrics, rank_note, bg_radius=bg_r,
                     layout=layout)
            made.append(str(sheet))
        except Exception as e:
            errs.append((cid, f"{type(e).__name__}: {e}"))
    return made, errs


def _run_plot(csv_path: Path, outdir: Path) -> "Path | None":
    """Draw the graph with PyPrism Plot and return the PNG it wrote."""
    sb.run_plots(csv_path, outdir)
    figs = sorted((outdir / "figures").glob("spotting_*.png"))
    return figs[0] if figs else None


def build_candidate_figure(cand: dict, rows, cache_dir: Path,
                           strains: list, control_col: int, outdir: Path,
                           opts: "sq.MeasureOptions | None" = None,
                           exclude=None, rank_note: str = "",
                           metrics: "dict | None" = None,
                           resolve=None, layout=None) -> "Path | None":
    """One sheet: marked spot montage left, its PyPrism graph right.

    Returns the sheet path, or None if the candidate could not be drawn (a
    missing cache entry or plotting failure -- in which case the montage is still
    written on its own so the run is not wasted).
    """
    opts = opts or sq.MeasureOptions()
    cid = candidate_id(cand, rows, layout)
    got = build_tidy_for_candidate(cand, rows, cache_dir, strains,
                                   control_col, exclude, resolve, layout)
    if got is None:
        return None
    tidy, plates = got

    outdir.mkdir(parents=True, exist_ok=True)
    # The per-candidate CSV and graph are intermediates. They go to a
    # temp folder so a Results tree holds sheets, not three files per candidate;
    # the numbers behind every sheet are already in timecourse_candidates.csv
    # and can be regenerated from the cache in seconds.
    work = Path(tempfile.mkdtemp(prefix="tcfig_"))
    try:
        csv_path = work / f"{cid}_normalized.csv"
        tidy.to_csv(csv_path, index=False, encoding="utf-8-sig")
        graph_png = _run_plot(csv_path, work)

        montage_png = work / f"{cid}_montage.png"
        # The level's position WITHIN a replicate block is what the montage
        # outlines -- see `_mark_row`.
        mark = _mark_row(rows, layout)
        sm.build_montage(f"TC|{cid}", plates, strains, opts, montage_png,
                         rep_label="Replicate", mark_row=mark,
                         mark_label="quantified" if mark is not None else None,
                         layout=layout, dpi=SHEET_MONTAGE_DPI,
                         clean_path=_clean_of(montage_png),
                         rotated_path=_rot_of(montage_png))

        sheet = outdir / f"{cid}.png"
        _compose(sheet, montage_png, graph_png, cand, rows, metrics, rank_note,
                 layout=layout)
        return sheet
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clean_of(montage_png: Path) -> Path:
    """Where a sheet montage's outline-free twin is written, beside it."""
    montage_png = Path(montage_png)
    return montage_png.with_name(f"{montage_png.stem}_clean.png")


def _rot_of(montage_png: Path) -> Path:
    """Where a sheet montage's quarter-turned copy is written, beside it."""
    montage_png = Path(montage_png)
    return montage_png.with_name(f"{montage_png.stem}_rot.png")


def _compose(out_path: Path, montage_png: Path, graph_png: "Path | None",
             cand: dict, rows, metrics: "dict | None",
             rank_note: str, bg_radius=None, layout=None) -> Path:
    """Lay the two panels on one sheet with a header naming the candidate.

    The arrangement is `spotting_sheet.layout`'s: side by side or stacked,
    whichever shows both panels larger, at close to their own resolution. The
    PNG records where each piece went, so the review window can rearrange the
    pieces for its pane and swap in a redrawn graph.
    """
    from PIL import Image
    import spotting_plots as sp
    import spotting_sheet as ss

    level = _level(rows, layout)
    # Where each spot and each strain's tick ended up, so the review can line
    # the spots up under the graph (spotting_sheet's "aligned" view).
    extra = {"level": int(level.index)}
    with Image.open(montage_png) as im:
        extra["montage_map"] = ss.png_record(im, sm.SPOT_KEY)
        montage = im.convert("RGB")
    graph = graph_h = None
    if graph_png is not None and Path(graph_png).exists():
        with Image.open(graph_png) as im:
            extra["graph_map"] = ss.png_record(im, sp.GRAPH_KEY)
            graph = im.convert("RGB")
        sideways = sp.horizontal_of(graph_png)
        if sideways.exists():
            with Image.open(sideways) as im:
                extra["graph_h_map"] = ss.png_record(im, sp.GRAPH_KEY)
                graph_h = im.convert("RGB")
    shots = _shots(cand)
    shown_rows = [r + 1 for r in level.rows(shots[0].plate)]
    rows_text = " & ".join(str(r) for r in shown_rows)
    for shot in shots[1:]:
        try:
            other = [r + 1 for r in level.rows(shot.plate)]
        except KeyError:
            continue
        if other != shown_rows:
            rows_text += (f"; plate {shot.plate} rows "
                          f"{' & '.join(str(r) for r in other)}")
    title = f"{cand['medium_label']}  |  {cand['tp_label']}"
    single = _single_level(layout)
    if not single:
        title += f"  |  {level.name} dilution (rows {rows_text})"
    sub = "     ".join(f"plate {s.plate}: {s.path.name}" for s in shots)
    # Median CV and the control's grey level used to follow here; they are in
    # the candidates CSV and the review's table, not on the figure.
    if metrics and metrics.get("n_significant") is not None:
        # Named, because the count is the experiment's own test -- the one the
        # graph beside it draws. A CSV from before it was recorded counted an
        # uncorrected t-test at p < 0.05, so that is what it says.
        test = metrics.get("significance_test")
        sub += ("          " + f"{int(metrics['n_significant'])} of "
                f"{int(metrics.get('n_strains', 0))} strains "
                + (f"significant ({test})" if isinstance(test, str) and test
                   else "p<0.05"))

    # The caveat travels with the figure, not just with the console output: a
    # sheet lifted out of the folder and pasted into a slide must still say what
    # it is.
    note = ("Triage only: this candidate was chosen by ranking, so the p-values "
            "shown are not those of a comparison fixed in advance. "
            "Re-run the main pipeline on the chosen photos to report a result.")
    if rank_note:
        note = f"{note}  ({rank_note})"
    header = [(title, 17, {"fontweight": "bold"}),
              (sub, 11, {"color": "#333333"}),
              (note, 9, {"color": "#8a5a00"})]
    if graph is None:
        header.append(("no graph -- plotting failed (the spot panel is still "
                       "valid)", 13, {"color": "#a00000"}))
    # The footer sits on its own line, not opposite the triage note:
    # right-aligning a second caption on the same line let the two collide.
    # Stating the display subtraction radius is not optional. It is deliberately
    # NOT this candidate's own ROI radius -- one radius per photo is what lets
    # the same plate look identical across its three dilution sheets, which is
    # the comparison these are for -- so the figure has to say what it used.
    rr = ([bg_radius] if bg_radius is None or np.isscalar(bg_radius)
          else list(bg_radius))
    shown = ", ".join(f"{float(r):.0f}" for r in rr if r) or "per-candidate"
    if single:
        foot = ("Spot images: fixed 0-255 display range, no brightness or "
                "contrast adjustment. Display background subtracted at ROI "
                f"radius {shown} px.")
    else:
        n_levels = len((layout or sb.classic_layout()).populated_levels())
        foot = ("Spot images: fixed 0-255 display range, no brightness or "
                "contrast adjustment. Amber outline marks the quantified "
                f"dilution row. Display background subtracted at ROI radius "
                f"{shown} px (each photo's largest across the {n_levels} "
                "dilutions, so the plate looks the same on all of its sheets).")
    carried = {"montage": montage}
    if graph is not None:
        carried["graph"] = graph
    if graph_h is not None:
        carried["graph_h"] = graph_h
    for name, path in (("montage_clean", _clean_of(montage_png)),
                       ("montage_rot", _rot_of(montage_png))):
        if path.exists():
            with Image.open(path) as im:
                carried[name] = im.convert("RGB")
    # The sheet shows whichever orientation of the plate lays out better
    # beside or above the graph; both travel with it for the review.
    panels = ss.best_montage(carried, ss.SHEET_BOX)
    # Pass 1 settles the arrangement and so the width the text has to fill;
    # pass 2 places the text strips, drawn to that width, around the panels.
    first = ss.layout({k: v.size for k, v in panels.items()}, ss.SHEET_BOX)
    width = first.size[0]
    pieces = dict(panels,
                  header=_text_strip(header, width),
                  footer=_text_strip([(foot, 8, {"color": "#666666"})], width))
    lay = ss.layout({k: v.size for k, v in pieces.items()},
                    (width, 10 ** 6), modes=(first.mode,),
                    max_scale=first.scale)
    return ss.save(ss.assemble(pieces, lay), lay, out_path, extra=extra,
                   panels=carried)


def _text_strip(lines, width_px: int):
    """Lines of text, wrapped to `width_px`, as a tightly cropped image.

    Type is sized to the sheet: a 15 in sheet at 150 dpi, the size these sheets
    always were, keeps its look; a wider sheet gets proportionally larger type.
    """
    import io
    import textwrap

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from PIL import Image

    dpi = 150 * width_px / (15 * 150)
    w_in = width_px / dpi
    fig = plt.figure(figsize=(w_in, 6), dpi=dpi, facecolor="white")
    y_pt = 6 * 72 - 4
    for text, pt, kw in lines:
        per_line = max(20, int((w_in - 0.2) * 72 / (pt * 0.55)))
        wrapped = textwrap.fill(text, per_line) if text else ""
        n = wrapped.count("\n") + 1
        fig.text(0.1 / w_in, y_pt / (6 * 72), wrapped, fontsize=pt,
                 ha="left", va="top", linespacing=1.2, **kw)
        y_pt -= n * pt * 1.25 + 0.35 * pt
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor="white",
                bbox_inches="tight", pad_inches=0.04)
    plt.close(fig)
    buf.seek(0)
    with Image.open(buf) as im:
        return im.convert("RGB")


def build_figures(df: "pd.DataFrame", cands: list, cache_dir: Path,
                  strains: list, control_col: int, outdir: Path,
                  n_per_medium: "int | None" = None,
                  opts: "sq.MeasureOptions | None" = None,
                  exclude=None, rank_note: str = "",
                  resolve=None, workers: int = 1, layout=None,
                  statistics: "dict | None" = None) -> list:
    """Draw a sheet per candidate -- every one by default, so they can be compared.

    `statistics` chooses the tests the graphs report, as keyword arguments to
    `spotting_batch.run_plots`; None keeps its defaults.

    `n_per_medium=None` means all; an integer keeps only that many per medium,
    taken off the top of the ranking the frame is already sorted by (so the
    figures can never disagree with the CSV).

    Two things make drawing all ~50 tractable rather than an hour of repeated
    work, and both matter because the cost is dominated by things that do NOT
    vary per sheet:

    * one PyPrism rendering pass for the whole run; the renderer loops over the
      distinct values of `treatment` and writes a figure per value, so every
      candidate's tidy frame is stacked into a single CSV with the candidate id
      as its `treatment`. Each candidate keeps its own `experiment`, so the
      normalisation and paired tests stay separate. What is saved is repeated
      interpreter start-up and library loading.
    * Candidates are drawn grouped by photo pairing, so the montage block cache
      in spotting_montage hits: the three dilution choices of one pairing differ
      only in which row is outlined, and now share one background subtraction.
    """
    import time

    made = []
    if df is None or df.empty:
        return made
    opts = opts or sq.MeasureOptions()

    # Selection first, then order for cache locality: same pairing together, and
    # its three dilutions adjacent.
    picked = []
    for medium, grp in df.groupby("medium", sort=False):
        g = grp if n_per_medium is None else grp.head(n_per_medium)
        for _, row in g.iterrows():
            cand = _match_candidate(cands, row)
            if cand is None:
                print(f"  ! no candidate matches {row.get('medium')} "
                      f"{row.get('timepoint')} "
                      f"{'+'.join(_photo_names(row))}; skipped")
                continue
            try:
                level = _rows_from_row(row, layout)
            except ValueError as e:
                print(f"  ! {e}; skipped")
                continue
            picked.append((medium, cand, level, row.to_dict()))
    # Sort on the FULL PATH, not the file name. A capture tree routinely names
    # every photo the same thing (`_9.JPG` in each timepoint/plate folder), so
    # keying on the name collapses this to (medium, dilution) -- which visits
    # every pairing at one dilution before returning to the first pairing, and
    # misses the block cache on literally every sheet. Measured: ~25 s a sheet
    # that way against ~2 s once the three dilutions of a pairing are adjacent.
    # Level index, not rows[0]: identical ordering for the classic layout, where
    # the least dilute pair starts on row 0, and still defined when a level's
    # rows differ between plates.
    picked.sort(key=lambda t: (t[0], tuple(str(s.path) for s in _shots(t[1])),
                               t[2].index))
    if not picked:
        return made

    # --- one tidy frame for everything, one plotting pass --------------------
    work = Path(tempfile.mkdtemp(prefix="tcfigs_"))
    try:
        frames, keep = [], []
        for medium, cand, rows, metrics in picked:
            got = build_tidy_for_candidate(cand, rows, cache_dir, strains,
                                           control_col, exclude, resolve, layout)
            if got is None:
                print(f"  ! no measurement for "
                      f"{candidate_id(cand, rows, layout)}; skipped")
                continue
            tidy, plates = got
            frames.append(tidy)
            keep.append((medium, cand, rows, metrics, plates))
        if not frames:
            return made

        t_pass = time.perf_counter()
        graphs = draw_graphs(frames, work, workers, statistics)

        # --- every subtraction the sheets need, once, in parallel -------------
        subs = prepare_subtractions(keep, cache_dir, opts, work, workers, layout)

        # --- compose the sheets, in parallel by PAIRING -----------------------
        # Grouped by pairing rather than sliced arbitrarily, so each worker
        # keeps the locality the serial loop depended on: a pairing's three
        # dilutions share the two subtracted images, loaded once at 96 MB each
        # instead of re-read per sheet.
        by_pair = {}
        for medium, cand, rows, metrics, plates in keep:
            # One radius per PHOTO, not per dilution -- see _display_radius.
            # Per plate, NOT shared between the pairing's two plates: a shared
            # value would make plate 1's cache entry depend on which plate 2 it
            # happened to be paired with, and the same photo would be
            # subtracted again for every pairing it appears in.
            bg_r = [_display_radius(s.path, s.plate, cache_dir, layout)
                    for s in _shots(cand)]
            tifs = []
            for s, r_disp in zip(_shots(cand), bg_r):
                t = None
                if subs and r_disp is not None:
                    ball = opts.resolve_ball_radius(
                        2 * float(r_disp) / sq.MEASURE_RADIUS_FRAC)
                    got = subs.get(sq.background_batch_key(s.path, ball))
                    t = str(got) if got else None
                tifs.append(t)
            m = dict(metrics)
            g = graphs.get(candidate_id(cand, rows, layout))
            m["_graph"] = str(g) if g else None
            key = tuple(str(s.path) for s in _shots(cand))
            by_pair.setdefault(key, []).append(
                (medium, cand, rows, m, plates, bg_r,
                 tifs if any(tifs) else None))

        groups = list(by_pair.values())
        n_chunks = max(1, min(workers, len(groups)))
        chunks = [[it for g in groups[i::n_chunks] for it in g]
                  for i in range(n_chunks)]
        chunks = [c for c in chunks if c]
        print(f"    composing {len(keep)} sheet(s) on {len(chunks)} worker(s) ...")
        t0 = time.perf_counter()
        results = []
        if len(chunks) == 1:
            results.append(_compose_chunk_worker(
                (chunks[0], strains, opts, str(outdir), rank_note, str(work),
                 layout)))
        else:
            from concurrent.futures import ProcessPoolExecutor, as_completed
            with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
                futs = [pool.submit(_compose_chunk_worker,
                                    (c, strains, opts, str(outdir), rank_note,
                                     str(work), layout))
                        for c in chunks]
                for fut in as_completed(futs):
                    results.append(fut.result())
        for sheets, errs in results:
            made.extend(Path(p) for p in sheets)
            for cid, err in errs:
                print(f"  ! figure failed for {cid}: {err}")
        made.sort()
        print(f"    {len(made)} sheet(s) composed in "
              f"{time.perf_counter() - t0:.0f}s")
        # For the next run's estimate. Only a pass where every sheet was drawn:
        # a failed sheet returns early and would make the pass look fast.
        if len(made) == len(keep):
            photos = {str(s.path) for _, cand, *_ in keep for s in _shots(cand)}
            est.record_figures(len(frames), len(photos), len(groups),
                               len(keep), workers,
                               time.perf_counter() - t_pass)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return made


def _sort_prefix(cand: dict, rows, layout=None) -> str:
    """Filename prefix that sorts the sheets the way you compare them.

    Timepoint ascending, then dilution least->most. Sorting by name in a file
    browser then walks the time course in order, which is the comparison the
    figures exist for; the candidate id alone sorts alphabetically and puts
    '19 Hours' before '9 Hours'.

    The level index is zero-padded to two digits once a design has more than
    ten levels, so d10 does not sort between d1 and d2. For ten or fewer it is
    a single digit, exactly the names every existing sheet already has.
    """
    hours = cand.get("hours")
    h = f"{float(hours):06.1f}" if hours is not None else "______"
    level = _level(rows, layout)
    n = len((layout or sb.classic_layout()).levels)
    d = f"{level.index:02d}" if n > 10 else f"{level.index}"
    return f"{h}h_d{d}_"


# Kept as the old name so any existing call site still works.
build_top_figures = build_figures


def _match_candidate(cands: list, row) -> "dict | None":
    """Find the candidate dict a scored row came from."""
    want = _photo_names(row)
    for c in cands:
        if (c["medium"] == row.get("medium")
                and c["tp_label"] == row.get("timepoint")
                and [s.path.name for s in _shots(c)] == want):
            return c
    return None


def _rows_from_row(row, layout=None) -> "sb.DilutionLevel":
    """The dilution level a scored candidate row used, looked up by name."""
    try:
        return (layout or sb.classic_layout()).by_name(row.get("dilution", ""))
    except KeyError:
        raise ValueError(f"unrecognised dilution {row.get('dilution')!r}") from None
