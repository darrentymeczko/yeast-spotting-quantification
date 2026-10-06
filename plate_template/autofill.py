"""Rule-driven placement, split into planning and applying.

A rule never becomes part of the template. It is evaluated once, produces an
explicit list of `(row, col, Cell)` writes, and those writes are what get
stored. Nothing re-derives a sample's identity later from the order it was
painted in, which is the whole point: a mislabelled sample is this tool's
worst failure mode, so the assignment has to be concrete and individually
correctable the moment it lands.

The split also gives the editor a free preview -- the same Plan can be drawn as
ghost cells before the user commits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .model import EMPTY, UNASSIGNED, Cell, CellKind, PlateSlot, Template, cell_ref
from .validate import Issue, Severity

Direction = Literal["down", "right", "up", "left"]

DELTA: dict[str, tuple[int, int]] = {
    "down": (1, 0),
    "right": (0, 1),
    "up": (-1, 0),
    "left": (0, -1),
}


@dataclass(frozen=True)
class RunFillSpec:
    """Assign a run of sample slots along one axis.

    `slots` names the slots explicitly and wins over `first_slot`; otherwise the
    run is `first_slot, first_slot + 1, ...` in travel order. A reversed drag is
    honoured rather than normalised -- that is how a mirrored panel is entered.
    """

    start: tuple[int, int]
    direction: Direction
    count: int
    first_slot: int = 1
    slots: list[int] | None = None
    replicate: int = 1
    dilution: int = 0


@dataclass(frozen=True)
class SeriesFillSpec:
    """Extend each source spot into a dilution series.

    `levels` counts the source, so levels=3 adds two more spots per source.
    """

    sources: list[tuple[int, int]]
    direction: Direction
    spacing: int = 1
    levels: int = 3


@dataclass(frozen=True)
class Plan:
    writes: list[tuple[int, int, Cell]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.writes)

    @property
    def cells(self) -> tuple[tuple[int, int], ...]:
        return tuple((r, c) for r, c, _ in self.writes)


def _in_bounds(plate: PlateSlot, r: int, c: int) -> bool:
    return 0 <= r < plate.rows and 0 <= c < plate.cols


# ---------------------------------------------------------------------------


def plan_run_fill(t: Template, plate_id: str, spec: RunFillSpec) -> Plan:
    plate = t.plate(plate_id)
    dr, dc = DELTA[spec.direction]
    slots = (
        list(spec.slots)
        if spec.slots is not None
        else [spec.first_slot + i for i in range(spec.count)]
    )
    count = min(spec.count, len(slots))

    writes: list[tuple[int, int, Cell]] = []
    off_grid = 0
    replaced: list[tuple[int, int]] = []
    r0, c0 = spec.start

    for i in range(count):
        r, c = r0 + dr * i, c0 + dc * i
        if not _in_bounds(plate, r, c):
            off_grid += 1
            continue
        if plate.get(r, c).kind is CellKind.SPOT:
            replaced.append((r, c))
        writes.append((r, c, Cell.spot(slots[i], spec.replicate, spec.dilution)))

    issues: list[Issue] = []
    if off_grid:
        issues.append(
            Issue(
                Severity.WARNING,
                "fill_runs_off_grid",
                f"{off_grid} of {count} cells fall outside the "
                f"{plate.rows}x{plate.cols} grid and were not placed",
                plate_id,
            )
        )
    if replaced:
        issues.append(
            Issue(
                Severity.WARNING,
                "fill_replaces_spots",
                f"{len(replaced)} existing spot(s) will be replaced",
                plate_id,
                tuple(replaced),
            )
        )
    return Plan(writes, issues)


def plan_series_fill(t: Template, plate_id: str, spec: SeriesFillSpec) -> Plan:
    plate = t.plate(plate_id)
    dr, dc = DELTA[spec.direction]
    step = (dr * spec.spacing, dc * spec.spacing)

    writes: list[tuple[int, int, Cell]] = []
    issues: list[Issue] = []

    for r0, c0 in spec.sources:
        if not _in_bounds(plate, r0, c0):
            continue
        source = plate.get(r0, c0)
        if source.placement is None:
            issues.append(
                Issue(
                    Severity.WARNING,
                    "series_source_not_a_spot",
                    f"{cell_ref(r0, c0)} holds no spot, so there is nothing to "
                    f"dilute from",
                    plate_id,
                    ((r0, c0),),
                )
            )
            continue

        p = source.placement
        for k in range(1, spec.levels):
            r, c = r0 + step[0] * k, c0 + step[1] * k
            if not _in_bounds(plate, r, c):
                issues.append(
                    Issue(
                        Severity.WARNING,
                        "series_target_off_grid",
                        f"the series from {cell_ref(r0, c0)} runs off the grid at "
                        f"level {p.dilution + k}",
                        plate_id,
                        ((r0, c0),),
                    )
                )
                break
            if plate.get(r, c).kind is CellKind.SPOT:
                issues.append(
                    Issue(
                        Severity.WARNING,
                        "series_target_occupied",
                        f"{cell_ref(r, c)} already holds a spot, so level "
                        f"{p.dilution + k} from {cell_ref(r0, c0)} was not placed",
                        plate_id,
                        ((r, c),),
                    )
                )
                continue
            writes.append(
                (r, c, Cell.spot(p.sample_slot, p.replicate, p.dilution + k))
            )

    return Plan(writes, issues)


def plan_duplicate_plate(
    t: Template, src_id: str, dst_id: str, replicate_offset: int
) -> Plan:
    """Copy a plate's geometry, shifting every replicate number.

    With `replicate_offset=2` this is exactly how plate 2 of the lab standard
    (replicates 3 and 4) is built from plate 1 (replicates 1 and 2).
    """
    src, dst = t.plate(src_id), t.plate(dst_id)
    if (src.rows, src.cols) != (dst.rows, dst.cols):
        return Plan(
            [],
            [
                Issue(
                    Severity.ERROR,
                    "duplicate_plate_shape_mismatch",
                    f"plate {src_id!r} is {src.rows}x{src.cols} but plate "
                    f"{dst_id!r} is {dst.rows}x{dst.cols}",
                    dst_id,
                )
            ],
        )

    writes: list[tuple[int, int, Cell]] = []
    for r, row in enumerate(src.cells):
        for c, cell in enumerate(row):
            if cell.placement is None:
                writes.append((r, c, cell))  # carry EMPTY / UNASSIGNED across
            else:
                p = cell.placement
                writes.append(
                    (
                        r,
                        c,
                        Cell.spot(
                            p.sample_slot, p.replicate + replicate_offset, p.dilution
                        ),
                    )
                )
    return Plan(writes, [])


def plan_paint(
    t: Template, plate_id: str, cells: list[tuple[int, int]], kind: CellKind
) -> Plan:
    """Mark cells empty or clear them back to unassigned."""
    plate = t.plate(plate_id)
    target = EMPTY if kind is CellKind.EMPTY else UNASSIGNED
    writes = [
        (r, c, target) for r, c in cells if _in_bounds(plate, r, c)
    ]
    return Plan(writes, [])


def apply_plan(plate: PlateSlot, plan: Plan) -> None:
    for r, c, cell in plan.writes:
        plate.set(r, c, cell)
