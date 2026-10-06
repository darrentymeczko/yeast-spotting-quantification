"""Structural guardrails for the shared look, in the spirit of the tools' own.

`uikit` sits underneath every tool, so two things about it must hold:

  1. `tokens` (and the package itself) import no tkinter. The tools' headless
     cores read their colours from `tokens`, and they must stay importable
     with no display -- the plate designer's and review's structure tests
     check those cores, and an indirect tkinter import would slip past them.
  2. It depends on nothing above it. A tool may import uikit; uikit importing
     a tool would make the tools depend on each other through it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "uikit"

#: Must import without tkinter.
HEADLESS = ("__init__.py", "tokens.py", "dpi.py")

#: Nothing in uikit may import these.
ABOVE = ("plate_template", "experiments", "results_review", "workbench",
         "spotting_app")


def _imported(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_package_exists():
    assert PACKAGE.is_dir()
    for name in HEADLESS:
        assert (PACKAGE / name).is_file(), name


@pytest.mark.parametrize("name", HEADLESS)
def test_the_headless_modules_never_import_tkinter(name):
    bad = {n for n in _imported(PACKAGE / name) if n.split(".")[0] == "tkinter"}
    assert not bad, f"uikit/{name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_uikit_depends_on_nothing_above_it(path):
    bad = {n for n in _imported(path)
           if n.split(".")[0] in ABOVE or n.split(".")[0].startswith("spotting_")}
    assert not bad, f"uikit/{path.name} imports {sorted(bad)}"


def test_reading_the_tokens_really_leaves_tkinter_unloaded():
    """The AST check above, confirmed in a fresh interpreter: what the tools'
    headless theme modules pull in when they read a colour."""
    code = ("import sys; import uikit.tokens, uikit.dpi; "
            "import plate_template.theme, results_review.theme; "
            "print('tkinter' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
