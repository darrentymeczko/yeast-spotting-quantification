"""Getting around: header sorting, and the arrow keys.

The rule the arrow keys follow, and the reason they are worth testing: moving
the highlight is not the same act as choosing. Somebody holding Down to flick
through a hundred candidates must not silently change which one the export will
use. Enter, or "Use this candidate", is the commitment.
"""

from __future__ import annotations

import math
import tkinter as tk

import pytest

from results_review.gui.panels import DEFAULT_SORT, SORT_KEYS, CandidateList
from results_review.model import Candidate


def cand(tp="20 Hours", hours=20.0, dil="least", score=5.0, cv=0.1, sig=3,
         order=0, p1="a.JPG", p2="b.JPG"):
    return Candidate(medium="GLU", medium_label="Glucose", timepoint=tp,
                     hours=hours, plate1=p1, plate2=p2, dilution=dil,
                     median_cv=cv, best_set_score=score, n_significant=sig,
                     n_strains=7, csv_order=order)


POOL = [
    cand(tp="20 Hours", hours=20.0, dil="middle", score=5.4, cv=0.10, sig=3,
         order=0),
    cand(tp="16 Hours", hours=16.0, dil="least", score=4.7, cv=0.05, sig=7,
         order=1),
    cand(tp="18 Hours", hours=18.0, dil="most", score=4.0, cv=0.71, sig=2,
         order=2),
]


@pytest.fixture
def widget(tk_root):
    picked = []
    previewed = []
    w = CandidateList(tk_root, on_select=previewed.append,
                      on_choose=picked.append)
    w.show(POOL, POOL[0], POOL[0], {c.key for c in POOL})
    w.previewed, w.picked = previewed, picked
    yield w
    w.destroy()


def rows(w):
    return [w._by_iid[i].timepoint for i in w.tree.get_children()]


# --- header sorting --------------------------------------------------------

def test_every_column_can_sort():
    """A header you can click must have something to sort on."""
    for key, _title, _w in CandidateList.COLUMNS:
        assert key in SORT_KEYS, f"column {key} has no sort defined"


def test_the_default_is_the_pipelines_ranking(widget):
    assert widget.sort_key == DEFAULT_SORT
    assert rows(widget)[0] == "20 Hours", "best score first"


def test_clicking_a_header_sorts_by_it(widget):
    widget.sort_by("cv")
    assert rows(widget) == ["16 Hours", "20 Hours", "18 Hours"], "lowest CV first"


def test_clicking_the_same_header_reverses(widget):
    widget.sort_by("cv")
    first = rows(widget)
    widget.sort_by("cv")
    assert rows(widget) == list(reversed(first))


def test_a_new_column_starts_the_useful_way_round(widget):
    """High score is good, low spread is good; each opens on its good end.

    Each is approached from a DIFFERENT column, because clicking the column
    already in use is the reverse gesture, not a fresh sort.
    """
    widget.sort_by("when")
    widget.sort_by("score")
    assert rows(widget)[0] == "20 Hours"      # highest score

    widget.sort_by("when")
    widget.sort_by("cv")
    assert rows(widget)[0] == "16 Hours"      # lowest CV

    widget.sort_by("when")
    widget.sort_by("sig")
    assert rows(widget)[0] == "16 Hours"      # most strains separating


def test_timepoint_sorts_by_hours_not_by_text(widget):
    """'9 Hours' must not land after '19 Hours'."""
    pool = [cand(tp="9 Hours", hours=9.0, order=0),
            cand(tp="19 Hours", hours=19.0, order=1)]
    widget.show(pool, pool[0], pool[0], {c.key for c in pool})
    widget.sort_by("when")
    assert rows(widget) == ["9 Hours", "19 Hours"]


def test_the_sorted_column_is_marked(widget):
    widget.sort_by("cv")
    assert "▲" in widget.tree.heading("cv")["text"]
    widget.sort_by("cv")
    assert "▼" in widget.tree.heading("cv")["text"]
    assert "▲" not in widget.tree.heading("score")["text"]
    assert "▼" not in widget.tree.heading("score")["text"]


def test_sorting_keeps_the_chosen_row_selected(widget):
    """Re-ordering must not lose your place."""
    widget.sort_by("cv")
    sel = widget.tree.selection()
    assert sel and widget._by_iid[sel[0]].key == POOL[0].key


def test_a_missing_score_never_sorts_to_the_top(widget):
    pool = [cand(tp="nan", score=float("nan"), order=0),
            cand(tp="real", score=1.0, order=1)]
    widget.show(pool, pool[1], pool[1], set())
    widget.sort_by("when")          # leave the score column first
    widget.sort_by("score")         # ...so this is a fresh sort, best first
    assert rows(widget)[0] == "real"


# --- arrow-key movement ----------------------------------------------------

def test_step_moves_the_highlight(widget):
    widget.tree.selection_set(widget.tree.get_children()[0])
    assert widget.step(1)
    assert widget.tree.selection()[0] == widget.tree.get_children()[1]


def test_step_does_not_wrap(widget):
    kids = widget.tree.get_children()
    widget.tree.selection_set(kids[0])
    assert not widget.step(-1), "stepping up from the top must stop, not wrap"
    widget.tree.selection_set(kids[-1])
    assert not widget.step(1), "stepping down from the end must stop"


def test_step_clamps_a_big_jump(widget):
    """Page Up/Down ask for ten; a short list must not refuse to move at all."""
    widget.tree.selection_set(widget.tree.get_children()[0])
    assert not widget.step(10), "a jump past the end reports that it stopped"
    assert widget.tree.selection()[0] == widget.tree.get_children()[0]


def test_step_on_an_empty_list_is_harmless(widget):
    widget.show([], None, None, set())
    assert not widget.step(1)


def test_stepping_previews_but_does_not_choose(widget):
    """The property that matters: flicking through must not commit anything."""
    widget.picked.clear()
    widget.previewed.clear()
    widget.tree.selection_set(widget.tree.get_children()[0])
    widget.step(1)
    widget.update()          # <<TreeviewSelect>> is delivered by the event loop
    assert widget.previewed, "moving should preview"
    assert not widget.picked, "moving must NOT choose"


def test_returning_chooses(widget):
    widget.picked.clear()
    widget.tree.selection_set(widget.tree.get_children()[1])
    widget._activated()
    assert len(widget.picked) == 1


# --- the numbers behind the sorts ------------------------------------------

@pytest.mark.parametrize("key", sorted(SORT_KEYS))
def test_sort_keys_are_orderable(key):
    """Every key must return something sortable for every candidate, NaN included."""
    fn, _desc = SORT_KEYS[key]
    values = [fn(c) for c in POOL + [cand(score=float("nan"),
                                          cv=float("nan"),
                                          hours=float("nan"))]]
    try:
        sorted(values)
    except TypeError as e:                   # pragma: no cover - a real failure
        pytest.fail(f"{key} produced unorderable keys: {e}")
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in values), (
        f"{key} lets a NaN through; NaN compares false against everything and "
        f"scrambles the order")
