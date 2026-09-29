"""Structural guardrails.

Two properties this tool is supposed to have are easy to state and easy to
erode silently, so they get asserted rather than remembered:

  1. the core is headless -- no tkinter outside the GUI modules
  2. the designer is decoupled from the measurement pipeline
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "plate_template"

#: Modules allowed to import tkinter. Everything else in the package top level
#: is core logic and must stay importable with no display attached.
GUI_MODULES = {"app.py"}

PIPELINE_NAMES = ("spotting_quant", "spotting_batch", "spotting_timecourse",
                  "spotting_montage", "spotting_pptx")


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def core_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.glob("*.py") if p.name not in GUI_MODULES)


def all_modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_the_package_exists():
    assert PACKAGE.is_dir()
    assert core_modules(), "no core modules found -- has the package moved?"


@pytest.mark.parametrize("path", core_modules(), ids=lambda p: p.name)
def test_core_modules_are_headless(path):
    offenders = {n for n in _imported_names(path) if n.split(".")[0] == "tkinter"}
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}; core logic must stay headless "
        f"so it can be unit tested without a display"
    )


@pytest.mark.parametrize("path", all_modules(), ids=lambda p: p.name)
def test_nothing_imports_the_pipeline(path):
    offenders = {
        n for n in _imported_names(path)
        if n.split(".")[0] in PIPELINE_NAMES
    }
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}; the designer is deliberately "
        f"decoupled from the measurement pipeline in this phase"
    )
