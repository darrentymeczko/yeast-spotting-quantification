import pytest

from plate_template.autofill import (
    DELTA,
    Plan,
    RunFillSpec,
    SeriesFillSpec,
    apply_plan,
    plan_duplicate_plate,
    plan_paint,
    plan_run_fill,
    plan_series_fill,
)
from plate_template.model import EMPTY, UNASSIGNED, Cell, CellKind, DilutionSpec, Template
from plate_template.presets import lab_standard_8x6
from plate_template.validate import Severity


def grid(rows: int = 4, cols: int = 4, levels: int = 3, plates: int = 1) -> Template:
    t = Template(name="t", rows=rows, cols=cols, dilution=DilutionSpec(levels=levels))
    for i in range(1, plates + 1):
        t.add_plate(str(i), control_slot=1)
    return t


def spots(t: Template, plate_id: str = "1") -> dict[tuple[int, int], tuple[int, int, int]]:
    return {
        (r, c): (p.sample_slot, p.replicate, p.dilution)
        for r, c, p in t.plate(plate_id).placements()
    }


def codes(plan: Plan) -> set[str]:
    return {i.code for i in plan.issues}


# ---------------------------------------------------------------------------
# Run fill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "direction,start,expected",
    [
        ("right", (0, 0), [(0, 0), (0, 1), (0, 2)]),
        ("down", (0, 0), [(0, 0), (1, 0), (2, 0)]),
        ("left", (0, 3), [(0, 3), (0, 2), (0, 1)]),
        ("up", (3, 0), [(3, 0), (2, 0), (1, 0)]),
    ],
)
def test_run_fill_walks_each_direction(direction, start, expected):
    t = grid()
    plan = plan_run_fill(
        t, "1", RunFillSpec(start=start, direction=direction, count=3)
    )
    assert [(r, c) for r, c, _ in plan.writes] == expected
    assert [cell.placement.sample_slot for _, _, cell in plan.writes] == [1, 2, 3]


def test_run_fill_honours_a_reversed_drag():
    """A reverse drag enters a mirrored panel; it must not be normalised."""
    t = grid()
    plan = plan_run_fill(t, "1", RunFillSpec(start=(0, 3), direction="left", count=4))
    apply_plan(t.plate("1"), plan)
    # Slot 1 lands at the right-hand end, slot 4 at the left.
    assert spots(t)[(0, 3)][0] == 1
    assert spots(t)[(0, 0)][0] == 4


def test_run_fill_explicit_slots_win_over_first_slot():
    t = grid()
    plan = plan_run_fill(
        t,
        "1",
        RunFillSpec(start=(0, 0), direction="right", count=3, first_slot=5, slots=[7, 2, 9]),
    )
    assert [cell.placement.sample_slot for _, _, cell in plan.writes] == [7, 2, 9]


def test_run_fill_carries_replicate_and_dilution():
    t = grid()
    plan = plan_run_fill(
        t,
        "1",
        RunFillSpec(start=(1, 0), direction="right", count=2, replicate=3, dilution=2),
    )
    assert all(
        cell.placement.replicate == 3 and cell.placement.dilution == 2
        for _, _, cell in plan.writes
    )


def test_run_fill_reports_running_off_the_grid_rather_than_truncating():
    t = grid(rows=4, cols=4)
    plan = plan_run_fill(t, "1", RunFillSpec(start=(0, 2), direction="right", count=4))
    assert len(plan.writes) == 2
    assert "fill_runs_off_grid" in codes(plan)


def test_run_fill_warns_before_replacing_existing_spots():
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 3)))
    plan = plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 3, first_slot=10))
    issue = next(i for i in plan.issues if i.code == "fill_replaces_spots")
    assert issue.severity is Severity.WARNING
    assert len(issue.cells) == 3
    # ...but it still plans the writes; overwriting is how you correct a drag.
    assert len(plan.writes) == 3


# ---------------------------------------------------------------------------
# Series fill
# ---------------------------------------------------------------------------


def test_series_fill_extends_each_source():
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 2)))
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0), (0, 1)], direction="down", levels=3)
    )
    apply_plan(t.plate("1"), plan)
    assert spots(t) == {
        (0, 0): (1, 1, 0), (1, 0): (1, 1, 1), (2, 0): (1, 1, 2),
        (0, 1): (2, 1, 0), (1, 1): (2, 1, 1), (2, 1): (2, 1, 2),
    }


def test_series_fill_levels_counts_the_source():
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 1)))
    for levels, expected_writes in ((2, 1), (3, 2), (4, 3)):
        plan = plan_series_fill(
            t, "1", SeriesFillSpec(sources=[(0, 0)], direction="down", levels=levels)
        )
        assert len(plan.writes) == expected_writes


def test_series_fill_respects_spacing():
    t = grid(rows=4, cols=4)
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 1)))
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0)], direction="down", spacing=2, levels=2)
    )
    assert [(r, c) for r, c, _ in plan.writes] == [(2, 0)]


def test_series_fill_can_run_diagonally_via_repeated_calls():
    # Irregular layouts are allowed; a series is not required to be vertical.
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 1)))
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0)], direction="right", levels=3)
    )
    assert [(r, c) for r, c, _ in plan.writes] == [(0, 1), (0, 2)]


def test_series_fill_reports_an_occupied_target_and_skips_it():
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 1)))
    t.plate("1").set(1, 0, Cell.spot(9, 9, 0))
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0)], direction="down", levels=3)
    )
    assert "series_target_occupied" in codes(plan)
    assert (1, 0) not in [(r, c) for r, c, _ in plan.writes]


def test_series_fill_reports_running_off_the_grid():
    t = grid(rows=2, cols=2)
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 1)))
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0)], direction="down", levels=4)
    )
    assert "series_target_off_grid" in codes(plan)


def test_series_fill_reports_a_source_with_no_spot():
    t = grid()
    plan = plan_series_fill(
        t, "1", SeriesFillSpec(sources=[(0, 0)], direction="down", levels=3)
    )
    assert "series_source_not_a_spot" in codes(plan)
    assert plan.writes == []


# ---------------------------------------------------------------------------
# Duplicate plate
# ---------------------------------------------------------------------------


def test_duplicate_plate_offsets_replicates_and_keeps_geometry():
    t = grid(plates=2)
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 3)))
    apply_plan(
        t.plate("1"),
        plan_series_fill(
            t, "1", SeriesFillSpec([(0, 0), (0, 1), (0, 2)], "down", levels=2)
        ),
    )
    apply_plan(t.plate("2"), plan_duplicate_plate(t, "1", "2", replicate_offset=2))

    src, dst = spots(t, "1"), spots(t, "2")
    assert set(src) == set(dst)  # geometry identical
    for pos, (slot, rep, dil) in src.items():
        assert dst[pos] == (slot, rep + 2, dil)


def test_duplicate_plate_carries_empty_and_unassigned_across():
    t = grid(plates=2)
    t.plate("1").set(0, 0, EMPTY)
    t.plate("1").set(0, 1, Cell.spot(1, 1, 0))
    apply_plan(t.plate("2"), plan_duplicate_plate(t, "1", "2", replicate_offset=1))
    assert t.plate("2").get(0, 0) is EMPTY
    assert t.plate("2").get(0, 2) is UNASSIGNED


def test_duplicate_plate_refuses_a_shape_mismatch():
    t = grid(plates=2)
    t.plate("2").cells = [[UNASSIGNED]]
    plan = plan_duplicate_plate(t, "1", "2", replicate_offset=2)
    assert plan.writes == []
    assert "duplicate_plate_shape_mismatch" in codes(plan)
    assert plan.issues[0].severity is Severity.ERROR


# ---------------------------------------------------------------------------
# Paint
# ---------------------------------------------------------------------------


def test_paint_marks_empty_and_clears():
    t = grid()
    apply_plan(t.plate("1"), plan_run_fill(t, "1", RunFillSpec((0, 0), "right", 2)))
    apply_plan(t.plate("1"), plan_paint(t, "1", [(0, 0)], CellKind.EMPTY))
    assert t.plate("1").get(0, 0) is EMPTY
    apply_plan(t.plate("1"), plan_paint(t, "1", [(0, 1)], CellKind.UNASSIGNED))
    assert t.plate("1").get(0, 1) is UNASSIGNED


def test_paint_ignores_out_of_bounds_cells():
    t = grid()
    plan = plan_paint(t, "1", [(0, 0), (99, 99)], CellKind.EMPTY)
    assert len(plan.writes) == 1


# ---------------------------------------------------------------------------
# The primitives must reproduce the shipped preset
# ---------------------------------------------------------------------------


def test_lab_standard_is_built_from_the_primitives():
    """M2's gate: the preset is now assembled by autofill, unchanged."""
    t = lab_standard_8x6()
    for plate_number, first_replicate in ((1, 1), (2, 3)):
        plate = t.plate(str(plate_number))
        for r in range(6):
            for c in range(8):
                p = plate.get(r, c).placement
                assert (p.sample_slot, p.replicate, p.dilution) == (
                    c + 1,
                    first_replicate + r // 3,
                    r % 3,
                )


def test_delta_table_is_complete():
    assert set(DELTA) == {"down", "right", "up", "left"}
    assert all(abs(dr) + abs(dc) == 1 for dr, dc in DELTA.values())
