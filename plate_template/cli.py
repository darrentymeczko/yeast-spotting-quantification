"""Command-line checker for plate templates.

    py -m plate_template.cli check <file> [<file> ...]

Deliberately GUI-free, so a hand-written or hand-edited template can be checked
on a machine with no display -- and so the format has a checker even when the
editor is unavailable.

Exit codes: 0 clean, 1 validation errors, 2 the file could not be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .schema import TemplateError, load
from .validate import Severity, validate

_LABEL = {
    Severity.ERROR: "ERROR",
    Severity.WARNING: "warn ",
    Severity.INFO: "info ",
}


def _report(path: Path, quiet: bool) -> int:
    try:
        template = load(path)
    except TemplateError as exc:
        # load() already names the file in its message.
        print(exc, file=sys.stderr)
        return 2

    issues = validate(template)
    errors = [i for i in issues if i.severity is Severity.ERROR]
    warnings = [i for i in issues if i.severity is Severity.WARNING]

    print(f"{path}  ({template.name})")
    for issue in issues:
        if quiet and issue.severity is not Severity.ERROR:
            continue
        where = f"[plate {issue.plate_id}] " if issue.plate_id else ""
        print(f"  {_LABEL[issue.severity]} {where}{issue.code}: {issue.message}")

    verdict = "OK" if not errors else "NOT ANALYSABLE"
    print(f"  -> {verdict}: {len(errors)} error(s), {len(warnings)} warning(s)\n")
    return 1 if errors else 0


def cmd_check(args: argparse.Namespace) -> int:
    worst = 0
    for raw in args.files:
        worst = max(worst, _report(Path(raw), args.quiet))
    return worst


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plate_template.cli",
        description="Check spotting-assay plate templates.",
    )
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="validate one or more template files")
    check.add_argument("files", nargs="+", help="template .json files")
    check.add_argument(
        "-q", "--quiet", action="store_true", help="show errors only"
    )
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
