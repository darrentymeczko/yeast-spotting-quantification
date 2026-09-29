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


def block_height(pd_: "sb.PlateData", layout=None) -> int:
    """Rows in one replicate block of this plate's montage.

    The lab design stacks two replicate blocks of three dilution rows, so a
    plate is drawn as two blocks of 3. A layout that divides its rows into equal
    blocks each holding every level once is drawn the same way at its own block
    size; any other design is drawn as a single block per plate, which is always
    correct if less compact.
    """
    n_rows = int(pd_.centers.shape[0])
    if layout is None or layout.is_classic():
        return n_rows // 2
    k = layout.block_rows()
    return k if k and n_rows % k == 0 else n_rows


def plate_blocks(pd_: "sb.PlateData", opts: sq.MeasureOptions,
                 radius: "float | None" = None,
                 proc: "np.ndarray | None" = None, layout=None):
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
    """
    r_disp = float(pd_.radius if radius is None else radius)
    st = pd_.ref.path.stat()
    n_rows, n_cols = int(pd_.centers.shape[0]), int(pd_.centers.shape[1])
    per_rep = block_height(pd_, layout)
    key = (str(pd_.ref.path), st.st_mtime_ns, st.st_size, int(pd_.ref.plate),
           opts.rgb_mode, opts.bg_mode, opts.bg_iters, opts.shrink, r_disp,
           n_rows, n_cols, per_rep)
    hit = _BLOCK_CACHE.get(key)
    if hit is not None:
        _BLOCK_CACHE.move_to_end(key)
        return hit
    # The DECODE, unlike the subtraction below, really is dilution-independent,
    # so it is cached separately and shared across all three dilution choices.
    img8 = _load_gray8_cached(pd_.ref.path, opts.rgb_mode)
    # Same background treatment the numbers come from, so the picture and
    # the quantification are showing the same thing.
    #
    # `proc` lets the caller supply that subtraction ready-made. It is the same
    # array this branch would compute, reused from a Python subtraction batch.
    if proc is None:
        ball = opts.resolve_ball_radius(2 * r_disp / sq.MEASURE_RADIUS_FRAC)
        proc, _, _ = sq.subtract_background(
            img8, ball_radius=ball, bg_centers=[], bg_radius=r_disp,
            iters=opts.bg_iters, shrink=opts.shrink, mode=opts.bg_mode)
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


def build_montage(combo: str, plates: list[sb.PlateData], strains: list[str | None],
                  opts: sq.MeasureOptions, out_path: Path,
                  rep_label: str = "Replicant",
                  vmin: float | None = None, vmax: float | None = None,
                  mark_row: int | None = None,
                  mark_label: str | None = None,
                  bg_radius=None, proc=None, layout=None) -> Path:
    """Draw the montage. `mark_row`, if given, outlines one dilution row.

    `mark_row` is the row's index WITHIN a replicate block (0 = least dilute,
    2 = most), so the same index is outlined in all four blocks -- which is
    exactly how a dilution choice applies. It is drawn as an overlay: no pixel
    of the image is altered, and the display range is untouched.
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

    blocks, masks = [], []
    procs = list(proc) if proc is not None else [None] * len(plates)
    for pd_, r, pr in zip(plates, radii, procs):
        b, m = plate_blocks(pd_, opts, radius=r, proc=pr, layout=layout)
        blocks.extend(b)
        masks.extend(m)

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
    lab_w = 0.30 * bw / CELL_PX               # label gutter, in cells
    fig_w = (bw / CELL_PX + lab_w) * 0.42
    fig_h = (len(blocks) * bh / CELL_PX) * 0.42 + 1.5

    fig = plt.figure(figsize=(fig_w, fig_h), facecolor="black")
    gs = fig.add_gridspec(len(blocks), 1, hspace=0.04,
                          left=lab_w / (bw / CELL_PX + lab_w), right=0.99,
                          top=1 - 1.35 / fig_h, bottom=0.01)

    for k, blk in enumerate(blocks):
        ax = fig.add_subplot(gs[k, 0])
        ax.imshow(blk, cmap="gray", vmin=vmin, vmax=vmax,
                  interpolation="bilinear", aspect="equal")
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.set_facecolor("black")
        ax.text(-0.012, 0.5, f"{rep_label} {k + 1}", transform=ax.transAxes,
                color="white", fontsize=15, ha="right", va="center")
        if mark_row is not None:
            # A cell is CELL_PX across and row i is centred at
            # (PAD_CELLS + i) * CELL_PX -- the same mapping the strain labels
            # use, so the box lands square on the row.
            # Half-height is PAD_CELLS, not 0.5 cells: the block is cropped
            # exactly PAD_CELLS past the outer row centres, so a taller box gets
            # clipped by the panel edge when the marked row is the first or last
            # one -- which is precisely the case that matters most to see.
            y_mid = (PAD_CELLS + mark_row) * CELL_PX
            box_h = 2 * PAD_CELLS * CELL_PX
            ax.add_patch(Rectangle((0, y_mid - box_h / 2), bw, box_h,
                                   fill=False, edgecolor=MARK_COLOR,
                                   linewidth=2.2))
            if mark_label and k == 0:
                ax.text(bw + 0.05 * CELL_PX, y_mid, mark_label,
                        color=MARK_COLOR, fontsize=11, ha="left", va="center")
        if k == 0:
            for j, name in enumerate(strains):
                if not name:
                    continue
                x = (PAD_CELLS + j) * CELL_PX
                ax.text(x, -0.06 * bh, name, color="white", fontsize=14,
                        rotation=60, rotation_mode="anchor",
                        ha="left", va="bottom")

    if manual:
        fig.text(0.995, 0.004, f"display range {vmin:g}-{vmax:g} (adjusted)",
                 color="white", fontsize=8, ha="right", va="bottom")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, facecolor="black", bbox_inches="tight")
    plt.close(fig)
    return out_path


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
