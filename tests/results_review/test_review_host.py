"""The review tool as a frame in a host, rather than the window itself.

It used to BE the Tk root. Now it builds into whatever host it is given --
its own window, or a tab of the workbench -- and closing it must leave nothing
running behind it: the queue pump is a timer that reschedules itself forever,
and in a tab the window it was scheduled on lives on.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from results_review import discovery, links
from results_review.app import ReviewApp

CANDIDATES = (
    "medium,medium_label,timepoint,hours,plate1,plate2,dilution,control_n,"
    "median_CV,control_mean,control_CV,n_strains,n_significant,rank_score,"
    "ranked_by,best_set_score\n"
    "GLU,Glucose,16 Hours,16.0,_9.JPG,_9_1.JPG,least,4,0.05,10.7,0.34,7,7,1.75,"
    "combined,4.72\n"
)


@pytest.fixture
def run(tmp_path, monkeypatch):
    # Hermetic: never read or write the real links file, never go looking
    # through the real data folders for this made-up set's photographs.
    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    monkeypatch.setattr(links, "SEARCH_HINTS", [])
    monkeypatch.setattr(discovery, "TIMECOURSE_RESULTS", tmp_path)
    results = tmp_path / "ZZ Host Test"
    results.mkdir()
    (results / discovery.CANDIDATES_CSV).write_text(CANDIDATES, encoding="utf-8")
    return discovery.load_set(results)


@pytest.fixture
def window(tk_root):
    win = tk.Toplevel(tk_root)
    win.withdraw()
    yield win
    try:
        win.destroy()
    except tk.TclError:
        pass


def _pending(widget) -> set[str]:
    return set(widget.tk.splitlist(widget.tk.call("after", "info")))


def test_it_builds_into_the_window_it_is_given(window, run):
    app = ReviewApp(window, run)
    assert isinstance(app, tk.Misc) and not isinstance(app, tk.Tk)
    assert app.winfo_toplevel() is window
    assert "ZZ Host Test" in window.title()


def test_closing_stops_the_pump_and_removes_the_scratch_graphs(window, run,
                                                               tmp_path):
    app = ReviewApp(window, run)
    window.update_idletasks()
    scratch = tmp_path / "rr_graph_scratch"
    scratch.mkdir()
    app._graph_dir = scratch
    pump = app._pump_id
    assert pump in _pending(window)

    assert app.host.close() is True
    assert pump not in _pending(window)
    assert not scratch.exists()


def test_unsaved_review_edits_can_keep_it_open(window, run, monkeypatch):
    from results_review import app as app_module

    app = ReviewApp(window, run)
    monkeypatch.setattr(type(app.ctl), "dirty", property(lambda _self: True))
    monkeypatch.setattr(app_module.messagebox, "askyesnocancel",
                        lambda *a, **k: None)        # Cancel
    assert app.host.close() is False
    assert window.winfo_exists()


def test_the_keys_belong_to_its_window_not_the_whole_process(window, run,
                                                             tk_root):
    ReviewApp(window, run)
    assert window.bind("<Control-r>")
    assert not tk_root.bind_all("<Control-r>")
