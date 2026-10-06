"""Which photographs an experiment has, and what was spotted in each grid cell.

Headless and free of the pipeline: everything here is read off the experiment,
its resolved photo folder and its plate template, through the experiment
layer's own functions. Nothing is parsed out of a filename and nothing is
re-derived, so the photos listed are exactly the photos a run would measure,
and each cell's strain, replicate and dilution are exactly what the run would
call them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from experiments import geometry
from experiments.intake import Resolution
from experiments.model import Experiment


@dataclass(frozen=True)
class Photo:
    """One photograph the run would measure, and what it is a photograph of."""

    relpath: str
    path: Path
    condition: str
    condition_label: str
    timepoint: "float | None"
    timepoint_label: str
    plate: int
    #: Which re-shot of this plate at this sitting -- 1 unless it was
    #: photographed more than once.
    shot: int
    #: The strain group the photo belongs to, when the experiment has several.
    group: "str | None" = None
    cloud_only: bool = False

    @property
    def when(self) -> str:
        if self.timepoint_label:
            return self.timepoint_label
        if self.timepoint is not None:
            return f"{self.timepoint:g} h"
        return ""

    @property
    def title(self) -> str:
        """`GLU · 16 Hours · plate 1 · shot 2`, for a caption."""
        bits = [self.condition_label or self.condition]
        if self.when:
            bits.append(self.when)
        bits.append(f"plate {self.plate}")
        if self.shot > 1:
            bits.append(f"shot {self.shot}")
        if self.group:
            bits.insert(0, self.group)
        return " · ".join(bits)


@dataclass(frozen=True)
class Cell:
    """What was spotted at one cell of the photographed grid."""

    row: int                 # 0-based, photograph grid
    col: int
    slot: int                # 1-based sample slot
    replicate: int           # 1-based biological replicate
    level: int               # 0-based dilution level
    level_label: str
    strain: "str | None"
    is_control: bool

    @property
    def label(self) -> str:
        who = self.strain or f"slot {self.slot} (empty)"
        ctrl = " (control)" if self.is_control else ""
        return f"{who}{ctrl} · rep {self.replicate} · {self.level_label}"


def _timepoint_key(value) -> float:
    return math.inf if value is None else float(value)


def photos(e: Experiment, res: Resolution) -> list[Photo]:
    """Every photo a run of `e` would measure, in reading order.

    Usable (resolved, not ignored) and of a declared condition -- the same
    filter `experiments.run` applies. Ordered by condition as the experiment
    lists them, then time, plate and re-shot, which is how the plates were
    photographed and so how somebody would expect to walk through them.
    """
    order = {code: i for i, code in enumerate(e.condition_codes())}
    root = Path(e.photo_root)
    out = []
    for row in res.usable():
        if row.condition not in order or row.plate is None:
            continue
        try:
            label = e.condition(row.condition).display()
        except KeyError:
            label = row.condition
        out.append(Photo(
            relpath=row.relpath, path=root / row.relpath,
            condition=row.condition, condition_label=label,
            timepoint=row.timepoint, timepoint_label=row.timepoint_label,
            plate=int(row.plate), shot=int(row.shot or 1),
            group=(row.set_key if e.strain_groups else None),
            cloud_only=bool(row.cloud_only)))
    out.sort(key=lambda p: (p.group or "", order[p.condition],
                            _timepoint_key(p.timepoint), p.plate, p.shot,
                            p.relpath.lower()))
    return out


def panel_for(e: Experiment, photo: Photo) -> Experiment:
    """The single-panel view of `e` that `photo` belongs to.

    With strain groups each group has its own strains and control; a photo is
    named and normalised against its own group's, never the first group's.
    """
    if e.strain_groups and photo.group in e.strain_groups:
        return e.for_group(photo.group)
    return e


def grid_shape(e: Experiment, template) -> tuple[int, int]:
    """(rows, cols) of the lattice as it appears in the photographs."""
    return geometry.oriented_grid_shape(template, e.photo_top)


def cells(e: Experiment, template, photo: Photo) -> dict[tuple[int, int], Cell]:
    """{(row, col): Cell} for every spot the template places on this plate.

    In photograph coordinates, so they index the measured arrays directly
    whichever way the camera was turned. Cells the template leaves empty are
    absent; a slot with no strain named is present, with `strain` None.
    """
    if template is None:
        return {}
    panel = panel_for(e, photo)
    control = panel.control_for(photo.condition)
    out = {}
    for level in range(geometry.level_count(template)):
        label = geometry.level_label(template, level)
        for r, c, placement in geometry.oriented_cells_for(
                template, str(photo.plate), level, e.photo_top):
            out.setdefault((r, c), Cell(
                row=r, col=c, slot=placement.sample_slot,
                replicate=placement.replicate, level=level, level_label=label,
                strain=panel.strain(placement.sample_slot),
                is_control=placement.sample_slot == control))
    return out


def siblings(cell: Cell, cell_map: dict) -> list[Cell]:
    """The same strain at the same dilution elsewhere on this plate.

    Its other biological replicates: the comparison an outlier is judged by.
    """
    return sorted((c for c in cell_map.values()
                   if c.slot == cell.slot and c.level == cell.level
                   and (c.row, c.col) != (cell.row, cell.col)),
                  key=lambda c: (c.replicate, c.row, c.col))
