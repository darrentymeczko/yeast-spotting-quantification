"""Checks a template against the things that make a design unanalysable.

Errors mean the template cannot drive an analysis. Warnings mean something
looks like a slip but might be deliberate -- a half-finished design is expected
to carry warnings, which is why saving is never blocked on them.

Validity is derived, never stored: nothing here writes to the template.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .model import CellKind, PlateSlot, Template, cell_ref


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    plate_id: str | None = None
    #: 0-based (row, col) positions, so the editor can highlight them.
    cells: tuple[tuple[int, int], ...] = ()

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


_pos = cell_ref


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def _check_structure(t: Template, out: list[Issue]) -> None:
    if not t.plates:
        out.append(
            Issue(Severity.ERROR, "no_plates", "the template has no plate slots")
        )
        return

    seen: dict[str, int] = {}
    for plate in t.plates:
        seen[plate.id] = seen.get(plate.id, 0) + 1
    for plate_id, count in seen.items():
        if count > 1:
            out.append(
                Issue(
                    Severity.ERROR,
                    "plate_ids_not_unique",
                    f"{count} plate slots share the id {plate_id!r}; "
                    f"ids must be unique so photos can be bound to them",
                    plate_id,
                )
            )

    if t.spot_count() == 0:
        out.append(
            Issue(
                Severity.ERROR,
                "no_placements",
                "no spots have been placed yet",
            )
        )

    for plate in t.plates:
        if plate.rows != t.rows or any(len(row) != t.cols for row in plate.cells):
            shape = f"{plate.rows}x{plate.cols}"
            out.append(
                Issue(
                    Severity.ERROR,
                    "ragged_grid",
                    f"plate {plate.id!r} is {shape} but the template grid is "
                    f"{t.rows}x{t.cols}",
                    plate.id,
                )
            )

        for r, c, p in plate.placements():
            if p.sample_slot < 1:
                out.append(
                    Issue(
                        Severity.ERROR,
                        "slot_out_of_range",
                        f"{_pos(r, c)}: sample slot {p.sample_slot} is not 1-based",
                        plate.id,
                        ((r, c),),
                    )
                )
            if p.replicate < 1:
                out.append(
                    Issue(
                        Severity.ERROR,
                        "rep_out_of_range",
                        f"{_pos(r, c)}: replicate {p.replicate} is not 1-based",
                        plate.id,
                        ((r, c),),
                    )
                )
            if not 0 <= p.dilution < t.dilution.levels:
                out.append(
                    Issue(
                        Severity.ERROR,
                        "dil_out_of_range",
                        f"{_pos(r, c)}: dilution {p.dilution} is outside "
                        f"0..{t.dilution.levels - 1}",
                        plate.id,
                        ((r, c),),
                    )
                )


# ---------------------------------------------------------------------------
# Controls -- normalisation happens within a plate
# ---------------------------------------------------------------------------


def _check_controls(t: Template, out: list[Issue]) -> None:
    for plate in t.plates:
        if plate.control_slot is None:
            out.append(
                Issue(
                    Severity.ERROR,
                    "no_control_designated",
                    f"plate {plate.id!r} has no control sample slot designated",
                    plate.id,
                )
            )
            continue

        control_cells = plate.cells_for_slot(plate.control_slot)
        if not control_cells:
            out.append(
                Issue(
                    Severity.ERROR,
                    "no_control_on_plate",
                    f"plate {plate.id!r} designates slot {plate.control_slot} as its "
                    f"control but has no spots for it; every strain is relativised "
                    f"to a control on its own plate",
                    plate.id,
                )
            )
            continue

        control_dilutions = {p.dilution for _, _, p in control_cells}
        present = {p.dilution for _, _, p in plate.placements()}
        missing = sorted(present - control_dilutions)
        if missing:
            names = ", ".join(t.dilution.label(d) for d in missing)
            out.append(
                Issue(
                    Severity.WARNING,
                    "control_missing_dilution",
                    f"plate {plate.id!r}: the control has no spot at dilution "
                    f"{names}, so that level cannot be scored on this plate",
                    plate.id,
                )
            )

    designated = {p.control_slot for p in t.plates if p.control_slot is not None}
    if len(designated) > 1:
        out.append(
            Issue(
                Severity.WARNING,
                "control_differs_between_plates",
                f"plates designate different control slots ("
                f"{', '.join(str(s) for s in sorted(designated))}); within one "
                f"template that is usually an editing slip",
            )
        )


# ---------------------------------------------------------------------------
# Duplicates -- the quiet killer
# ---------------------------------------------------------------------------


def _check_duplicates(t: Template, out: list[Issue]) -> None:
    seen: dict[tuple[int, int, int], list[tuple[str, int, int]]] = {}
    for plate, r, c, p in t.all_placements():
        key = (p.sample_slot, p.replicate, p.dilution)
        seen.setdefault(key, []).append((plate.id, r, c))

    for (slot, rep, dil), places in seen.items():
        if len(places) < 2:
            continue
        where = ", ".join(f"plate {pid} {_pos(r, c)}" for pid, r, c in places)
        out.append(
            Issue(
                Severity.ERROR,
                "duplicate_placement",
                f"slot {slot} replicate {rep} dilution {dil} is placed "
                f"{len(places)} times ({where}); it would be counted more than "
                f"once in the mean",
                places[0][0],
                tuple((r, c) for _, r, c in places),
            )
        )


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _check_coverage(t: Template, out: list[Issue]) -> None:
    for plate in t.plates:
        unassigned = tuple(
            (r, c)
            for r, row in enumerate(plate.cells)
            for c, cell in enumerate(row)
            if cell.kind is CellKind.UNASSIGNED
        )
        if unassigned:
            n = len(unassigned)
            out.append(
                Issue(
                    Severity.WARNING,
                    "unassigned_cells",
                    f"plate {plate.id!r} has {n} undecided "
                    f"{_plural(n, 'cell')}; mark them empty once you are sure",
                    plate.id,
                    unassigned,
                )
            )

        expected = set(range(t.dilution.levels))
        units: dict[tuple[int, int], set[int]] = {}
        for _, _, p in plate.placements():
            units.setdefault((p.sample_slot, p.replicate), set()).add(p.dilution)
        for (slot, rep), dilutions in sorted(units.items()):
            missing = sorted(expected - dilutions)
            if missing:
                names = ", ".join(t.dilution.label(d) for d in missing)
                out.append(
                    Issue(
                        Severity.WARNING,
                        "incomplete_series",
                        f"plate {plate.id!r}: slot {slot} replicate {rep} has no "
                        f"spot at dilution {names}",
                        plate.id,
                        tuple(
                            (r, c)
                            for r, c, p in plate.placements()
                            if (p.sample_slot, p.replicate) == (slot, rep)
                        ),
                    )
                )

    # A unit whose series straddles plates gets relativised to two different
    # controls. Legitimate for a series too long for one grid, so only a warning.
    unit_plates: dict[tuple[int, int], set[str]] = {}
    for plate, _, _, p in t.all_placements():
        unit_plates.setdefault((p.sample_slot, p.replicate), set()).add(plate.id)
    for (slot, rep), plate_ids in sorted(unit_plates.items()):
        if len(plate_ids) > 1:
            out.append(
                Issue(
                    Severity.WARNING,
                    "replicate_split_across_plates",
                    f"slot {slot} replicate {rep} is spread over plates "
                    f"{', '.join(sorted(plate_ids))}; normalisation is per-plate",
                )
            )

    slots = t.slots_present()
    if slots:
        gaps = sorted(set(range(1, max(slots) + 1)) - slots)
        if gaps:
            out.append(
                Issue(
                    Severity.WARNING,
                    "slot_numbering_gap",
                    f"sample {_plural(len(gaps), 'slot')} "
                    f"{', '.join(str(s) for s in gaps)} "
                    f"{_plural(len(gaps), 'is', 'are')} skipped; the panel runs "
                    f"1..{max(slots)}",
                )
            )

    coverage: dict[int, set[int]] = {}
    for _, _, _, p in t.all_placements():
        coverage.setdefault(p.sample_slot, set()).add(p.replicate)
    if len(set(frozenset(v) for v in coverage.values())) > 1:
        counts = ", ".join(
            f"slot {s}: {len(reps)}" for s, reps in sorted(coverage.items())
        )
        out.append(
            Issue(
                Severity.WARNING,
                "ragged_replicate_coverage",
                f"samples do not all have the same replicates ({counts})",
            )
        )


# ---------------------------------------------------------------------------
# Layout hints -- only once a plate has been fully decided
# ---------------------------------------------------------------------------


def _is_decided(plate: PlateSlot) -> bool:
    return all(
        cell.kind is not CellKind.UNASSIGNED for row in plate.cells for cell in row
    )


def _check_layout_hints(t: Template, out: list[Issue]) -> None:
    for plate in t.plates:
        if not _is_decided(plate) or not plate.cells:
            continue
        blank_rows = [
            r
            for r, row in enumerate(plate.cells)
            if all(cell.kind is not CellKind.SPOT for cell in row)
        ]
        blank_cols = [
            c
            for c in range(plate.cols)
            if all(row[c].kind is not CellKind.SPOT for row in plate.cells)
        ]
        if blank_rows or blank_cols:
            parts = []
            if blank_rows:
                parts.append(
                    f"{_plural(len(blank_rows), 'row')} "
                    f"{', '.join(str(r + 1) for r in blank_rows)}"
                )
            if blank_cols:
                parts.append(
                    f"{_plural(len(blank_cols), 'column')} "
                    f"{', '.join(str(c + 1) for c in blank_cols)}"
                )
            out.append(
                Issue(
                    Severity.WARNING,
                    "empty_row_or_col",
                    f"plate {plate.id!r} has no spots in {' or '.join(parts)}; "
                    f"the grid may be larger than the assay",
                    plate.id,
                )
            )


# ---------------------------------------------------------------------------


def _summary(t: Template) -> Issue:
    slots = t.sample_slots()
    reps = len(t.replicates())
    return Issue(
        Severity.INFO,
        "summary",
        f"{slots} sample {_plural(slots, 'slot')}, "
        f"{reps} {_plural(reps, 'replicate')}, "
        f"{t.dilution.levels} dilution {_plural(t.dilution.levels, 'level')}, "
        f"{len(t.plates)} {_plural(len(t.plates), 'plate')}, "
        f"{t.spot_count()} spots",
    )


def validate(t: Template) -> list[Issue]:
    issues: list[Issue] = []
    _check_structure(t, issues)
    _check_controls(t, issues)
    _check_duplicates(t, issues)
    _check_coverage(t, issues)
    _check_layout_hints(t, issues)
    issues.append(_summary(t))
    issues.sort(key=lambda i: (_RANK[i.severity], i.plate_id or "", i.code))
    return issues


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity is Severity.ERROR]
