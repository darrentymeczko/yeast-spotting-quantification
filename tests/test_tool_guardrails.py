"""Guardrails that keep the tools able to share one window.

The plate designer, the experiment designer and the review window each run on
their own, and also side by side in one process. Two kinds of thing break
that, silently:

  * Process-wide state. A Tk interpreter has ONE style database, so a tool
    that configures a bare stock style ("Treeview", "TNotebook.Tab") restyles
    every other tool's widgets too -- whichever was built last wins. The shared
    look belongs to `uikit.theme`; a tool that wants something different names
    its own style ("Plates.TNotebook.Tab"). Likewise `bind_all`: a shortcut
    bound that way fires in every tool, so Ctrl+Z in one undoes another.

  * The window itself. Hosted as a tab, a tool's `self.root` is the
    workbench's window. A tool that retitles it, swaps its menubar, takes its
    close button or destroys it has hijacked the whole program. All of that
    goes through `self.host` (`uikit.host`).

Checked as text, because the failure is silent: nothing breaks, the other
tool just quietly looks or behaves wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("plate_template", "experiments", "data_review", "results_review")

#: `.configure("Treeview"` or `.map("TNotebook.Tab"` -- a style name with no
#: prefix, i.e. starting with the stock class name itself.
_BARE_STYLE = re.compile(
    r"\.(configure|map|layout)\(\s*['\"]"
    r"(T[A-Z]\w*|Treeview|Heading|Toolbutton|Sash)(\.[\w.]+)?['\"]")


def _sources():
    for tool in TOOLS:
        yield from sorted((ROOT / tool).rglob("*.py"))


@pytest.mark.parametrize("path", list(_sources()),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_tool_restyles_a_stock_widget_class(path):
    offenders = [
        f"{path.name}:{i}: {line.strip()}"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _BARE_STYLE.search(line)
    ]
    assert not offenders, (
        "configure a prefixed style instead (e.g. 'Plates.TNotebook.Tab'); a "
        "bare one changes every tool in the window:\n" + "\n".join(offenders))


#: Things only a host may do to the window.
_WINDOW_TAKEOVER = re.compile(
    r"\.bind_all\("
    r"|self\.root\.(title|protocol|destroy|geometry|minsize)\("
    r"|self\.root\.config(ure)?\(\s*menu="
    r"|report_callback_exception\s*=|def report_callback_exception")


@pytest.mark.parametrize("path", list(_sources()),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_tool_takes_over_the_window(path):
    offenders = [
        f"{path.name}:{i}: {line.strip()}"
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _WINDOW_TAKEOVER.search(line)
    ]
    assert not offenders, (
        "go through self.host (uikit.host) instead -- hosted as a tab, the "
        "window belongs to the whole program:\n" + "\n".join(offenders))


@pytest.mark.parametrize("path", list(_sources()),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_tool_depends_on_the_program_around_it(path):
    """The workbench imports the tools, never the other way round, so each tool
    still runs on its own."""
    text = path.read_text(encoding="utf-8")
    bad = re.findall(r"^\s*(?:from|import)\s+(workbench|spotting_app)\b",
                     text, flags=re.MULTILINE)
    assert not bad, f"{path.name} imports {sorted(set(bad))}"
