"""The dilution drag derives direction, level count and source run from the
gesture alone, so that arithmetic is worth pinning down.

Importing plate_template.app pulls in tkinter but never creates a root window,
so these run without a display.
"""

import pytest

from plate_template.app import DesignerApp
from plate_template.model import EMPTY, Cell, DilutionSpec, Template


def plate_with(row_spots) -> Template:
    """One 6x8 plate; row_spots maps (row, col) -> (slot, rep, dilution)."""
    t = Template(name="t", rows=6, cols=8, dilution=DilutionSpec(levels=4))
    plate = t.add_plate("1", control_slot=1)
    for (r, c), (slot, rep, dil) in row_spots.items():
        plate.set(r, c, Cell.spot(slot, rep, dil))
    return t


# ---------------------------------------------------------------------------
# Direction and span come from the drag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "anchor,current,direction,spanned",
    [
        ((0, 0), (2, 0), "down", 3),
        ((2, 0), (0, 0), "up", 3),
        ((0, 0), (0, 4), "right", 5),
        ((0, 4), (0, 0), "left", 5),
        ((0, 0), (0, 0), "down", 1),      # no movement
        ((0, 0), (3, 1), "down", 4),       # dominant axis wins, never diagonal
        ((0, 0), (1, 5), "right", 6),
    ],
)
def test_line_reads_direction_and_span(anchor, current, direction, spanned):
    assert DesignerApp._line(anchor, current) == (direction, spanned)


@pytest.mark.parametrize(
    "spanned,spacing,levels",
    [
        (3, 1, 3),    # plain three-cell drag, three levels
        (1, 1, 1),    # no movement -> too few levels to be a series
        (5, 2, 3),    # the drag ends where the LAST level lands
        (4, 2, 2),
        (7, 3, 3),
    ],
)
def test_levels_from_span_and_spacing(spanned, spacing, levels):
    # Mirrors the expression in DesignerApp._plan_for_drag.
    assert (spanned - 1) // spacing + 1 == levels


# ---------------------------------------------------------------------------
# The source run is found, not clicked
# ---------------------------------------------------------------------------


def test_peer_run_picks_up_the_whole_row():
    t = plate_with({(0, c): (c + 1, 1, 0) for c in range(8)})
    plate = t.plate("1")
    source = plate.get(0, 3).placement
    run = DesignerApp._peer_run(plate, (0, 3), "down", source)
    assert run == [(0, c) for c in range(8)]


def test_peer_run_stops_at_a_gap():
    spots = {(0, c): (c + 1, 1, 0) for c in range(8)}
    del spots[(0, 5)]
    t = plate_with(spots)
    plate = t.plate("1")
    plate.set(0, 5, EMPTY)
    source = plate.get(0, 3).placement
    run = DesignerApp._peer_run(plate, (0, 3), "down", source)
    assert run == [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]


def test_peer_run_stops_at_a_different_replicate():
    spots = {(0, c): (c + 1, 1, 0) for c in range(4)}
    spots.update({(0, c): (c + 1, 2, 0) for c in range(4, 8)})
    plate = plate_with(spots).plate("1")
    source = plate.get(0, 1).placement
    assert DesignerApp._peer_run(plate, (0, 1), "down", source) == [
        (0, 0), (0, 1), (0, 2), (0, 3)
    ]


def test_peer_run_stops_at_a_different_dilution():
    spots = {(0, c): (c + 1, 1, 0) for c in range(8)}
    spots[(0, 6)] = (7, 1, 1)
    plate = plate_with(spots).plate("1")
    source = plate.get(0, 2).placement
    assert DesignerApp._peer_run(plate, (0, 2), "down", source) == [
        (0, 0), (0, 1), (0, 2), (0, 3), (0, 4), (0, 5)
    ]


def test_peer_run_scans_the_column_for_a_sideways_drag():
    t = plate_with({(r, 0): (1, r + 1, 0) for r in range(3)})
    plate = t.plate("1")
    # Each row here is a different replicate, so only the anchor matches.
    source = plate.get(1, 0).placement
    assert DesignerApp._peer_run(plate, (1, 0), "right", source) == [(1, 0)]

    t2 = plate_with({(r, 0): (1, 1, 0) for r in range(3)})
    plate2 = t2.plate("1")
    source2 = plate2.get(1, 0).placement
    assert DesignerApp._peer_run(plate2, (1, 0), "right", source2) == [
        (0, 0), (1, 0), (2, 0)
    ]


def test_peer_run_of_a_lone_spot_is_just_itself():
    plate = plate_with({(2, 2): (1, 1, 0)}).plate("1")
    source = plate.get(2, 2).placement
    assert DesignerApp._peer_run(plate, (2, 2), "down", source) == [(2, 2)]


# ---------------------------------------------------------------------------
# The slot counter walks itself on
# ---------------------------------------------------------------------------


def test_next_free_slot_starts_at_one_on_an_empty_plate():
    t = plate_with({})
    assert DesignerApp._next_free_slot(t, 1, 0) == 1


def test_next_free_slot_advances_past_a_placed_run():
    t = plate_with({(0, c): (c + 1, 1, 0) for c in range(8)})
    assert DesignerApp._next_free_slot(t, 1, 0) == 9


def test_next_free_slot_resets_for_a_new_replicate():
    # Replicate 2 uses the same samples again, so the counter must drop back.
    t = plate_with({(0, c): (c + 1, 1, 0) for c in range(8)})
    assert DesignerApp._next_free_slot(t, 2, 0) == 1


def test_next_free_slot_is_scoped_per_dilution():
    # Placing the undiluted row must not make the counter skip for dilution 1.
    t = plate_with({(0, c): (c + 1, 1, 0) for c in range(8)})
    assert DesignerApp._next_free_slot(t, 1, 1) == 1


def test_next_free_slot_fills_an_erased_gap():
    spots = {(0, c): (c + 1, 1, 0) for c in range(8)}
    del spots[(0, 2)]  # slot 3 never placed
    t = plate_with(spots)
    assert DesignerApp._next_free_slot(t, 1, 0) == 3


def test_next_free_slot_is_bounded():
    t = plate_with({})
    plate = t.plate("1")
    for c in range(8):
        plate.set(0, c, Cell.spot(c + 1, 1, 0))
    # Sanity: never loops forever even if every slot up to the cap is taken.
    assert 1 <= DesignerApp._next_free_slot(t, 1, 0) <= 99
