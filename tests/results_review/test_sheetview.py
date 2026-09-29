"""The sheet pane holds two pictures, and must not confuse them.

Written after a real bug: a badly placed edit left the tail of `__init__`
inside `show_toggle`, so every caption refresh silently re-ran the
initialisation and reset the redrawn graph to None. The window then reported
"graph redrawn" while showing nothing -- the failure mode was a method that
quietly did a second, unrelated job.

So these check the state machine rather than the pixels: a redrawn graph
survives being displayed, a new candidate discards it, and the toggle never
offers a picture that is not there.
"""

from __future__ import annotations

import tkinter as tk

import pytest

from results_review.gui.browser import SheetView
from results_review.model import Candidate

CAND = Candidate(medium="GLU", medium_label="Glucose", timepoint="20 Hours",
                 hours=20.0, plate1="a.JPG", plate2="b.JPG", dilution="middle")
OTHER = Candidate(medium="GLU", medium_label="Glucose", timepoint="16 Hours",
                  hours=16.0, plate1="a.JPG", plate2="b.JPG", dilution="least")


@pytest.fixture
def png(tmp_path):
    from test_preview import png_bytes

    p = tmp_path / "graph.png"
    p.write_bytes(png_bytes())
    return p


@pytest.fixture
def view(tk_root):
    v = SheetView(tk_root)
    v.pack()
    v.update_idletasks()
    yield v
    v.destroy()


# --- the bug ---------------------------------------------------------------

def test_a_redrawn_graph_survives_being_displayed(view, png):
    """The regression: showing it must not be what throws it away."""
    view.show(CAND, None)
    view.show_updated(png, publication=False)
    assert view.has_updated, "the graph was lost between setting and drawing it"
    assert view._updated == png
    assert view._updated_is_publication is False


def test_refreshing_the_caption_keeps_the_graph(view, png):
    view.show(CAND, None)
    view.show_updated(png)
    view._caption()
    view._caption()
    assert view.has_updated


def test_the_toggle_button_does_not_reset_state(view, png):
    view.show(CAND, None)
    view.show_updated(png)
    view.show_toggle(True)
    view.show_toggle(False)
    assert view.has_updated
    assert view.cache is not None


# --- the state machine -----------------------------------------------------

def test_a_new_candidate_discards_the_old_graph(view, png):
    """Another candidate's graph would be the wrong numbers entirely."""
    view.show(CAND, None)
    view.show_updated(png)
    view.show(OTHER, None)
    assert not view.has_updated
    assert not view._showing_updated


def test_toggle_needs_something_to_toggle_to(view, png):
    view.show(CAND, None)
    assert view.toggle() is False, "nothing to swap to yet"
    view.show_updated(png)
    assert view.toggle() is True
    assert not view._showing_updated          # now on the pipeline's sheet
    assert view.toggle() is True
    assert view._showing_updated              # and back


def _gridded(widget) -> bool:
    """Whether grid is managing this widget.

    `winfo_ismapped` is False for everything inside a withdrawn window, so it
    cannot tell "removed from the layout" from "the test has no visible root".
    `winfo_manager` reports exactly what grid_remove() changes.
    """
    return widget.winfo_manager() != ""


def test_the_toggle_button_is_hidden_until_there_is_a_graph(view, png):
    view.show(CAND, None)
    view.update_idletasks()
    assert not _gridded(view.btn_toggle)
    view.show_updated(png)
    view.update_idletasks()
    assert _gridded(view.btn_toggle)


def test_the_caption_distinguishes_nonpublication_previews(view, png):
    view.show(CAND, None)
    view.show_updated(png, publication=False)
    assert "no significance testing" in view.caption.cget("text").lower()

    view.show_updated(png, publication=True)
    assert "no significance testing" not in view.caption.cget("text").lower()
    assert "redrawn" in view.caption.cget("text").lower()


def test_photo_controls_only_appear_over_an_image(view, png):
    view.show(CAND, png)
    view.update_idletasks()
    assert view.display_controls.winfo_manager() == ""

    view._show_controls()
    view.update_idletasks()
    assert view.display_controls.winfo_manager() == "place"
    placed = view.display_controls.place_info()
    assert int(placed["x"]) == 0
    assert int(placed["y"]) == 0
    assert int(placed["width"]) == view.canvas.winfo_width()

    view._hide_controls()
    assert view.display_controls.winfo_manager() == ""

    view.clear()
    view._show_controls()
    assert view.display_controls.winfo_manager() == ""


def test_photo_controls_only_reveal_in_their_top_edge_region(view, png):
    from types import SimpleNamespace

    view.show(CAND, png)
    view._photo_motion(SimpleNamespace(y=view.display_controls.winfo_reqheight()
                                       + 20))
    assert view.display_controls.winfo_manager() == ""

    view._photo_motion(SimpleNamespace(y=2))
    assert view.display_controls.winfo_manager() == "place"

    view._photo_motion(SimpleNamespace(y=view.display_controls.winfo_reqheight()
                                       + 20))
    assert view.display_controls.winfo_manager() == ""


def test_display_adjustments_are_preview_only_and_resettable(view, png):
    before = png.read_bytes()
    view.show(CAND, png)
    view._brightness_var.set(1.4)
    view._contrast_var.set(1.6)
    view._adjusted()
    view._draw()

    assert view._brightness_text.get() == "140%"
    assert view._contrast_text.get() == "160%"
    assert png.read_bytes() == before

    view.reset_view()
    assert view._brightness_var.get() == pytest.approx(1.0)
    assert view._contrast_var.get() == pytest.approx(1.0)
    assert view._brightness_text.get() == "100%"
    assert view._contrast_text.get() == "100%"


def test_figure_can_zoom_pan_and_return_to_fit(view, png):
    view.show(CAND, png)
    assert view.canvas.bind("<MouseWheel>")
    assert view.canvas.bind("<B1-Motion>")

    view._zoom_by(1.25)
    assert view._zoom == pytest.approx(1.25)
    assert view._zoom_text.get() == "125%"

    # Pan state is restored along with zoom even if the small test PNG itself
    # is not large enough to expose a scrollable edge.
    view._pan_x, view._pan_y = 12, -8
    view.reset_view()
    assert view._zoom == 1.0
    assert (view._pan_x, view._pan_y) == (0.0, 0.0)


def test_zoom_is_bounded(view):
    from results_review.gui.browser import MAX_ZOOM, MIN_ZOOM

    for _ in range(20):
        view._zoom_by(2)
    assert view._zoom == MAX_ZOOM
    for _ in range(20):
        view._zoom_by(0.5)
    assert view._zoom == MIN_ZOOM


def test_app_restores_a_candidates_redrawn_sheet_after_navigation(tmp_path):
    from types import SimpleNamespace

    from results_review.app import ReviewApp

    updated = tmp_path / "updated.png"
    updated.write_bytes(b"present")

    class Sheet:
        def __init__(self):
            self.calls = []

        def show(self, cand, original):
            self.calls.append(("original", cand, original))

        def show_updated(self, path):
            self.calls.append(("updated", path))

    sheet = Sheet()
    fake = SimpleNamespace(
        sheet=sheet,
        ctl=SimpleNamespace(run=SimpleNamespace(sheet=lambda _cand: "original")),
        _stale_media=set(),
        _redrawn_sheets={CAND.key: updated},
    )
    ReviewApp._show_sheet(fake, CAND)
    assert [call[0] for call in sheet.calls] == ["original", "updated"]
    assert sheet.calls[-1][1] == updated


def test_a_stale_redraw_is_not_restored(tmp_path):
    from types import SimpleNamespace

    from results_review.app import ReviewApp

    updated = tmp_path / "updated.png"
    updated.write_bytes(b"present")
    sheet = SimpleNamespace(show=lambda *_args: None,
                            show_updated=lambda *_args: pytest.fail(
                                "stale redraw was restored"))
    fake = SimpleNamespace(
        sheet=sheet,
        ctl=SimpleNamespace(run=SimpleNamespace(sheet=lambda _cand: "original")),
        _stale_media={"GLU"},
        _redrawn_sheets={CAND.key: updated},
    )
    ReviewApp._show_sheet(fake, CAND)


def test_spot_table_has_one_manual_omit_action(tk_root):
    from results_review.gui.table import SpotTable

    table = SpotTable(tk_root, on_raw=lambda *_: None,
                      on_outlier=lambda *_: None, on_note=lambda *_: None,
                      on_revert=lambda *_: None)
    try:
        assert table.tree.bind("<space>") == ""
        assert not hasattr(table, "on_excluded")
        assert "outlier" in table.winfo_children()[0].cget("text").lower()
    finally:
        table.destroy()


def test_spot_tables_have_a_bottom_gutter(tk_root):
    from results_review.gui.panels import CandidateList
    from results_review.gui.table import SpotTable

    candidates = CandidateList(tk_root, on_select=lambda *_: None,
                               on_choose=lambda *_: None)
    table = SpotTable(tk_root, on_raw=lambda *_: None,
                      on_outlier=lambda *_: None, on_note=lambda *_: None,
                      on_revert=lambda *_: None)
    try:
        assert table.bottom_gutter.cget("font") == candidates.count.cget("font")
        assert (table.bottom_gutter.grid_info()["pady"] ==
                candidates.count.grid_info()["pady"])
    finally:
        candidates.destroy()
        table.destroy()


def test_graph_right_gutter_matches_summary_table(view, tk_root):
    from results_review.gui.table import SpotTable

    table = SpotTable(tk_root, on_raw=lambda *_: None,
                      on_outlier=lambda *_: None, on_note=lambda *_: None,
                      on_revert=lambda *_: None)
    try:
        graph_right = view.canvas.grid_info()["padx"]
        table_right = table.panes.grid_info()["padx"]
        assert graph_right == table_right
    finally:
        table.destroy()


def test_p_value_fits_in_the_summary_without_horizontal_scrolling(tk_root):
    from results_review import theme
    from results_review.gui.table import SpotTable

    table = SpotTable(tk_root, on_raw=lambda *_: None,
                      on_outlier=lambda *_: None, on_note=lambda *_: None,
                      on_revert=lambda *_: None)
    try:
        assert "p" in table.summary["columns"]
        widths = sum(int(table.summary.column(key, "width"))
                     for key in table.summary["columns"])
        assert widths <= theme.SUMMARY_W - 2 * theme.PAD
    finally:
        table.destroy()
