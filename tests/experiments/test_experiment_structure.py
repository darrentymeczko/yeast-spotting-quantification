"""Structural guardrails, mirroring tests/plate_template/test_no_gui_imports.py.

The experiment layer sits between a headless designer and a heavyweight
pipeline, so both properties matter here:

  1. the core is headless -- no tkinter outside the GUI modules
  2. only the bridge may import the pipeline, so authoring an experiment never
     costs a numpy import
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "experiments"

#: Modules allowed to import tkinter.
GUI_MODULES = {"app.py"}

#: Modules allowed to import the measurement pipeline. `run.py` is the bridge
#: -- converting a resolved experiment into the shapes `src/` already consumes
#: is its entire job. `cli.py` may reach it to run one.
BRIDGE_MODULES = {"run.py", "cli.py"}

PIPELINE_NAMES = ("spotting_quant", "spotting_batch", "spotting_timecourse",
                  "spotting_montage", "spotting_pptx", "spotting_plots")


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


@pytest.mark.parametrize(
    "path",
    [p for p in all_modules() if p.name not in BRIDGE_MODULES],
    ids=lambda p: p.name,
)
def test_only_the_bridge_imports_the_pipeline(path):
    offenders = {n for n in _imported_names(path) if n.split(".")[0] in PIPELINE_NAMES}
    assert not offenders, (
        f"{path.name} imports {sorted(offenders)}; only {sorted(BRIDGE_MODULES)} "
        f"may reach the pipeline, so designing an experiment costs no numpy import"
    )


@pytest.mark.parametrize("name", sorted({"model.py", "schema.py", "validate.py",
                                         "profiles.py", "intake.py"}))
def test_core_modules_import_cleanly(name):
    """The headless core must import with nothing but the standard library."""
    import importlib

    module = importlib.import_module(f"experiments.{name[:-3]}")
    assert module is not None
