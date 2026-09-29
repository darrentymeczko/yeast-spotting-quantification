import pytest

from plate_template.model import (
    EMPTY,
    UNASSIGNED,
    Cell,
    CellKind,
    DilutionSpec,
    Placement,
    Template,
    TokenError,
)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_placement_token_round_trip():
    for slot in range(1, 21):
        for rep in range(1, 7):
            for dil in range(0, 6):
                p = Placement(slot, rep, dil)
                assert Placement.parse(p.token()) == p


def test_cell_token_round_trip():
    for cell in (UNASSIGNED, EMPTY, Cell.spot(3, 2, 1)):
        assert Cell.from_token(cell.token) == cell


def test_sentinel_tokens():
    assert UNASSIGNED.token == "?"
    assert EMPTY.token == "."
    assert Cell.from_token("?") is UNASSIGNED
    assert Cell.from_token(".") is EMPTY
    assert Cell.from_token("s1r1d0").kind is CellKind.SPOT


@pytest.mark.parametrize(
    "bad",
    [
        "",           # nothing at all
        "s1d0",       # no replicate
        "s1r1",       # no dilution
        "S1R1D0",     # tokens are case-sensitive
        "s1r1d0x",    # trailing junk
        "s1r1d-1",    # negative dilution
        " s1r1d0",    # leading space
        "s1r1d0 ",    # trailing space
        "sxr1d0",     # non-numeric
        "1r1d0",      # missing the s
    ],
)
def test_rejects_malformed_tokens(bad):
    with pytest.raises(TokenError):
        Placement.parse(bad)


@pytest.mark.parametrize("bad", ["s0r1d0", "s1r0d0"])
def test_rejects_zero_indices(bad):
    # slot and replicate are 1-based; 0 means the writer got the base wrong.
    with pytest.raises(TokenError):
        Placement.parse(bad)


def test_token_error_names_the_token():
    with pytest.raises(TokenError, match="nonsense"):
        Placement.parse("nonsense")


# ---------------------------------------------------------------------------
# DilutionSpec
# ---------------------------------------------------------------------------


def test_dilution_labels_fall_back_when_unset():
    d = DilutionSpec(levels=3)
    assert d.label(0) == "d0"
    d2 = DilutionSpec(levels=3, labels=["least", "middle", "most"])
    assert d2.label(0) == "least"
    assert d2.label(2) == "most"
    assert d2.label(9) == "d9"  # out of range falls back rather than raising


def test_dilution_factor():
    assert DilutionSpec(levels=3).factor(2) is None
    assert DilutionSpec(levels=3, fold=10).factor(0) == 1
    assert DilutionSpec(levels=3, fold=10).factor(2) == 100


# ---------------------------------------------------------------------------
# Grid queries
# ---------------------------------------------------------------------------


def _two_by_two() -> Template:
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=2))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(0, 1, Cell.spot(2, 1, 0))
    p.set(1, 0, Cell.spot(1, 1, 1))
    p.set(1, 1, Cell.spot(2, 1, 1))
    return t


def test_plate_queries():
    t = _two_by_two()
    p = t.plate("1")
    assert p.slots_present() == {1, 2}
    assert p.replicates_present() == {1}
    assert p.units() == {(1, 1), (2, 1)}
    assert [(r, c) for r, c, _ in p.cells_for(0)] == [(0, 0), (0, 1)]
    assert [(r, c) for r, c, _ in p.cells_for_slot(2)] == [(0, 1), (1, 1)]


def test_template_derives_panel_size_and_replicates():
    t = _two_by_two()
    assert t.sample_slots() == 2
    assert t.replicates() == {1}
    assert t.spot_count() == 4


def test_scorable_dilutions_requires_full_coverage_and_a_control():
    t = _two_by_two()
    p = t.plate("1")
    assert p.scorable_dilutions() == {0, 1}

    # Knock a hole in dilution 1: it is no longer scorable, dilution 0 still is.
    p.set(1, 1, UNASSIGNED)
    assert p.scorable_dilutions() == {0}

    # With no control designated nothing is scorable.
    p.control_slot = None
    assert p.scorable_dilutions() == set()


def test_control_cells_and_override():
    t = _two_by_two()
    assert [(r, c) for r, c, _ in t.control_cells("1", 0)] == [(0, 0)]
    # The override parameter is the seam a per-treatment control plugs into.
    assert [(r, c) for r, c, _ in t.control_cells("1", 0, control_slot=2)] == [(0, 1)]


def test_plate_lookup_raises_for_unknown_id():
    with pytest.raises(KeyError):
        _two_by_two().plate("nope")


# ---------------------------------------------------------------------------
# resize / add_plate
# ---------------------------------------------------------------------------


def test_resize_preserves_the_overlapping_region():
    t = _two_by_two()
    t.resize(3, 1)
    p = t.plate("1")
    assert (t.rows, t.cols) == (3, 1)
    assert p.rows == 3 and p.cols == 1
    assert p.get(0, 0) == Cell.spot(1, 1, 0)   # kept
    assert p.get(1, 0) == Cell.spot(1, 1, 1)   # kept
    assert p.get(2, 0) is UNASSIGNED           # new row
    assert p.slots_present() == {1}            # column 2 was dropped


def test_resize_rows_are_independent_lists():
    t = _two_by_two()
    t.resize(3, 3)
    p = t.plate("1")
    p.set(2, 2, Cell.spot(9, 9, 0))
    assert p.get(0, 2) is UNASSIGNED  # would fail if rows shared a list


def test_add_plate_inherits_the_first_plate_control():
    t = _two_by_two()
    second = t.add_plate("2")
    assert second.control_slot == 1
    assert second.rows == t.rows and second.cols == t.cols
    assert all(c is UNASSIGNED for row in second.cells for c in row)
    assert t.has_plate("2")
