"""The bridge: an experiment, run through the pipelines that already exist.

This module reimplements nothing. `src/spotting_quant.py` measures spots,
`src/spotting_timecourse.py` scores every candidate and picks a best set, and
`src/spotting_batch.py` quantifies handpicked photos. All of that is validated
and stays exactly as it is. What was missing was a way to drive it that did not
require the photos to be in one particular folder layout and the answers to be
typed at a console prompt.

So the work here is translation, in two directions:

    an Experiment          -> the `cfg` dict the pipeline already reads
    resolved PhotoRows     -> the `Shot` / `PhotoRef` records it already takes

and then one extra output, `experiment.json`, written beside the results so the
review tool can show real strain names instead of re-deriving them.

This is the only module besides `cli` allowed to import the pipeline, so that
authoring an experiment never costs a numpy import;
`tests/experiments/test_experiment_structure.py` enforces that.
"""

from __future__ import annotations

import json
import os
import sys
from argparse import Namespace
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import BUNDLE, REPO, geometry, intake, schema
from .model import QUANTIFY, TIMECOURSE, Experiment

SRC = REPO / "src"
# Packaged, this is also what lets an edit to `src/` take effect without a
# rebuild: the folder beside the .exe goes on the path ahead of the built-in
# copy. When there is no such folder, the built-in copy is used instead.
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class RunError(RuntimeError):
    """A run could not be started. Messages say what is missing."""


def _pipeline():
    """Import the pipeline lazily, so merely importing this module is cheap."""
    import spotting_batch as sb
    import spotting_timecourse as tc

    return sb, tc


# ---------------------------------------------------------------------------
# Experiment -> the config the pipeline reads
# ---------------------------------------------------------------------------


def to_layout(template, experiment_top: str = "top"):
    """The template's dilution levels as a `spotting_batch.DilutionLayout`.

    This is what frees the time course from three levels: it scores, names and
    draws whatever levels the design declares. Each level's cells carry their
    own sample slot and replicate straight off the template's placements. Plate
    ids become the plate NUMBERS the pipeline pairs photos by.

    Returns None without a template, which the pipeline reads as the classic
    three-level layout.
    """
    if template is None:
        return None
    sb, _ = _pipeline()
    levels = []
    for index in range(geometry.level_count(template)):
        per_plate = []
        for plate_id in geometry.plate_ids(template):
            try:
                number = int(plate_id)
            except ValueError:
                raise RunError(
                    f"plate id {plate_id!r} is not a number, so it cannot be "
                    f"matched to a photographed plate") from None
            cells = tuple(
                sb.LevelCell(r, c, p.sample_slot, p.replicate)
                for r, c, p in geometry.oriented_cells_for(
                    template, plate_id, index, experiment_top
                ))
            if cells:
                per_plate.append((number, cells))
        levels.append(sb.DilutionLevel(index, geometry.level_label(template, index),
                                       tuple(per_plate)))
    rows, cols = geometry.oriented_grid_shape(template, experiment_top)
    layout = sb.DilutionLayout(tuple(levels), rows, cols)
    # Hand back the canonical classic object when the design IS the lab's, so
    # every cache key, filename and precompute takes the long-standing path.
    return (sb.classic_layout()
            if experiment_top == "top" and layout == sb.classic_layout()
            else layout)


def to_pipeline_config(e: Experiment) -> dict:
    """The `cfg` dict `spotting_timecourse` expects.

    Shaped exactly like a `timecourse_config.json`, because that is what
    `medium_cfg` reads. The per-condition control lands in `media`, which is the
    seam the branch added for it; nothing in the pipeline needs to change.
    """
    media = {}
    for c in e.conditions:
        media[c.code] = {
            "control_col": e.control_for(c.code),
            "exclude": list(c.exclude),
        }
    return {
        "strains": list(e.strains),
        "control_col": e.control_slot,
        "exclude": [],
        "from_set": e.set_key or "",
        "media": media,
    }


def to_shots(e: Experiment, res: intake.Resolution):
    """Resolved photos as `spotting_timecourse.Shot` records.

    Only photos for a declared condition are handed over: an undeclared one has
    no control and no strain names, and `check_resolution` has already said so.
    """
    _, tc = _pipeline()
    root = Path(e.photo_root)
    declared = set(e.condition_codes())
    shots = []
    for row in res.usable():
        if row.condition not in declared:
            continue
        label = e.condition(row.condition).display()
        shots.append(
            tc.Shot(
                path=root / row.relpath,
                tp_hours=row.timepoint if row.timepoint is not None else 0.0,
                tp_label=row.timepoint_label or f"{row.timepoint:g} Hours",
                medium=row.condition,
                medium_label=label,
                plate=row.plate,
            )
        )
    return shots


def to_photo_refs(e: Experiment, res: "intake.Resolution | None" = None):
    """Photos as `spotting_batch.PhotoRef` records, grouped by combo.

    Handpicked mode uses the photographs the user chose outright. Only when
    there are no picks does it fall back to whatever the naming profile could
    read, which is what a migrated `spotting_config.json` relies on -- its
    photos were never picked in a window, they were named on disk.
    """
    if e.picks:
        return _refs_from_picks(e)
    if res is None:
        return {}
    return _refs_from_resolution(e, res)


def _refs_from_picks(e: Experiment):
    sb, _ = _pipeline()
    root = Path(e.photo_root)
    combos: dict[str, list] = {}
    for code in e.condition_codes():
        for plate_id, relpath in sorted(e.picked_plates(code).items()):
            try:
                plate = int(plate_id)
            except ValueError:
                # The pipeline numbers plates; a template using a non-numeric
                # plate id cannot drive it, and silently renumbering would
                # reassign biological replicates.
                raise RunError(
                    f"plate id {plate_id!r} is not a number, so it cannot be "
                    f"matched to a biological replicate"
                ) from None
            ref = sb.PhotoRef(path=root / relpath, set_id=e.set_key or e.name,
                              plate=plate, treatment=code)
            combos.setdefault(ref.combo, []).append(ref)
    for refs in combos.values():
        refs.sort(key=lambda r: (r.plate, r.path.name))
    return combos


def _refs_from_resolution(e: Experiment, res: intake.Resolution):
    sb, _ = _pipeline()
    root = Path(e.photo_root)
    declared = set(e.condition_codes())
    combos: dict[str, list] = {}
    for row in res.usable():
        if row.condition not in declared:
            continue
        ref = sb.PhotoRef(
            path=root / row.relpath,
            set_id=e.set_key or e.name,
            plate=row.plate,
            treatment=row.condition,
        )
        combos.setdefault(ref.combo, []).append(ref)
    for refs in combos.values():
        refs.sort(key=lambda r: (r.plate, r.path.name))
    return combos


# ---------------------------------------------------------------------------
# Resolving an experiment's photos
# ---------------------------------------------------------------------------


def prepare(e: Experiment) -> intake.Resolution:
    """Scan and resolve this experiment's photo folder."""
    root = Path(e.photo_root)
    if not root.is_dir():
        raise RunError(f"photo folder not found: {root}")
    files, complaints = intake.scan_images(root)
    for c in complaints:
        print(f"  ! {c}")
    if not files:
        raise RunError(f"no images under {root}")
    return intake.resolve(e, files)


def default_results_dir(e: Experiment) -> Path:
    """Where this experiment's results go.

    The two pipelines keep separate roots -- `Results/Timecourse` is triage,
    `Results/Spotting` is a result to report -- and that separation is kept.
    """
    sb, tc = _pipeline()
    root = sb.TIMECOURSE_RESULTS if e.is_timecourse else sb.MAIN_RESULTS
    return root / tc.safe_dirname(e.name)


# ---------------------------------------------------------------------------
# The handoff file
# ---------------------------------------------------------------------------

#: Written beside the results. Additive: every file the review tool already
#: reads is still written exactly as before, so `results_review` keeps working
#: whether or not this is present.
HANDOFF_NAME = "experiment.json"


def write_handoff(e: Experiment, res: intake.Resolution, outdir: Path,
                  layout=None) -> Path:
    """Record what was run, for the review tool to read.

    A resolved snapshot, not a reference: the experiment file may be edited
    again tomorrow, and the results folder has to keep saying what produced it.
    """
    sb, _ = _pipeline()
    photos = [
        {
            "relpath": r.relpath,
            "condition": r.condition,
            "timepoint": r.timepoint,
            "timepoint_label": r.timepoint_label,
            "plate": r.plate,
            "shot": r.shot,
        }
        for r in res.usable()
        if r.condition in set(e.condition_codes())
    ]
    payload = {
        "schema_version": e.schema_version,
        "kind": "spotting_experiment_run",
        "written": datetime.now().isoformat(timespec="seconds"),
        "experiment": schema.to_dict(e),
        "controls": {c.code: e.control_for(c.code) for c in e.conditions},
        "strains": list(e.strains),
        # The exact `cfg` the run was driven with, shaped like a
        # timecourse_config.json. The review tool re-derives per-spot numbers
        # itself, so it must normalise against the same control this run used;
        # handing it the identical dict removes any chance of the two drifting.
        # The config the review tool drives its rebuilds with. It carries the
        # dilution layout too: rebuilding per-spot numbers from the cache means
        # knowing which rows each level name meant, and without it the review
        # could only assume the classic three.
        "pipeline_config": {
            **to_pipeline_config(e),
            **({"dilution_layout": layout.to_dict()}
               if layout is not None and not layout.is_classic() else {}),
        },
        "dilution_layout": (layout.to_dict()
                            if layout is not None and not layout.is_classic()
                            else None),
        "photo_root": e.photo_root,
        "photos": photos,
        # The cache tag the measurements were made under. Invalidation is
        # manual (see the README), so recording it is how a stale results
        # folder can later be told apart from a current one.
        "cache_key_version": _cache_tag(sb),
    }
    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / HANDOFF_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)
    return path


def _cache_tag(sb) -> str:
    """The trailing version tag in `spotting_batch._cache_key`, e.g. "v23"."""
    import inspect
    import re

    try:
        source = inspect.getsource(sb._cache_key)
    except (OSError, TypeError):
        return ""
    m = re.search(r"\|(v\d+)", source)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


#: Exactly the attributes `spotting_timecourse.run_one` reads, with the values
#: its own argument parser defaults to. Built explicitly rather than by calling
#: that parser, so that a new pipeline flag surfaces as a clear AttributeError
#: here rather than as a silently different default -- and `rank_by` in
#: particular stays "combined", matching run_timecourse.bat, so an experiment
#: run and a console run rank candidates identically.
#: `tests/experiments/test_matches_pipeline.py` asserts this set stays in step.
RUN_ONE_ARGS = {
    "out": None,
    "cache_dir": None,
    "workers": 0,
    "timing": False,
    "estimate": False,
    "rank_by": "combined",
    "figures": "all",
}


def _timecourse_args(outdir: Path, **overrides) -> Namespace:
    """The argument namespace `spotting_timecourse.run_one` reads."""
    args = Namespace(**RUN_ONE_ARGS)
    args.out = outdir
    for key, value in overrides.items():
        if value is not None or key in ("out",):
            setattr(args, key, value)
    return args


def run(e: Experiment, *, outdir: Path | None = None, estimate: bool = False,
        workers: int | None = None, res: intake.Resolution | None = None,
        template=None) -> int:
    """Run this experiment. Returns the pipeline's exit code."""
    if res is None:
        res = prepare(e)
    outdir = Path(outdir) if outdir else default_results_dir(e)

    if e.mode == TIMECOURSE:
        return _run_timecourse(e, res, outdir, estimate=estimate, workers=workers,
                               template=template)
    if e.mode == QUANTIFY:
        return _run_quantify(e, res, outdir, estimate=estimate, template=template)
    raise RunError(f"unknown mode {e.mode!r}")


def _run_timecourse(e, res, outdir, *, estimate, workers, template=None) -> int:
    _, tc = _pipeline()
    if template is None:
        template = load_template(e)
    layout = to_layout(template, e.photo_top)
    shots = to_shots(e, res)
    if not shots:
        raise RunError(
            "no photo resolved to a declared condition; run "
            "`experiments.cli scan` on the folder to see why"
        )

    cfg = to_pipeline_config(e)
    tree = tc.Tree(path=Path(e.photo_root), label=e.name, set_hint=e.set_key)
    args = _timecourse_args(outdir, estimate=estimate, workers=workers)
    _configure_timecourse_workers(tc, args)

    code = tc.run_one(tree, args, cfg, multi=False, shots=shots, layout=layout)
    if code == 0 and not estimate:
        write_handoff(e, res, outdir, layout=layout)
    return code


def _configure_timecourse_workers(tc, args) -> None:
    """Fall back to serial work when Windows blocks multiprocessing.

    Some managed Windows installations allow the Conda interpreter and the
    scientific extension modules but block ``_multiprocessing.pyd`` through an
    Application Control policy.  Importing ``ProcessPoolExecutor`` then fails
    before the pipeline can reach its existing one-worker branch.  The work is
    still valid serially, so install a small serial dispatcher and force every
    later figure stage to one worker as well.
    """
    try:
        from concurrent.futures import ProcessPoolExecutor  # noqa: F401
    except (ImportError, OSError) as exc:
        args.workers = 1
        tc.measure_all = lambda jobs, cache_dir, workers, timing=False: (
            _measure_all_serial(tc, jobs, timing=timing)
        )
        print(
            "\n  ! Parallel workers are unavailable on this computer "
            f"({exc}).\n"
            "    Continuing safely with one worker; quantification will be "
            "slower."
        )


def _measure_all_serial(tc, jobs, *, timing=False):
    """The pipeline's measurement loop without importing multiprocessing."""
    import time

    errors, elapsed = [], []
    jobs = list(jobs)
    total = len(jobs)
    started = time.perf_counter()
    done_n = 0
    for path, _rows, error, seconds, cached in tc._measure_chunk(jobs):
        done_n += 1
        if error:
            errors.append((path, error))
        elapsed.append((seconds, cached))
        print(f"\r    {done_n}/{total}", end="", flush=True)
    print()

    if timing:
        wall = time.perf_counter() - started
        fresh = sorted(seconds for seconds, cached in elapsed if not cached)
        hits = sum(1 for _, cached in elapsed if cached)
        print(f"    timing: {wall / 60:.1f} min wall-clock on 1 worker(s) "
              f"for {total} job(s)")
        if fresh:
            middle = fresh[len(fresh) // 2]
            print(f"            {len(fresh)} measured: "
                  f"min {fresh[0]:.0f}s / median {middle:.0f}s / "
                  f"max {fresh[-1]:.0f}s each")
        if hits:
            print(f"            {hits} served from cache")
    return errors, elapsed


# ---------------------------------------------------------------------------
# Handpicked quantification
# ---------------------------------------------------------------------------



def build_tidy(e: Experiment, template, code: str, plates) -> "object":
    """One tidy frame for one condition, laid out by the plate template.

    The same columns `spotting_batch.build_tidy` produces, and the same
    normalisation -- but which rows hold which dilution, and which replicate a
    row belongs to, are read off the template instead of assumed.

    `spotting_batch` derives them arithmetically from the lab's own design:
    three levels spotted twice down six rows, so `row % 3` is the level and
    `(plate - 1) * 2 + 1 + row // 3` is the replicate. That is correct for that
    design and wrong for any other, which is the whole reason a template exists.
    Reading `Placement` gives the same answers for the lab standard and the
    right ones for six levels, or two, or an asymmetric layout.

    `plates` is [(plate_id, PlateData)].
    """
    import pandas as pd
    import spotting_quant as sq

    control = e.control_for(code)
    dropped = set(e.exclude_for(code))
    experiment = f"{e.name} {code}" if e.set_key is None else f"Set {e.set_key} {code}"

    rows = []
    for plate_id, data in plates:
        index = resolved_level(e, template, code, plate_id)
        for r, c, placement in geometry.oriented_cells_for(
                template, plate_id, index, e.photo_top):
            name = e.strain(placement.sample_slot)
            if not name:
                continue                     # slot holds no strain
            rows.append({
                "experiment": experiment,
                "treatment": code,
                "set": e.set_key or e.name,
                "plate": data.ref.plate,
                "image": data.ref.path.name,
                "replicate": f"rep{placement.replicate}",
                "dilution_row": r + 1,
                "dilution": geometry.level_label(template, placement.dilution),
                "strain_col": placement.sample_slot,
                "strain": name,
                "raw_growth": float(data.net[r, c]),
                "artifact": bool(data.rim[r, c]),
                "excluded": placement.sample_slot in dropped,
                "is_control": placement.sample_slot == control,
            })

    full = pd.DataFrame(rows)
    if full.empty:
        return full

    keep = full[~full["excluded"]].copy()
    if control in dropped or keep.empty:
        print(f"    ! {experiment}: the control was excluded; "
              f"skipping normalisation for this condition.")
        return full

    # Whether a control can serve as a denominator depends on this plate's own
    # measurement noise. Copied from `spotting_batch.build_tidy`, comment and
    # all, because it is a measured property of the media rather than a choice:
    # a fixed gray cutoff cannot work across media whose signal range differs by
    # an order of magnitude, and using one nulled visibly-grown glycerol controls.
    noise = max([sq.bg_noise(d.bg_samples) for _, d in plates] or [0.0])
    min_control = max(sq.MIN_CONTROL_GRAY, sq.CONTROL_NOISE_MULT * noise)

    keep = sq.add_relative_growth(keep, control_col=control,
                                  group_keys=["experiment"],
                                  min_control=min_control)
    key = ["experiment", "replicate", "strain_col"]
    added = [c for c in keep.columns if c not in full.columns]
    return full.merge(keep[key + added], on=key, how="left")


def resolved_level(e: Experiment, template, code: str, plate_id) -> int | None:
    """The dilution level chosen for one plate of one condition, as an index."""
    return geometry.resolve_level(template, e.dilution_for(code, plate_id))


def load_template(e: Experiment):
    """The plate template this experiment is bound to, or None.

    Handpicked quantification cannot run without it: which rows hold which
    dilution level is a property of the design, not of the photograph.
    """
    if not e.template_path:
        return None
    from plate_template.schema import TemplateError, load

    # BUNDLE last: the default template ships inside the .exe, not beside it.
    for candidate in (Path(e.template_path), REPO / e.template_path,
                      BUNDLE / e.template_path):
        if candidate.exists():
            try:
                return load(candidate)
            except TemplateError:
                return None
    return None


def _run_quantify(e, res, outdir, *, estimate, template=None) -> int:
    """Measure the chosen photos once and write the tidy data and figures.

    The same sequence `spotting_batch.main` runs, with the answers taken from
    the experiment instead of from console prompts.
    """
    import pandas as pd

    sb, _ = _pipeline()
    import spotting_quant as sq

    if template is None:
        template = load_template(e)
    combos = to_photo_refs(e, res)
    if not combos:
        raise RunError(
            "no photo resolved to a declared condition; run "
            "`experiments.cli scan` on the folder to see why"
        )

    if template is None:
        raise RunError(
            "no plate template is bound, so there is no way to know which rows "
            "hold which dilution level"
        )

    # Only the plates actually being run need a level; a condition declared but
    # never photographed is not a reason to refuse the others.
    missing = []
    for combo, refs in sorted(combos.items()):
        code = combo.split("|", 1)[1]
        for ref in refs:
            index = resolved_level(e, template, code, str(ref.plate))
            if not geometry.is_valid_level(template, index):
                missing.append(f"{code} plate {ref.plate}")
    if missing:
        raise RunError(
            f"no dilution level chosen for {', '.join(missing)}. One level is "
            f"scored per plate and it cannot be picked for you"
        )

    cache = sb.PROJECT_ROOT / sb.CACHE_DIR
    n_photos = sum(len(refs) for refs in combos.values())
    print(f"\n  {n_photos} photo(s) in {len(combos)} condition(s)")
    if estimate:
        opts = sq.MeasureOptions()
        todo = sum(
            1 for refs in combos.values() for ref in refs
            if not (cache / f"{sb._cache_key(ref.path, opts)}.npz").exists()
        )
        print(f"  {todo} not yet cached; at ~{60} s each that is about "
              f"{todo * 60 / 60:.0f} min")
        return 0

    cache.mkdir(parents=True, exist_ok=True)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    frames = []
    for combo in sb.sort_combos(combos):
        refs = combos[combo]
        code = combo.split("|", 1)[1]
        print(f"\n  {combo}: measuring {len(refs)} plate(s) ...")

        plates = []
        for ref in refs:
            plate_id = str(ref.plate)
            index = resolved_level(e, template, code, plate_id)
            # The rows this level occupies on THIS plate, read off the design.
            # `spotting_quant` sizes its measuring ROI to fit them, and accepts
            # however many there are -- one per plate, two, or more.
            rows = geometry.oriented_rows_for(
                template, plate_id, index, e.photo_top
            )
            # `replace` rather than mutating the instance: the pipeline's own
            # idiom (spotting_timecourse.py:1764), and it keeps the options
            # hashable for the cache key.
            #
            # The lattice to look for comes from the template too. The engine
            # derives the expected spot pitch from it, so a 12x16 design is
            # found at a 12x16 pitch rather than being read as a sparse 8x6.
            grid_rows, grid_cols = geometry.oriented_grid_shape(
                template, e.photo_top
            )
            opts = replace(sq.MeasureOptions(), quant_rows=rows,
                           n_rows=grid_rows, n_cols=grid_cols)
            plates.append((plate_id, sb.measure(ref, opts, cache)))

        tidy = build_tidy(e, template, code, plates)
        if not tidy.empty:
            frames.append(tidy)

    if not frames:
        print("  Nothing could be quantified.", file=sys.stderr)
        return 1

    tidy = pd.concat(frames, ignore_index=True)
    tidy = sq.flag_outliers(tidy, group_keys=["experiment"])
    csv_path = outdir / "spotting_results_normalized.csv"
    tidy.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n  wrote {csv_path}")

    sb.run_plots(csv_path, outdir)
    sb.write_condition_matrix(outdir)
    write_handoff(e, res, outdir)
    return 0
