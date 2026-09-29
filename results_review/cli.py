"""Apply a saved review without opening the window.

    py -m results_review.cli "Results/Timecourse/Set01" [--no-figures]

Two uses. Re-exporting every set after the pipeline is re-run, without sitting
through fifteen windows; and checking, in a terminal or a test, that a review
still produces what it produced -- `--dry-run` rebuilds and reports without
writing anything.

A set with no `review.json` exports the pipeline's own picks, which makes this a
plain "write best/ into chosen/ shape" for sets nobody has reviewed yet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import discovery, links, review as rv
from .model import Review
from .rebuild import RebuildError, rebuild, require_engine


def _report(run, review: Review, frames, problems) -> None:
    print(f"\n  {run.label}")
    for f in frames:
        medium = f.medium
        c = f.candidate
        mark = "pipeline pick" if rv.is_default(run, review, medium) else "CHOSEN"
        print(f"    {medium:>7}  {c.timepoint:>10}  {c.dilution:>6}  [{mark}]"
              f"   {c.detail}")
        if f.n_edited:
            print(f"             {f.n_edited} corrected spot(s)")
        for m in f.messages:
            print(f"             ! {m}")
    for p in problems:
        print(f"    ! {p}")


def run_one(results_dir: Path, *, figures: bool = True, montages: bool = True,
            dry_run: bool = False) -> int:
    run = discovery.load_set(results_dir)
    review = rv.with_defaults(run, rv.load_for(run))

    root = links.resolve(run.label, review.capture_root, run.results_dir)
    if root is None:
        print(f"  ! {run.label}: the capture folder is not linked and could "
              f"not be found. Open it once in the window, or pass --photos.",
              file=sys.stderr)
        return 1
    try:
        cfg = links.load_config(root)
    except Exception as e:
        print(f"  ! {run.label}: {e}", file=sys.stderr)
        return 1

    frames, problems = [], []
    for medium in run.media:
        cand = rv.chosen_candidate(run, review, medium)
        if cand is None:
            problems.append(f"{medium}: the recorded pick is not in this run")
            continue
        try:
            frames.append(rebuild(root, run.label, cfg, cand,
                                  review.edits_for(medium)))
        except RebuildError as e:
            problems.append(f"{medium}: {e}")

    _report(run, review, frames, problems)
    if not frames:
        return 1
    if dry_run:
        print("    (dry run -- nothing written)")
        return 0

    from .export import export
    result = export(run, review, frames, root, cfg, figures=figures,
                    montages=montages,
                    progress=lambda m: print(f"    {m}"))
    for w in result.warnings:
        print(f"    ! {w}")
    if result.ok:
        print(f"    wrote {result.csv_path.parent}")
    return 0 if result.ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="results_review.cli",
        description="Apply a saved review and write chosen/ without the GUI.")
    ap.add_argument("results", nargs="*", type=Path,
                    help="Results/Timecourse/<set> folders; all of them if none "
                         "is given")
    ap.add_argument("--photos", type=Path,
                    help="capture tree to use (only meaningful for ONE set)")
    ap.add_argument("--no-figures", action="store_true",
                    help="skip PyPrism figures; write the CSVs only")
    ap.add_argument("--no-montages", action="store_true",
                    help="skip the spot montages (each processes two plate images)")
    ap.add_argument("--dry-run", action="store_true",
                    help="rebuild and report, write nothing")
    args = ap.parse_args(argv)

    # Checked once, up front. This command only ever rebuilds, so finding out
    # per set that it cannot would just repeat the same message N times.
    try:
        require_engine()
    except RebuildError as e:
        print(f"\n  {e}\n", file=sys.stderr)
        return 1

    targets = args.results or discovery.list_sets()
    if not targets:
        print(f"  No timecourse results under {discovery.TIMECOURSE_RESULTS}.",
              file=sys.stderr)
        return 1
    if args.photos is not None:
        if len(targets) != 1:
            print("  --photos applies to a single set; name one.",
                  file=sys.stderr)
            return 1
        links.remember(discovery.load_set(targets[0]).label, args.photos)

    bad = 0
    for target in targets:
        try:
            bad += run_one(target, figures=not args.no_figures,
                           montages=not args.no_montages, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ! {target}: {type(e).__name__}: {e}", file=sys.stderr)
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
