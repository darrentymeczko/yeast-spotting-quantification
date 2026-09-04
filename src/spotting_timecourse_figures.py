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
             same plot_spotting.R the real pipeline uses

The graph is not an approximation of the real figure. The tidy frame behind it
goes through `spotting_batch.build_tidy` and then the same R script, with the
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
PROJECT_ROOT = HERE.parent
import spotting_quant as sq          # noqa: E402
import spotting_batch as sb          # noqa: E402
import spotting_montage as sm        # noqa: E402

# Figure geometry. The montage is tall and narrow (four stacked blocks) and the
# graph is close to square, so the sheet is laid out to give each its natural
# aspect rather than forcing them to a common width.
SHEET_W_IN = 15.0
SHEET_H_IN = 8.0
MONTAGE_FRAC = 0.46          # share of the sheet width the spot panel gets


def safe_name(s: str) -> str:
    """Filesystem- and R-safe token. Mirrors safe() in plot_spotting.R."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("_")


def candidate_id(cand: dict, rows: tuple) -> str:
    """A stable, readable name for one (pairing, dilution) candidate.

    Both photo stems are in the name because technical replicates mean several
    pairings share a timepoint and medium, and a sheet that could not be told
    apart from its neighbour would be useless.
    """
    dil = _dil_choice(rows)
    return safe_name(f"{cand['medium']}_{cand['tp_label']}_"
                     f"{cand['plate1'].path.stem}-{cand['plate2'].path.stem}_"
                     f"{dil}")


def _dil_choice(rows: tuple) -> str:
    """Map a 0-based row pair back to its dilution name ('least'/'middle'/'most')."""
    want = tuple(int(r) for r in rows)
    for name, pair in sb.DILUTIONS.items():
        if tuple(pair) == want:
            return name
    raise ValueError(f"{rows} is not one of the three dilution row pairs")


def build_tidy_for_candidate(cand: dict, rows: tuple, cache_dir: Path,
                             strains: list, control_col: int,
                             exclude=None, resolve=None):
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

    plates = []
    for shot in (cand["plate1"], cand["plate2"]):
        try:
            plates.append(tc._cached_measure(shot.path, shot.plate,
                                             tuple(r + 1 for r in rows),
                                             cache_dir))
        except Exception:
            return None

    combo = f"{cand.get('set_id', 'TC')}|{candidate_id(cand, rows)}"
    dil = {"mode": "combo", "choice": _dil_choice(rows)}
    tidy = sb.build_tidy(combo, plates, strains, control_col, dil, exclude)
    if tidy is None or tidy.empty:
        return None
    return tidy, plates


@lru_cache(maxsize=None)
def _display_radius(path: Path, plate: int, cache_dir: Path) -> "float | None":
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
    for name in sb.DILUTION_ORDER:
        rows = sb.DILUTIONS[name]
        try:
            pd_ = tc._cached_measure(path, plate, tuple(r + 1 for r in rows),
                                     cache_dir)
        except Exception:
            continue
        radii.append(float(pd_.radius))
    return max(radii) if radii else None


PHOTOS_PER_FIJI_BATCH = 4


def _fiji_batch_worker(job):
    """Worker: one FIJI run over several (path, ball_radius) pairs."""
    import spotting_quant as _sq
    jobs, out_dir = job
    try:
        got = _sq.fiji_subtract_background_batch(
            [(Path(p), r) for p, r in jobs], Path(out_dir))
        return {k: str(v) for k, v in got.items()}
    except Exception:
        return {}


def prepare_subtractions(keep, cache_dir: Path, opts: "sq.MeasureOptions",
                         work: Path, workers: int = 1) -> dict:
    """Background-subtract every photo the sheets need, once, in parallel.

    The sheets need ONE subtraction per photo -- `_display_radius` is keyed on
    the photo, not the pairing or the dilution, so the same photo re-used across
    pairings shares a result. That was already true, but the subtractions were
    run one at a time from inside the compose loop, each starting its own
    headless FIJI: on Set04 that is 36 JVM launches and roughly eight minutes
    for a tree with 96 sheets.

    Here every photo's radius is resolved first (all of it comes off the
    measurement cache), then the subtractions run as batched FIJI jobs spread
    across a process pool. Returns {(path, ball_radius) key: tif path}; a photo
    missing from the result simply falls back to being subtracted in place.
    """
    import spotting_quant as _sq

    wanted = {}
    for _medium, cand, _rows, _metrics, _plates in keep:
        for s in (cand["plate1"], cand["plate2"]):
            r_disp = _display_radius(s.path, s.plate, cache_dir)
            if r_disp is None:
                continue
            ball = opts.resolve_ball_radius(2 * float(r_disp)
                                            / sq.MEASURE_RADIUS_FRAC)
            wanted[_sq.fiji_batch_key(s.path, ball)] = (str(s.path), ball)
    if not wanted or opts.bg_mode != "fiji":
        return {}

    jobs = list(wanted.values())
    groups = [jobs[i:i + PHOTOS_PER_FIJI_BATCH]
              for i in range(0, len(jobs), PHOTOS_PER_FIJI_BATCH)]
    print(f"    subtracting {len(jobs)} photo(s) in {len(groups)} FIJI "
          f"batch(es) on {min(workers, len(groups))} worker(s) ...")
    t0 = time.perf_counter()
    out = {}
    if workers <= 1 or len(groups) == 1:
        for gi, g in enumerate(groups):
            out.update(_fiji_batch_worker((g, str(work / f"bg{gi}"))))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=min(workers, len(groups))) as pool:
            futs = [pool.submit(_fiji_batch_worker,
                                (g, str(work / f"bg{gi}")))
                    for gi, g in enumerate(groups)]
            for fut in as_completed(futs):
                out.update(fut.result())
    print(f"    {len(out)} subtraction(s) in {time.perf_counter() - t0:.0f}s")
    return out


def _r_chunk_worker(job):
    """Worker: one R call over a slice of the candidates."""
    import pandas as _pd
    import spotting_batch as _sb
    frames, out_dir = job
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "candidates.csv"
    _pd.concat(frames, ignore_index=True).to_csv(
        csv_path, index=False, encoding="utf-8-sig")
    try:
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            _sb.run_r(csv_path, out_dir)
    except Exception:
        return {}
    return {p.stem[len("spotting_"):]: str(p)
            for p in (out_dir / "figures").glob("spotting_*.png")}


def draw_graphs(frames, work: Path, workers: int = 1) -> dict:
    """Draw every candidate's graph, splitting the work across R processes.

    plot_spotting.R loops over the distinct values of `treatment` and writes a
    figure per value, so one call already served every candidate -- but one call
    means one core, and at ~0.9 s a graph a tree of 47 sheets spent 45 s here
    with fifteen cores idle. Each candidate is independent (its own
    `experiment`, its own normalisation, its own paired test), so the frames
    split cleanly across several calls.

    Every chunk writes into its own directory, which is what keeps this safe:
    R also emits spotting_paired_ttests.csv per run, and concurrent calls
    sharing an outdir would overwrite each other's copy. That file is not used
    here -- only the PNGs are -- and `work` is discarded at the end.

    Returns {candidate id: graph path} merged across chunks.
    """
    if not frames:
        return {}
    n_chunks = max(1, min(workers, len(frames)))
    chunks = [frames[i::n_chunks] for i in range(n_chunks)]
    chunks = [c for c in chunks if c]
    print(f"    drawing {len(frames)} graph(s) in {len(chunks)} R call(s) "
          f"...")
    t0 = time.perf_counter()
    graphs = {}
    if len(chunks) == 1:
        graphs.update(_r_chunk_worker((chunks[0], str(work / "r0"))))
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
            futs = [pool.submit(_r_chunk_worker, (c, str(work / f"r{i}")))
                    for i, c in enumerate(chunks)]
            for fut in as_completed(futs):
                graphs.update(fut.result())
    print(f"    R finished in {time.perf_counter() - t0:.0f}s "
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

    items, strains, opts, outdir, rank_note, work = job
    made, errs = [], []
    cur_key, cur_proc = None, [None, None]
    for (medium, cand, rows, metrics, plates, bg_r, sub_keys) in items:
        cid = candidate_id(cand, rows)
        try:
            pair_key = (str(cand["plate1"].path), str(cand["plate2"].path))
            if sub_keys and pair_key != cur_key:
                cur_proc = [tifffile.imread(t).astype(np.float64) if t else None
                            for t in sub_keys]
                cur_key = pair_key
            montage_png = Path(work) / f"{cid}_montage.png"
            _sm.build_montage(f"TC|{cid}", plates, strains, opts, montage_png,
                              rep_label="Replicate",
                              mark_row=int(rows[0]) % (_sq.N_ROWS // 2),
                              mark_label="quantified", bg_radius=bg_r,
                              proc=(cur_proc if sub_keys else None))
            sheet = (Path(outdir) / safe_name(medium)
                     / f"{_sort_prefix(cand, rows)}{cid}.png")
            gp = metrics.pop("_graph", None)
            _compose(sheet, montage_png, Path(gp) if gp else None,
                     cand, rows, metrics, rank_note, bg_radius=bg_r)
            made.append(str(sheet))
        except Exception as e:
            errs.append((cid, f"{type(e).__name__}: {e}"))
    return made, errs


def _run_r(csv_path: Path, outdir: Path) -> "Path | None":
    """Draw the graph with plot_spotting.R and return the PNG it wrote."""
    sb.run_r(csv_path, outdir)
    figs = sorted((outdir / "figures").glob("spotting_*.png"))
    return figs[0] if figs else None


def build_candidate_figure(cand: dict, rows: tuple, cache_dir: Path,
                           strains: list, control_col: int, outdir: Path,
                           opts: "sq.MeasureOptions | None" = None,
                           exclude=None, rank_note: str = "",
                           metrics: "dict | None" = None,
                           resolve=None) -> "Path | None":
    """One sheet: marked spot montage on the left, its own R graph on the right.

    Returns the sheet path, or None if the candidate could not be drawn (a
    missing cache entry, or R unavailable -- in which case the montage is still
    written on its own so the run is not wasted).
    """
    opts = opts or sq.MeasureOptions()
    cid = candidate_id(cand, rows)
    got = build_tidy_for_candidate(cand, rows, cache_dir, strains,
                                   control_col, exclude, resolve)
    if got is None:
        return None
    tidy, plates = got

    outdir.mkdir(parents=True, exist_ok=True)
    # The per-candidate CSV and the R figure are intermediates. They go to a
    # temp folder so a Results tree holds sheets, not three files per candidate;
    # the numbers behind every sheet are already in timecourse_candidates.csv
    # and can be regenerated from the cache in seconds.
    work = Path(tempfile.mkdtemp(prefix="tcfig_"))
    try:
        csv_path = work / f"{cid}_normalized.csv"
        tidy.to_csv(csv_path, index=False, encoding="utf-8-sig")
        graph_png = _run_r(csv_path, work)

        montage_png = work / f"{cid}_montage.png"
        # `rows` is a (lo, hi) pair one row apart in each replicate block, so
        # its position WITHIN a block is what the montage needs.
        mark = int(rows[0]) % (sq.N_ROWS // 2)
        sm.build_montage(f"TC|{cid}", plates, strains, opts, montage_png,
                         rep_label="Replicate", mark_row=mark,
                         mark_label="quantified")

        sheet = outdir / f"{cid}.png"
        _compose(sheet, montage_png, graph_png, cand, rows, metrics, rank_note)
        return sheet
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _compose(out_path: Path, montage_png: Path, graph_png: "Path | None",
             cand: dict, rows: tuple, metrics: "dict | None",
             rank_note: str, bg_radius=None) -> Path:
    """Lay the two panels on one sheet with a header naming the candidate."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.image as mpimg

    fig = plt.figure(figsize=(SHEET_W_IN, SHEET_H_IN), facecolor="white")
    gs = fig.add_gridspec(1, 2, width_ratios=[MONTAGE_FRAC, 1 - MONTAGE_FRAC],
                          left=0.012, right=0.988, top=0.88, bottom=0.04,
                          wspace=0.04)

    ax = fig.add_subplot(gs[0, 0])
    ax.imshow(mpimg.imread(str(montage_png)))
    ax.axis("off")

    ax = fig.add_subplot(gs[0, 1])
    if graph_png is not None and Path(graph_png).exists():
        ax.imshow(mpimg.imread(str(graph_png)))
    else:
        ax.text(0.5, 0.5, "no graph -- R unavailable\n(the spot panel is still "
                          "valid)", ha="center", va="center", fontsize=13,
                color="#a00000", transform=ax.transAxes)
    ax.axis("off")

    dil = _dil_choice(rows)
    title = (f"{cand['medium_label']}  |  {cand['tp_label']}  |  "
             f"{dil} dilution (rows {rows[0] + 1} & {rows[1] + 1})")
    sub = (f"plate 1: {cand['plate1'].path.name}     "
           f"plate 2: {cand['plate2'].path.name}")
    if metrics:
        bits = []
        if metrics.get("median_CV") is not None:
            bits.append(f"median CV {float(metrics['median_CV']):.3f}")
        if metrics.get("control_mean") is not None:
            bits.append(f"control {float(metrics['control_mean']):.1f} gray")
        if metrics.get("n_significant") is not None:
            bits.append(f"{int(metrics['n_significant'])} of "
                        f"{int(metrics.get('n_strains', 0))} strains p<0.05")
        if bits:
            sub += "          " + "   ".join(bits)

    fig.text(0.012, 0.975, title, fontsize=17, fontweight="bold",
             ha="left", va="top")
    fig.text(0.012, 0.932, sub, fontsize=11, color="#333333",
             ha="left", va="top")
    # The caveat travels with the figure, not just with the console output: a
    # sheet lifted out of the folder and pasted into a slide must still say what
    # it is.
    note = ("Triage only: this candidate was chosen by ranking, so the p-values "
            "shown are not those of a comparison fixed in advance. "
            "Re-run the main pipeline on the chosen photos to report a result.")
    if rank_note:
        note = f"{note}  ({rank_note})"
    fig.text(0.012, 0.902, note, fontsize=9, color="#8a5a00",
             ha="left", va="top")
    # On its own line, not opposite the triage note: right-aligning a second
    # caption on the same line let the two collide on a wide sheet.
    # Stating the display subtraction radius is not optional. It is deliberately
    # NOT this candidate's own ROI radius -- one radius per photo is what lets
    # the same plate look identical across its three dilution sheets, which is
    # the comparison these are for -- so the figure has to say what it used.
    rr = ([bg_radius] if bg_radius is None or np.isscalar(bg_radius)
          else list(bg_radius))
    shown = ", ".join(f"{float(r):.0f}" for r in rr if r) or "per-candidate"
    fig.text(0.012, 0.014,
             "Spot images: fixed 0-255 display range, no brightness or contrast "
             "adjustment. Amber outline marks the quantified dilution row. "
             f"Display background subtracted at ROI radius {shown} px "
             "(each photo's largest across the three dilutions, so the plate "
             "looks the same on all of its sheets).",
             fontsize=8, color="#666666", ha="left", va="bottom")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, facecolor="white", bbox_inches="tight")
    plt.close(fig)
    return out_path


def build_figures(df: "pd.DataFrame", cands: list, cache_dir: Path,
                  strains: list, control_col: int, outdir: Path,
                  n_per_medium: "int | None" = None,
                  opts: "sq.MeasureOptions | None" = None,
                  exclude=None, rank_note: str = "",
                  resolve=None, workers: int = 1) -> list:
    """Draw a sheet per candidate -- every one by default, so they can be compared.

    `n_per_medium=None` means all; an integer keeps only that many per medium,
    taken off the top of the ranking the frame is already sorted by (so the
    figures can never disagree with the CSV).

    Two things make drawing all ~50 tractable rather than an hour of repeated
    work, and both matter because the cost is dominated by things that do NOT
    vary per sheet:

    * ONE Rscript call for the whole run. plot_spotting.R already loops over the
      distinct values of `treatment` and writes a figure per value, so every
      candidate's tidy frame is stacked into a single CSV with the candidate id
      as its `treatment`. Each candidate keeps its own `experiment`, so the
      normalisation and the paired tests stay exactly as separate as they were
      when this ran R once per candidate. What is saved is ~50 interpreter
      start-ups and ~50 library loads.
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
                      f"{row.get('timepoint')} {row.get('plate1')}+"
                      f"{row.get('plate2')}; skipped")
                continue
            picked.append((medium, cand, _rows_from_row(row), row.to_dict()))
    # Sort on the FULL PATH, not the file name. A capture tree routinely names
    # every photo the same thing (`_9.JPG` in each timepoint/plate folder), so
    # keying on the name collapses this to (medium, dilution) -- which visits
    # every pairing at one dilution before returning to the first pairing, and
    # misses the block cache on literally every sheet. Measured: ~25 s a sheet
    # that way against ~2 s once the three dilutions of a pairing are adjacent.
    picked.sort(key=lambda t: (t[0], str(t[1]["plate1"].path),
                               str(t[1]["plate2"].path), t[2][0]))
    if not picked:
        return made

    # --- one tidy frame for everything, one R call ---------------------------
    work = Path(tempfile.mkdtemp(prefix="tcfigs_"))
    try:
        frames, keep = [], []
        for medium, cand, rows, metrics in picked:
            got = build_tidy_for_candidate(cand, rows, cache_dir, strains,
                                           control_col, exclude, resolve)
            if got is None:
                print(f"  ! no measurement for {candidate_id(cand, rows)}; "
                      f"skipped")
                continue
            tidy, plates = got
            frames.append(tidy)
            keep.append((medium, cand, rows, metrics, plates))
        if not frames:
            return made

        graphs = draw_graphs(frames, work, workers)

        # --- every subtraction the sheets need, once, in parallel -------------
        subs = prepare_subtractions(keep, cache_dir, opts, work, workers)

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
            bg_r = [_display_radius(s.path, s.plate, cache_dir)
                    for s in (cand["plate1"], cand["plate2"])]
            tifs = []
            for s, r_disp in zip((cand["plate1"], cand["plate2"]), bg_r):
                t = None
                if subs and r_disp is not None:
                    ball = opts.resolve_ball_radius(
                        2 * float(r_disp) / sq.MEASURE_RADIUS_FRAC)
                    got = subs.get(sq.fiji_batch_key(s.path, ball))
                    t = str(got) if got else None
                tifs.append(t)
            m = dict(metrics)
            g = graphs.get(candidate_id(cand, rows))
            m["_graph"] = str(g) if g else None
            key = (str(cand["plate1"].path), str(cand["plate2"].path))
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
                (chunks[0], strains, opts, str(outdir), rank_note, str(work))))
        else:
            from concurrent.futures import ProcessPoolExecutor, as_completed
            with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
                futs = [pool.submit(_compose_chunk_worker,
                                    (c, strains, opts, str(outdir), rank_note,
                                     str(work)))
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
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return made


def _sort_prefix(cand: dict, rows: tuple) -> str:
    """Filename prefix that sorts the sheets the way you compare them.

    Timepoint ascending, then dilution least->most. Sorting by name in a file
    browser then walks the time course in order, which is the comparison the
    figures exist for; the candidate id alone sorts alphabetically and puts
    '19 Hours' before '9 Hours'.
    """
    hours = cand.get("hours")
    h = f"{float(hours):06.1f}" if hours is not None else "______"
    d = sb.DILUTION_ORDER.index(_dil_choice(rows))
    return f"{h}h_d{d}_"


# Kept as the old name so any existing call site still works.
build_top_figures = build_figures


def _match_candidate(cands: list, row) -> "dict | None":
    """Find the candidate dict a scored row came from."""
    for c in cands:
        if (c["medium"] == row.get("medium")
                and c["tp_label"] == row.get("timepoint")
                and c["plate1"].path.name == row.get("plate1")
                and c["plate2"].path.name == row.get("plate2")):
            return c
    return None


def _rows_from_row(row) -> tuple:
    """The 0-based dilution row pair a scored row used."""
    name = str(row.get("dilution", "")).strip().lower()
    if name in sb.DILUTIONS:
        return tuple(sb.DILUTIONS[name])
    raise ValueError(f"unrecognised dilution {row.get('dilution')!r}")
