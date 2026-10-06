"""Starting points for a new template."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Callable

from .autofill import (
    RunFillSpec,
    SeriesFillSpec,
    apply_plan,
    plan_duplicate_plate,
    plan_run_fill,
    plan_series_fill,
)
from .model import DilutionSpec, Template

# Fixed so the shipped preset serialises byte-for-byte identically every time;
# it doubles as the golden file the schema tests lock the format against.
LAB_STANDARD_ID = "9f2c3a1e-7d44-4b8a-8c31-6e0f5a2b1d90"
LAB_STANDARD_CREATED = "2026-09-11"


def blank(
    rows: int = 6,
    cols: int = 8,
    levels: int = 3,
    name: str = "Untitled",
    plates: int = 1,
) -> Template:
    t = Template(
        name=name,
        rows=rows,
        cols=cols,
        dilution=DilutionSpec(levels=levels),
        id=str(uuid.uuid4()),
        created=date.today().isoformat(),
    )
    for i in range(1, plates + 1):
        t.add_plate(str(i), f"Plate {i}")
    return t


def lab_standard_8x6() -> Template:
    """The layout this pipeline was hardcoded to before templates existed.

    Reproduces `src/spotting_batch.py`'s arithmetic exactly: sample slot is the
    column, replicate is `(plate - 1) * 2 + 1 + row // 3`, dilution is `row % 3`.

    Built through the autofill primitives rather than by hand, so the golden
    file also exercises run fill, series fill and plate duplication.
    """
    t = Template(
        name="Martin Lab standard 8x6",
        description=(
            "Eight sample slots across, three dilutions down, twice per plate. "
            "Plate 1 carries biological replicates 1 and 2, plate 2 carries 3 and 4."
        ),
        rows=6,
        cols=8,
        dilution=DilutionSpec(levels=3, fold=10, labels=["least", "middle", "most"]),
        id=LAB_STANDARD_ID,
        created=LAB_STANDARD_CREATED,
    )

    first = t.add_plate("1", "Plate 1 (reps 1+2)", control_slot=1)
    for replicate, top_row in ((1, 0), (2, 3)):
        apply_plan(
            first,
            plan_run_fill(
                t,
                "1",
                RunFillSpec(
                    start=(top_row, 0),
                    direction="right",
                    count=t.cols,
                    first_slot=1,
                    replicate=replicate,
                    dilution=0,
                ),
            ),
        )
        apply_plan(
            first,
            plan_series_fill(
                t,
                "1",
                SeriesFillSpec(
                    sources=[(top_row, c) for c in range(t.cols)],
                    direction="down",
                    spacing=1,
                    levels=t.dilution.levels,
                ),
            ),
        )

    second = t.add_plate("2", "Plate 2 (reps 3+4)", control_slot=1)
    apply_plan(second, plan_duplicate_plate(t, "1", "2", replicate_offset=2))
    return t


def list_presets() -> list[tuple[str, str, Callable[[], Template]]]:
    return [
        ("lab8x6", "Martin Lab standard 8x6", lab_standard_8x6),
        ("blank", "Blank 8x6, one plate", blank),
    ]
