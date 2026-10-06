"""Locate the spots on every photograph an experiment has, for the data review.

    py -m data_review.cli detect <experiment.spotexp.json> [--workers N] [--estimate]
    py -m data_review.cli status <experiment.spotexp.json>

`detect` is the measurement pass of a run and nothing else: every photo the run
would measure is put through the pipeline's own `measure_all`, with the jobs
built by its own `build_jobs`, into the same cache. A later run therefore finds
every photo already measured, and the data review sees exactly the grid the run
will use. Photos already in the cache are not touched.

`status` says how many photos have their spots located, and how many flags the
data review holds.

Exit codes follow `experiments.cli`: 0 fine, 1 problems, 2 unreadable.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import sys
from argparse import Namespace
from pathlib import Path

from . import catalog, flags as flagfile

EXIT_OK, EXIT_ISSUES, EXIT_UNREADABLE = 0, 1, 2


def _load(path: Path):
    """(experiment, its photos, layout), or raise SystemExit with a message."""
    from experiments import run as bridge
    from experiments import schema

    from . import spots

    try:
        e = schema.load(path)
    except schema.ExperimentError as exc:
        print(f"! {exc}")
        raise SystemExit(EXIT_UNREADABLE)
    try:
        res = bridge.prepare(e)
    except bridge.RunError as exc:
        print(f"! {exc}")
        raise SystemExit(EXIT_UNREADABLE)
    template = bridge.load_template(e)
    if template is None:
        print("! no plate template is bound, so the grid to look for is unknown")
        raise SystemExit(EXIT_UNREADABLE)
    try:
        layout = spots.layout_for(e, template)
    except bridge.RunError as exc:
        print(f"! {exc}")
        raise SystemExit(EXIT_UNREADABLE)
    return e, catalog.photos(e, res), layout


def cmd_status(args) -> int:
    from . import spots

    path = Path(args.file)
    e, photos, layout = _load(path)
    done = sum(1 for p in photos if spots.is_detected(p.path, layout))
    print(f"{e.name}: spots located on {done} of {len(photos)} photo(s)")
    review = flagfile.load_for_experiment(path)
    print(f"  data review: {review.n_plates} plate(s) and {review.n_spots} "
          f"spot(s) flagged; {sum(review.is_reviewed(p.relpath) for p in photos)} "
          f"photo(s) looked at")
    return EXIT_OK


def cmd_detect(args) -> int:
    from experiments import run as bridge

    from . import spots

    path = Path(args.file)
    e, photos, layout = _load(path)
    sb, _ = spots._engine()
    import spotting_estimate as est
    import spotting_timecourse as tc

    print(f"{e.name}: locating spots for the data review")
    todo = [p for p in photos if not spots.is_detected(p.path, layout)]
    missing = [p for p in todo if not p.path.exists()]
    todo = [p for p in todo if p.path.exists()]
    for p in missing[:10]:
        print(f"  ! not found: {p.relpath}")
    print(f"  {len(photos)} photo(s); {len(photos) - len(todo) - len(missing)} "
          f"already located, {len(todo)} to do")
    workers = args.workers or max(1, min(8, (multiprocessing.cpu_count() or 2) // 2))
    if todo:
        # Every photo to do is uncached, and they are dealt out round-robin, so
        # the busiest worker gets the rounded-up share.
        load = len(todo) if workers <= 1 else -(-len(todo) // workers)
        per_photo, _ = est.measure_s(workers)
        print(f"  at ~{per_photo:.0f} s a photo, about "
              f"{est.format_minutes(load * per_photo)} on {workers} worker(s)")
        stubs = sum(1 for p in todo if p.cloud_only)
        if stubs:
            print(f"  {stubs} are cloud-only; each is copied locally while it is "
                  f"measured, then the copy is deleted")
    if args.estimate or not todo:
        if not todo:
            print("  Every photo already has its spots located.")
        return EXIT_ISSUES if missing else EXIT_OK

    # The pipeline's own job builder, fed one-photo "candidates": a job per
    # photo, every dilution level of its plate measured in the one pass.
    cache = spots.cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    shots = [{"plates": (tc.Shot(path=p.path, tp_hours=p.timepoint or 0.0,
                                 tp_label=p.when, medium=p.condition,
                                 medium_label=p.condition_label, plate=p.plate),)}
             for p in todo]
    jobs = tc.build_jobs(shots, cache, None if layout.is_classic() else layout)
    run_args = Namespace(workers=workers)
    bridge._configure_timecourse_workers(tc, run_args)
    print(f"\n  Locating spots on {run_args.workers} worker(s) ...")
    errors, _ = tc.measure_all(jobs, cache, run_args.workers)
    for where, error in errors[:10]:
        print(f"  ! {Path(where).name}: {error}")
    if len(errors) > 10:
        print(f"  ! ... and {len(errors) - 10} more")

    done = sum(1 for p in photos if spots.is_detected(p.path, layout))
    print(f"\n  Spots located on {done} of {len(photos)} photo(s).")
    return EXIT_ISSUES if (errors or missing) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data_review",
                                     description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    detect = sub.add_parser("detect", help="locate the spots on every photo")
    detect.add_argument("file", help="an experiment .spotexp.json")
    detect.add_argument("--workers", type=int, default=None)
    detect.add_argument("--estimate", action="store_true",
                        help="say how much work it would be, then stop")
    detect.set_defaults(func=cmd_detect)
    status = sub.add_parser("status", help="how far the data review has got")
    status.add_argument("file", help="an experiment .spotexp.json")
    status.set_defaults(func=cmd_status)
    return parser


#: Started from the data review in a console window of its own, the job is told
#: a file to write its exit code into the moment it ends: the console then waits
#: for Enter, so the process ending is not when the work finished.
STATUS_ENV = "DATA_REVIEW_JOB_STATUS"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    status_file = os.environ.pop(STATUS_ENV, "")
    code = EXIT_ISSUES
    try:
        code = int(args.func(args) or 0)
    except SystemExit as exc:
        code = int(exc.code or 0)
    finally:
        if status_file:
            try:
                Path(status_file).write_text(str(code), encoding="utf-8")
            except OSError:
                pass
    return code


if __name__ == "__main__":
    sys.exit(main())
