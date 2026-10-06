"""The workbench: every tool in one window, as tabs.

What has to hold for tools to share a window without trampling each other:

  * the menubar is the ACTIVE document's -- its own menus swap in and out
  * a shortcut reaches the active document and no other
  * closing a tab asks that document first, and can be refused
  * one file, one tab
  * a closed tab leaves nothing running behind it
  * starting the window costs no numpy
"""

from __future__ import annotations

import json
import subprocess
import sys
import tkinter as tk
from pathlib import Path

import pytest

from workbench.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "plate_template" / "templates" / "lab_standard_8x6.json"

CANDIDATES = (
    "medium,medium_label,timepoint,hours,plate1,plate2,dilution,control_n,"
    "median_CV,control_mean,control_CV,n_strains,n_significant,rank_score,"
    "ranked_by,best_set_score\n"
    "GLU,Glucose,16 Hours,16.0,_9.JPG,_9_1.JPG,least,4,0.05,10.7,0.34,7,7,1.75,"
    "combined,4.72\n"
)


@pytest.fixture
def hermetic(tmp_path, monkeypatch):
    """Never read or write the person's own review links."""
    from results_review import links

    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    monkeypatch.setattr(links, "SEARCH_HINTS", [])
    return tmp_path


@pytest.fixture
def shell(tk_root, hermetic):
    from workbench.shell import Shell

    window = tk.Toplevel(tk_root)
    window.withdraw()
    sh = Shell(window, Settings(hermetic / "workbench.json"), restore=False)
    yield sh
    try:
        sh.exit(force=True)
    except tk.TclError:
        pass


@pytest.fixture
def result_set(hermetic, monkeypatch):
    from results_review import discovery

    monkeypatch.setattr(discovery, "TIMECOURSE_RESULTS", hermetic)
    folder = hermetic / "ZZ Workbench Test"
    folder.mkdir()
    (folder / discovery.CANDIDATES_CSV).write_text(CANDIDATES, encoding="utf-8")
    return folder


def menus(shell) -> list[str]:
    mb = shell.menubar
    end = mb.index("end")
    return [] if end is None else [mb.entrycget(i, "label")
                                   for i in range(end + 1)]


# --- tabs and menus -------------------------------------------------------------


def test_home_shows_the_programs_own_menus(shell):
    assert shell.active is None
    assert menus(shell) == ["File", "Window", "Help"]


def test_explorer_starts_closed_and_refresh_preserves_expansion(shell):
    tree = shell.explorer.tree
    assert all(not tree.item(i, "open") for i in tree.get_children())
    first = tree.get_children()[0]
    tree.item(first, open=True)
    shell.explorer.refresh()
    assert tree.item(first, "open")
    assert all(not tree.item(i, "open") for i in tree.get_children()[1:])


def test_home_is_one_click_from_every_part_of_the_tab(shell):
    from workbench.shell import HOME
    doc = shell.open_document("experiment")
    shell.root.deiconify()
    shell.root.update()
    tab = shell.tabs._tabs[id(HOME)]
    for widget in tab._widgets():
        shell.activate(doc)
        shell.root.update()
        widget.event_generate("<ButtonPress-1>", x=2, y=1)
        widget.event_generate("<ButtonRelease-1>", x=2, y=1)
        shell.root.update()
        assert shell.active is None, str(widget)
        assert shell.home.winfo_ismapped()
        assert not doc.frame.winfo_ismapped()


def test_only_one_top_menu_row(shell):
    assert not hasattr(shell, "commandbar")
    shell.open_document("experiment")
    assert menus(shell).count("Help") == 1


def test_review_keeps_save_and_undo_without_the_duplicate_toolbar(shell, result_set):
    doc = shell.open_document("review", result_set)
    assert "Edit" in menus(shell)
    calls = []
    doc.commands.update(save=lambda: calls.append("save"), undo=lambda: calls.append("undo"))
    shell.file_menu.invoke("Save")
    shell.edit_menu.invoke("Undo")
    assert calls == ["save", "undo"]


def test_review_recompute_stays_visible_after_edits(shell, result_set, monkeypatch):
    from results_review.app import ReviewApp

    calls = []
    monkeypatch.setattr(ReviewApp, "_redraw_graph", lambda self: calls.append(self))
    doc = shell.open_document("review", result_set)
    app = doc.app
    shell.root.geometry("1200x800")
    shell.root.deiconify()
    app._stale_media.add(app.ctl.medium)
    app._refresh()
    shell.root.update()

    button = app.btn_redraw
    assert "Recompute data" in button.cget("text")
    assert "out of date" in button.cget("text")
    assert button.winfo_ismapped()
    assert button.winfo_rootx() >= app.winfo_rootx()
    assert (button.winfo_rootx() + button.winfo_width()
            <= app.winfo_rootx() + app.winfo_width())
    assert (button.winfo_rooty() + button.winfo_height()
            <= app.stats.winfo_rooty())
    button.invoke()
    assert calls == [app]
    app._set_enabled(False)
    button.invoke()
    assert calls == [app]


def test_each_kind_of_document_opens_in_a_tab(shell, result_set):
    plate = shell.open_document("plate")
    experiment = shell.open_document("experiment")
    review = shell.open_document("review", result_set)
    assert None not in (plate, experiment, review)
    assert shell.documents == [plate, experiment, review]
    assert shell.active is review
    assert review.tab_label == "ZZ Workbench Test"


def test_the_menubar_follows_the_active_tab(shell):
    plate = shell.open_document("plate")
    assert menus(shell) == ["File", "Edit", "View", "Window", "Help"]
    shell.open_document("experiment")
    assert menus(shell) == ["File", "Edit", "Window", "Help"]
    shell.activate(plate)
    assert menus(shell) == ["File", "Edit", "View", "Window", "Help"]
    shell.activate(None)
    assert menus(shell) == ["File", "Window", "Help"]


def test_a_documents_file_menu_ends_with_close_tab_and_exit(shell):
    doc = shell.open_document("plate")
    file_menu = shell.menubar.nametowidget(shell.menubar.entrycget(0, "menu"))
    labels = [file_menu.entrycget(i, "label")
              for i in range(file_menu.index("end") + 1)
              if file_menu.type(i) == "command"]
    assert labels[-2:] == ["Close tab", "Exit Spotting Quantification"]
    assert doc.close_label == "Close tab"


# --- shortcuts ------------------------------------------------------------------


def test_a_shortcut_reaches_the_active_document_only(shell, tk_root):
    a = shell.open_document("plate")
    b = shell.open_document("plate")
    calls = []
    a.keys["<Control-z>"] = lambda _e: calls.append("a") or "break"
    b.keys["<Control-z>"] = lambda _e: calls.append("b") or "break"

    shell.activate(a)
    assert shell._dispatch("<Control-z>", None) == "break"
    shell.activate(b)
    shell._dispatch("<Control-z>", None)
    shell.activate(None)
    assert shell._dispatch("<Control-z>", None) is None
    assert calls == ["a", "b"]


def test_shortcuts_are_bound_to_the_window_not_the_process(shell, tk_root):
    shell.open_document("plate")
    assert shell.root.bind("<Control-z>")
    assert not tk_root.bind_all("<Control-z>")


# --- opening and closing --------------------------------------------------------


def test_the_same_file_is_opened_once(shell):
    first = shell.open_document("plate", TEMPLATE)
    shell.activate(None)
    again = shell.open_document("plate", TEMPLATE)
    assert again is first
    assert len(shell.documents) == 1
    assert shell.active is first


def test_a_document_can_refuse_to_close(shell, monkeypatch):
    from plate_template import app as plate_app

    doc = shell.open_document("plate")
    doc.app.controller.set_name("changed")
    assert doc.dirty
    monkeypatch.setattr(plate_app.messagebox, "askyesnocancel",
                        lambda *a, **k: None)              # Cancel
    assert shell.close_document(doc) is False
    assert doc in shell.documents


def test_closing_a_tab_shows_the_next_one(shell):
    a = shell.open_document("plate")
    b = shell.open_document("experiment")
    assert shell.close_document(b) is True
    assert shell.documents == [a]
    assert shell.active is a
    assert not b.frame.winfo_exists()


def test_reordered_tabs_drive_navigation_menus_closing_and_session(shell, hermetic):
    from workbench.shell import HOME

    paths = [hermetic / f"plate-{i}.json" for i in range(3)]
    for path in paths:
        path.write_bytes(TEMPLATE.read_bytes())
    a, b, c = [shell.open_document("plate", path) for path in paths]
    shell.activate(b)
    assert shell.tabs.move(c, 1)
    assert shell.tabs.keys() == [HOME, c, a, b]
    assert shell.documents == [c, a, b]
    assert shell.active is b
    assert [path for _, path in shell.settings.session()] == [paths[2], paths[0], paths[1]]
    saved = json.loads(shell.settings.path.read_text(encoding="utf-8"))
    assert [item["path"] for item in saved["session"]] == [str(paths[i]) for i in (2, 0, 1)]
    menu = shell.window_menu
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) == "radiobutton"]
    assert labels == ["Home", c.tab_label, a.tab_label, b.tab_label]
    shell.step_tab(-1)
    assert shell.active is a
    shell.step_tab(-1)
    assert shell.active is c
    assert shell.close_document(c)
    assert shell.active is a
    assert shell.documents == [a, b]


def test_a_closed_review_tab_leaves_nothing_running(shell, result_set):
    doc = shell.open_document("review", result_set)
    pump = doc.app._pump_id
    pending = set(shell.root.tk.splitlist(shell.root.tk.call("after", "info")))
    assert pump in pending
    assert shell.close_document(doc) is True
    pending = set(shell.root.tk.splitlist(shell.root.tk.call("after", "info")))
    assert pump not in pending


def test_exit_asks_every_document_and_can_be_refused(shell, monkeypatch):
    from plate_template import app as plate_app

    doc = shell.open_document("plate")
    doc.app.controller.set_name("changed")
    monkeypatch.setattr(plate_app.messagebox, "askyesnocancel",
                        lambda *a, **k: None)
    assert shell.exit() is False
    assert shell.root.winfo_exists()


def test_a_document_that_cannot_open_leaves_no_tab(shell, tmp_path, monkeypatch):
    from workbench import shell as shell_module

    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    shown = []
    monkeypatch.setattr(shell_module.messagebox, "showerror",
                        lambda *a, **k: shown.append(a))
    monkeypatch.setattr(shell_module, "ERROR_LOG", tmp_path / "error.log")
    assert shell.open_document("plate", bad) is None
    assert shell.documents == [] and shown


# --- remembering ----------------------------------------------------------------


def test_an_opened_file_is_remembered(shell):
    shell.open_document("plate", TEMPLATE)
    assert [p.resolve() for p, _ in shell.settings.recent("plate")] == [
        TEMPLATE.resolve()]
    saved = json.loads(shell.settings.path.read_text(encoding="utf-8"))
    assert saved["session"] == [{"kind": "plate", "path": str(TEMPLATE)}]


def test_settings_survive_a_corrupt_file(tmp_path):
    path = tmp_path / "workbench.json"
    path.write_text("{not json", encoding="utf-8")
    settings = Settings(path)
    assert settings.session() == [] and settings.recent("plate") == []
    settings.set("show_home", False)
    settings.save()
    assert Settings(path).get("show_home") is False


def test_a_read_only_settings_file_is_never_written(tmp_path):
    path = tmp_path / "workbench.json"
    settings = Settings(path, read_only=True)
    settings.set("show_home", False)
    settings.save()
    assert not path.exists()


# --- cost of starting -----------------------------------------------------------


def test_starting_the_window_imports_no_science_stack():
    code = ("import sys; import workbench.app, workbench.shell; "
            "heavy = [m for m in ('numpy', 'pandas', 'matplotlib', 'skimage', "
            "'spotting_timecourse', 'spotting_batch') if m in sys.modules]; "
            "print(heavy)")
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"


def test_the_workbench_selftest_exits_cleanly():
    done = subprocess.run([sys.executable, "-m", "workbench", "--selftest"],
                          cwd=ROOT, capture_output=True, text=True, timeout=180,
                          env={**__import__("os").environ,
                               "PYTHONIOENCODING": "utf-8"})
    if "no display" in done.stderr or "TclError" in done.stderr:
        pytest.skip("no display available")
    assert done.returncode == 0, done.stderr
    assert "selftest OK" in done.stdout
