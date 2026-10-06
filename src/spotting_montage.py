#!/usr/bin/env python3
"""
spotting_montage.py -- publication montages of the spots themselves.

Reproduces the classic figure that accompanies the quantification: the 8-bit,
background-subtracted plate with the agar knocked back to black, cropped to the
spot grid and stacked one biological replicate per block, strains labelled
across the top and replicates down the left.

    Replicate 1 = plate 1, dilution rows 1-3
    Replicate 2 = plate 1, dilution rows 4-6
    Replicate 3 = plate 2, dilution rows 1-3
    Replicate 4 = plate 2, dilution rows 4-6

Two things it does that a manual crop in Fiji cannot:

* Each block is resampled through the affine map fitted to that plate's own 48
  spot centres, so a plate photographed a few degrees off square comes out
  square and every block's columns line up with every other block's.
* All blocks are displayed over the same fixed 0-255 range with no brightness
  or contrast adjustment of any kind, so a spot that looks brighter really is
  brighter, and nothing about how grown a strain appears has been altered.
  Adjust brightness afterwards in an application that records the change.

Usage:
    python spotting_montage.py                 # every treatment-set combination
    python spotting_montage.py --combo 4 K-OAc # just one
    python spotting_montage.py --list
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The repository root: source lives in src/, but the photo folder and
# everything a run writes live beside it, not inside it.
from spotting_paths import PROJECT_ROOT   # noqa: E402
import spotting_quant as sq        # noqa: E402
import spotting_batch as sb        # noqa: E402

# How far past the outermost spot centres to crop, in spot pitches. A spot is
# roughly half a pitch across, so this leaves about a quarter-spot of agar and
# keeps the plate rim -- which sits just beyond the outer rows -- out of frame.
PAD_CELLS = 0.32
# Output resolution: pixels per grid cell (one spot + its share of the gap).
CELL_PX = 170
# The plate rim/meniscus is a bright arc just inside the agar edge. Because the
# spot grid is rectangular and the plate is round, the CORNERS of each block's
# crop reach furthest from the plate centre and are the only place that arc gets
# into frame. Everything past this fraction of the agar radius is masked to the
# display black point, which removes the corner wedges. Measured on set 2 K-OAc:
# flat agar runs to ~0.88R, the arc sits at 0.90-0.98R.
AGAR_KEEP_FRAC = 0.90
# ...except the spot disks themselves, which are never masked: the outermost row
# is pipetted at ~0.91R, so a plain circular cut would slice through real spots.
# Any rim left in frame after this is physically underneath a spot, and those
# spots are already rim-flagged in the quantification.
SPOT_HALO = 1.15
# Colour of the quantified-row outline. Amber reads clearly against both
# the black agar and a bright spot, and is not a colour any spot can be.
MARK_COLOR = "#ffb000"
# Plates whose resampled blocks are kept in memory. A plate is two ~449x1299
# float32 blocks plus their bool masks -- about 6 MB -- so this is a ~190 MB
# ceiling. Sized to hold a whole capture tree (30 photos for Set09) rather than
# just a working set, because that is what makes each photo's background
# subtraction happen exactly ONCE per run instead of once per pairing it
# appears in. Lower it if a much larger tree ever strains memory.
BLOCK_CACHE_SIZE = 32
# Decoded 8-bit photos. 24 MB each as uint8; unlike the blocks these ARE shared
# across dilution choices, so a small ring covers a pairing and its neighbours.
IMG_CACHE_SIZE = 6
_IMG_CACHE: "OrderedDict" = OrderedDict()
_BLOCK_CACHE: "OrderedDict" = OrderedDict()
# Display range. The montage is shown over the full 8-bit scale, exactly as the
# background-subtracted image comes out -- NO automatic brightness or contrast.
# Auto-stretching would silently alter how grown every spot looks, which is not
# permissible in a figure unless the adjustment is stated; adjust brightness
# downstream in an application that records what was done. On the faint media
# (K-OAc, glycerol) this correctly renders the spots dim.
DISPLAY_MIN = 0.0
DISPLAY_MAX = 255.0


def _affine_from_centers(centers: np.ndarray) -> np.ndarray:
    """Least-squares map (col, row, 1) -> (x, y) from all 48 spot centres.

    Fitting the whole grid rather than using row/column positions directly is
    what removes plate tilt: rotation lives in the off-diagonal terms, so
    sampling through this map yields a square block from a crooked photo.
    """
    n_rows, n_cols = centers.shape[:2]
    jj, ii = np.meshgrid(np.arange(n_cols), np.arange(n_rows))
    A = np.column_stack([jj.ravel(), ii.ravel(), np.ones(jj.size)])
    xy = centers.reshape(-1, 2)[:, ::-1]          # centres are (y, x)
    coef, *_ = np.linalg.lstsq(A, xy, rcond=None)  # (3, 2)
    return coef


def _sample_block(img: np.ndarray, coef: np.ndarray, row0: int,
                  n_rows: int, n_cols: int) -> np.ndarray:
    """Resample one replicate block onto a regular grid."""
    from scipy.ndimage import map_coordinates

    # The output size must match the span actually sampled, or the block is
    # scaled differently in x and y and the spots come out oval.
    span_c = (n_cols - 1) + 2 * PAD_CELLS
    span_r = (n_rows - 1) + 2 * PAD_CELLS
    w = int(round(span_c * CELL_PX))
    h = int(round(span_r * CELL_PX))
    # Output pixel -> grid coordinate (col, row), then -> source pixel.
    cols = np.linspace(-PAD_CELLS, (n_cols - 1) + PAD_CELLS, w)
    rows = np.linspace(row0 - PAD_CELLS, row0 + (n_rows - 1) + PAD_CELLS, h)
    gj, gi = np.meshgrid(cols, rows)
    flat = np.column_stack([gj.ravel(), gi.ravel(), np.ones(gj.size)])
    xy = flat @ coef                                   # (N, 2) as (x, y)
    vals = map_coordinates(img, [xy[:, 1], xy[:, 0]], order=1, mode="nearest")
    # The source coordinates come back too: the rim mask has to be built in
    # plate space, not output space, because the block is a resampled crop.
    return vals.reshape(h, w), xy[:, 1].reshape(h, w), xy[:, 0].reshape(h, w)


def _load_gray8_cached(path: Path, rgb_mode) -> np.ndarray:
    """`sq.load_gray8`, memoized on (path, mtime, size, rgb_mode).

    A 24 MP photo decodes to 24 MB as uint8, so a handful of them is cheap to
    hold, and every dilution choice of every pairing that photo appears in wants
    the identical array. Returned READ-ONLY: callers must not write through it,
    and `subtract_background` does not.
    """
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size, rgb_mode)
    hit = _IMG_CACHE.get(key)
    if hit is None:
        hit = sq.load_gray8(path, rgb_mode=rgb_mode)
        hit.flags.writeable = False
        _IMG_CACHE[key] = hit
        while len(_IMG_CACHE) > IMG_CACHE_SIZE:
            _IMG_CACHE.popitem(last=False)
    else:
        _IMG_CACHE.move_to_end(key)
    return hit


def block_height(pd_: "sb.PlateData", layout=None, whole: bool = False) -> int:
    """Rows in one replicate block of this plate's montage.

    The lab design stacks two replicate blocks of three dilution rows, so a
    plate is drawn as two blocks of 3. A layout that divides its rows into equal
    blocks each holding every level once is drawn the same way at its own block
    size; any other design is drawn as a single block per plate, which is always
    correct if less compact. `whole` draws every plate as one block regardless,
    as a rotated plate is.
    """
    n_rows = int(pd_.centers.shape[0])
    if whole:
        return n_rows
    if layout is None or layout.is_classic():
        return n_rows // 2
    k = layout.block_rows()
    return k if k and n_rows % k == 0 else n_rows


def rotate_plate(pd_: "sb.PlateData") -> "sb.PlateData":
    """The same plate with its spot grid turned a quarter turn anticlockwise.

    Nothing about the photograph changes: only which spot is called row i,
    column j. The montage resamples each block along its grid, so drawing a
    plate with a turned grid draws the plate turned -- and every label is then
    planned for the turned grid, so it comes out upright and in its place.
    Grid cell (r, c) becomes (n_cols - 1 - c, r), as `rotate_layout` maps it.
    """
    return dataclasses.replace(
        pd_, centers=np.rot90(pd_.centers).copy(),
        rim=np.rot90(pd_.rim).copy(), net=np.rot90(pd_.net).copy())


def rotate_layout(layout, plates) -> "sb.DilutionLayout":
    """`layout` (None: the lab design) turned as `rotate_plate` turns a plate."""
    design = (sb.classic_layout(sorted({int(p.ref.plate) for p in plates}))
              if layout is None else layout)
    n_cols = design.n_cols
    levels = tuple(
        sb.DilutionLevel(lv.index, lv.name, tuple(
            (no, tuple(sb.LevelCell(n_cols - 1 - c.col, c.row, c.slot,
                                    c.replicate) for c in cells))
            for no, cells in lv.plates))
        for lv in design.levels)
    return sb.DilutionLayout(levels, design.n_cols, design.n_rows)


def _subtract(pd_: "sb.PlateData", opts: sq.MeasureOptions, r_disp: float):
    """The display background subtraction of one plate's photo."""
    img8 = _load_gray8_cached(pd_.ref.path, opts.rgb_mode)
    ball = opts.resolve_ball_radius(2 * r_disp / sq.MEASURE_RADIUS_FRAC)
    proc, _, _ = sq.subtract_background(
        img8, ball_radius=ball, bg_centers=[], bg_radius=r_disp,
        iters=opts.bg_iters, shrink=opts.shrink, mode=opts.bg_mode)
    return proc


def plate_blocks(pd_: "sb.PlateData", opts: sq.MeasureOptions,
                 radius: "float | None" = None,
                 proc=None, layout=None, whole: bool = False):
    """The two replicate blocks of one plate, with their rim masks.

    Split out of `build_montage` so repeated draws of the same plate can share
    the work -- a 24 MP decode plus a full background subtraction.

    `radius` overrides the ROI radius used for the display background
    subtraction and the spot halo, and IS THE ONLY WAY the three dilution
    choices of one plate can share this work. By default the radius comes from
    `pd_.radius`, which is sized to the chosen dilution rows: measured on Set09
    16h GLU the same plate gives 103.5 / 96.4 / 91.6 px for least / middle /
    most, i.e. ball radii of 250 / 234 / 223. So by default the three choices
    really do produce three different subtractions, and the cache key carries
    the radius so they never share one by accident.

    Passing a fixed `radius` -- the time-course sheets pass each photo's largest,
    which is the least-dilute row's and is closest to the protocol's own "largest
    spot diameter + 20" -- makes the picture depend on the photo alone. The plate
    then looks identical across its three sheets, so only the marked row changes,
    and one subtraction serves all three. The caller owns saying so on the figure:
    the image is no longer subtracted with the exact radius that candidate's
    numbers used.

    The cache is bounded to `BLOCK_CACHE_SIZE` entries. Blocks are kept as
    float32 -- they are display data bound for imshow over a 0-255 range, and
    float64 doubled the memory for no visible difference.

    `proc` may also be a function returning the subtraction, called only on a
    cache miss: that is how a plate drawn both as photographed and rotated
    shares one subtraction without paying for it when both are cached.
    """
    r_disp = float(pd_.radius if radius is None else radius)
    st = pd_.ref.path.stat()
    n_rows, n_cols = int(pd_.centers.shape[0]), int(pd_.centers.shape[1])
    per_rep = block_height(pd_, layout, whole)
    # The first grid row's ends tell a turned grid from an unturned one even
    # when both are square.
    turn = tuple(np.round(np.concatenate([pd_.centers[0, 0],
                                          pd_.centers[0, -1]]), 1))
    key = (str(pd_.ref.path), st.st_mtime_ns, st.st_size, int(pd_.ref.plate),
           opts.rgb_mode, opts.bg_mode, opts.bg_iters, opts.shrink, r_disp,
           n_rows, n_cols, per_rep, turn)
    hit = _BLOCK_CACHE.get(key)
    if hit is not None:
        _BLOCK_CACHE.move_to_end(key)
        return hit
    # Same background treatment the numbers come from, so the picture and
    # the quantification are showing the same thing.
    #
    # `proc` lets the caller supply that subtraction ready-made. It is the same
    # array this branch would compute, reused from a Python subtraction batch.
    if callable(proc):
        proc = proc()
    if proc is None:
        proc = _subtract(pd_, opts, r_disp)
    coef = _affine_from_centers(pd_.centers)
    pcy, pcx = pd_.plate_center

    blocks, masks = [], []
    for r0 in range(0, n_rows, per_rep):
        blk, sy, sx = _sample_block(proc, coef, r0, per_rep, n_cols)
        keep = np.hypot(sy - pcy, sx - pcx) <= AGAR_KEEP_FRAC * pd_.plate_radius
        halo = (SPOT_HALO * r_disp / sq.MEASURE_RADIUS_FRAC) ** 2
        # ...but only spots that are not themselves rim-flagged. A flagged
        # spot is one the rim has already spoiled; it is dropped from the
        # quantification, and exempting it here just re-admits the glare as
        # a blown-out disc that reads as enormous growth.
        blk_c = pd_.centers[r0:r0 + per_rep].reshape(-1, 2)
        blk_f = pd_.rim[r0:r0 + per_rep].reshape(-1)
        for cyx, flagged in zip(blk_c, blk_f):
            if flagged:
                continue
            keep |= (sy - cyx[0]) ** 2 + (sx - cyx[1]) ** 2 <= halo
        blocks.append(blk.astype(np.float32))
        masks.append(keep)

    _BLOCK_CACHE[key] = (blocks, masks)
    while len(_BLOCK_CACHE) > BLOCK_CACHE_SIZE:
        _BLOCK_CACHE.popitem(last=False)
    return blocks, masks


def column_strains(plate: int, n_rows: int, n_cols: int, strains: list,
                   layout=None) -> list:
    """The strain names in each montage column, top row first.

    The lab design puts one strain per column, sample slot = column, so the
    answer is just `strains`. A template is free to put several strains in one
    column -- a 96-well plate of 24 strains spotted as 4-spot runs does -- and
    then a label per slot index would name columns that hold something else
    entirely. So the names are read off the layout's own cells, restricted to
    the rows of the first replicate block (`n_rows`), which is the block the
    labels sit above.
    """
    def name(slot):
        i = int(slot) - 1
        return strains[i] if 0 <= i < len(strains) and strains[i] else None

    if layout is None or layout.is_classic():
        return [[n] if (n := name(j + 1)) else [] for j in range(n_cols)]
    at = layout.slot_at(plate)
    out = []
    for j in range(n_cols):
        names = [name(at[(i, j)]) for i in range(n_rows) if (i, j) in at]
        out.append(list(dict.fromkeys(n for n in names if n)))
    return out


def label_spans(per_col: list) -> list:
    """Merge runs of adjacent columns holding the same strains.

    Returns [(first_col, last_col, names)]. Replicate spots laid side by side
    then get one label centred over the run rather than the same name repeated.
    """
    spans = []
    for j, names in enumerate(per_col):
        if spans and spans[-1][2] == names and names:
            spans[-1] = (spans[-1][0], j, names)
        else:
            spans.append((j, j, names))
    return [s for s in spans if s[2]]


# ---------------------------------------------------------------------------
# Label planning -- where each strain's name goes, read off the layout
# ---------------------------------------------------------------------------
#
# A strain's spots form a rectangle on the photographed grid: a column of
# dilutions in the lab design, a run of replicates along a row in a 96-well
# design rotated onto its side, a 2x2 quad in some other. The label belongs
# against that rectangle, on the side the rectangle runs along:
#
#   "columns"  names above the columns they head. When strains are stacked
#              down a column (several runs per column), the block is split
#              between them and a label lane is opened in each gap.
#   "rows"     names beside the rows they head. When several runs share a
#              row, the block is split between them likewise, and each run
#              gets the lane on its left.
#
# Whichever needs fewer splits wins; the lab design needs none in "columns".
# Splitting only ever adds black lanes BETWEEN pieces of the block -- no spot
# pixel is moved relative to its own neighbours or altered in value. A layout
# that fits neither (strains that are not rectangles, or two strains competing
# for one label position) falls back to "stacked": every name in a column,
# top row first, over that column.


@dataclasses.dataclass
class LabelPlan:
    mode: str           # "columns", "rows" or "stacked"
    groups: list        # per block: [(lo, hi)] cell intervals split apart
    labels: list        # per block: [(group, lo, hi, names)]


def _merge(intervals) -> list:
    """Union of overlapping (lo, hi) cell intervals. Abutting ones stay apart."""
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b, out[-1][1]))
        else:
            out.append((a, b))
    return out


def _group_of(groups, x) -> int:
    return next(g for g, (a, b) in enumerate(groups) if a <= x <= b)


def _rects(at: dict) -> "list | None":
    """Each 4-connected run of one slot as (r0, r1, c0, c1, slot).

    None if any run is not a filled rectangle -- an L-shaped strain has no
    single side to label.
    """
    seen, out = set(), []
    for cell in sorted(at):
        if cell in seen:
            continue
        slot = at[cell]
        stack, comp = [cell], []
        seen.add(cell)
        while stack:
            i, j = stack.pop()
            comp.append((i, j))
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb not in seen and at.get(nb) == slot:
                    seen.add(nb)
                    stack.append(nb)
        rs, cs = [c[0] for c in comp], [c[1] for c in comp]
        r0, r1, c0, c1 = min(rs), max(rs), min(cs), max(cs)
        if len(comp) != (r1 - r0 + 1) * (c1 - c0 + 1):
            return None
        out.append((r0, r1, c0, c1, slot))
    return out


def _columns_plan(rects, name) -> "LabelPlan | None":
    groups_all, labels_all = [], []
    for rs in rects:
        groups = _merge([(r0, r1) for r0, r1, *_ in rs])
        used, labels = set(), []
        for r0, r1, c0, c1, slot in rs:
            g = _group_of(groups, r0)
            for c in range(c0, c1 + 1):
                if (g, c) in used:
                    return None     # two strains want the same column header
                used.add((g, c))
            if (n := name(slot)):
                labels.append((g, c0, c1, (n,)))
        groups_all.append(groups)
        labels_all.append(labels)
    return LabelPlan("columns", groups_all, labels_all)


def _rows_plan(rects, name) -> "LabelPlan | None":
    # Blocks are stacked vertically, so their columns must line up: one set
    # of column splits serves every block.
    groups = _merge([(c0, c1) for rs in rects for _, _, c0, c1, _ in rs])
    labels_all = []
    for rs in rects:
        used, labels = set(), []
        for r0, r1, c0, c1, slot in rs:
            g = _group_of(groups, c0)
            for r in range(r0, r1 + 1):
                if (g, r) in used:
                    return None     # two strains want the same row label
                used.add((g, r))
            if (n := name(slot)):
                labels.append((g, r0, r1, (n,)))
        labels_all.append(labels)
    return LabelPlan("rows", [groups] * len(rects), labels_all)


def _stacked_plan(block_cells, name) -> LabelPlan:
    groups_all, labels_all = [], []
    for at in block_cells:
        if not at:
            groups_all.append([])
            labels_all.append([])
            continue
        n_rows = max(i for i, _ in at) + 1
        n_cols = max(j for _, j in at) + 1
        per_col = []
        for j in range(n_cols):
            names = [name(at[(i, j)]) for i in range(n_rows) if (i, j) in at]
            per_col.append(list(dict.fromkeys(n for n in names if n)))
        groups_all.append([(0, n_rows - 1)])
        labels_all.append([(0, j0, j1, tuple(names))
                           for j0, j1, names in label_spans(per_col)])
    return LabelPlan("stacked", groups_all, labels_all)


def plan_labels(block_cells: list, strains: list) -> LabelPlan:
    """Decide where every strain label goes.

    `block_cells` is, per drawn block, {(row, col): sample slot} in
    block-relative photograph coordinates -- i.e. after the template has been
    oriented onto the photo. `strains[slot - 1]` names a slot.
    """
    def name(slot):
        i = int(slot) - 1
        return strains[i] if 0 <= i < len(strains) and strains[i] else None

    rects = [_rects(at) for at in block_cells]
    if all(r is not None for r in rects):
        cands = [p for p in (_columns_plan(rects, name), _rows_plan(rects, name))
                 if p is not None]
        if cands:
            # Fewest splits wins; on a tie, `min` keeps "columns", the classic.
            return min(cands, key=lambda p: max((len(g) for g in p.groups),
                                                default=0))
    return _stacked_plan(block_cells, name)


def block_replicates(reps: dict):
    """How replicates run through one block: (single, by_col, by_row).

    `single` is the one replicate number when the whole block is one
    replicate; otherwise `by_col` ({col: rep}) or `by_row` ({row: rep}) is
    filled when every column (or row) holds a single replicate.
    """
    vals = set(reps.values())
    if len(vals) == 1:
        return next(iter(vals)), None, None
    if not vals:
        return None, None, None

    def along(axis):
        out = {}
        for rc, rep in reps.items():
            if out.setdefault(rc[axis], rep) != rep:
                return None
        return out
    by_col = along(1)
    return None, by_col, (None if by_col else along(0))


def _text_in(text: str, pt: float) -> float:
    """Approximate width of one line of text, in inches."""
    return len(text) * pt * GLYPH_W / 72


def _rotated_h_in(text: str, pt: float) -> float:
    """Height of a label set at the montage's 60-degree slant, in inches."""
    return _text_in(text, pt) * 0.866 + pt / 72 * 0.5


def _column_label_specs(labels) -> tuple:
    """Draw specs for one lane of column headers, and the lane's height.

    One strain over one column is the classic slanted label. A name spanning
    several columns, or several names over one, is set level and stacked,
    shrunk if need be to fit the span; if even the smallest type cannot fit,
    it falls back to one slanted line.
    """
    specs, h_in = [], 0.0
    for _, j0, j1, names in labels:
        if j0 == j1 and len(names) == 1:
            specs.append(((PAD_CELLS + j0) * CELL_PX, names[0], LABEL_PT, False))
            h_in = max(h_in, _rotated_h_in(names[0], LABEL_PT))
            continue
        room = (j1 - j0 + 1) * IN_PER_CELL * 0.95
        widest = max(names, key=len)
        pt = LABEL_PT
        while pt > LABEL_MIN_PT and _text_in(widest, pt) > room:
            pt -= 1
        if _text_in(widest, pt) <= room:
            x = (PAD_CELLS + (j0 + j1) / 2) * CELL_PX
            specs.append((x, "\n".join(names), pt, True))
            h_in = max(h_in, len(names) * pt * 1.25 / 72)
        else:
            text = " / ".join(names)
            specs.append(((PAD_CELLS + j0) * CELL_PX, text, LABEL_PT, False))
            h_in = max(h_in, _rotated_h_in(text, LABEL_PT))
    return specs, h_in


def _pieces(groups, length: float, lanes: list) -> list:
    """Split one axis of a block at its group boundaries.

    Returns [(src0, src1, dst0)] in block pixels. Lane g opens just before
    piece g; a block with no groups is one piece with no lane.
    """
    if not groups:
        return [(0, int(length), lanes[0] if lanes else 0.0)]
    starts = [0] + [int(round((PAD_CELLS + a - 0.5) * CELL_PX))
                    for a, _ in groups[1:]]
    ends = starts[1:] + [int(length)]
    out, shift = [], 0.0
    for s0, s1, lane in zip(starts, ends, lanes):
        shift += lane
        out.append((s0, s1, s0 + shift))
    return out


def _to_dst(pieces, s: float) -> float:
    for s0, s1, d0 in pieces:
        if s <= s1:
            return d0 + (s - s0)
    s0, _, d0 = pieces[-1]
    return d0 + (s - s0)


def _plate_mark_rows(plates, layout, mark_row) -> "dict | None":
    """`mark_row` as {plate: every grid row it outlines on that plate}.

    `mark_row` counts rows within a replicate block; turned, the plate is one
    block and those rows are columns, so the rotated copy needs them spelled
    out for the whole plate.
    """
    if mark_row is None:
        return None
    out = {}
    for pd_ in plates:
        plate = int(pd_.ref.plate)
        n, per = int(pd_.centers.shape[0]), block_height(pd_, layout)
        if isinstance(mark_row, dict):
            idx = mark_row.get(plate, ())
        elif np.isscalar(mark_row):
            idx = [mark_row]
        else:
            idx = mark_row
        out[plate] = tuple(sorted({int(i) + b for b in range(0, n, per)
                                   for i in idx if int(i) + b < n}))
    return out


def _row_runs(rows) -> list:
    """Consecutive runs of row indices, as [(first, last)]."""
    runs = []
    for r in sorted({int(r) for r in rows}):
        if runs and r == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], r)
        else:
            runs.append((r, r))
    return runs


# Strain label type size, and the smallest a stacked label may shrink to so
# its widest name fits over its columns before it falls back to one rotated
# line. Approximate glyph width as a fraction of the type size, for that fit.
LABEL_PT = 14
LABEL_MIN_PT = 9
GLYPH_W = 0.62
# Inches per grid cell in the montage figure (see `fig_w` below).
IN_PER_CELL = 0.42


def build_montage(combo: str, plates: list[sb.PlateData], strains: list[str | None],
                  opts: sq.MeasureOptions, out_path: Path,
                  rep_label: str = "Replicant",
                  vmin: float | None = None, vmax: float | None = None,
                  mark_row=None,
                  mark_label: str | None = None,
                  bg_radius=None, proc=None, layout=None,
                  dpi: int = 200, clean_path: "Path | None" = None,
                  rotated_path: "Path | None" = None,
                  whole_plate: bool = False,
                  mark_axis: str = "rows") -> Path:
    """Draw the montage. `mark_row`, if given, outlines the dilution rows.

    `mark_row` is a row index WITHIN a replicate block (0 = least dilute,
    2 = most), so the same index is outlined in all four blocks -- which is
    exactly how a dilution choice applies. It may also be a sequence of such
    indices, or {plate number: indices} when the rows differ between plates;
    adjacent rows are outlined as one box. It is drawn as an overlay: no pixel
    of the image is altered, and the display range is untouched.

    `clean_path`, if given, also receives the same montage without the
    outline -- written only when there is an outline to leave off.

    `rotated_path`, if given, also receives the montage a quarter turn
    anticlockwise: each plate turned whole (see `rotate_plate`), its labels
    planned afresh for the turned grid so they read upright, and the
    quantified rows -- now columns -- outlined. A wide plate becomes a tall
    one, which is what lets a sheet or the review use whichever shape fits.
    Both share each plate's background subtraction.

    `whole_plate` draws each plate as one block; `mark_axis` "cols" reads
    `mark_row` as {plate: columns} and outlines columns. Both are how the
    rotated copy is drawn.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    set_id, treatment = combo.split("|", 1)

    # bg_radius: one value for every plate, or one PER plate. Per-plate is what
    # the time-course sheets use -- it keeps each photo's display subtraction a
    # function of that photo alone, so it is computed once however many pairings
    # the photo takes part in.
    if bg_radius is None or np.isscalar(bg_radius):
        radii = [bg_radius] * len(plates)
    else:
        radii = list(bg_radius)
        if len(radii) != len(plates):
            raise ValueError(f"bg_radius has {len(radii)} entries for "
                             f"{len(plates)} plates")

    if rotated_path is not None:
        # One subtraction per plate, computed only if some draw needs it.
        given = list(proc) if proc is not None else [None] * len(plates)
        memo: dict = {}

        def shared(i, pd_, r):
            def get():
                if i not in memo:
                    memo[i] = (given[i] if given[i] is not None else
                               _subtract(pd_, opts, float(
                                   pd_.radius if r is None else r)))
                return memo[i]
            return get

        procs = [shared(i, p, r) for i, (p, r) in enumerate(zip(plates, radii))]
        common = dict(rep_label=rep_label, vmin=vmin, vmax=vmax,
                      bg_radius=bg_radius, proc=procs, dpi=dpi)
        build_montage(combo, plates, strains, opts, out_path,
                      mark_row=mark_row, mark_label=mark_label,
                      layout=layout, clean_path=clean_path, **common)
        build_montage(combo, [rotate_plate(p) for p in plates], strains, opts,
                      rotated_path,
                      mark_row=_plate_mark_rows(plates, layout, mark_row),
                      mark_axis="cols", layout=rotate_layout(layout, plates),
                      whole_plate=True, **common)
        return out_path

    # The layout says which slot and replicate sits at each photographed cell,
    # already oriented onto the photo. Without one, the lab design.
    classic = layout is None or layout.is_classic()
    design = (sb.classic_layout(sorted({int(p.ref.plate) for p in plates}))
              if layout is None else layout)

    blocks, masks, owners = [], [], []
    block_cells, block_reps = [], []
    spots = []          # (block, row in block, col, slot, replicate, level)
    procs = list(proc) if proc is not None else [None] * len(plates)
    for pd_, r, pr in zip(plates, radii, procs):
        b, m = plate_blocks(pd_, opts, radius=r, proc=pr, layout=layout,
                            whole=whole_plate)
        k0 = len(blocks)
        blocks.extend(b)
        masks.extend(m)
        owners.extend([int(pd_.ref.plate)] * len(b))
        per_rep = block_height(pd_, layout, whole_plate)
        n_cols = int(pd_.centers.shape[1])
        for lv in design.levels:
            try:
                cells = lv.cells(int(pd_.ref.plate))
            except KeyError:
                continue
            for c in cells:
                if c.row < per_rep * len(b) and c.col < n_cols:
                    spots.append((k0 + c.row // per_rep, c.row % per_rep,
                                  c.col, c.slot, c.replicate, lv.index))
        at = design.cell_at(int(pd_.ref.plate))
        for r0 in range(0, per_rep * len(b), per_rep):
            here = {(i - r0, j): c for (i, j), c in at.items()
                    if r0 <= i < r0 + per_rep and j < n_cols}
            block_cells.append({rc: c.slot for rc, c in here.items()})
            block_reps.append({rc: c.replicate for rc, c in here.items()})

    def marked(k):
        if mark_row is None:
            return []
        if isinstance(mark_row, dict):
            return _row_runs(mark_row.get(owners[k], ()))
        if np.isscalar(mark_row):
            return _row_runs([mark_row])
        return _row_runs(mark_row)

    # No display stretch: vmin/vmax are the fixed 8-bit bounds unless the
    # caller passes an explicit range, which is then stamped on the figure so
    # the adjustment travels with the image.
    manual = (vmin is not None) or (vmax is not None)
    vmin = DISPLAY_MIN if vmin is None else float(vmin)
    vmax = DISPLAY_MAX if vmax is None else float(vmax)
    if vmax <= vmin:
        raise ValueError(f"display max ({vmax}) must exceed display min ({vmin})")

    # Rim mask applied last, so masked pixels land exactly on the black point.
    blocks = [np.where(m, b, vmin) for b, m in zip(blocks, masks)]

    bh, bw = blocks[0].shape
    px_per_in = CELL_PX / IN_PER_CELL
    gap_px = 0.16 * CELL_PX          # label baseline to the spots it names

    # Where every strain label goes is read off the layout: see `plan_labels`.
    plan = plan_labels(block_cells, strains)

    # Replicate numbering. A block that is one replicate is named for it down
    # the left, as the lab design always was. A block holding several -- a
    # 96-well design with the replicates side by side along each strain's
    # run -- is named for its plate instead, and its replicates are ticked
    # along the columns (or rows) they occupy.
    rep_info = [block_replicates(r) for r in block_reps]
    block_names = []
    for k, (single, _, _) in enumerate(rep_info):
        if single is not None:
            block_names.append(f"{rep_label} {k + 1 if classic else single}")
        else:
            block_names.append(f"Plate {owners[k]}")
    tick_pt = 9
    tick_px = tick_pt * 1.6 / 72 * px_per_in

    # Lanes, in block pixels. Column headers repeat only when they change from
    # the header above them, so the lab design is labelled once, over the top.
    x_lanes, y_lanes, header_specs, show_ticks = [], [], [], []
    if plan.mode == "rows":
        groups = plan.groups[0] if plan.groups else []
        widths = [0.0] * len(groups)
        for labels in plan.labels:
            for g, _, _, names in labels:
                widths[g] = max(widths[g], _text_in(names[0], LABEL_PT))
        lane = [(w + 0.2) * px_per_in if w else 0.0 for w in widths]
        x_lanes = [lane] * len(blocks)
    prev_head, prev_ticks = None, None
    for k in range(len(blocks)):
        _, by_col, _ = rep_info[k]
        ticks = by_col is not None and by_col != prev_ticks
        show_ticks.append(ticks)
        prev_ticks = by_col
        groups = plan.groups[k]
        if plan.mode == "rows":
            y_lanes.append([tick_px if ticks else 0.0])
            header_specs.append([])
            continue
        x_lanes.append([0.0])
        lanes, specs = [], []
        for g in range(max(len(groups), 1)):
            head = [(j0, j1, names) for gg, j0, j1, names in plan.labels[k]
                    if gg == g]
            sp, h_in = _column_label_specs([(g, *h) for h in head])
            if head == prev_head or not head:
                sp, h_in = [], 0.0
            if head:
                prev_head = head
            extra = tick_px if (g == 0 and ticks) else 0.0
            lanes.append((h_in * px_per_in + gap_px + 0.1 * CELL_PX if sp
                          else 0.0) + extra)
            specs.append(sp)
        y_lanes.append(lanes)
        header_specs.append(specs)

    xs = [_pieces(plan.groups[k] if plan.mode == "rows" else [], bw, x_lanes[k])
          for k in range(len(blocks))]
    ys = [_pieces(plan.groups[k] if plan.mode != "rows" else [], bh, y_lanes[k])
          for k in range(len(blocks))]
    widths_px = [bw + sum(x_lanes[k]) for k in range(len(blocks))]
    heights_px = [bh + sum(y_lanes[k]) for k in range(len(blocks))]
    W = max(widths_px)

    lab_w = (max(_text_in(n, 15) for n in block_names) + 0.15) * px_per_in / CELL_PX
    hspace = 0.04
    mean_h = sum(heights_px) / len(heights_px)
    total_px = sum(heights_px) + hspace * mean_h * (len(blocks) - 1)
    fig_w = (W / CELL_PX + lab_w) * IN_PER_CELL
    fig_h = total_px / CELL_PX * IN_PER_CELL + 0.3

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor="black")
    gs = fig.add_gridspec(len(blocks), 1, hspace=hspace,
                          height_ratios=heights_px,
                          left=lab_w / (W / CELL_PX + lab_w), right=0.99,
                          top=1 - 0.15 / fig_h, bottom=0.15 / fig_h)

    axes, marks = [], []
    for k, blk in enumerate(blocks):
        ax = fig.add_subplot(gs[k, 0])
        axes.append(ax)
        xp, yp = xs[k], ys[k]
        for sx0, sx1, dx0 in xp:
            for sy0, sy1, dy0 in yp:
                ax.imshow(blk[sy0:sy1, sx0:sx1], cmap="gray", vmin=vmin,
                          vmax=vmax, interpolation="bilinear", aspect="equal",
                          extent=(dx0, dx0 + sx1 - sx0, dy0 + sy1 - sy0, dy0))
        ax.set_xlim(0, W)
        ax.set_ylim(heights_px[k], 0)
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.set_facecolor("black")
        ax.text(-0.012, 0.5, block_names[k], transform=ax.transAxes,
                color="white", fontsize=15, ha="right", va="center")
        for n, (r0, r1) in enumerate(marked(k)):
            # A cell is CELL_PX across and row i is centred at
            # (PAD_CELLS + i) * CELL_PX -- the same mapping the strain labels
            # use, so the box lands square on the rows.
            # The box reaches PAD_CELLS past the outer marked row centres, not
            # 0.5 cells: the block is cropped exactly PAD_CELLS past the outer
            # row centres, so a taller box gets clipped by the panel edge when
            # the marked row is the first or last one -- which is precisely the
            # case that matters most to see.
            # Turned, the same runs are columns: the box spans the block's
            # height instead of its width.
            lo, hi = r0 * CELL_PX, (r1 + 2 * PAD_CELLS) * CELL_PX
            cols = mark_axis == "cols"
            y0, y1 = (0, bh) if cols else (lo, hi)
            x0, x1 = (lo, hi) if cols else (0, bw)
            # One box per piece the marked rows cross, so no box spans a lane.
            for sx0, sx1, dx0 in xp:
                for sy0, sy1, dy0 in yp:
                    a, b = max(y0, sy0), min(y1, sy1)
                    c, d = max(x0, sx0), min(x1, sx1)
                    if b <= a or d <= c:
                        continue
                    marks.append(ax.add_patch(Rectangle(
                        (dx0 + c - sx0, dy0 + a - sy0), d - c, b - a,
                        fill=False, edgecolor=MARK_COLOR, linewidth=2.2)))
            if mark_label and k == 0 and n == 0 and not cols:
                ax.text(W + 0.05 * CELL_PX,
                        (_to_dst(yp, y0) + _to_dst(yp, y1)) / 2, mark_label,
                        color=MARK_COLOR, fontsize=11, ha="left", va="center")

        _, by_col, by_row = rep_info[k]
        tick = 0.0
        if show_ticks[k]:
            tick = tick_px
            for j, rep in by_col.items():
                ax.text(_to_dst(xp, (PAD_CELLS + j) * CELL_PX), yp[0][2] - 0.25 * tick_px,
                        f"R{rep}", color="#9a9a9a", fontsize=tick_pt,
                        ha="center", va="bottom")
        if by_row is not None and not mark_label:
            for i, rep in by_row.items():
                ax.text(W + 0.05 * CELL_PX, _to_dst(yp, (PAD_CELLS + i) * CELL_PX),
                        f"R{rep}", color="#9a9a9a", fontsize=tick_pt,
                        ha="left", va="center")

        if plan.mode == "rows":
            for g, r0, r1, names in plan.labels[k]:
                y = _to_dst(yp, (PAD_CELLS + (r0 + r1) / 2) * CELL_PX)
                ax.text(xp[g][2] - 0.12 * CELL_PX, y, names[0], color="white",
                        fontsize=LABEL_PT, ha="right", va="center")
            continue
        for g, specs in enumerate(header_specs[k]):
            y = yp[g][2] - gap_px - (tick if g == 0 else 0.0)
            for x, text, pt, stacked in specs:
                if stacked:
                    ax.text(x, y, text, color="white", fontsize=pt,
                            ha="center", va="bottom", multialignment="center",
                            linespacing=1.15)
                else:
                    ax.text(x, y, text, color="white", fontsize=pt,
                            rotation=60, rotation_mode="anchor",
                            ha="left", va="bottom")

    if manual:
        fig.text(0.995, 0.004, f"display range {vmin:g}-{vmax:g} (adjusted)",
                 color="white", fontsize=8, ha="right", va="bottom")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The tight crop is computed here rather than by savefig, so the spot map
    # written with the image is in exactly the pixels the image ends up with.
    # Draw first: equal-aspect axes only settle their final box at draw time.
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bbox = fig.get_tightbbox(renderer).padded(0.1)
    spot_map = _spot_map(fig, axes, xs, ys, spots, strains, bbox, dpi)
    fig.savefig(out_path, dpi=dpi, facecolor="black", bbox_inches=bbox,
                metadata={SPOT_KEY: spot_map})
    if clean_path is not None and marks:
        # The same picture without the outline, pixel for pixel aligned with
        # it: where a big spot runs under the outline, a viewer cutting single
        # spots out needs the plate that is underneath it.
        for m in marks:
            m.set_visible(False)
        fig.savefig(clean_path, dpi=dpi, facecolor="black", bbox_inches=bbox,
                    metadata={SPOT_KEY: spot_map})
    plt.close(fig)
    return out_path


#: PNG text key listing every spot drawn in a montage: where it is in the
#: written PNG, and which strain, replicate and dilution level it is. A viewer
#: uses it to cut single spots out (see spotting_sheet's aligned view).
SPOT_KEY = "spotting-spots"


def _spot_map(fig, axes, xs, ys, spots, strains, bbox, dpi) -> str:
    import json

    def to_png(ax, x, y):
        dx, dy = ax.transData.transform((x, y))
        return ((dx / fig.dpi - bbox.x0) * dpi, (bbox.y1 - dy / fig.dpi) * dpi)

    def piece(pieces, s):
        for s0, s1, d0 in pieces:
            if s0 <= s < s1:
                return d0, d0 + (s1 - s0)
        s0, s1, d0 = pieces[-1]
        return d0, d0 + (s1 - s0)

    out = []
    for k, i, j, slot, rep, level in spots:
        ax, xp, yp = axes[k], xs[k], ys[k]
        sx, sy = (PAD_CELLS + j) * CELL_PX, (PAD_CELLS + i) * CELL_PX
        cx, cy = _to_dst(xp, sx), _to_dst(yp, sy)
        x, y = to_png(ax, cx, cy)
        x1, _ = to_png(ax, cx + CELL_PX / 2, cy)
        # The stretch of plate image this spot sits in -- a crop around the
        # spot must stay inside it, clear of label lanes and block gaps.
        (px0, px1), (py0, py1) = piece(xp, sx), piece(yp, sy)
        c0, c1 = to_png(ax, px0, py0), to_png(ax, px1, py1)
        clip = [round(min(c0[0], c1[0]), 1), round(min(c0[1], c1[1]), 1),
                round(max(c0[0], c1[0]), 1), round(max(c0[1], c1[1]), 1)]
        name = (strains[slot - 1]
                if 0 < slot <= len(strains) and strains[slot - 1] else None)
        out.append({"x": round(x, 1), "y": round(y, 1),
                    "r": round(abs(x1 - x), 1), "clip": clip, "strain": name,
                    "slot": int(slot), "rep": int(rep), "level": int(level)})
    size = [round(bbox.width * dpi), round(bbox.height * dpi)]
    return json.dumps({"size": size, "spots": out})


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?",
                    default=PROJECT_ROOT / "Spotting Assays")
    ap.add_argument("--combo", nargs=2, metavar=("SET", "TREATMENT"),
                    help="Only this treatment-set combination, e.g. --combo 4 K-OAc")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output folder (default Results/Spotting/montages beside the code).")
    ap.add_argument("--label", default="Replicant",
                    help="Row label text (default 'Replicant').")
    ap.add_argument("--ball-radius", type=float, default=None, metavar="PX",
                    help="Override the sliding-paraboloid radius.")
    ap.add_argument("--display-min", type=float, default=None, metavar="V",
                    help="Override the display black point (default 0, i.e. no "
                         "adjustment). Setting it stamps the range on the figure.")
    ap.add_argument("--display-max", type=float, default=None, metavar="V",
                    help="Override the display white point (default 255).")
    ap.add_argument("--suffix", default="", help="Appended to output filenames.")
    ap.add_argument("--list", action="store_true", help="List combinations and exit.")
    args = ap.parse_args(argv)

    folder: Path = args.folder
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2

    combos, _ = sb.discover(folder)
    keys = sb.sort_combos(combos)
    if args.list:
        for k in keys:
            s, t = k.split("|", 1)
            print(f"  set {s:>2}  {t}")
        return 0
    if args.combo:
        want = f"{args.combo[0]}|{args.combo[1]}"
        if want not in combos:
            print(f"No such combination: {want}. Try --list.", file=sys.stderr)
            return 2
        keys = [want]

    cfg = sb.load_config(folder / sb.CONFIG_NAME)
    opts = sq.MeasureOptions(ball_radius=args.ball_radius)
    outdir = args.out or sb.MAIN_RESULTS / "montages"

    for k in keys:
        s, t = k.split("|", 1)
        entry = cfg.get("sets", {}).get(s)
        if not entry:
            print(f"  ! set {s} has no strain names in the config; skipping.")
            continue
        plates = [sb.measure(ref,
                             dataclasses.replace(opts, nudge=sb.plate_nudges(cfg, ref)),
                             folder / sb.CACHE_DIR)
                  for ref in combos[k]]
        safe = f"montage_set{s}_{t.replace(chr(47), chr(45))}{args.suffix}.png"
        p = build_montage(k, plates, entry["strains"], opts, outdir / safe,
                          rep_label=args.label,
                          vmin=args.display_min, vmax=args.display_max)
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
