"""Dilution levels read off the plate template, however many there are.

The pipeline has always assumed three levels spotted twice down six rows. That
is one way to fill a 6x8 grid; six levels spotted once each is another, and the
plate designer draws either. These tests cover the shapes the arithmetic could
not express.

The grid itself stays 6x8 -- `spotting_quant.detect_grid` fits exactly that
lattice -- so none of this needs the detector to change.
"""

import pytest

from experiments import geometry
from plate_template import presets
from plate_template.model import Cell, DilutionSpec, PlateSlot, Template


def lab():
    return presets.lab_standard_8x6()


def custom(levels: int, reps_per_plate: int, *, labels=None, plates=("1", "2"),
           rows=6, cols=8) -> Template:
    """A template of `levels` levels, each spotted `reps_per_plate` times.

    Rows run level 0..N-1 for the first replicate, then again for the next, the
    way the lab's own design does with three levels and two replicates.
    """
    t = Template(name=f"{levels}x{reps_per_plate}", rows=rows, cols=cols,
                 dilution=DilutionSpec(levels=levels, fold=10,
                                       labels=list(labels or [])))
    for plate_no, pid in enumerate(plates, start=1):
        plate = t.add_plate(pid, control_slot=1)
        for r in range(rows):
            block, level = divmod(r, levels)
            if block >= reps_per_plate:
                continue
            replicate = (plate_no - 1) * reps_per_plate + block + 1
            for c in range(cols):
                plate.cells[r][c] = Cell.spot(c + 1, replicate, level)
    return t


# --- counting and naming ----------------------------------------------------


def test_the_lab_standard_still_reads_as_three_named_levels():
    assert geometry.levels(lab()) == [(0, "1 - least"), (1, "2 - middle"),
                                      (2, "3 - most")]


def test_six_levels_are_all_offered():
    t = custom(6, 1)
    assert geometry.level_count(t) == 6
    assert [i for i, _ in geometry.levels(t)] == [0, 1, 2, 3, 4, 5]


def test_unnamed_levels_are_numbered_rather_than_called_d0():
    """`DilutionSpec.label` falls back to "d0", which reads like a variable."""
    t = custom(6, 1)
    assert geometry.level_label(t, 0) == "level 1"
    assert geometry.level_display(t, 0) == "1"
    assert geometry.level_display(t, 5) == "6"


def test_a_partly_named_design_names_what_it_can():
    t = custom(4, 1, labels=["neat", "1:10"])
    assert geometry.level_display(t, 0) == "1 - neat"
    assert geometry.level_display(t, 1) == "2 - 1:10"
    assert geometry.level_display(t, 3) == "4"


def test_no_template_offers_no_levels():
    assert geometry.levels(None) == []
    assert geometry.level_count(None) == 0


# --- which rows a level occupies --------------------------------------------


def test_two_replicates_put_a_level_on_two_rows():
    t = custom(3, 2)
    assert geometry.rows_for(t, "1", 0) == (1, 4)
    assert geometry.rows_for(t, "1", 2) == (3, 6)


def test_one_replicate_per_plate_puts_a_level_on_one_row():
    t = custom(6, 1)
    for index in range(6):
        assert geometry.rows_for(t, "1", index) == (index + 1,)


def test_three_replicates_of_two_levels_put_a_level_on_three_rows():
    t = custom(2, 3)
    assert geometry.rows_for(t, "1", 0) == (1, 3, 5)
    assert geometry.rows_for(t, "1", 1) == (2, 4, 6)


def test_rows_are_one_based_for_the_engine():
    """`roi_radius_for_rows` filters on 1 <= r <= N_ROWS; a 0 would vanish."""
    for t in (lab(), custom(6, 1), custom(2, 3)):
        for index in range(geometry.level_count(t)):
            rows = geometry.rows_for(t, "1", index)
            assert rows and min(rows) >= 1 and max(rows) <= t.rows


def test_a_level_nothing_was_spotted_at_has_no_rows():
    assert geometry.rows_for(custom(3, 2), "1", 9) == ()


def test_an_unknown_plate_has_no_rows():
    assert geometry.rows_for(lab(), "nonesuch", 0) == ()


def test_every_level_of_a_full_design_is_scorable():
    t = custom(6, 1)
    assert geometry.scorable_levels(t, "1") == [0, 1, 2, 3, 4, 5]


# --- replicates -------------------------------------------------------------


def test_replicates_continue_across_plates():
    """Four biological replicates over two plates, as the lab design has."""
    t = custom(3, 2)
    reps = {p.replicate for pid in ("1", "2")
            for i in range(3)
            for _r, _c, p in geometry.cells_for(t, pid, i)}
    assert reps == {1, 2, 3, 4}


def test_six_levels_still_give_one_replicate_per_plate():
    t = custom(6, 1)
    assert {p.replicate for _r, _c, p in geometry.cells_for(t, "1", 0)} == {1}
    assert {p.replicate for _r, _c, p in geometry.cells_for(t, "2", 0)} == {2}


# --- resolving a stored choice ----------------------------------------------


@pytest.mark.parametrize("value, want", [
    (0, 0), (2, 2), ("0", 0), ("2", 2),
    ("least", 0), ("middle", 1), ("most", 2), ("MOST", 2),
    (None, None), ("", None), ("nonsense", None), (-1, None), (True, None),
])
def test_resolving_a_stored_level(value, want):
    assert geometry.resolve_level(lab(), value) == want


def test_a_templates_own_names_win_over_the_classic_ones():
    """A design that renames its levels still reads its own files."""
    t = custom(3, 2, labels=["most", "middle", "least"])   # deliberately reversed
    assert geometry.resolve_level(t, "most") == 0
    assert geometry.resolve_level(t, "least") == 2


def test_an_out_of_range_index_survives_resolution_to_be_reported():
    """So the message can say "level 8, but the template declares 3"."""
    assert geometry.resolve_level(lab(), 7) == 7
    assert not geometry.is_valid_level(lab(), 7)
    assert geometry.is_valid_level(lab(), 2)
    assert not geometry.is_valid_level(lab(), None)


# --- the grid the engine can actually find -----------------------------------


def test_the_lab_grid_is_supported():
    assert geometry.grid_supported(lab())
    assert geometry.grid_shape(lab()) == (6, 8)


def test_more_levels_on_the_same_grid_are_supported():
    assert geometry.grid_supported(custom(6, 1))
    assert geometry.grid_supported(custom(2, 3))


@pytest.mark.parametrize("rows, cols", [(8, 12), (12, 8), (12, 16), (16, 24)])
def test_a_bigger_grid_is_supported_and_reports_its_own_size(rows, cols):
    """The lattice is no longer fixed: the engine is told what to look for."""
    t = custom(4, 1, rows=rows, cols=cols)
    assert geometry.grid_supported(t)
    assert geometry.grid_shape(t) == (rows, cols)


@pytest.mark.parametrize("rows, cols", [(1, 8), (6, 1), (1, 1)])
def test_a_grid_too_thin_to_fit_a_lattice_is_not(rows, cols):
    """One row or column leaves no agar gap to read the background from."""
    assert not geometry.grid_supported(custom(1, 1, rows=rows, cols=cols))


def test_no_template_is_not_supported_and_falls_back_to_the_default_shape():
    assert not geometry.grid_supported(None)
    assert geometry.grid_shape(None) == (geometry.DEFAULT_ROWS,
                                         geometry.DEFAULT_COLS)
