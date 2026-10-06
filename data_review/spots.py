"""Where detection put the spots on a photograph, and what it measured there.

Read straight out of the measurement cache (`.spotting_cache/`), which is where
the pipeline keeps every photo's detected grid. Nothing is detected here --
that is `cli detect`'s job, run as a background job -- so opening a photo is a
file read of a few kilobytes, and what is drawn is exactly the grid the run
will measure: same cache entry, same key, same numbers.

The key is `spotting_batch._cache_key` itself, not a copy of it. It depends on
the photo's name, size and modification time and on the lattice being looked
for, so a photo that has been replaced or re-saved since it was detected simply
reads as not detected yet, rather than showing somebody else's grid.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path


def _engine():
    """The pipeline modules, imported when first needed.

    `experiments.run` puts `src/` on the path, so the modules found are the
    same ones the run uses rather than a second copy.
    """
    from experiments import run as bridge  # noqa: F401  (sets up sys.path)
    import spotting_batch as sb
    import spotting_quant as sq

    return sb, sq


def layout_for(e, template):
    """The run's `DilutionLayout` for this experiment (classic without a template)."""
    from experiments import run as bridge

    sb, _ = _engine()
    return bridge.to_layout(template, e.photo_top) or sb.classic_layout()


def cache_dir() -> Path:
    sb, _ = _engine()
    return sb.PROJECT_ROOT / sb.CACHE_DIR


def measure_options(layout):
    """The options a run measures this design with -- which is what keys the cache."""
    sb, sq = _engine()
    return replace(sq.MeasureOptions(), n_rows=layout.n_rows, n_cols=layout.n_cols)


def cache_file(path: Path, layout) -> "Path | None":
    """This photo's cache entry, or None when the photo itself is not there."""
    sb, _ = _engine()
    try:
        return cache_dir() / f"{sb._cache_key(Path(path), measure_options(layout))}.npz"
    except OSError:
        return None


def is_detected(path: Path, layout) -> bool:
    cf = cache_file(path, layout)
    return cf is not None and cf.exists()


@dataclass
class Detected:
    """One photo's detected grid, in full-resolution pixels."""

    #: (rows, cols, 2) -- the (y, x) of every grid position.
    centers: "object"
    plate_center: tuple
    plate_radius: float
    #: {(row, col): ROI radius} -- each cell at its own dilution level's ROI,
    #: the size the run measures it with when that level is scored.
    radius: dict = field(default_factory=dict)
    #: {(row, col): background-subtracted grey value}, at the same ROI.
    net: dict = field(default_factory=dict)
    #: Cells the engine itself flagged: the ROI overlaps the rim or a label.
    rim: set = field(default_factory=set)
    #: The smallest control grey that can serve as a divisor on this plate.
    floor: float = 0.0
    #: True when some cell's own dilution level was not in the cache entry and
    #: its value is from another level's ROI instead.
    approximate: bool = False

    @property
    def shape(self) -> tuple[int, int]:
        return int(self.centers.shape[0]), int(self.centers.shape[1])

    @property
    def pitch(self) -> float:
        """Typical centre-to-centre spacing, in pixels."""
        import numpy as np

        c = self.centers
        steps = []
        if c.shape[1] > 1:
            steps.append(np.hypot(*(c[:, 1:] - c[:, :-1]).reshape(-1, 2).T))
        if c.shape[0] > 1:
            steps.append(np.hypot(*(c[1:, :] - c[:-1, :]).reshape(-1, 2).T))
        if not steps:
            return 2.0 * max(self.radius.values() or [1.0])
        return float(np.median(np.concatenate(steps)))

    def center(self, row: int, col: int) -> tuple[float, float]:
        """(y, x) of one cell, 0-based."""
        y, x = self.centers[row, col]
        return float(y), float(x)

    def nearest(self, y: float, x: float) -> "tuple[int, int] | None":
        """The cell at full-resolution (y, x), or None between the spots."""
        import numpy as np

        d = np.hypot(self.centers[..., 0] - y, self.centers[..., 1] - x)
        i = int(np.argmin(d))
        r, c = divmod(i, self.centers.shape[1])
        return (r, c) if float(d[r, c]) <= 0.5 * self.pitch else None


def load(path: Path, layout, plate: int) -> "Detected | None":
    """The detected grid of one photo of template plate `plate`, or None.

    None means "not detected yet" -- no cache entry for this photo as it now
    is. A readable entry that lacks some dilution level's ROI is still used;
    those cells borrow another level's numbers and `approximate` says so.
    """
    import numpy as np

    sb, sq = _engine()
    cf = cache_file(path, layout)
    if cf is None or not cf.exists():
        return None
    try:
        with np.load(cf) as z:
            data = {k: z[k] for k in z.files}
    except Exception:
        return None
    if "centers" not in data:
        return None

    found = Detected(centers=np.asarray(data["centers"], float),
                     plate_center=tuple(float(v) for v in data.get("plate_center", (0, 0))),
                     plate_radius=float(data.get("plate_radius", 0.0)))
    rowsets = sorted({k[:-len("_net")] for k in data if k.endswith("_net")})
    if not rowsets:
        return None

    def take(key: str, r: int, c: int) -> None:
        found.net[(r, c)] = float(data[f"{key}_net"][r, c])
        found.radius[(r, c)] = float(data[f"{key}_radius"])
        if bool(data[f"{key}_rim"][r, c]):
            found.rim.add((r, c))

    floors = []
    for level in layout.levels:
        try:
            cells = level.cells(plate)
        except KeyError:
            continue                          # this level places nothing here
        key = sb._rowset_key(level.quant_rows(plate))
        if f"{key}_net" not in data:
            key = rowsets[0]
            found.approximate = True
        for cell in cells:
            take(key, cell.row, cell.col)
        floors.append(max(sq.MIN_CONTROL_GRAY,
                          sq.CONTROL_NOISE_MULT * sq.bg_noise(data[f"{key}_bg_samples"])))

    # Grid positions the design leaves empty are still drawn, so the whole
    # lattice the run looked for is visible.
    rows, cols = found.shape
    for r in range(rows):
        for c in range(cols):
            if (r, c) not in found.net:
                take(rowsets[0], r, c)
    found.floor = max(floors) if floors else 0.0
    return found


def control_mean(found: Detected, cell_map: dict, level: int) -> "float | None":
    """Mean grey of the control spots at one dilution level on this plate.

    The divisor the run's normalisation starts from: controls the engine
    flagged as artifacts are left out, as it leaves them out. None when no
    usable control was spotted at this level, or it did not grow above the
    plate's noise floor.
    """
    values = [found.net[(c.row, c.col)] for c in cell_map.values()
              if c.is_control and c.level == level
              and (c.row, c.col) in found.net and (c.row, c.col) not in found.rim]
    if not values:
        return None
    mean = sum(values) / len(values)
    return mean if mean > found.floor else None
