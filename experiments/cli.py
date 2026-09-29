"""Headless entry point for the experiment layer.

    py -m experiments.cli scan    <photo folder> [--mode timecourse|quantify]
    py -m experiments.cli check   <experiment.json>...
    py -m experiments.cli migrate <spotting_config.json | capture tree>...

`scan` is the one to reach for when a lab's photos are not being read properly:
it prints what the layout was understood to mean and what each photo resolved
to, which is the same reading the GUI shows in its intake table.

Exit codes follow `plate_template.cli`: 0 clean, 1 problems found, 2 unreadable.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import BUNDLE, REPO, intake, migrate, schema, validate
from .model import MODES, TIMECOURSE, Experiment

EXIT_OK, EXIT_ISSUES, EXIT_UNREADABLE = 0, 1, 2

#: Where experiment files are written, the sibling of `Plate Templates/`.
#: Deliberately NOT "Experiments": Windows paths are case-insensitive, so that
#: folder would be the `experiments/` package itself and the files would land
#: inside the source tree.
DEFAULT_OUT = REPO / "Experiment Designs"

#: What a migrated experiment is bound to unless told otherwise.
#: `plate_template.presets.lab_standard_8x6` reproduces `spotting_batch`'s
#: hardcoded row/column arithmetic exactly -- slot = column, replicate =
#: (plate-1)*2 + 1 + row//3, dilution = row % 3 -- so binding migrated work to
#: it is exact rather than approximate.
DEFAULT_TEMPLATE = "plate_template/templates/lab_standard_8x6.json"

_MARK = {"error": "E", "warning": "W", "info": "-"}


def _print_issues(issues) -> int:
    for issue in issues:
        where = f" [{issue.condition}]" if getattr(issue, "condition", None) else ""
        print(f"  {_MARK[issue.severity.value]}{where} {issue.message}")
    return sum(1 for i in issues if i.is_error)


def _load_template(e: Experiment, base: Path):
    """The bound plate template, or None. Never fatal -- validation reports it."""
    if not e.template_path:
        return None
    from plate_template import schema as tschema

    for candidate in (Path(e.template_path), base / e.template_path):
        if candidate.exists():
            try:
                return tschema.load(candidate)
            except tschema.TemplateError as exc:
                print(f"  ! template: {exc}")
                return None
    return None


# ---------------------------------------------------------------------------


def cmd_scan(args) -> int:
    root = Path(args.folder)
    files, complaints = intake.scan_images(root)
    for c in complaints:
        print(f"  ! {c}")
    if not files:
        print(f"{root}: no images found")
        return EXIT_UNREADABLE

    profile, why = intake.infer_profile(files, mode=args.mode)
    print(f"{root}")
    print(f"  {len(files)} images")
    for line in why:
        print(f"  * {line}")

    e = Experiment(name=root.name, mode=args.mode, photo_root=str(root), profile=profile)
    res = intake.resolve(e, files)
    ok, unresolved = res.usable(), res.unresolved()
    print(f"  resolved {len(ok)} of {len(res.rows)}")
    print(f"  conditions: {', '.join(res.conditions()) or '(none)'}")
    if args.mode == TIMECOURSE:
        tps = sorted({r.timepoint for r in ok if r.timepoint is not None})
        print(f"  timepoints: {', '.join(f'{t:g}' for t in tps) or '(none)'}")
    print(f"  plates: {', '.join(str(p) for p in sorted({r.plate for r in ok})) or '(none)'}")

    if args.verbose:
        for row in res.rows:
            bits = [row.condition or "?", row.timepoint_label or "-",
                    f"plate {row.plate}" if row.plate else "plate ?", f"shot {row.shot}"]
            print(f"    {row.status:<10} {row.relpath}  ({', '.join(bits)})")
    elif unresolved:
        for row in unresolved[: args.limit]:
            print(f"    ? {row.relpath}  missing: {', '.join(row.missing)}")
        if len(unresolved) > args.limit:
            print(f"    ... and {len(unresolved) - args.limit} more")

    errors = _print_issues(intake.check_resolution(e, res))
    return EXIT_ISSUES if errors else EXIT_OK


def cmd_check(args) -> int:
    worst = EXIT_OK
    for name in args.files:
        path = Path(name)
        print(path)
        try:
            e = schema.load(path)
        except schema.ExperimentError as exc:
            print(f"  ! {exc}")
            worst = max(worst, EXIT_UNREADABLE)
            continue

        issues = list(validate.validate(e, _load_template(e, path.parent)))
        root = Path(e.photo_root)
        if root.is_dir():
            files, complaints = intake.scan_images(root)
            for c in complaints:
                print(f"  ! {c}")
            issues += intake.check_resolution(e, intake.resolve(e, files))
        elif e.photo_root:
            print(f"  ! photo folder not found: {root}")
            worst = max(worst, EXIT_ISSUES)

        if _print_issues(issues):
            worst = max(worst, EXIT_ISSUES)
    return worst


def _template_binding(arg: str | None) -> tuple[str, str, object]:
    """(id, stored path, loaded template) for the template to bind migrations to."""
    rel = arg or DEFAULT_TEMPLATE
    path = Path(rel) if Path(rel).is_absolute() else REPO / rel
    if not path.exists() and not Path(rel).is_absolute():
        path = BUNDLE / rel          # the default template ships inside the .exe
    if not path.exists():
        print(f"  ! plate template not found: {path}")
        return "", rel, None
    from plate_template import schema as tschema

    try:
        template = tschema.load(path)
    except tschema.TemplateError as exc:
        print(f"  ! plate template: {exc}")
        return "", rel, None
    return template.id, rel, template


def cmd_migrate(args) -> int:
    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    worst = EXIT_OK
    made: list[Experiment] = []
    template_id, template_path, template = _template_binding(args.template)
    binding = {"template_id": template_id, "template_path": template_path}

    for name in args.sources:
        source = Path(name)
        print(source)
        try:
            if source.is_file():
                experiments, notes = migrate.from_spotting_config(source, **binding)
            else:
                trees = migrate.find_capture_trees(source)
                if not trees:
                    print("  ! no timecourse_config.json here or one level below")
                    worst = max(worst, EXIT_UNREADABLE)
                    continue
                experiments, notes = [], []
                for tree in trees:
                    e, tree_notes = migrate.from_capture_tree(tree, **binding)
                    experiments.append(e)
                    notes += [f"{tree.name}: {n}" for n in tree_notes]
        except migrate.MigrationError as exc:
            print(f"  ! {exc}")
            worst = max(worst, EXIT_UNREADABLE)
            continue

        for note in notes:
            print(f"  * {note}")
        made += experiments

    if not made:
        return max(worst, EXIT_UNREADABLE)

    for e in made:
        path = out_dir / f"{e.name}.spotexp.json"
        if path.exists() and not args.force:
            print(f"  ! {path.name} already exists; pass --force to overwrite")
            worst = max(worst, EXIT_ISSUES)
            continue
        if args.dry_run:
            print(f"  would write {path}")
        else:
            schema.save(e, path, bump_revision=False)
            print(f"  wrote {path}")
        if _print_issues(validate.validate(e, template)):
            worst = max(worst, EXIT_ISSUES)
    return worst


def cmd_run(args) -> int:
    # Imported here, not at module load, so `scan` and `check` stay usable on a
    # machine with no numpy -- the same reason `results_review` keeps its
    # browsing layer free of pipeline imports.
    from . import run as runner

    path = Path(args.file)
    try:
        e = schema.load(path)
    except schema.ExperimentError as exc:
        print(f"! {exc}")
        return EXIT_UNREADABLE

    print(f"{e.name}  ({e.mode})")
    try:
        res = runner.prepare(e)
    except runner.RunError as exc:
        print(f"! {exc}")
        return EXIT_UNREADABLE

    template = _load_template(e, path.parent)
    issues = list(validate.validate(e, template))
    issues += intake.check_resolution(e, res)
    errors = _print_issues(issues)
    if errors and not args.force:
        print(f"  {errors} error(s); not running. Fix them, or pass --force.")
        return EXIT_ISSUES

    try:
        code = runner.run(
            e,
            outdir=Path(args.out) if args.out else None,
            estimate=args.estimate,
            workers=args.workers,
            res=res,
            template=template,
        )
    except runner.RunError as exc:
        print(f"! {exc}")
        return EXIT_UNREADABLE
    return EXIT_OK if code == 0 else EXIT_ISSUES


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="experiments", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="read a photo folder and say what is in it")
    scan.add_argument("folder")
    scan.add_argument("--mode", choices=MODES, default=TIMECOURSE)
    scan.add_argument("-v", "--verbose", action="store_true",
                      help="list every photo and what it resolved to")
    scan.add_argument("--limit", type=int, default=10,
                      help="how many unresolved photos to list (default 10)")
    scan.set_defaults(func=cmd_scan)

    check = sub.add_parser("check", help="validate experiment files")
    check.add_argument("files", nargs="+")
    check.set_defaults(func=cmd_check)

    mig = sub.add_parser("migrate", help="convert the pipelines' existing config files")
    mig.add_argument("sources", nargs="+",
                     help="a spotting_config.json, or a folder of capture trees")
    mig.add_argument("--out", help=f"output folder (default {DEFAULT_OUT.name}/)")
    mig.add_argument("--template", default=None,
                     help=f"plate template to bind to (default {DEFAULT_TEMPLATE})")
    mig.add_argument("--force", action="store_true", help="overwrite existing files")
    mig.add_argument("--dry-run", action="store_true", help="say what would be written")
    mig.set_defaults(func=cmd_migrate)

    run = sub.add_parser("run", help="quantify an experiment's photos")
    run.add_argument("file", help="an experiment .spotexp.json")
    run.add_argument("--out", help="results folder (default Results/...)")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--estimate", action="store_true",
                     help="say how much work it would be, then stop")
    run.add_argument("--force", action="store_true",
                     help="run even though validation found errors")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # A run launched by the source-tree GUI gets a dedicated console. Keep its
    # final output visible until acknowledged, including a traceback from an
    # unexpected startup failure. The packaged launcher already provides the
    # equivalent exception handling and pause for its own console.
    pause = os.environ.pop("EXPERIMENT_GUI_RUN_PAUSE", "") == "1"
    if not pause:
        return args.func(args)
    try:
        try:
            code = args.func(args)
        except Exception:
            import traceback

            traceback.print_exc()
            code = 1
    finally:
        try:
            input("\n  Press Enter to close this progress window ... ")
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
