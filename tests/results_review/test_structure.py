"""Structural guardrails, in the spirit of tests/plate_template.

Three properties this package is supposed to have, each easy to state and easy
to erode silently:

  1. the core is headless -- no tkinter outside `gui/` and `app.py`
  2. browsing does not need the measurement stack -- the engine and pandas are
     imported inside functions, never at module import time
  3. nothing here writes to the pipeline's own output
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "results_review"

#: Allowed to import tkinter. Everything else must stay importable with no
#: display attached, so it can be unit tested and driven from the CLI.
GUI_MODULES = {"app.py", "__main__.py"}

#: The measurement engine, plus the heavy science stack it drags in. These may
#: be imported INSIDE a function -- `rebuild` and `export` genuinely need them --
#: but never at module level, or opening the browser would require all of it.
HEAVY = {"spotting_quant", "spotting_batch", "spotting_timecourse",
         "spotting_timecourse_figures", "spotting_montage", "spotting_pptx",
         "numpy", "pandas", "scipy", "skimage", "matplotlib", "tifffile"}

#: Modules that may import the heavy stack at all (inside functions).
MAY_USE_ENGINE = {"rebuild.py", "export.py", "cli.py", "controller.py"}


def modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def gui_free_modules() -> list[Path]:
    return sorted(p for p in PACKAGE.glob("*.py") if p.name not in GUI_MODULES)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _names(node) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def _module_level_names(path: Path) -> set[str]:
    """Imports that run when the module is imported, not ones inside a def."""
    tree = _tree(path)
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= _names(node)
        elif isinstance(node, (ast.If, ast.Try)):
            # `try: import PIL` / `if TYPE_CHECKING:` still run at import time.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    out |= _names(sub)
    return out


def test_the_package_exists():
    assert PACKAGE.is_dir()
    assert gui_free_modules(), "no core modules found -- has the package moved?"


@pytest.mark.parametrize("path", gui_free_modules(), ids=lambda p: p.name)
def test_core_modules_are_headless(path):
    bad = {n for n in _names(_tree(path)) if n.split(".")[0] == "tkinter"}
    assert not bad, (
        f"{path.name} imports {sorted(bad)}; core logic must stay headless so "
        f"it can be unit tested and run from the CLI without a display")


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_the_heavy_stack_is_never_imported_at_module_level(path):
    bad = {n for n in _module_level_names(path) if n.split(".")[0] in HEAVY}
    assert not bad, (
        f"{path.name} imports {sorted(bad)} at module level. Browsing results "
        f"must not require the measurement stack -- import it inside the "
        f"function that needs it")


@pytest.mark.parametrize("path", modules(), ids=lambda p: p.name)
def test_only_the_rebuild_layer_touches_the_engine(path):
    if path.name in MAY_USE_ENGINE:
        return
    bad = {n for n in _names(_tree(path))
           if n.split(".")[0].startswith("spotting_")}
    assert not bad, (
        f"{path.name} imports {sorted(bad)}; the engine belongs behind "
        f"rebuild.py and export.py")


def test_nothing_writes_to_the_pipelines_own_output():
    """`best/` is the pipeline's. This tool reads it and never writes it.

    Checked as a text property because it is a promise made in the README and
    in the export dialog, and a stray `best_dir / ...` in a write path would
    break it without failing anything else.
    """
    offenders = []
    for path in modules():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "best_dir" not in line:
                continue
            if any(w in line for w in ("write", "mkdir", "to_csv", "savefig",
                                       "open(", "unlink", "rmtree")):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "these lines look like writes into best/:\n" + \
        "\n".join(offenders)


def test_the_launcher_exists_and_runs_this_package():
    bat = ROOT / "run_review.bat"
    assert bat.exists(), "run_review.bat is how this is actually started"
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "results_review.app" in text
    assert "results_review.cli" in text
