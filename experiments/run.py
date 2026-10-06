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
from .model import DATA_OUTPUT, QUANTIFY, TIMECOURSE, Experiment

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
    if e.strain_groups:
        raise RunError("Choose a single strain-group view before building a pipeline config.")
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
    if e.strain_groups:
        raise RunError("Split strain groups before creating timecourse shots.")
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
    if e.strain_groups:
        raise RunError("Split strain groups before creating plate references.")
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


def review_plates(review) -> dict:
    """relpath -> reason for the plates a data review flagged (none without one)."""
    if review is None:
        return {}
    return {path: flag.reason for path, flag in review.plates.items()}


def write_handoff(e: Experiment, res: intake.Resolution, outdir: Path,
                  layout=None, review=None, review_path=None) -> Path:
    """Record what was run, for the review tool to read.

    A resolved snapshot, not a reference: the experiment file may be edited
    again tomorrow, and the results folder has to keep saying what produced it.

    `review` is the experiment's data review (`data_review.flags.DataFlags`)
    and `review_path` where it is saved. Both are recorded: the snapshot says
    what was flagged when this ran, and the path lets the results review show
    flags made since.
    """
    sb, _ = _pipeline()
    photos = [
        {
            "relpath": r.relpath,
            "condition": r.condition,
            "condition_label": e.condition(r.condition).display(),
            "timepoint": r.timepoint,
            "timepoint_label": r.timepoint_label,
            "plate": r.plate,
            "shot": r.shot,
            "strain_group": r.set_key or e.set_key,
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
            "resolved_photos": photos,
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
        "data_review": {
            "file": str(review_path) if review_path else "",
            "flags": review.to_dict() if review is not None else None,
        },
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
        template=None, review=None, review_path=None) -> int:
    """Run this experiment. Returns the pipeline's exit code.

    `review` is the experiment's data review (`data_review.flags.DataFlags`),
    if it has one. The multi-step analysis excludes what it flags; the endpoint
    analysis is left exactly as it is, and only records the flags beside its
    results so the results review can mark them.
    """
    if res is None:
        res = prepare(e)
    outdir = Path(outdir) if outdir else default_results_dir(e)
    flagged = review_plates(review)

    if e.strain_groups:
        # Split BEFORE creating shots: Shot itself has no strain-group axis.
        # Each child keeps its own config, normalisation and review snapshot.
        from hashlib import sha256
        from .validate import blocking, validate
        if template is None:
            template = load_template(e)
        errors = blocking(validate(e, template)
                          + intake.check_resolution(e, res, template, flagged))
        if errors:
            raise RunError("\n".join(i.message for i in errors))
        _, tc = _pipeline()
        codes = []
        for key in e.strain_groups:
            # Hash prevents names sanitising to the same folder on Windows.
            folder = f"group-{tc.safe_dirname(key)[:60]}-{sha256(key.encode('utf-8')).hexdigest()[:12]}"
            print(f"\n  Strain group: {key}")
            # Flags are keyed by photo, so every group's run reads the same set.
            codes.append(run(e.for_group(key), outdir=outdir / folder,
                             estimate=estimate, workers=workers,
                             res=intake.group_resolution(res, key), template=template,
                             review=review, review_path=review_path))
        return next((code for code in codes if code), 0)

    handoff = {"review": review, "review_path": review_path}
    if e.mode == TIMECOURSE:
        runner = lambda: _run_timecourse(e, res, outdir, estimate=estimate,
                                         workers=workers, template=template,
                                         **handoff)
    elif e.mode == QUANTIFY:
        runner = lambda: _run_quantify(e, res, outdir, estimate=estimate,
                                       template=template, **handoff)
    else:
        raise RunError(f"unknown mode {e.mode!r}")
    if e.multi_step.enabled:
        from .multistep import design_errors, exclusions, selected_rows
        if template is None:
            template = load_template(e)
        errors = design_errors(e, template, res, flagged)
        if template is None:
            errors.append("Multi-step analysis requires a plate template.")
        if errors:
            raise RunError("\n".join(errors))
        selected = selected_rows(e, res)
        left_out = exclusions(e, flagged)
        print(f"  Additional {e.multi_step.method} analysis: "
              f"{len(selected)} selected photographs, "
              f"{len(e.multi_step.hours)} timepoint(s).")
        if review is not None:
            by_review = sum(1 for r in selected if r.relpath in flagged)
            spots = sum(len(review.spots_on(r.relpath)) for r in selected
                        if r.relpath not in left_out)
            print(f"  Data review: {by_review} flagged plate(s) and {spots} "
                  f"flagged spot(s) are left out of it.")
        if not estimate:
            # The legacy runner only honours a custom directory if it exists.
            outdir.mkdir(parents=True, exist_ok=True)
    baseline_path = outdir / ("best" if e.is_timecourse else "") / "spotting_results_normalized.csv"
    previous_baseline_time = baseline_path.stat().st_mtime_ns if baseline_path.exists() else None
    code = runner()
    if e.multi_step.enabled and not estimate:
        # Missing legacy candidates must not discard a valid additional analysis.
        # Its graph simply has no current-endpoint marker when no baseline exists.
        fresh_baseline = (baseline_path.exists()
                          and baseline_path.stat().st_mtime_ns != previous_baseline_time)
        run_multi_step(e, res, template, outdir,
                       include_baseline=(code == 0 and fresh_baseline),
                       review=review)
    return code


def measure_multi_step(e, res, template, review=None):
    """One measurement per actual physical plate/time; no candidate products.

    Read the same cached pixels as the current pipeline. Keep technical failure
    records; do not apply statistical outlier deletion to this additional path.

    What the data review flagged is excluded with its reason: a flagged plate
    like a photo excluded in the dialog, a flagged spot like an image artifact
    -- which also invalidates any contrast that used it as the matched control.
    """
    import pandas as pd
    import numpy as np
    import spotting_quant as sq
    from .multistep import exclusions, selected_rows
    sb, tc = _pipeline()
    layout = to_layout(template, e.photo_top)
    level = layout.levels[e.multi_step.dilution]
    cache = sb.PROJECT_ROOT / sb.CACHE_DIR
    root = Path(e.photo_root)
    observations = []
    selected = selected_rows(e, res)
    excluded = exclusions(e, review_plates(review))
    for n, row in enumerate(selected, 1):
        reason = excluded.get(row.relpath, "")
        data = None
        floor = sq.MIN_CONTROL_GRAY
        if not reason:
            try:
                data = tc._cached_measure(root / row.relpath, row.plate,
                                          level.quant_rows(row.plate), cache, layout)
                floor = max(sq.MIN_CONTROL_GRAY,
                            sq.CONTROL_NOISE_MULT * sq.bg_noise(data.bg_samples))
            except Exception as exc:
                reason = f"measurement failed: {type(exc).__name__}: {exc}"
        label = e.multi_step.plate_ids.get(row.relpath, "unassigned excluded plate")
        physical = json.dumps([e.set_key or e.name, row.condition, row.plate, label], ensure_ascii=False)
        # An excluded duplicate must remain auditable without becoming a second
        # observation of the same biological spot in the statistical input.
        if reason:
            physical = json.dumps([physical, "excluded", row.relpath], ensure_ascii=False)
        for cell in level.cells(row.plate):
            strain = e.strain(cell.slot)
            if not strain:
                continue
            reasons = [reason] if reason else []
            if cell.slot in e.exclude_for(row.condition):
                reasons.append("strain excluded in experiment")
            if data is not None and data.rim[cell.row, cell.col]:
                reasons.append("image artifact / unreliable ROI")
            # Level cells are photograph-grid cells, as the review keys them.
            flagged = (review.spot_reason(row.relpath, cell.row + 1, cell.col + 1)
                       if review is not None else "")
            if flagged:
                reasons.append(f"data review: {flagged}")
            observations.append({
                "condition": row.condition, "strain": strain, "strain_col": cell.slot,
                "replicate": f"rep{cell.replicate}", "template_plate": row.plate,
                "technical_plate": label, "physical_plate": physical,
                "hours": row.timepoint, "dilution": e.multi_step.dilution,
                "image": row.relpath, "row": cell.row + 1, "column": cell.col + 1,
                "raw_growth": float(data.net[cell.row, cell.col]) if data is not None else np.nan,
                "detection_limit": floor, "is_control": cell.slot == e.control_for(row.condition),
                "qc_reason": "; ".join(reasons)})
        print(f"  Multi-step measurements: {n}/{len(selected)}", flush=True)
    if not observations:
        raise RunError("No spots available for multi-step analysis.")
    return pd.DataFrame(observations)


def _multi_step_baseline(e, res, outdir):
    """Map the legacy endpoint to its actual hour, not an inferred photo order."""
    import pandas as pd
    location = Path(outdir) / ("best" if e.is_timecourse else "") / "spotting_results_normalized.csv"
    if not location.exists():
        return None
    baseline = pd.read_csv(location)
    baseline["hours"] = float("nan")
    if e.is_timecourse:
        for row in res.usable():
            label = f"{e.name} {row.condition} {row.timepoint_label or f'{row.timepoint:g} Hours'}"
            match = baseline["treatment"] == label
            baseline.loc[match, "hours"] = row.timepoint
            baseline.loc[match, "treatment"] = row.condition
    else:
        by_path = {r.relpath: r for r in res.usable()}
        for c in e.conditions:
            picked_hours = {by_path[p].timepoint for p in e.picked_plates(c.code).values() if p in by_path}
            if len(picked_hours) == 1 and None not in picked_hours:
                baseline.loc[baseline.treatment == c.code, "hours"] = next(iter(picked_hours))
    return baseline


def run_multi_step(e, res, template, outdir, *, include_baseline=True, review=None):
    """Technical-only analysis of several hours runs once per hour; repeated measures runs once."""
    s = e.multi_step
    if s.scope == "technical" and len(s.hours) > 1:
        from dataclasses import replace
        observations = measure_multi_step(e, res, template, review)
        destination = None
        for h in s.hours:
            single = replace(s, hours=(h,))
            part = observations[observations.hours == h]
            if part.empty:
                continue
            print(f"  Technical-replicate analysis at {h:g} h", flush=True)
            destination = _run_multi_step_once(e, res, outdir, single, part, include_baseline,
                                               suffix=f"-{h:g}h", review=review)
        return destination
    return _run_multi_step_once(e, res, outdir, s, measure_multi_step(e, res, template, review),
                                include_baseline, review=review)


def _run_multi_step_once(e, res, outdir, settings, observations, include_baseline, suffix="",
                         review=None):
    import spotting_multistep as multi
    from .multistep import to_dict
    print("  Fitting additional multi-step analysis ...", flush=True)
    result = multi.analyze(observations, settings, e.statistics.alpha)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = Path(outdir) / "multi_step" / f"{stamp}-{settings.scope}-{settings.method}{suffix}"
    baseline = _multi_step_baseline(e, res, outdir) if include_baseline else None
    multi.write_results(result, destination, settings, e.statistics.alpha, baseline)
    # Record selection/exclusion even for ignored or unresolved photos, which
    # cannot produce a measurement row. Never erase a previous analysis run.
    from .multistep import exclusions, selected_rows
    selected = {r.relpath for r in selected_rows(e, res)}
    excluded = exclusions(e, review_plates(review))
    selection = [{"image": r.relpath, "status": r.status,
                  "selected": r.relpath in selected,
                  "technical_plate": e.multi_step.plate_ids.get(r.relpath),
                  "exclusion_reason": excluded.get(r.relpath, ""),
                  "data_review_spots": (len(review.spots_on(r.relpath))
                                        if review is not None else 0)}
                 for r in res.rows]
    (destination / "selection.json").write_text(json.dumps(selection, indent=2, ensure_ascii=False), encoding="utf-8")
    (destination / "experiment.json").write_text(schema.dumps_experiment(e), encoding="utf-8")
    if review is not None:
        # The flags exactly as they stood for this analysis: the review file
        # beside the experiment goes on changing, this record must not.
        (destination / "data_review.json").write_text(
            json.dumps(review.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    latest = Path(outdir) / "multi_step" / "latest.json"
    temp = latest.with_suffix(".tmp")
    temp.write_text(json.dumps({"directory": destination.name, "settings": to_dict(settings)}, indent=2), encoding="utf-8")
    os.replace(temp, latest)
    for diagnostic in result.diagnostics:
        if diagnostic["status"] != "ok":
            print(f"  ! {diagnostic['condition']} / {diagnostic['strain']}: {diagnostic['reason']}")
    print(f"  Additional graphs and statistics: {destination}")
    return destination


def _run_timecourse(e, res, outdir, *, estimate, workers, template=None,
                    review=None, review_path=None) -> int:
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
    # Data only: every candidate is still scored and ranked, and the best
    # per medium still gets its per-spot CSV -- only the sheets are not drawn.
    figures = "none" if e.output == DATA_OUTPUT else None
    args = _timecourse_args(outdir, estimate=estimate, workers=workers,
                            figures=figures)
    _configure_timecourse_workers(tc, args)

    code = tc.run_one(tree, args, cfg, multi=False, shots=shots, layout=layout,
                      statistics=e.statistics.plot_kwargs())
    if code == 0 and not estimate:
        write_handoff(e, res, outdir, layout=layout, review=review,
                      review_path=review_path)
        seed_review_statistics(e, outdir)
    return code


#: The review tool's own file, beside the results. Named here rather than
#: imported so the bridge does not depend on the review package.
REVIEW_NAME = "review.json"


def seed_review_statistics(e: Experiment, outdir: Path) -> Path:
    """Start the review tool on the tests this run's figures were drawn with.

    Only the `statistics` block is written; picks and spot edits already in a
    `review.json` are kept. Without this the review tool would open on its own
    defaults and disagree with the figures it is showing.
    """
    path = Path(outdir) / REVIEW_NAME
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    data.setdefault("version", 1)
    data["statistics"] = e.statistics.review_dict()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


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

    wall = time.perf_counter() - started
    if not errors:    # as tc.measure_all: a failure would make it look fast
        import spotting_estimate as est

        est.record_measure(sum(1 for _, cached in elapsed if not cached),
                           wall, 1)

    if timing:
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


def _run_quantify(e, res, outdir, *, estimate, template=None, review=None,
                  review_path=None) -> int:
    """Measure the chosen photos once and write the tidy data and figures.

    The same sequence `spotting_batch.main` runs, with the answers taken from
    the experiment instead of from console prompts.
    """
    import time

    import pandas as pd

    sb, _ = _pipeline()
    import spotting_estimate as est
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

    def options(code, ref):
        """The options this plate is measured, and so cached, under."""
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
        return replace(sq.MeasureOptions(), quant_rows=rows,
                       n_rows=grid_rows, n_cols=grid_cols)

    cache = sb.PROJECT_ROOT / sb.CACHE_DIR
    n_photos = sum(len(refs) for refs in combos.values())
    print(f"\n  {n_photos} photo(s) in {len(combos)} condition(s)")
    # Checked with the options each plate is really measured under: the grid
    # is part of the cache key, so a default-options check miscounts any
    # design that is not 8x6.
    todo = sum(
        1 for combo, refs in combos.items() for ref in refs
        if not (cache / f"{sb._cache_key(ref.path, options(combo.split('|', 1)[1], ref))}.npz").exists()
    )
    if estimate:
        # Plates are measured one at a time here, then one graph is drawn per
        # condition.
        per_photo, _ = est.measure_s(1)
        graphs = 0 if e.output == DATA_OUTPUT else len(combos)
        seconds = todo * per_photo + graphs * est.GRAPH_S + est.OVERHEAD_S
        print(f"  {todo} not yet cached; measured one at a time at "
              f"~{per_photo:.0f} s each, the run takes about "
              f"{est.format_minutes(seconds)}")
        return 0

    cache.mkdir(parents=True, exist_ok=True)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    frames = []
    started = time.perf_counter()
    for combo in sb.sort_combos(combos):
        refs = combos[combo]
        code = combo.split("|", 1)[1]
        print(f"\n  {combo}: measuring {len(refs)} plate(s) ...")

        plates = [(str(ref.plate), sb.measure(ref, options(code, ref), cache))
                  for ref in refs]

        tidy = build_tidy(e, template, code, plates)
        if not tidy.empty:
            frames.append(tidy)
    est.record_measure(todo, time.perf_counter() - started, 1)

    if not frames:
        print("  Nothing could be quantified.", file=sys.stderr)
        return 1

    tidy = pd.concat(frames, ignore_index=True)
    tidy = sq.flag_outliers(tidy, group_keys=["experiment"])
    csv_path = outdir / "spotting_results_normalized.csv"
    tidy.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"\n  wrote {csv_path}")

    if e.output == DATA_OUTPUT:
        # The CSV already holds both raw_growth (grey value) and
        # relative_growth (normalised to the control); nothing is drawn.
        print("  data only: graphs, charts and statistics were not made")
        write_handoff(e, res, outdir, review=review, review_path=review_path)
        return 0

    sb.run_plots(csv_path, outdir, **e.statistics.plot_kwargs())
    sb.write_condition_matrix(outdir)
    write_handoff(e, res, outdir, review=review, review_path=review_path)
    return 0
