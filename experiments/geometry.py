"""Reading the plate template's geometry: dilution levels, rows, replicates.

The pipeline has always assumed one shape -- three dilution levels spotted twice
down a six-row plate, so that rows 1 and 4 are the least dilute, 2 and 5 the
middle, 3 and 6 the most:

    spotting_batch.DILUTIONS = {"least": (0, 3), "middle": (1, 4), "most": (2, 5)}

That is one valid way to fill a 6x8 grid, not the only one. The same grid holds
six dilution levels with a single replicate per plate, or two levels with three,
and the plate designer will happily draw any of them -- a `Placement` already
carries its own dilution index and replicate number.

So this module answers the questions the pipeline used to answer with
arithmetic, by reading the template instead:

    which rows hold dilution level d on plate P
    which sample slot and replicate a given cell belongs to
    how many levels there are, and what to call them

The grid itself stays 6x8, because `spotting_quant.detect_grid` fits exactly
that lattice. Supporting more DILUTION LEVELS needs none of that to change --
only the interpretation of the rows, which is what lives here.

Levels are identified by their 0-based index, matching `Placement.dilution`.
Names are for display only: a template may name three levels or none, and a
six-level design has no natural vocabulary at all.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

#: What the lab has always called its three levels, least dilute first. Used
#: only to read back a choice stored before levels were indexed, and by
#: `spotting_config.json`, which spells them out.
CLASSIC_LABELS = ("least", "middle", "most")

#: The lattice the engine looks for when nothing says otherwise -- the lab's own
#: 8 columns by 6 rows. It is a DEFAULT, not a requirement: `detect_grid` takes
#: the grid size as an argument and derives the expected spot pitch from it, so
#: a template of another size is measured at that size.
#:
#: Re-declared rather than imported so this stays usable without numpy, the same
#: convention `results_review/discovery.py` follows;
#: `tests/experiments/test_bridge_matches_pipeline.py` asserts they agree.
DEFAULT_ROWS, DEFAULT_COLS = 6, 8

#: Below this the lattice fit has nothing to work with, and
#: `background_gap_centers` has no gap between diagonally adjacent spots to read
#: the agar from.
MIN_ROWS = MIN_COLS = 2


def level_count(template) -> int:
    return template.dilution.levels if template is not None else 0


def level_label(template, index) -> str:
    """A name for one level: the template's own, or a plain ordinal.

    `DilutionSpec.label` falls back to "d0", which reads like a variable rather
    than a dilution; an unnamed level is called "level 1" instead.

    Accepts a stored name as well as an index, so callers holding a value
    straight out of a file do not have to resolve it first.
    """
    index = index if isinstance(index, int) else resolve_level(template, index)
    if index is None:
        return "-"
    if template is not None:
        labels = template.dilution.labels
        if 0 <= index < len(labels) and str(labels[index]).strip():
            return str(labels[index]).strip()
    return f"level {index + 1}"


def level_display(template, index) -> str:
    """How a level is offered in the window: always numbered, named when named."""
    index = index if isinstance(index, int) else resolve_level(template, index)
    if index is None:
        return "-"
    label = level_label(template, index)
    ordinal = f"{index + 1}"
    return ordinal if label == f"level {index + 1}" else f"{ordinal} - {label}"


def levels(template) -> list[tuple[int, str]]:
    """Every dilution level the template declares, as (index, display name)."""
    return [(i, level_display(template, i)) for i in range(level_count(template))]


def resolve_level(template, value) -> int | None:
    """A stored dilution choice as a level index, whatever shape it was saved in.

    Accepts the index itself, and the level NAMES written by older files and by
    `spotting_config.json` ("middle"). Names are matched against the template's
    own labels first, then against the lab's classic three, so a template that
    renames its levels still reads its own files.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Out-of-range indices come back as themselves rather than as None, so
        # the caller can say "level 7, but the template declares 3" instead of
        # the far less useful "no level chosen". `is_valid_level` is the range
        # check.
        return value if value >= 0 else None
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    lowered = text.lower()
    if template is not None:
        for i, label in enumerate(template.dilution.labels):
            if str(label).strip().lower() == lowered:
                return i
    if lowered in CLASSIC_LABELS:
        return CLASSIC_LABELS.index(lowered)
    return None


def is_valid_level(template, index: int | None) -> bool:
    return index is not None and 0 <= index < level_count(template)


# ---------------------------------------------------------------------------
# Where a level lives on a plate
# ---------------------------------------------------------------------------


def _plate(template, plate_id):
    if template is None:
        return None
    for plate in template.plates:
        if str(plate.id) == str(plate_id):
            return plate
    return None


def cells_for(template, plate_id, index: int) -> list[tuple[int, int, object]]:
    """Every spot of one dilution level on one plate, as (row, col, Placement).

    Rows and columns are 0-based, matching the measured arrays.
    """
    plate = _plate(template, plate_id)
    if plate is None:
        return []
    return sorted(plate.cells_for(index), key=lambda t: (t[0], t[1]))


def oriented_cell(row: int, col: int, rows: int, cols: int,
                  experiment_top: str = "top") -> tuple[int, int]:
    """Map a template cell to its row/column in the photograph.

    ``experiment_top`` names the photograph edge toward which the template's
    top points.  The mapping is the same as rotating the template, while the
    original photograph remains untouched.
    """
    if experiment_top == "top":
        return row, col
    if experiment_top == "right":
        return col, rows - 1 - row
    if experiment_top == "bottom":
        return rows - 1 - row, cols - 1 - col
    if experiment_top == "left":
        return cols - 1 - col, row
    raise ValueError(f"unknown experiment top {experiment_top!r}")


def oriented_cells_for(template, plate_id, index: int,
                       experiment_top: str = "top") -> list[tuple[int, int, object]]:
    """A level's cells in photograph coordinates."""
    rows, cols = grid_shape(template)
    out = [
        (*oriented_cell(row, col, rows, cols, experiment_top), placement)
        for row, col, placement in cells_for(template, plate_id, index)
    ]
    return sorted(out, key=lambda item: (item[0], item[1]))


def rows_for(template, plate_id, index: int) -> tuple[int, ...]:
    """The 1-based grid rows holding one dilution level on one plate.

    This is what `spotting_quant` sizes its measuring ROI to fit, so it is the
    direct replacement for `spotting_batch.DILUTIONS[choice]` -- except that it
    is read off the design rather than assumed, and may be any number of rows.
    """
    return tuple(sorted({r + 1 for r, _, _ in cells_for(template, plate_id, index)}))


def oriented_rows_for(template, plate_id, index: int,
                      experiment_top: str = "top") -> tuple[int, ...]:
    """The 1-based photograph rows occupied by a dilution level."""
    return tuple(sorted({
        row + 1 for row, _, _ in
        oriented_cells_for(template, plate_id, index, experiment_top)
    }))


def scorable_levels(template, plate_id) -> list[int]:
    """Levels that can actually be quantified on this plate.

    `PlateSlot.scorable_dilutions` already encodes the rule: a level is usable
    only when every unit on the plate has a spot at it and the control is among
    them, because each spot is relativised to a control on its own plate.
    """
    plate = _plate(template, plate_id)
    return sorted(plate.scorable_dilutions()) if plate is not None else []


def plate_ids(template) -> list[str]:
    if template is None:
        return []
    return [str(p.id) for p in template.plates]


def grid_shape(template) -> tuple[int, int]:
    """(rows, cols) of the lattice to look for on a photograph of this design."""
    if template is None:
        return (DEFAULT_ROWS, DEFAULT_COLS)
    return (int(template.rows), int(template.cols))


def oriented_grid_shape(template, experiment_top: str = "top") -> tuple[int, int]:
    """Lattice shape as it appears in the photograph."""
    rows, cols = grid_shape(template)
    return ((cols, rows) if experiment_top in ("left", "right")
            else (rows, cols))


def grid_supported(template) -> bool:
    """Whether the engine can find this template's lattice.

    Any size is measurable -- the expected spot pitch is derived from the grid,
    so a 12x16 design is looked for at a 12x16 pitch. The only real floor is
    that a lattice needs at least two rows and two columns: fewer gives the fit
    nothing to work with and leaves no agar gap between diagonally adjacent
    spots for the background reads.
    """
    if template is None:
        return False
    rows, cols = grid_shape(template)
    return rows >= MIN_ROWS and cols >= MIN_COLS
