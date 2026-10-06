"""Structural guardrails, in the spirit of tests/results_review/test_structure.py.

  1. `flags.py` is the contract other tools read, so it imports nothing but the
     standard library -- the run and the results review can read flags on a
     machine without the measurement stack, and without pulling in a window
  2. the core is headless: no tkinter outside the window and its widgets
  3. the heavy stack is only imported inside functions, so the window opens
     before numpy has finished loading
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "data_review"

GUI_MODULES = {"app.py", "__main__.py"}
HEAVY = {"spotting_quant", "spotting_batch", "spotting_timecourse", "numpy",
         "pandas", "scipy", "skimage", "matplotlib", "tifffile"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _names(node) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            out.add("." * n.level + (n.module or ""))
    return out


def _module_level(path: Path) -> set[str]:
    out: set[str] = set()
    for node in _tree(path).body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.If, ast.Try)):
            out |= _names(node)
    return out


def test_the_flags_contract_is_standard_library_only():
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    bad = {n for n in _names(_tree(PACKAGE / "flags.py"))
           if n.split(".")[0] not in stdlib}
    assert not bad, f"flags.py imports {sorted(bad)}"


@pytest.mark.parametrize("path", sorted(p for p in PACKAGE.glob("*.py")
                                        if p.name not in GUI_MODULES),
                         ids=lambda p: p.name)
def test_core_modules_are_headless(path):
    bad = {n for n in _names(_tree(path)) if n.split(".")[0] == "tkinter"}
    assert not bad, f"{path.name} imports {sorted(bad)}"


@pytest.mark.parametrize("path", sorted(PACKAGE.rglob("*.py")),
                         ids=lambda p: str(p.relative_to(PACKAGE)))
def test_the_heavy_stack_is_never_imported_at_module_level(path):
    bad = {n for n in _module_level(path) if n.split(".")[0] in HEAVY}
    assert not bad, f"{path.name} imports {sorted(bad)} at module level"


def test_the_launcher_exists_and_runs_this_package():
    bat = ROOT / "run_data_review.bat"
    assert bat.exists()
    text = bat.read_bytes()
    assert b"data_review.app" in text
    assert b"\r\n" in text, "cmd.exe mis-parses a .bat without CRLF line endings"
