"""Data model for a spotting-assay plate template.

A template describes the *geometry* of an assay and nothing else: how big the
grid is, which cells hold which sample slot, biological replicate and dilution
level, and which sample slot acts as the control on each plate.

It is deliberately strain-agnostic. A `sample_slot` is an abstract 1-based index
meaning "the Nth sample in the panel"; binding those slots to actual strain
names belongs to a separate layer, so one template can serve many experiments
that follow the same protocol with different strains.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator

SCHEMA_VERSION = 1
KIND = "spotting_plate_template"

# s<slot>r<replicate>d<dilution>, e.g. "s1r1d0". Anchored and case-sensitive so
# a near-miss is an error rather than a silent misread.
TOKEN_RE = re.compile(r"^s(\d+)r(\d+)d(\d+)$")

EMPTY_TOKEN = "."
UNASSIGNED_TOKEN = "?"


class TokenError(ValueError):
    """A cell token could not be parsed."""


def cell_ref(r: int, c: int) -> str:
    """Human-facing 1-based cell reference for a 0-based (row, col)."""
    return f"r{r + 1}c{c + 1}"


class CellKind(Enum):
    #: No decision has been made about this cell yet.
    UNASSIGNED = "?"
    #: A positive claim that nothing was spotted here.
    EMPTY = "."
    #: Carries a Placement.
    SPOT = "spot"


@dataclass(frozen=True, slots=True)
class Placement:
    """What was spotted in one cell.

    `sample_slot` and `replicate` are 1-based, matching the pipeline's existing
    `strain_col` / `rep1..rep4` conventions. `dilution` is 0-based, where 0 is
    the least dilute spot.
    """

    sample_slot: int
    replicate: int
    dilution: int

    def token(self) -> str:
        return f"s{self.sample_slot}r{self.replicate}d{self.dilution}"

    @classmethod
    def parse(cls, tok: str) -> "Placement":
        m = TOKEN_RE.match(tok)
        if m is None:
            raise TokenError(
                f"{tok!r} is not a spot token; expected s<slot>r<rep>d<dilution> "
                f"such as 's1r1d0', or {EMPTY_TOKEN!r} for empty, "
                f"or {UNASSIGNED_TOKEN!r} for unassigned"
            )
        slot, rep, dil = (int(g) for g in m.groups())
        if slot < 1:
            raise TokenError(f"{tok!r}: sample slot is 1-based, got {slot}")
        if rep < 1:
            raise TokenError(f"{tok!r}: replicate is 1-based, got {rep}")
        return cls(slot, rep, dil)


@dataclass(frozen=True, slots=True)
class Cell:
    kind: CellKind
    placement: Placement | None = None

    @property
    def token(self) -> str:
        if self.placement is not None:
            return self.placement.token()
        return self.kind.value

    @classmethod
    def from_token(cls, tok: str) -> "Cell":
        if tok == UNASSIGNED_TOKEN:
            return UNASSIGNED
        if tok == EMPTY_TOKEN:
            return EMPTY
        return cls(CellKind.SPOT, Placement.parse(tok))

    @classmethod
    def spot(cls, sample_slot: int, replicate: int, dilution: int) -> "Cell":
        return cls(CellKind.SPOT, Placement(sample_slot, replicate, dilution))


UNASSIGNED = Cell(CellKind.UNASSIGNED)
EMPTY = Cell(CellKind.EMPTY)


@dataclass
class DilutionSpec:
    """How many dilution levels the design uses, and optional metadata.

    `levels` is the only thing the designer needs. `fold` and `labels` are
    carried for downstream figures; nothing here requires them.
    """

    levels: int
    fold: float | None = None
    labels: list[str] = field(default_factory=list)

    def label(self, dilution: int) -> str:
        if 0 <= dilution < len(self.labels):
            return self.labels[dilution]
        return f"d{dilution}"

    def factor(self, dilution: int) -> float | None:
        return None if self.fold is None else self.fold**dilution


@dataclass
class PlateSlot:
    """One physical plate in the design, bound to a photograph at analysis time."""

    id: str
    label: str = ""
    control_slot: int | None = None
    cells: list[list[Cell]] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return len(self.cells)

    @property
    def cols(self) -> int:
        return len(self.cells[0]) if self.cells else 0

    def get(self, r: int, c: int) -> Cell:
        return self.cells[r][c]

    def set(self, r: int, c: int, cell: Cell) -> None:
        self.cells[r][c] = cell

    def placements(self) -> Iterator[tuple[int, int, Placement]]:
        for r, row in enumerate(self.cells):
            for c, cell in enumerate(row):
                if cell.placement is not None:
                    yield r, c, cell.placement

    def cells_for(self, dilution: int) -> list[tuple[int, int, Placement]]:
        return [t for t in self.placements() if t[2].dilution == dilution]

    def cells_for_slot(self, slot: int) -> list[tuple[int, int, Placement]]:
        return [t for t in self.placements() if t[2].sample_slot == slot]

    def slots_present(self) -> set[int]:
        return {p.sample_slot for _, _, p in self.placements()}

    def replicates_present(self) -> set[int]:
        return {p.replicate for _, _, p in self.placements()}

    def units(self) -> set[tuple[int, int]]:
        """The (sample_slot, replicate) pairs spotted on this plate."""
        return {(p.sample_slot, p.replicate) for _, _, p in self.placements()}

    def scorable_dilutions(self) -> set[int]:
        """Dilution levels at which this plate could actually be quantified.

        The pipeline scores one dilution level per plate and relativises every
        spot to the control on that same plate, so a level is only usable when
        every unit present on the plate has a spot at it and the control is
        among them.
        """
        if self.control_slot is None:
            return set()
        all_units = self.units()
        by_dilution: dict[int, set[tuple[int, int]]] = {}
        for _, _, p in self.placements():
            by_dilution.setdefault(p.dilution, set()).add((p.sample_slot, p.replicate))
        return {
            d
            for d, present in by_dilution.items()
            if present == all_units
            and any(slot == self.control_slot for slot, _ in present)
        }


@dataclass
class Template:
    name: str = "Untitled"
    rows: int = 6
    cols: int = 8
    dilution: DilutionSpec = field(default_factory=lambda: DilutionSpec(levels=3))
    plates: list[PlateSlot] = field(default_factory=list)
    id: str = ""
    revision: int = 1
    description: str = ""
    created: str = ""
    schema_version: int = SCHEMA_VERSION

    def plate(self, plate_id: str) -> PlateSlot:
        for p in self.plates:
            if p.id == plate_id:
                return p
        raise KeyError(f"no plate slot with id {plate_id!r}")

    def has_plate(self, plate_id: str) -> bool:
        return any(p.id == plate_id for p in self.plates)

    def all_placements(self) -> Iterator[tuple[PlateSlot, int, int, Placement]]:
        for plate in self.plates:
            for r, c, p in plate.placements():
                yield plate, r, c, p

    def slots_present(self) -> set[int]:
        return {p.sample_slot for _, _, _, p in self.all_placements()}

    def sample_slots(self) -> int:
        """Panel size, derived from what has been placed -- never declared."""
        slots = self.slots_present()
        return max(slots) if slots else 0

    def replicates(self) -> set[int]:
        return {p.replicate for _, _, _, p in self.all_placements()}

    def spot_count(self) -> int:
        return sum(1 for _ in self.all_placements())

    def control_cells(
        self, plate_id: str, dilution: int, control_slot: int | None = None
    ) -> list[tuple[int, int, Placement]]:
        """Control spots on one plate at one dilution.

        `control_slot` overrides the plate's own designation; that parameter is
        the seam where a per-treatment override plugs in without the template
        needing to know treatments exist.
        """
        plate = self.plate(plate_id)
        slot = control_slot if control_slot is not None else plate.control_slot
        if slot is None:
            return []
        return [t for t in plate.cells_for(dilution) if t[2].sample_slot == slot]

    def resize(self, rows: int, cols: int) -> None:
        """Change the grid on every plate, keeping the overlapping region."""
        for plate in self.plates:
            grid = [[UNASSIGNED] * cols for _ in range(rows)]
            for r in range(min(rows, plate.rows)):
                for c in range(min(cols, plate.cols)):
                    grid[r][c] = plate.cells[r][c]
            plate.cells = grid
        self.rows, self.cols = rows, cols

    def add_plate(
        self, plate_id: str, label: str = "", control_slot: int | None = None
    ) -> PlateSlot:
        if control_slot is None and self.plates:
            control_slot = self.plates[0].control_slot
        plate = PlateSlot(
            id=plate_id,
            label=label or f"Plate {plate_id}",
            control_slot=control_slot,
            cells=[[UNASSIGNED] * self.cols for _ in range(self.rows)],
        )
        self.plates.append(plate)
        return plate
