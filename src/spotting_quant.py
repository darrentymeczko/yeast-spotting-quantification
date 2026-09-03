#!/usr/bin/env python3
"""
spotting_quant.py -- Automated quantification of yeast spotting assays.

Implements the manual ImageJ + Excel + R workflow from:
  Petropavlovskiy, Tauro, Lajoie & Duennwald,
  "A Quantitative Imaging-Based Protocol for Yeast Growth and Survival on
  Agar Plates", STAR Protocols 1:100182 (2020).

Pipeline (protocol steps 12-30):
  8-bit grayscale (ImageJ-compatible RGB conversion)              [step 13]
    -> rolling-ball background subtraction, single pass, float,
       no clipping; ImageJ's block-minimum shrink schedule        [steps 15-18]
    -> automatic 8x6 spot-grid detection, or hand-recorded ROIs   [implicit]
    -> per-spot mean gray over a fixed ImageJ-oval ROI            [steps 21-23]
    -> subtract mean background (may go negative -- kept)         [step 24]
    -> (interactive) pick one dilution row per replicate          [step 20]
    -> normalize each spot to the control in its row              [steps 26-27]
    -> mean +/- SD across replicates + one-way ANOVA + Tukey HSD  [steps 28-30]

CALIBRATION STATUS: the numbers this produces have NOT yet been shown to match
hand ImageJ measurement. The previous version disagreed badly (Pearson r 0.61,
mean abs error 0.35 on relative growth) and several causes have since been
fixed -- see subtract_background, measure_plate and _shrink_min for the three
that mattered most. Calibration against fresh hand-measured ground truth is
still outstanding; run tests/calibrate.py before trusting any output.

The signal here is small: spots read ~146-178 gray against ~151 agar, so the
entire usable range is ~28 gray units. Anything that costs 1-2 gray is a 5-10%
per-spot error that compounds through the control ratio. That is why the
grayscale conversion mode, the ROI mask rule and the shrink filter all matter
and are all configurable.

Plate layout (this lab):
  8 columns x 6 rows = 48 spots (48-pin frogger).
  Columns = strains (col 1 = control). Rows = serial dilutions grouped by
  biological replicate: rows 1-3 = replicate 1 (row 1 least dilute),
  rows 4-6 = replicate 2. Grown spots are BRIGHTER than the agar background.

Input folder layout (point the script at <root>):
  <root>/<treatment>/<plate>/<image>   e.g. Glucose/Plate 1/_9.JPG
  Each treatment has 2 plate folders; each plate folder holds one image to
  score. Two plates x two biological replicates = 4 replicates per strain.
  Each treatment is analyzed independently (controls are per-plate).

Usage:
  python spotting_quant.py <root> [--out results] [--debug] [--no-prompt]

  # export ROIs from one image for hand measurement in Fiji:
  python spotting_quant.py <image.jpg> --export-rois tests/gt/plate1/rois

  # measure at hand-recorded ROIs instead of auto-detecting:
  python spotting_quant.py <root> --rois tests/gt/plate1/gt_rois.csv

Grid detection has been verified good on real plates (all 48 spots correctly
located); the calibration risk is in the measurement maths, not the geometry.
Run with --debug for detection diagnostics.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration -- CALIBRATION TARGETS
# ---------------------------------------------------------------------------

N_COLS = 8            # strains across the plate (column 1 = control)
N_ROWS = 6            # dilution rows (1-3 = replicate 1, 4-6 = replicate 2)

# Strain per column, left -> right (column 1 is the control). Edit to match the
# plate layout; must have exactly N_COLS entries.
STRAIN_NAMES = ["WT BY", "ΔAIM25", "ΔGRX5", "ΔYHR1",
                "ΔPIM1", "ΔNCL1", "ΔGRE3", "ΔTRR2"]

BALL_PAD = 20         # added to the spot diameter to size the ball (px)
BG_TOL = 3.0          # max spread (gray units) allowed among 5 background samples
BG_ITERS = 1          # rolling-ball passes; see subtract_background on why >1 is wrong

N_BG_SAMPLES = 5      # background readings per plate (protocol step 22)

# ImageJ's Subtract Background dialog field is labelled RADIUS; the protocol
# text (step 15) specifies a ball DIAMETER of spot_diameter + 20 px. Which the
# original authors actually typed is unknowable from the paper, so it is
# recorded per-plate in gt_meta.csv and --ball-radius overrides everything.
# When True, the fallback treats (spot_diameter + BALL_PAD) as a diameter and
# halves it; when False it is passed to skimage as a radius verbatim.
BALL_PAD_IS_DIAMETER = False

DEFAULT_RGB_MODE = "unweighted"   # ImageJ factory default; "weighted" = 0.299/0.587/0.114

# Spot-measurement knob:
MEASURE_RADIUS_FRAC = 0.9  # measuring disk radius as fraction of detected spot radius

IMAGE_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}


# ---------------------------------------------------------------------------
# Image loading + 8-bit grayscale  (protocol step 13)
# ---------------------------------------------------------------------------

def load_gray8(path: Path, rgb_mode: str = DEFAULT_RGB_MODE,
               rescale: bool = True) -> np.ndarray:
    """Load any supported image as an 8-bit grayscale array (0-255).

    Reproduces ImageJ's RGB -> 8-bit conversion so gray values are directly
    comparable to hand ImageJ measurements:

      rgb_mode="unweighted"  (R+G+B)/3      -- ImageJ factory default
      rgb_mode="weighted"    0.299R + 0.587G + 0.114B
                             (Edit > Options > Conversions > "Weighted RGB
                             conversions"; record which was used in gt_meta.csv)

    The two differ by ~1.6 gray of differential signal on these plates, which is
    ~6% of the usable dynamic range -- a first-order calibration parameter, not
    a footnote. ImageJ rounds half UP; numpy's np.round is banker's rounding, so
    np.floor(x + 0.5) is used deliberately.

    Images already 8-bit are NOT contrast-stretched. 16-bit input is reduced by
    >> 8; float input passes through unscaled. A per-image min..max stretch is
    deliberately NOT applied -- it would destroy comparability between plates
    and would silently rescale the 32-bit TIFFs the Fiji cross-check writes.
    """
    if rgb_mode not in ("unweighted", "weighted"):
        raise ValueError(f"rgb_mode must be 'unweighted' or 'weighted', got {rgb_mode!r}.")

    ext = path.suffix.lower()
    if ext in {".tif", ".tiff"}:
        import tifffile
        arr = tifffile.imread(str(path))
    else:
        from PIL import Image
        arr = np.asarray(Image.open(path))

    arr = np.asarray(arr)
    if arr.ndim == 3:                       # drop alpha, collapse to luminance
        rgb = arr[..., :3].astype(np.float64)
        if rgb_mode == "weighted":
            arr = rgb @ np.array([0.299, 0.587, 0.114])
        else:
            arr = rgb.sum(axis=2) / 3.0
    arr = arr.astype(np.float64)

    if rescale and np.issubdtype(np.asarray(arr).dtype, np.floating) and arr.max() > 255:
        # Integer input wider than 8-bit (e.g. 16-bit TIFF): drop the low byte.
        # This is deterministic, unlike ImageJ's display-range-dependent 16->8
        # conversion, so record the source bit depth alongside any GT captured
        # from such an image.
        arr = arr / 256.0

    return np.clip(np.floor(arr + 0.5), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Rolling-ball background subtraction  (protocol steps 15-18)
# ---------------------------------------------------------------------------

def _shrink_factor(radius: float) -> int:
    """ImageJ BackgroundSubtracter's fixed shrink schedule (px radius -> factor).

    Estimating the background on a downscaled copy is what makes this tractable
    on 24 MP images -- skimage's rolling_ball cost scales with area x radius.
    ImageJ uses this exact schedule, so matching it removes one free parameter.

    UNRESOLVED -- measured on plate1.JPG at ball radius 107, one pass:

        shrink=8 (this schedule)    1.4 s   background spread 5.56   FAILS
        shrink=4                    6.1 s   background spread 4.14   FAILS
        shrink=2                   73.8 s   background spread 2.96   passes
        shrink=1                 >  550 s   impractical

    where "passes" means the protocol's own acceptance test (step 17: the five
    background reads must agree within BG_TOL = 3 gray). So the schedule ImageJ
    uses is the one setting that does NOT satisfy the protocol's criterion when
    paired with skimage's rolling_ball, and shrink=2 is 50x slower.

    That is not necessarily a contradiction: ImageJ's BackgroundSubtracter also
    pre-smooths 3x3, trims the paraboloid arc by radius, and clamps the computed
    background below the original pixel value. Copying one component of a
    different algorithm faithfully does not have to reproduce its result.

    The default is left on ImageJ's schedule deliberately -- switching to
    shrink=2 would be tuning against a proxy (the spread criterion) rather than
    against hand measurement, which is the mistake this whole rewrite exists to
    undo. Ground truth settles it: run
        python tests/calibrate.py --gt <dir> --sweep shrink 8,4,2
    and take whichever minimises the S3/S5 error against ImageJ, not whichever
    makes the background flattest.
    """
    if radius <= 10:
        return 1
    if radius <= 30:
        return 2
    if radius <= 100:
        return 4
    return 8


def _shrink_min(work: np.ndarray, factor: int) -> np.ndarray:
    """Downscale by `factor` taking the MINIMUM of each block.

    This is the critical detail. ImageJ's shrinkImage takes a block minimum;
    skimage.transform.resize(anti_aliasing=True) takes a block MEAN. A mean
    raises the shrunk image under and around bright spots, which raises the
    interpolated background exactly where the spots are, which then subtracts
    the faint spots away. On plates whose entire signal is ~28 gray units that
    is the difference between measuring a spot and measuring zero.
    """
    if factor <= 1:
        return work
    h, w = work.shape
    ph, pw = (-h) % factor, (-w) % factor
    if ph or pw:                       # pad with the edge value, not zeros
        work = np.pad(work, ((0, ph), (0, pw)), mode="edge")
    hh, ww = work.shape[0] // factor, work.shape[1] // factor
    return work.reshape(hh, factor, ww, factor).min(axis=(1, 3))


def _rolling_ball_background(work: np.ndarray, radius: float,
                             shrink: int | None = None) -> np.ndarray:
    """Rolling-ball background for `work`, estimated on a block-minimum
    downscaled copy (ImageJ's schedule) and bilinearly enlarged back.

    Pass shrink=1 to disable downscaling entirely -- accurate but minutes, not
    seconds, on a 24 MP image.
    """
    from skimage.restoration import rolling_ball
    from skimage.transform import resize

    factor = _shrink_factor(radius) if shrink is None else max(1, int(shrink))
    if factor == 1:
        return rolling_ball(work, radius=radius)

    small = _shrink_min(work, factor)
    bg_small = rolling_ball(small, radius=max(1.0, radius / factor))
    return resize(bg_small, work.shape, order=1, preserve_range=True)


# ---------------------------------------------------------------------------
# Sliding paraboloid  (what protocol step 15 actually specifies)
# ---------------------------------------------------------------------------

def _fh_lower_envelope(f: np.ndarray, coeff2: float) -> np.ndarray:
    """D[q] = min over p of ( f[p] + coeff2*(q-p)^2 ), independently for every
    column, down axis 0.

    This is the Felzenszwalb & Huttenlocher (2012) lower-envelope-of-parabolas
    scan: O(n) per column rather than O(n*radius), which is what makes a
    full-resolution paraboloid affordable on a 24 MP plate photo. The scan is
    sequential in q but identical in form for every column, so it is vectorised
    ACROSS columns -- the inner "pop a parabola" loop runs until no column still
    needs popping instead of per column.
    """
    n, m = f.shape
    if n == 1:
        return f.copy()

    cols = np.arange(m)
    v = np.zeros((n, m), dtype=np.intp)          # vertex index of each parabola
    z = np.empty((n + 1, m), dtype=np.float64)   # breakpoints between parabolas
    z[0] = -np.inf
    z[1] = np.inf
    k = np.zeros(m, dtype=np.intp)               # index of the current parabola

    for q in range(1, n):
        fq = f[q]
        cq = coeff2 * q * q
        while True:
            vk = v[k, cols]
            s = (((fq + cq) - (f[vk, cols] + coeff2 * vk * vk))
                 / (2.0 * coeff2 * (q - vk)))
            bad = s <= z[k, cols]                # z[0] is -inf, so k never < 0
            if not bad.any():
                break
            k[bad] -= 1
        k += 1
        v[k, cols] = q
        z[k, cols] = s
        z[k + 1, cols] = np.inf

    out = np.empty_like(f)
    k[:] = 0
    for q in range(n):
        while True:
            adv = z[k + 1, cols] < q
            if not adv.any():
                break
            k[adv] += 1
        vk = v[k, cols]
        out[q] = coeff2 * (q - vk) ** 2 + f[vk, cols]
    return out


def _para_erode(a: np.ndarray, c: float) -> np.ndarray:
    """Greyscale erosion by the parabolic structuring function -c*y^2."""
    return _fh_lower_envelope(a, c)


def _para_dilate(a: np.ndarray, c: float) -> np.ndarray:
    """Greyscale dilation by the same parabolic structuring function."""
    return -_fh_lower_envelope(-a, c)


def paraboloid_background(work: np.ndarray, radius: float,
                          presmooth: bool = True) -> np.ndarray:
    """Background under `work` traced by a paraboloid of curvature 0.5/radius
    sliding beneath the surface -- ImageJ's "sliding paraboloid" option, which
    is what the protocol (step 15) actually specifies.

    Sliding a shape under a surface and taking the envelope of its positions IS
    the morphological OPENING by that shape, and the paraboloid
    c*(x^2 + y^2) is separable, so the exact 2-D result is

        open = dilate_x( dilate_y( erode_y( erode_x(f) ) ) )

    with each 1-D step an O(n) lower-envelope scan. ImageJ approximates this by
    sliding along x, y and both diagonals repeatedly; the separable form is the
    thing that approximation converges to, so this is at least as faithful and
    needs no shrink schedule -- which removes the block-MIN / block-MEAN
    dilemma that made the rolling-ball path either fail to flatten (MIN) or eat
    ~2x of the spot signal (MEAN).

    presmooth mirrors ImageJ: estimate on a 3x3-mean-smoothed copy so single-
    pixel noise cannot pin the paraboloid down, then clamp the background to
    never exceed the true pixel value.
    """
    coeff2 = 0.5 / max(1.0, float(radius))

    src = work
    if presmooth:
        from scipy import ndimage as ndi
        src = ndi.uniform_filter(work, size=3, mode="nearest")

    e = _para_erode(np.ascontiguousarray(src), coeff2)
    e = _para_erode(np.ascontiguousarray(e.T), coeff2).T
    d = _para_dilate(np.ascontiguousarray(e), coeff2)
    d = _para_dilate(np.ascontiguousarray(d.T), coeff2).T

    bg = np.ascontiguousarray(d)
    if presmooth:
        bg = np.minimum(bg, work)     # background can never exceed the data
    return bg


# ---------------------------------------------------------------------------
# FIJI backend  (the reference implementation, driven headlessly)
# ---------------------------------------------------------------------------

# Where to look for FIJI. First hit wins; None disables the backend.
FIJI_CANDIDATES = [
    r"C:/Program Files/Fiji.app/ImageJ-win64.exe",
    r"C:/Program Files/Fiji.app/fiji-windows-x64.exe",
    r"C:/Program Files/Fiji.app/ImageJ-win32.exe",
]

_FIJI_MACRO = """
args = getArgument();
p = split(args, "|");
open(p[0]);
run("8-bit");
run("32-bit");
run("Subtract Background...", "rolling=" + p[2] + " sliding");
saveAs("Tiff", p[1]);
close();
"""


def find_fiji() -> str | None:
    """Path to a FIJI executable, or None."""
    import shutil
    for c in FIJI_CANDIDATES:
        if Path(c).is_file():
            return c
    return shutil.which("ImageJ-win64") or shutil.which("fiji")


def fiji_subtract_background(image_path: Path, radius: float,
                             debug: bool = False) -> np.ndarray | None:
    """Run FIJI's own Subtract Background (sliding paraboloid) on a file.

    This is the reference implementation of the operation the protocol calls
    for, and on this corpus it flattens slightly better than the local one:
    background spread 0.08 gray against 0.32 on 2.1K-OAc, measured through the
    identical ROIs. It is also the tool the lab's earlier quantifications were
    done in, so results stay comparable with them.

    The macro converts to 8-bit (matching ImageJ's own RGB conversion) and then
    to 32-bit BEFORE subtracting, because an 8-bit result is clipped at zero.
    Clipping rectifies the noise around the agar, biasing the background upward
    and pushing faint spots negative once that inflated value is subtracted.

    Returns the subtracted image as float64, or None if FIJI is unavailable or
    fails -- the caller then falls back to the local implementation.
    """
    import subprocess
    import tempfile

    exe = find_fiji()
    if not exe:
        return None
    try:
        import tifffile
    except ImportError:
        return None

    with tempfile.TemporaryDirectory(prefix="spotfiji_") as td:
        macro = Path(td) / "bg.ijm"
        macro.write_text(_FIJI_MACRO, encoding="utf-8")
        out = Path(td) / "out.tif"
        arg = f"{Path(image_path).resolve()}|{out}|{radius:.0f}"
        try:
            r = subprocess.run([exe, "--headless", "--console", "-macro",
                                str(macro), arg],
                               capture_output=True, text=True, timeout=600,
                               encoding="utf-8", errors="replace")
        except Exception as e:
            if debug:
                print(f"    (FIJI failed to launch: {e})")
            return None
        if not out.exists():
            if debug:
                print(f"    (FIJI produced no output; rc={r.returncode})")
            return None
        return tifffile.imread(str(out)).astype(np.float64)


def subtract_background(img8: np.ndarray, *, ball_radius: float,
                        bg_centers: list[tuple[float, float]], bg_radius: float,
                        iters: int = BG_ITERS, shrink: int | None = None,
                        mode: str = "paraboloid", fiji_path: Path | None = None,
                        debug: bool = False) -> tuple[np.ndarray, int, float]:
    """Subtract the rolling-ball background (protocol steps 15-18).

    Returns (processed_image_float64, n_iterations, final_background_spread).

    Two deliberate departures from the earlier implementation:

    * The result is float64, not uint8. Casting to uint8 truncates toward zero,
      losing ~0.5 gray per pixel on a signal whose full range is ~28 gray -- a
      ~2% systematic loss applied after subtraction, where it cannot be reasoned
      about.

    * Nothing is clipped at zero. After a good subtraction the agar sits at ~0
      with symmetric noise; clipping RECTIFIES that noise, and the mean of
      max(0, N(0,s)) is s/sqrt(2*pi) ~ 0.4s. The five background disks then read
      systematically high, measure_plate subtracts that inflated value from
      every spot, and faint spots are driven negative. Negative net growth is
      meaningful (no growth) and must be allowed to propagate.

    `iters` defaults to 1. Rolling-ball subtraction is NOT idempotent: once the
    background is flat the spots are the only structure left, so further passes
    carve into them. The protocol's "repeat until the background readings agree"
    is a human instruction whose intended remedy is choosing a better ball. The
    BG_TOL check is therefore reported as a diagnostic, not used as a loop
    condition.
    """
    work = img8.astype(np.float64)
    radius = max(1.0, ball_radius)

    spread = 0.0
    n = max(1, int(iters))
    for it in range(1, n + 1):
        if mode == "fiji":
            # FIJI returns the SUBTRACTED image, not the background, so it
            # replaces `work` outright rather than being subtracted from it.
            res = fiji_subtract_background(fiji_path, radius, debug=debug)
            if res is None:
                if debug:
                    print("    (FIJI unavailable; using the local paraboloid)")
                bg = paraboloid_background(work, radius)
            else:
                work = res
                bg = None
        elif mode == "paraboloid":
            bg = paraboloid_background(work, radius)
        elif mode == "rollingball":
            bg = _rolling_ball_background(work, radius, shrink=shrink)
        else:
            raise ValueError(f"unknown background mode {mode!r}")
        if bg is not None:
            work = work - bg
        samples = [_disk_mean(work, cy, cx, bg_radius) for cy, cx in bg_centers]
        spread = (max(samples) - min(samples)) if samples else 0.0
        if debug:
            extra = ("" if mode == "paraboloid" else
                     f", shrink={_shrink_factor(radius) if shrink is None else shrink}")
            print(f"    {mode} iter {it} (r={radius:.0f}{extra}): "
                  f"background spread = {spread:.2f}")

    if spread > BG_TOL:
        print(f"    ! background did not flatten: spread = {spread:.2f} > "
              f"{BG_TOL} gray units. Check --ball-radius (currently {radius:.0f}) "
              f"or the evenness of the plate lighting; do NOT simply iterate more.")
    return work, n, spread


def _roi_mean(img: np.ndarray, bx: float, by: float,
              width: float, height: float) -> tuple[float, int]:
    """Mean over an oval ROI given as an ImageJ bounding box; returns (mean, n_px).

    Matches ImageJ's OvalRoi.getMask(), which tests the PIXEL CENTRE against the
    ellipse -- pixel (X, Y) is inside when

        ((X + 0.5 - cx) / rx)^2 + ((Y + 0.5 - cy) / ry)^2 <= 1

    with cx = bx + width/2. The difference from testing integer indices is only a
    handful of boundary pixels, but calibration stage S1 gates at 0.5 gray, so
    the ambiguity is worth removing outright.

    n_px is returned so the calibration harness can compare it against ImageJ's
    reported `Area`: a pixel-count mismatch is an instant, unambiguous signal
    that the geometry is wrong rather than the values.
    """
    h, w = img.shape
    rx, ry = width / 2.0, height / 2.0
    cx, cy = bx + rx, by + ry

    x0, x1 = max(0, int(np.floor(bx))), min(w, int(np.ceil(bx + width)))
    y0, y1 = max(0, int(np.floor(by))), min(h, int(np.ceil(by + height)))
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0

    ys, xs = np.ogrid[y0:y1, x0:x1]
    mask = (((xs + 0.5 - cx) / max(rx, 1e-9)) ** 2
            + ((ys + 0.5 - cy) / max(ry, 1e-9)) ** 2) <= 1.0
    n_px = int(mask.sum())
    if not n_px:
        return 0.0, 0
    return float(img[y0:y1, x0:x1][mask].mean()), n_px


def _disk_mean(img: np.ndarray, cy: float, cx: float, r: float) -> float:
    """Mean value of pixels within radius r of (cy, cx). Thin wrapper on
    _roi_mean for callers that think in centres rather than bounding boxes."""
    return _roi_mean(img, cx - r, cy - r, 2 * r, 2 * r)[0]


# ---------------------------------------------------------------------------
# Automatic grid detection  (CALIBRATION TARGET)
# ---------------------------------------------------------------------------

# Detection tuning (see _debug_detect.png overlays; calibrated on real plates):
DETECT_LONG_SIDE = 1500   # downscale so the long image side is ~this for detection
PLATE_THRESH = 70         # gray level separating the bright plate from black table
RIM_ERODE_FRAC = 0.12     # erode plate radius inward by this to drop the bright rim
SPOT_RADIUS_FRAC = 0.05   # spot radius as a fraction of plate radius (initial guess)
SPACING_FRAC = 0.197      # spot-to-spot spacing as a fraction of plate radius
                          # (frogger geometry; near-constant across the lab's plates)

# An ROI whose far edge reaches past this fraction of the plate radius can pick
# up the bright rim / meniscus glare. Measured case: plate2 row 6 ΔTRR2 is empty
# agar but read 1.67x the control because rim glare clipped the ROI, which was
# the single largest disagreement with the hand quantification.
RIM_SAFE_FRAC = 0.86

# Fraction of a measuring ROI that must overlap an artifact before the spot is
# flagged. Not "touches at all": a sliver of rim on one edge moves the mean by a
# fraction of a gray level, and over-flagging costs real data -- on these plates
# it flagged the row's WT control, which the whole row is normalised against.
ARTIFACT_OVERLAP_FRAC = 0.10

# A near-the-rim ROI is only reported as contaminated if this fraction of its
# pixels is actually rim-bright. Position alone is not evidence: measured on
# 2.x K-OAc, corner ROIs at 0.87-0.90R contained 0.0-0.1% rim-bright pixels
# (clean spots, wrongly excluded and costing replicates) while the genuinely
# spoiled one at 0.95R contained 36%.
RIM_BRIGHT_FRAC = 0.08
# How far above the plate's own colony brightness a pixel must be to count as
# rim/meniscus glare. Glare is far brighter than any colony, so this test can
# never fire on faint growth -- which is the failure this whole flag has to avoid.
RIM_BRIGHT_MARGIN = 12.0


@dataclass
class Grid:
    centers: np.ndarray          # shape (N_ROWS, N_COLS, 2) -> full-res (y, x)
    spot_radius: float           # full-res
    measure_radius: float        # full-res disk used for spot + background reads
    largest_diameter: float      # full-res
    plate_center: tuple          # full-res (y, x)
    plate_radius: float          # full-res
    plate_known: bool = True     # False when the geometry was inferred from
                                 # hand ROIs rather than measured off the image,
                                 # in which case rim_flags cannot be computed
    artifact_flag: np.ndarray | None = None   # N_ROWS x N_COLS bool: ROI overlaps
                                 # a non-spot bright structure (plate rim, the
                                 # frosted label rectangle, bolts)
    spot_radii: np.ndarray | None = None      # per-spot footprint radius
    spot_exists: np.ndarray | None = None     # per-spot "is there a spot at all"
    base_radius: float = 0.0     # ROI radius BEFORE the dilution-row shrink.
                                 # With these three, roi_radius_for_rows() gives
                                 # the ROI for any row choice in ~0 time, which
                                 # is what lets every dilution be precomputed:
                                 # detection is 41s, the shrink is 0.9s.


def _remove_small(mask, n_px):
    """remove_small_objects across skimage versions.

    0.26 deprecated `min_size` in favour of `max_size`. Verified equivalent on
    this data: min_size=N and max_size=N keep exactly the same objects (the old
    parameter already dropped objects of size <= N, despite the name).
    """
    from skimage.morphology import remove_small_objects
    try:
        return remove_small_objects(mask, max_size=int(n_px))
    except TypeError:                        # skimage < 0.26
        return remove_small_objects(mask, int(n_px))


def _white_tophat(g: np.ndarray, footprint: np.ndarray) -> np.ndarray:
    """`skimage.morphology.white_tophat`, via OpenCV when it is installed.

    The disc here is ~80 px across on the detection image, and scipy's greyscale
    opening costs 14.6 s at that size -- on its own the second-largest slice of
    a measurement. OpenCV runs the identical morphology with SIMD kernels in
    4.2 s.

    Verified bit-for-bit (`np.array_equal`) against the skimage result on real
    plates, which is the only reason this is safe to swap in: the tophat feeds
    spot detection, so any numerical difference would move every centre, change
    every measurement, and invalidate every cached .npz. Border handling is the
    one place the two could disagree, so it is pinned to REFLECT to match
    scipy's default rather than left at OpenCV's constant border.

    Falls back to skimage if OpenCV is missing or refuses the input.
    """
    try:
        import cv2
        return cv2.morphologyEx(g, cv2.MORPH_TOPHAT,
                                np.asarray(footprint, dtype=np.uint8),
                                borderType=cv2.BORDER_REFLECT)
    except Exception:
        from skimage.morphology import white_tophat
        return white_tophat(g, footprint)


def _closing(mask, footprint):
    """binary_closing -> closing (0.26). Identical for boolean input and the
    symmetric disk footprints used here."""
    try:
        from skimage.morphology import closing
        return closing(mask, footprint)
    except ImportError:                      # pragma: no cover
        from skimage.morphology import binary_closing
        return binary_closing(mask, footprint)


def _dilation(mask, footprint):
    """binary_dilation -> dilation (0.26). Identical for boolean input and the
    symmetric disk footprints used here."""
    try:
        from skimage.morphology import dilation
        return dilation(mask, footprint)
    except ImportError:                      # pragma: no cover
        from skimage.morphology import binary_dilation
        return binary_dilation(mask, footprint)


def detect_grid(img8: np.ndarray, debug: bool = False,
                nudge: dict | None = None,
                quant_rows: tuple | None = None) -> Grid:
    """Locate the N_ROWS x N_COLS spot lattice fully automatically on a real
    plate photographed on a dark background.

    Pipeline: downscale -> find the agar disc as the largest INSCRIBED circle
    (distance-transform peak, immune to the bright bracket/bolts touching the
    plate) -> circular agar mask -> white top-hat to pull spot-sized bright
    blobs off the smooth agar -> detect spot centroids -> fit a rigid lattice
    with near-constant spacing (per-axis 1-D fit + best-window selection), so
    it locks on even when most spots are faint or missing. Orientation: row 1
    at TOP, column 1 (control) at LEFT.
    """
    from skimage.transform import resize
    from skimage.filters import threshold_otsu
    from skimage.measure import label, regionprops
    from skimage.morphology import disk
    from scipy import ndimage as ndi

    ds = max(1, round(max(img8.shape) / DETECT_LONG_SIDE))
    g = resize(img8.astype(float), (img8.shape[0] // ds, img8.shape[1] // ds),
               order=1, preserve_range=True) if ds > 1 else img8.astype(float)

    # 1) plate disc via largest inscribed circle (robust to the bracket)
    plate = ndi.binary_fill_holes(g > PLATE_THRESH)
    lbl = label(plate)
    if lbl.max() == 0:
        raise RuntimeError("No bright plate region found (check PLATE_THRESH).")
    pp = max(regionprops(lbl), key=lambda p: p.area)
    dt = ndi.distance_transform_edt(lbl == pp.label)
    cy, cx = (float(v) for v in np.unravel_index(np.argmax(dt), dt.shape))
    r_eq = float(dt.max())

    # 2) circular agar mask (rim excluded)
    Y, X = np.ogrid[:g.shape[0], :g.shape[1]]
    agar = (X - cx) ** 2 + (Y - cy) ** 2 < ((1 - RIM_ERODE_FRAC) * r_eq) ** 2

    # 3) white top-hat isolates spot-sized bright features
    sr = max(6, int(r_eq * SPOT_RADIUS_FRAC))
    th = _white_tophat(g, disk(int(sr * 1.6)))
    th_in = np.where(agar, th, 0.0)

    # 3b) spot centroids + size estimate
    vpos = th_in[agar]; vpos = vpos[vpos > 0]
    spot_diam_ds = 2 * sr
    cen = np.empty((0, 2))
    if vpos.size:
        m = (th_in > threshold_otsu(vpos)) & agar
        m = _remove_small(m, int(np.pi * (sr * 0.5) ** 2))
        bp = [p for p in regionprops(label(m)) if p.eccentricity < 0.9]
        if bp:
            areas = np.array([p.area for p in bp]); med = np.median(areas)
            good = [p for p in bp if 0.3 * med <= p.area <= 3 * med]
            if good:
                spot_diam_ds = float(np.median(
                    [p.equivalent_diameter_area for p in good]))
                cen = np.array([[p.centroid[1], p.centroid[0]] for p in good])
    if len(cen):   # drop any off-plate false positives (bracket/label)
        cen = cen[np.hypot(cen[:, 0] - cx, cen[:, 1] - cy) < 0.9 * r_eq]

    # 4) rigid lattice fit (near-constant spacing, tilt-aware)
    s, lattice, tilt = _fit_lattice(cen, cx, cy, r_eq)

    # 4b) mask non-spot bright structures so ROI centring cannot chase them
    spot_radius = spot_diam_ds / 2.0 * ds
    measure_radius = spot_radius * MEASURE_RADIUS_FRAC
    avoid, flag_mask = _artifact_mask(g, agar, cx, cy, r_eq, spot_diam_ds)
    th_clean = np.where(avoid, 0.0, th_in)

    # 4c) centre each ROI on its spot, using the cleaned map
    centers_ds = _refine_centers(th_clean, lattice, s,
                                 measure_radius / ds, debug=debug)
    centers = centers_ds * ds

    # 4c-bis) re-centre at FULL resolution. Everything above ran at 1/ds scale.
    # Two passes: the disc has to match the spot, but the spot size is only
    # trustworthy once the centres are, so centre with the detection-pass
    # estimate, measure the diameter properly, then centre again with it.
    plate_c = (cy * ds, cx * ds)
    plate_r = r_eq * ds
    centers = refine_centers_fullres(img8, centers, spot_radius,
                                     plate_c, plate_r, debug=debug)
    fw = measure_spot_diameter(img8, centers, debug=debug)
    if fw is not None:
        # Iterate: each pass measures against better centres than the last, so a
        # spot the previous pass could not resolve often resolves now. One pass
        # left ~11px of movement still on the table.
        for _ in range(FULLRES_MAX_ITERS):
            prev = centers
            centers = refine_centers_fullres(img8, centers, fw / 2.0,
                                             plate_c, plate_r, debug=debug)
            moved = float(np.median(np.hypot(*(centers - prev).reshape(-1, 2).T)))
            fw = measure_spot_diameter(img8, centers, debug=debug) or fw
            if moved < FULLRES_SETTLED_PX:
                break
        if FULLRES_TEXTURE_RESCUE:
            centers = refine_centers_fullres(img8, centers, fw / 2.0, plate_c,
                                             plate_r, texture_rescue=True,
                                             debug=debug)
            fw = measure_spot_diameter(img8, centers, debug=debug) or fw

    # 4c-and-a-half) manual overrides. Detection is good but not infallible, and
    # on a faint spot lying on the plate's brightness gradient every automatic
    # estimator can agree on a position that is visibly wrong to someone who
    # knows the plate. A nudge recorded in the config is explicit and reviewable;
    # a constant tuned until one spot looks right is neither.
    if nudge:
        for (ri, ci), (dy, dx) in nudge.items():
            if 1 <= ri <= N_ROWS and 1 <= ci <= N_COLS:
                centers[ri - 1, ci - 1] += (float(dy), float(dx))
                if debug:
                    print(f"    manual nudge r{ri}c{ci}: ({dy:+.0f},{dx:+.0f}) px")

    # 4c-ter) size the measuring ROI from the growth footprint. Until now it came
    # from the detection-pass Otsu estimate, which reads ~20% small on faint
    # media -- a 159px ROI on a 205px spot, measuring the dim centre and
    # excluding the dense outer ring where most of the growth is.
    foot = measure_spot_footprint(img8, centers, debug=debug)
    if foot is not None:
        spot_radius = foot / 2.0
        measure_radius = spot_radius * ROI_FOOTPRINT_FRAC
    centers_ds = centers / ds

    # 4c-and-three-quarters) shrink the ROI so it fits inside every spot being
    # compared, per the protocol. See measure_spot_radii for the reasoning, and
    # roi_radius_for_rows for why this is split out.
    spot_radii, spot_exists = measure_spot_radii(img8, centers, measure_radius,
                                                plate_c, plate_r)
    base_radius = measure_radius
    undersized = np.zeros(spot_radii.shape, bool)
    if quant_rows:
        measure_radius, _, _ = roi_radius_for_rows(
            spot_radii, spot_exists, quant_rows, base_radius, debug=debug)

    # 4c-quater) verify each ROI placement against that spot's own outline.
    # A spot with NO growth also has no outline, but its ROI reads agar wherever
    # it sits, so its position does not matter and a zero is a valid result.
    # Only spots with real signal AND an unresolvable edge are flagged.
    radii = measure_spot_outlines(img8, centers, measure_radius, plate_c, plate_r)
    net = np.array([[_spot_net(img8, centers[i, j], measure_radius)
                     for j in range(N_COLS)] for i in range(N_ROWS)])
    ref = float(np.nanmax(net)) if np.isfinite(net).any() else 0.0
    strong = np.isfinite(radii) & (net > OUTLINE_STRONG * ref) if ref > 0         else np.zeros(radii.shape, bool)
    good_r = radii[strong]
    unverified = np.zeros(radii.shape, bool)
    if good_r.size >= 6:
        med_r = float(np.median(good_r))
        has_signal = (net > OUTLINE_MIN_SIGNAL * ref) if ref > 0             else np.zeros(net.shape, bool)
        bad = ~np.isfinite(radii) | (np.abs(radii - med_r) > OUTLINE_TOL * med_r)
        unverified = bad & has_signal
        if debug and unverified.any():
            spots = [(i + 1, j + 1) for i in range(N_ROWS) for j in range(N_COLS)
                     if unverified[i, j]]
            print(f"    outline check: median radius {med_r:.0f}px; "
                  f"unverifiable placement at {spots}")

    # 4d) flag ROIs that still overlap an artifact
    art_flag = _rim_contaminated(g, centers_ds, measure_radius / ds,
                                 cx, cy, r_eq)
    art_flag = art_flag | unverified | undersized
    if debug and art_flag.any():
        print(f"    artifact-adjacent ROIs: "
              f"{[(i+1, j+1) for i in range(N_ROWS) for j in range(N_COLS) if art_flag[i, j]]}")
    # 4e) re-measure the spot diameter at FULL resolution now that the centres
    # are known. Only `largest_diameter` -- which sizes the sliding paraboloid --
    # is taken from this. spot_radius/measure_radius keep the detection-pass
    # value: the ROI geometry is what the hand-quantification was validated
    # against, and resizing it would move every number in the dataset.
    largest_diameter = fw if fw is not None else spot_diam_ds * ds

    if debug:
        print(f"    plate r~{r_eq*ds:.0f}px, spot diam~{spot_diam_ds*ds:.0f}px, "
              f"spacing~{s*ds:.0f}px, tilt={tilt:+.2f}deg, "
              f"spots detected={len(cen)}")
    return Grid(centers=centers, spot_radius=spot_radius,
                measure_radius=measure_radius, largest_diameter=largest_diameter,
                plate_center=(cy * ds, cx * ds), plate_radius=r_eq * ds,
                artifact_flag=art_flag, spot_radii=spot_radii,
                spot_exists=spot_exists, base_radius=base_radius)


# Fraction of the spot's plateau intensity that marks its edge.
#
# Half-maximum (0.5) is the textbook choice -- for a sharp-edged disc seen
# through a symmetric blur it sits on the true edge regardless of contrast.
# A yeast spot is NOT such a disc: it carries a diffuse optical halo past the
# growth, so 0.5 lands outside the colonies. Checked two ways on 2.1K-OAc:
# circles overlaid on the nine best-grown spots, and a radial profile of local
# texture (the spot is speckled with micro-colonies, bare agar is not, so
# texture marks the growth edge independently of brightness AND of baseline).
# Texture falls to half at 192 px and to nothing by 240 px; half-maximum said
# 248 px, i.e. past any remaining colonies. 0.70 reproduces the texture edge
# and the hand measurement (~200 px) on that plate.
#
# The two estimators do not agree on one constant across media (on glucose
# texture reads much wider), so this is a calibrated choice, not a derived one.
# It matters less than it looks: relative growth moves by hundredths anywhere in
# R = 197-267. What the radius really controls is how much spot signal the
# subtraction eats -- raw values rise ~5% going from R=197 to R=267.
EDGE_FRAC = 0.70
# Spots used to define the footprint: the best-grown this fraction of the plate.
# A barely-grown spot has no plateau to take a half-maximum of.
DIAM_STRENGTH_PCT = 60.0


def _nearest_center_dist(ys: np.ndarray, xs: np.ndarray,
                         C: np.ndarray, grid_like: bool = False) -> np.ndarray:
    """Distance from every pixel of a patch to the nearest of the 48 centres.

    The same field is needed by every full-resolution pass -- centring, the
    diameter profile, the footprint, the outline check, the per-spot radii --
    which between them rebuild it about fifteen times per plate over patches of
    a few hundred thousand pixels. This used to be a cKDTree query over all 48
    centres for every pixel.

    The saving is locality, not the tree. A patch spans a fraction of the plate,
    so almost every centre is far too distant to be nearest to any pixel in it,
    and can be discarded before any per-pixel work happens. The test below is
    exact, not a heuristic: with pc the patch centre and R its half-diagonal,
    every pixel p satisfies |p - c| >= |pc - c| - R and |p - c_j| <= |pc - c_j| +
    R, so a centre that is nearest to some pixel must have |pc - c| <=
    min_j |pc - c_j| + 2R. Keeping exactly that set cannot change any answer.
    In practice it leaves 1-9 of the 48. Verified against the cKDTree result,
    bit-for-bit, for all 48 spots at each patch size the callers use.

    `grid_like` promises that `ys` and `xs` came from `np.mgrid` -- ys constant
    along each row, xs constant down each column. That lets the squares be
    formed on the two 1-D axes and broadcast, so no (H, W, n) array is built at
    all: one H*W pass per surviving centre instead of one H*W*n pass through
    temporaries several times the size of the patch. Measured 6-13x faster
    across the patch sizes the callers use, bit-for-bit identical to the general
    path. It is a parameter rather than something sniffed from the arrays
    because a wrong guess would silently corrupt every measurement; a caller
    that does not pass it just gets the general path.

    Chunked over rows so the (H, W, n) intermediate never lands in memory whole.
    """
    C = np.asarray(C, dtype=float)
    ys = np.asarray(ys, dtype=float)
    xs = np.asarray(xs, dtype=float)

    pcy = 0.5 * (float(ys[0, 0]) + float(ys[-1, -1]))
    pcx = 0.5 * (float(xs[0, 0]) + float(xs[-1, -1]))
    half_diag = 0.5 * float(np.hypot(ys[-1, -1] - ys[0, 0],
                                     xs[-1, -1] - xs[0, 0]))
    d = np.hypot(C[:, 0] - pcy, C[:, 1] - pcx)
    C = C[d <= d.min() + 2.0 * half_diag]

    if grid_like and C.shape[0] and ys.ndim == 2:
        yv = ys[:, 0]
        xv = xs[0, :]
        best = None
        for cy, cx in C:
            d2 = ((yv - cy) ** 2)[:, None] + ((xv - cx) ** 2)[None, :]
            best = d2 if best is None else np.minimum(best, d2, out=best)
        return np.sqrt(best, out=best)

    out = np.empty(ys.shape, dtype=float)
    rows = max(1, int(4_000_000 // max(ys.shape[1] * C.shape[0], 1)))
    for a in range(0, ys.shape[0], rows):
        b = min(a + rows, ys.shape[0])
        dy = ys[a:b, :, None] - C[None, None, :, 0]
        dx = xs[a:b, :, None] - C[None, None, :, 1]
        np.sqrt((dy * dy + dx * dx).min(axis=2), out=out[a:b])
    return out


def _radial_bin_mean(rr: np.ndarray, values: np.ndarray, mask: np.ndarray,
                     bins: np.ndarray, step: float,
                     min_px: int = 30) -> np.ndarray:
    """Mean of `values` in each annulus of `bins`, over the pixels in `mask`.

    Equivalent to the obvious loop --

        for k, a in enumerate(bins):
            sel = (rr >= a) & (rr < a + step) & mask
            if sel.sum() > min_px:
                out[k] = values[sel].mean()

    -- but in one pass instead of one full-patch boolean comparison per bin.
    With ~100 bins over a ~300k-pixel patch, 48 spots, and the caller running up
    to seven times per plate, that loop was tens of thousands of full-patch
    passes. Bins are empty (NaN) below the pixel floor exactly as before.

    The sums are accumulated in a different order than a per-bin mean, so
    results agree to floating-point round-off rather than bit-for-bit.
    """
    r = rr[mask]
    v = values[mask]
    idx = np.floor(r / step).astype(np.intp)
    keep = (idx >= 0) & (idx < bins.size)
    idx, v = idx[keep], v[keep]
    cnt = np.bincount(idx, minlength=bins.size)
    tot = np.bincount(idx, weights=v, minlength=bins.size)
    out = np.full(bins.size, np.nan)
    ok = cnt > min_px
    out[ok] = tot[ok] / cnt[ok]
    return out


def measure_spot_diameter(img8: np.ndarray, centers: np.ndarray,
                          debug: bool = False) -> float | None:
    """Spot diameter from the mean radial intensity profile, at full resolution.

    The detection pass sizes spots by Otsu-thresholding a white top-hat of the
    DOWNSCALED image. That estimate is biased small, and biased by an amount
    that depends on the medium: Otsu puts its cut partway up the spot's
    intensity profile, and the fainter and softer the spot's edge, the further
    inside the true footprint that cut lands. Measured against this function it
    reads ~10-25% low on K-OAc and glycerol but is nearly exact on glucose.

    Here the profile is built at full resolution instead, and the edge is taken
    at half of the spot's plateau intensity, which is contrast-independent.

    Two details matter:

    * The baseline is agar at least 0.42 pitch from EVERY spot centre. An
      annulus around the spot itself does not work -- neighbouring spots are
      only one pitch away, so a wide annulus sits on top of them and the
      baseline comes out too high, which inflates the apparent diameter.
    * Each annulus average uses only pixels closer to this spot than to any
      other, so the tail of one spot never counts toward its neighbour's.

    Returns None when too few spots are usable, in which case the caller should
    keep the detection-pass estimate.
    """
    C = np.asarray(centers, float).reshape(-1, 2)
    if len(C) < 4:
        return None
    pitch = float(np.median(np.diff(np.asarray(centers, float)[0, :, 1])))
    if not np.isfinite(pitch) or pitch <= 0:
        return None

    rmax = 0.70 * pitch
    step = max(2.0, pitch / 100.0)
    bins = np.arange(0.0, rmax, step)
    n_inner = max(3, int(round(0.20 * pitch / step)))   # the flat top

    profiles, strength = [], []
    for cy, cx in C:
        y0, y1 = int(cy - rmax), int(cy + rmax)
        x0, x1 = int(cx - rmax), int(cx + rmax)
        if y0 < 0 or x0 < 0 or y1 >= img8.shape[0] or x1 >= img8.shape[1]:
            continue
        ys, xs = np.mgrid[y0:y1, x0:x1]
        rr = np.hypot(ys - cy, xs - cx)
        patch = img8[y0:y1, x0:x1].astype(float)
        dmin = _nearest_center_dist(ys, xs, C, grid_like=True)
        clear = dmin > 0.42 * pitch
        if clear.sum() < 500:
            continue
        own = dmin >= rr - 1e-6
        base = patch[clear].mean()
        prof = _radial_bin_mean(rr, patch, own, bins, step) - base
        profiles.append(prof)
        strength.append(np.nanmean(prof[:n_inner]))

    if len(profiles) < 4:
        return None
    profiles = np.asarray(profiles)
    strength = np.asarray(strength)
    keep = strength >= np.nanpercentile(strength, DIAM_STRENGTH_PCT)
    if keep.sum() < 4:
        return None

    with np.errstate(invalid="ignore"):
        m = np.nanmean(profiles[keep], axis=0)
    plateau = float(np.nanmean(m[:n_inner]))
    if not np.isfinite(plateau) or plateau <= 0:
        return None

    level = EDGE_FRAC * plateau
    above = np.where(np.isfinite(m) & (m > level))[0]
    if not len(above):
        return None
    i = int(above[-1])
    if i + 1 < bins.size and np.isfinite(m[i + 1]) and m[i] > m[i + 1]:
        # linear interpolation across the crossing, so the answer is not
        # quantised to the bin width
        t = (m[i] - level) / (m[i] - m[i + 1])
        r_edge = bins[i] + t * step
    else:
        r_edge = bins[i]
    if debug:
        print(f"    spot profile: plateau {plateau:.2f} gray over "
              f"{keep.sum()} spots, half-max edge r={r_edge:.0f}px "
              f"-> diameter {2 * r_edge:.0f}px")
    return float(2.0 * r_edge)


# Full-resolution ROI centring. Detection runs at DETECT_LONG_SIDE, which on a
# 6000 px photo is a 4x downscale: one detection pixel is four real ones, and the
# measuring ROI is only ~20 px across there. Centring errors of a few detection
# pixels therefore land as ~20 px in the real image -- about a quarter of the ROI
# radius -- which pulls agar into the ROI on one side and clips the spot on the
# other. This pass re-centres each ROI against the full-resolution pixels.
# Search radius, as a fraction of the spot pitch. Deliberately modest. Widening
# it to 0.30 to reach corner spots that pin at the boundary BACKFIRED: the ROIs
# migrated onto the meniscus instead, which offers a strong (7 gray) and stable
# optimum that is not a spot at all. A pinned corner spot falling back to its
# lattice position is the safe outcome; chasing it is not.
FULLRES_SEARCH_PITCH = 0.15
# A spot's own correction is accepted on EVIDENCE -- it has a real correlation
# peak and did not run to the edge of the search window -- not on agreement with
# a lattice model. Spots are pipetted by hand, so they genuinely sit off-lattice
# individually: on 2.1K-OAc the per-spot disagreement with the best-fit affine
# runs median 9.5px but p90 59px, and an affine cap threw away a third of the
# confident spots, r4c2's 18px correction among them.
FULLRES_MIN_PEAK_FRAC = 0.20  # of the plate's own well-grown peak response
# Texture as a rescue signal for spots brightness cannot place. Correct in
# principle -- it recovers r1c1 on 2.1K-OAc, which otherwise sits 51px toward the
# rim on blank agar -- but DESTABILISING as tried: faint spots flip between the
# two answers from one iteration to the next, the loop stops converging (68px
# moves on the final pass), and enough rescues land on the meniscus that the
# footprint measurement blows up from 211px to 535px and the rim-flag count goes
# 1 -> 3. Left off until the rescue can be restricted (brightness PINNED only,
# applied once after convergence, with a stricter texture threshold).
# ...so it is applied ONCE, after the brightness loop has converged, and only to
# spots brightness could not place. Run inside the loop it oscillates.
FULLRES_TEXTURE_RESCUE = True
FULLRES_TEX_RESCUE_FRAC = 0.20   # separates r1c1 (0.28) from the empty corners
                                 # r6c8 (0.15) and r6c1 (0.05) on 2.1K-OAc
# Only agar counts. Outside this fraction of the plate radius sits the bright
# meniscus and then the black table, and BOTH wreck a disc correlation -- glare
# reads as +27 gray where a real K-OAc spot reads 4-8, and the black table reads
# negative, so the filter is pulled onto the rim from one side and shoved off it
# from the other. Every corner spot on 2.1K-OAc ran to the search boundary
# before this mask existed.
FULLRES_AGAR_FRAC = 0.90
# Minimum share of the ROI that must be agar for a candidate position to be
# considered at all. This is what actually keeps corner ROIs off the rim: at 0.50
# a position half on the meniscus still scored, and on 2.1K-OAc three of the four
# corners slid outward to ~0.90R with barely half the ROI on agar.
FULLRES_MIN_VALID = 0.85
FULLRES_MAX_ITERS = 5         # re-centring is iterated to a fixed point
FULLRES_SETTLED_PX = 1.5      # median movement below which it has converged


def refine_centers_fullres(img8: np.ndarray, centers: np.ndarray,
                           disc_radius: float,
                           plate_center: tuple | None = None,
                           plate_radius: float | None = None,
                           texture_rescue: bool = False,
                           debug: bool = False) -> np.ndarray:
    """Re-centre every ROI on its spot using full-resolution pixels.

    Per spot: flatten the local agar with a least-squares plane (the matched
    filter otherwise walks off toward whichever side of the patch is brighter),
    correlate with a disc the size of the SPOT (not of the measuring ROI, which
    is smaller: a disc that fits comfortably inside a larger uniform spot has a
    nearly flat correlation peak, so the fit is under-constrained and settles
    wherever noise pushes it. Measured on 2.1K-OAc, the spot-edge asymmetry a
    centring leaves behind is 0.61 gray uncorrected, 0.58 with an ROI-sized
    disc -- i.e. barely better -- and 0.29 with a spot-sized one). Then take
    the peak with sub-pixel parabolic interpolation.

    The per-spot answers are then reconciled with an affine fit over the whole
    grid. A faint or absent spot has no peak to find and its filter latches onto
    noise or onto rim glare; such spots are pulled back to the position the
    lattice predicts, so one bad spot can never drag its ROI off the grid.
    """
    from scipy.signal import fftconvolve
    from scipy.ndimage import uniform_filter

    C = np.asarray(centers, float)
    n_rows, n_cols = C.shape[:2]
    flat_c = C.reshape(-1, 2)
    pitch = float(np.median(np.diff(C[0, :, 1])))
    if not np.isfinite(pitch) or pitch <= 0:
        return C

    search = max(4, int(round(FULLRES_SEARCH_PITCH * pitch)))
    rr_k = max(4, int(round(disc_radius)))
    # The patch has to hold the whole search window PLUS a disc radius beyond it,
    # or the correlation at the edge of the window reads off the end of the patch
    # and the peak there is meaningless.
    half = int(round(search + rr_k + 0.05 * pitch))
    kk = np.arange(-rr_k, rr_k + 1)
    KY, KX = np.meshgrid(kk, kk, indexing="ij")
    disk = ((KY ** 2 + KX ** 2) <= rr_k * rr_k).astype(np.float32)
    disk_area = float(disk.sum())

    n_spots = flat_c.shape[0]
    cand = np.zeros((2, n_spots, 2))          # [brightness, texture] x spot x (dy, dx)
    peak = np.full((2, n_spots), -np.inf)
    pin = np.ones((2, n_spots), bool)
    for k, (cy, cx) in enumerate(flat_c):
        y0, x0 = int(round(cy)) - half, int(round(cx)) - half
        y1, x1 = y0 + 2 * half, x0 + 2 * half
        if y0 < 0 or x0 < 0 or y1 >= img8.shape[0] or x1 >= img8.shape[1]:
            continue
        ys, xs = np.mgrid[y0:y1, x0:x1]
        P = img8[y0:y1, x0:x1].astype(np.float32)
        dmin = _nearest_center_dist(ys, xs, flat_c, grid_like=True)
        # Agar only -- for the baseline fit AND for the correlation.
        if plate_center is not None and plate_radius:
            valid = (np.hypot(ys - plate_center[0], xs - plate_center[1])
                     <= FULLRES_AGAR_FRAC * plate_radius)
        else:
            valid = np.ones(P.shape, bool)
        agar = (dmin > 0.40 * pitch) & valid
        if agar.sum() < 300:
            continue
        A = np.column_stack([ys[agar].ravel(), xs[agar].ravel(),
                             np.ones(int(agar.sum()))])
        co, *_ = np.linalg.lstsq(A, P[agar].ravel(), rcond=None)
        flat = (P - (co[0] * ys + co[1] * xs + co[2])) * valid
        # Texture as a second, independent signal. Brightness is the sharper cue
        # on a well-grown spot, but near the rim it rides the plate's smooth
        # brightness gradient and slides outward; texture cannot, because the
        # gradient is smooth and carries no speckle. On 2.1K-OAc r1c1 sat 51px
        # toward the rim on blank agar until texture rescued it.
        # Only the rescue pass consumes it, and that is one of the seven calls
        # per plate -- the other six used to build it and throw it away, at two
        # full-resolution box filters per spot.
        tex = None
        if texture_rescue:
            sd = np.sqrt(np.maximum(uniform_filter(P.astype(float) * P, 7)
                                    - uniform_filter(P.astype(float), 7) ** 2, 0))
            tex = ((sd - np.median(sd[agar])) * valid).astype(np.float32)

        # Masked correlation: the MEAN over agar pixels inside the disc, not a
        # sum over everything. Without the denominator a disc that overhangs the
        # plate edge is scored on fewer pixels and the comparison is meaningless.
        den = fftconvolve(valid.astype(np.float32), disk, mode="same")
        ok_den = den > FULLRES_MIN_VALID * disk_area
        cyl, cxl = int(round(cy)) - y0, int(round(cx)) - x0
        for f_i, field in enumerate((flat, tex) if texture_rescue else (flat,)):
            resp = np.where(ok_den,
                            fftconvolve(field, disk, mode="same") / np.maximum(den, 1e-6),
                            -np.inf)
            win = resp[cyl - search:cyl + search + 1, cxl - search:cxl + search + 1]
            if win.shape[0] < 3 or win.shape[1] < 3 or not np.isfinite(win).any():
                continue
            safe = np.where(np.isfinite(win), win, -1e18)
            iy, ix = np.unravel_index(int(np.argmax(safe)), win.shape)
            if not np.isfinite(win[iy, ix]):
                continue
            dy = _parabolic_peak(safe[:, ix], iy)
            dx = _parabolic_peak(safe[iy, :], ix)
            cand[f_i, k] = (iy - search + dy, ix - search + dx)
            peak[f_i, k] = float(win[iy, ix])
            pin[f_i, k] = (abs(iy - search) >= search - 1) or (abs(ix - search) >= search - 1)
        continue

    # Which corrections to trust. A well-grown spot gives a correlation peak of
    # several gray levels; a faint or absent one gives ~0.2 and its filter walks
    # to the edge of the search window chasing noise. Both signatures are
    # checked, because either alone lets a runaway through.
    # Brightness first; texture only rescues a spot brightness could not place.
    disp = np.zeros((n_spots, 2))
    strong = np.zeros(n_spots, bool)
    refs = []
    for f_i in (0, 1):
        pk = peak[f_i]
        good = pk > -np.inf
        refs.append(float(np.median(pk[good][pk[good] >= np.percentile(pk[good], 75)]))
                    if good.any() else np.inf)
    for f_i in ((0, 1) if texture_rescue else (0,)):
        thr = (FULLRES_MIN_PEAK_FRAC if f_i == 0 else FULLRES_TEX_RESCUE_FRAC) * refs[f_i]
        take = (~strong) & (~pin[f_i]) & (peak[f_i] > thr)
        disp[take] = cand[f_i][take]
        strong |= take

    # Spots with no usable peak fall back to the grid's own smooth deformation,
    # fitted to the spots that DID resolve. That is the right prior for a spot
    # whose position cannot be measured -- and only for those.
    jj, ii = np.meshgrid(np.arange(n_cols), np.arange(n_rows))
    A = np.column_stack([jj.ravel(), ii.ravel(), np.ones(jj.size)])
    if strong.sum() >= 4:
        coef, *_ = np.linalg.lstsq(A[strong], disp[strong], rcond=None)
        model = A @ coef
    else:
        model = np.zeros_like(disp)

    final = np.where(strong[:, None], disp, model)

    if debug:
        moved = np.hypot(final[:, 0], final[:, 1])
        n_tex = int((strong & pin[0]).sum() + (strong & (peak[0] <= FULLRES_MIN_PEAK_FRAC * refs[0])).sum())
        print(f"    full-res re-centring: median {np.median(moved):.1f}px, "
              f"max {moved.max():.1f}px, {int(strong.sum())}/{n_spots} measured "
              f"directly ({n_tex} rescued by texture)")
    return (flat_c + final).reshape(n_rows, n_cols, 2)


# Fraction of the spot's core texture that marks the edge of the growth. The
# texture profile holds near 100% across the spot then falls through zero within
# roughly one bin, so this threshold sits on a near-vertical part of the curve
# and the exact value barely matters (50% and 25% differ by ~3px on real plates).
FOOTPRINT_FRAC = 0.50
# Measuring ROI as a fraction of the measured footprint. 1.0 puts the ROI on the
# outer edge of the growth.
ROI_FOOTPRINT_FRAC = 1.00


def measure_spot_footprint(img8: np.ndarray, centers: np.ndarray,
                           debug: bool = False) -> float | None:
    """Outer diameter of the growth, from a radial profile of local TEXTURE.

    Brightness is the wrong signal for the outer edge. A spot carries a diffuse
    optical halo that extends well past the colonies and decays slowly, so an
    intensity contour keeps creeping outward with no yeast under it -- on
    2.1K-OAc the intensity profile does not reach agar level until ~440px, more
    than twice the actual spot.

    Texture separates them cleanly: the spot is speckled with micro-colonies and
    bare agar is not, and the halo -- being an optical effect rather than
    growth -- carries no speckle either. The profile therefore holds flat across
    the spot and drops through the agar level at the edge of the colonies.

    Requires well-centred inputs: measured before the full-resolution centring
    pass, this returns garbage (the edge smears out and the profile tail
    becomes noise). Run it after `refine_centers_fullres`.
    """
    from scipy.ndimage import uniform_filter

    C = np.asarray(centers, float)
    flat_c = C.reshape(-1, 2)
    if len(flat_c) < 4:
        return None
    pitch = float(np.median(np.diff(C[0, :, 1])))
    if not np.isfinite(pitch) or pitch <= 0:
        return None

    rmax = 0.70 * pitch
    step = max(2.0, pitch / 100.0)
    bins = np.arange(0.0, rmax, step)
    n_inner = max(3, int(round(0.12 * pitch / step)))

    prof, strength = [], []
    for cy, cx in flat_c:
        y0, y1 = int(cy - rmax), int(cy + rmax)
        x0, x1 = int(cx - rmax), int(cx + rmax)
        if y0 < 0 or x0 < 0 or y1 >= img8.shape[0] or x1 >= img8.shape[1]:
            continue
        ys, xs = np.mgrid[y0:y1, x0:x1]
        rr = np.hypot(ys - cy, xs - cx)
        P = img8[y0:y1, x0:x1].astype(float)
        dmin = _nearest_center_dist(ys, xs, flat_c, grid_like=True)
        own = dmin >= rr - 1e-6
        sd = np.sqrt(np.maximum(uniform_filter(P * P, 7) - uniform_filter(P, 7) ** 2, 0))
        t = _radial_bin_mean(rr, sd, own, bins, step)
        prof.append(t)
        strength.append(P[rr < 0.15 * pitch].mean())

    if len(prof) < 4:
        return None
    prof = np.asarray(prof)
    strength = np.asarray(strength)
    keep = strength >= np.nanpercentile(strength, DIAM_STRENGTH_PCT)
    if keep.sum() < 4:
        return None
    with np.errstate(invalid="ignore"):
        t = np.nanmean(prof[keep], axis=0)
    core = float(np.nanmean(t[:n_inner]))
    floor = float(np.nanmedian(t[bins > 0.55 * pitch]))
    if not np.isfinite(core) or not np.isfinite(floor) or core <= floor:
        return None

    f = (t - floor) / (core - floor)
    above = np.where(np.isfinite(f) & (f > FOOTPRINT_FRAC))[0]
    if not len(above):
        return None
    i = int(above[-1])
    if i + 1 < bins.size and np.isfinite(f[i + 1]) and f[i] > f[i + 1]:
        r_edge = bins[i] + (f[i] - FOOTPRINT_FRAC) / (f[i] - f[i + 1]) * step
    else:
        r_edge = bins[i]
    if debug:
        print(f"    spot footprint (texture): core {core:.3f}, agar {floor:.3f}, "
              f"edge r={r_edge:.0f}px -> diameter {2 * r_edge:.0f}px")
    return float(2.0 * r_edge)


def _spot_net(img8: np.ndarray, center, radius: float) -> float:
    """Mean gray inside one ROI above the surrounding agar. Used only to decide
    whether a spot has enough signal for its placement to matter."""
    cy, cx = float(center[0]), float(center[1])
    w = int(round(1.6 * radius))
    y0, x0 = int(round(cy)) - w, int(round(cx)) - w
    if y0 < 0 or x0 < 0 or y0 + 2 * w >= img8.shape[0] or x0 + 2 * w >= img8.shape[1]:
        return float("nan")
    ys, xs = np.mgrid[y0:y0 + 2 * w, x0:x0 + 2 * w]
    P = img8[y0:y0 + 2 * w, x0:x0 + 2 * w].astype(float)
    rr = np.hypot(ys - cy, xs - cx)
    ring = (rr > 1.25 * radius) & (rr <= 1.55 * radius)
    if ring.sum() < 100:
        return float("nan")
    return float(P[rr < radius].mean() - np.median(P[ring]))


# The reference radius MUST come from well-grown spots only. A spot with no
# growth has no edge, so its fitted radius inflates (136-176px on 2.1K-OAc
# against ~110 for healthy ones); include those and the median lands at 142 and
# the genuinely unverifiable spots stop looking unusual.
OUTLINE_TOL = 0.25            # allowed deviation from the well-grown median
OUTLINE_STRONG = 0.30         # of the plate max: spots that define the reference
OUTLINE_MIN_SIGNAL = 0.15     # of the plate max: enough signal to be worth flagging


def measure_spot_outlines(img8: np.ndarray, centers: np.ndarray,
                          measure_radius: float,
                          plate_center: tuple | None = None,
                          plate_radius: float | None = None) -> np.ndarray:
    """Per-spot outline radius, by casting rays and finding where texture drops.

    Every centring estimator agrees on a normal spot, but on a faint spot lying
    on the plate's brightness gradient they all report "already correct" while
    the ROI is visibly off. What separates those spots is that their EDGE cannot
    be resolved: this fit returns ~108px on healthy spots (matching the ROI) and
    150+ where placement could not be independently confirmed.

    Returns NaN where no edge was found.
    """
    from scipy.ndimage import uniform_filter, map_coordinates

    C = np.asarray(centers, float)
    flat_c = C.reshape(-1, 2)
    pitch = float(np.median(np.diff(C[0, :, 1])))
    W = int(round(1.9 * measure_radius))
    out = np.full(len(flat_c), np.nan)
    rs = np.arange(0.30 * measure_radius, 1.7 * measure_radius, 2.0)

    for k, (cy, cx) in enumerate(flat_c):
        y0, x0 = int(round(cy)) - W, int(round(cx)) - W
        if y0 < 0 or x0 < 0 or y0 + 2 * W >= img8.shape[0] or x0 + 2 * W >= img8.shape[1]:
            continue
        ys, xs = np.mgrid[y0:y0 + 2 * W, x0:x0 + 2 * W]
        Q = img8[y0:y0 + 2 * W, x0:x0 + 2 * W].astype(float)
        dmin = _nearest_center_dist(ys, xs, flat_c, grid_like=True)
        if plate_center is not None and plate_radius:
            valid = (np.hypot(ys - plate_center[0], xs - plate_center[1])
                     <= FULLRES_AGAR_FRAC * plate_radius)
        else:
            valid = np.ones(Q.shape, bool)
        agar = (dmin > 0.40 * pitch) & valid
        if agar.sum() < 300:
            continue
        sd = uniform_filter(np.sqrt(np.maximum(
            uniform_filter(Q * Q, 7) - uniform_filter(Q, 7) ** 2, 0)), 15)
        T = sd - np.median(sd[agar])
        inner = float(T[np.hypot(ys - cy, xs - cx) < 0.5 * measure_radius].mean())
        if not np.isfinite(inner) or inner <= 0:
            continue
        cy0, cx0 = cy - y0, cx - x0
        pts = []
        for th in np.arange(0, 360, 4) * np.pi / 180.0:
            prof = uniform_filter(map_coordinates(
                T, [cy0 + rs * np.sin(th), cx0 + rs * np.cos(th)],
                order=1, mode="nearest"), 5)
            idx = np.where(prof > 0.5 * inner)[0]
            if not len(idx):
                continue
            j = int(idx[-1])
            if j + 1 < len(prof) and prof[j] > prof[j + 1]:
                t = (prof[j] - 0.5 * inner) / (prof[j] - prof[j + 1])
                r_e = rs[j] + t * 2.0
            else:
                r_e = rs[j]
            pts.append((cy + r_e * np.sin(th), cx + r_e * np.cos(th)))
        if len(pts) < 20:
            continue
        P = np.asarray(pts)
        rad = np.nan
        for _ in range(3):
            Yv, Xv = P[:, 0], P[:, 1]
            A = np.column_stack([2 * Xv, 2 * Yv, np.ones(len(Xv))])
            sol, *_ = np.linalg.lstsq(A, Xv ** 2 + Yv ** 2, rcond=None)
            fx, fy = sol[0], sol[1]
            rad = float(np.sqrt(max(sol[2] + fx * fx + fy * fy, 1.0)))
            d = np.abs(np.hypot(Yv - fy, Xv - fx) - rad)
            keep = d <= max(8.0, 2.5 * float(np.median(d)))
            if keep.sum() < 20:
                break
            P = P[keep]
        out[k] = rad
    return out.reshape(C.shape[:2])


# ROI sizing from the spots themselves. The protocol wants one circular ROI that
# fits INSIDE every spot being compared, so no ROI picks up bare agar. That means
# the smallest spot sets the size -- but only among spots that actually exist:
#
#  * spot size tracks the dilution series by design (least-dilute rows measure
#    107-116px radius on 2.1K-OAc, most-dilute rows 16-22px), so the minimum is
#    taken over the rows actually being quantified, not the whole plate;
#  * a strain that does not grow has no spot to fit inside. GRX5 on K-OAc reads
#    at the search floor in every row. Including it collapses the ROI to nothing,
#    so unresolved spots are skipped for sizing and simply read ~0, as they should.
# Two separate questions, which an intensity threshold wrongly answers at once:
#
#   how BIG is the spot   -> inner edge of the fall in the texture profile.
#     The steepest point sits in the MIDDLE of the transition, so an ROI drawn
#     there takes in the outer half of the fall-off -- visible as a rim of bare
#     agar inside the circle. The transition width comes from the profile
#     itself (amplitude / steepest slope), and the inner edge is half a width
#     further in. Widths run 8-29px on 2.1K-OAc.
#     A shape property, so it does not move with brightness. A fixed texture
#     level does: on 2.1K-OAc it sized dNCL1 (net 1.7) at 51px and dYHB1
#     (net 4.0) at 113px, when both spots are plainly the same size. By
#     gradient they read 111 and 107.
#
#   is there a spot AT ALL -> height of the profile above the far-field agar.
#     dGRX5 does not grow on K-OAc and reads 0.5, -0.3, -0.1, 0.2, -0.5, 0.1
#     sigmas across the six rows; every other strain in the least-dilute rows
#     is >= 1.1. Gradient alone cannot see this -- it happily finds a "steepest
#     point" in noise and returned 59px for dGRX5.
SPOT_EXISTS_SIGMA = 0.8       # profile height above agar, in robust sigmas
SIZE_SEARCH_FLOOR = 0.05      # smallest radius the ray search can report
SIZE_FLOOR_FRAC = 0.25        # never shrink below this fraction of the footprint
# A strict minimum is the most outlier-sensitive statistic there is, and one bad
# spot ruins a whole plate: on 7.2GLU fifteen spots measured 107-112px and dGSH1
# row 2 measured 38 (it measures 108 in row 5, so that one is a measurement
# failure), dragging the ROI to 76px; on 3.2GLY dURM1 measured 22 in both rows
# (consistently undergrown, so that one is real) and the ROI hit the floor at
# 51px. Either way a single spot must not size the other fifteen. Spots this far
# below the plate's own median are dropped from the sizing and FLAGGED instead --
# the ROI genuinely does not fit inside them, which is what the flag is for.
# The cutoff is MAD-based, not a fixed fraction of the median. Real spot radii on
# one plate are remarkably tight -- MAD 2-6px against medians of 74-110 -- while
# the bad ones sit far below (7.2GLU: 38 against 107-112; 3.2GLY: 22, 22, 29, 61
# against 88-97). A fixed 0.60x median let 61 and 79 through and still
# underestimated those plates. MAD adapts to each plate's own scatter, so a plate
# with genuinely graded sizes keeps them and a plate with a tight cluster plus
# stragglers drops only the stragglers.
SIZE_OUTLIER_MADS = 3.0       # reject below median - this many MADs
SIZE_MAD_FLOOR = 0.05         # of the median: keeps a near-zero MAD from
                              # rejecting everything below the median
# How much of the radius the transition-width correction may consume. This is
# what adapts the rule to how UNDERGROWN a spot is, without needing to classify
# the medium. A confluent spot has a sharp edge (width 9-14px on glucose and on
# 2.1K-OAc) and is unaffected by the cap; a sparse, undergrown spot thins out
# gradually from the centre, which inflates amplitude/slope to 34-45px and made
# the correction eat ~20% of the true radius -- dOXR1 on 1.1GLY came out 67px
# when its edge is at 77. Capping leaves sharp spots untouched and stops diffuse
# ones being over-corrected: 7.2GLU 214->214, 2.1K-OAc 178->186, 1.1GLY 134->152.
SIZE_WIDTH_CAP = 0.20


def roi_radius_for_rows(spot_radii, spot_exists, quant_rows, base_radius,
                        debug: bool = False) -> tuple:
    """ROI radius that fits inside every resolved spot of `quant_rows`.

    Split out of detect_grid so the same detection can be costed once and reused
    for every dilution choice: the expensive part (centring, iterated to a fixed
    point) is 41s per plate and does not depend on the rows at all, while this is
    under a second. Returns (radius, n_dropped, median_spot).
    """
    if spot_radii is None or not quant_rows:
        return base_radius, 0, float("nan")
    sel = [r - 1 for r in quant_rows if 1 <= r <= N_ROWS]
    cand = spot_radii[sel] if sel else spot_radii
    have = spot_exists[sel] if sel else spot_exists
    resolved = np.isfinite(cand) & have
    if resolved.sum() < 2:
        return base_radius, 0, float("nan")
    vals = cand[resolved]
    med_spot = float(np.median(vals))
    mad = 1.4826 * float(np.median(np.abs(vals - med_spot)))
    mad = max(mad, SIZE_MAD_FLOOR * med_spot)
    usable = resolved & (cand >= med_spot - SIZE_OUTLIER_MADS * mad)
    if usable.sum() < 2:
        usable = resolved
    n_drop = int(resolved.sum() - usable.sum())
    fit = float(np.min(cand[usable]))
    new_r = max(fit, SIZE_FLOOR_FRAC * base_radius)
    if debug:
        print(f"    ROI sized to smallest resolved spot in rows "
              f"{sorted(quant_rows)}: {base_radius:.0f} -> {new_r:.0f}px"
              + ("  (floored)" if new_r > fit else "")
              + (f"; {n_drop} undersized spot(s) excluded from sizing "
                 f"(median spot {med_spot:.0f}px)" if n_drop else ""))
    return new_r, n_drop, med_spot


def measure_spot_radii(img8: np.ndarray, centers: np.ndarray,
                       measure_radius: float,
                       plate_center: tuple | None = None,
                       plate_radius: float | None = None):
    """Per-spot radius and whether a spot is there at all.

    Returns (radii, exists), both shaped like the grid. `radii` is the radius of
    steepest decline in the spot's radial texture profile; `exists` is True where
    that profile rises meaningfully above the surrounding agar.
    """
    from scipy.ndimage import uniform_filter, map_coordinates

    C = np.asarray(centers, float)
    flat_c = C.reshape(-1, 2)
    pitch = float(np.median(np.diff(C[0, :, 1])))
    W = int(round(1.9 * measure_radius))
    rs = np.arange(SIZE_SEARCH_FLOOR * measure_radius, 1.7 * measure_radius, 2.0)
    thetas = np.arange(0, 360, 10) * np.pi / 180.0

    # Memoized on the spot: the sigma-sampling loop below and the main loop both
    # ask for the first up-to-12 spots, and this is three full-resolution box
    # filters over a (2W)^2 patch each time.
    _tex_cache: dict = {}

    def texture(cy, cx):
        key = (int(round(cy)), int(round(cx)))
        if key in _tex_cache:
            return _tex_cache[key]
        y0, x0 = key[0] - W, key[1] - W
        if y0 < 0 or x0 < 0 or y0 + 2 * W >= img8.shape[0] or x0 + 2 * W >= img8.shape[1]:
            _tex_cache[key] = None
            return None
        Q = img8[y0:y0 + 2 * W, x0:x0 + 2 * W].astype(float)
        sd = uniform_filter(np.sqrt(np.maximum(
            uniform_filter(Q * Q, 7) - uniform_filter(Q, 7) ** 2, 0)), 15)
        _tex_cache[key] = (sd, y0, x0)
        return _tex_cache[key]

    # plate-wide agar texture scatter, for the existence test
    samples = []
    for cy, cx in flat_c:
        got = texture(cy, cx)
        if got is None:
            continue
        sd, y0, x0 = got
        ys, xs = np.mgrid[y0:y0 + 2 * W, x0:x0 + 2 * W]
        m = _nearest_center_dist(ys, xs, flat_c,
                                 grid_like=True) > 0.40 * pitch
        if plate_center is not None and plate_radius:
            m &= np.hypot(ys - plate_center[0], xs - plate_center[1])                 <= FULLRES_AGAR_FRAC * plate_radius
        if m.sum():
            samples.append(sd[m])
        if len(samples) >= 12:
            break
    if not samples:
        return np.full(C.shape[:2], np.nan), np.zeros(C.shape[:2], bool)
    A = np.concatenate(samples)
    sigma = 1.4826 * float(np.median(np.abs(A - np.median(A))))
    if sigma <= 0:
        sigma = 1e-6

    n_core = max(3, int(0.25 * measure_radius / 2))
    lo = n_core
    radii = np.full(len(flat_c), np.nan)
    exists = np.zeros(len(flat_c), bool)
    for k, (cy, cx) in enumerate(flat_c):
        got = texture(cy, cx)
        if got is None:
            continue
        sd, y0, x0 = got
        cy0, cx0 = cy - y0, cx - x0
        rays = np.array([map_coordinates(
            sd, [cy0 + rs * np.sin(t), cx0 + rs * np.cos(t)], order=1, mode="nearest")
            for t in thetas])
        # median across rays: debris on one ray cannot move the answer
        prof = uniform_filter(np.median(rays, axis=0), 9)
        plateau = float(np.median(prof[:n_core]))
        asym = float(np.median(prof[rs > 1.35 * measure_radius]))
        amp = plateau - asym
        exists[k] = amp > SPOT_EXISTS_SIGMA * sigma
        grad = np.gradient(prof, rs)
        hi = len(rs) - 3
        if hi > lo:
            j = lo + int(np.argmin(grad[lo:hi]))
            slope = -float(grad[j])
            # amplitude / slope is the width of the ramp; clamp it so a very soft
            # edge cannot eat the whole spot
            width = min(amp / slope, SIZE_WIDTH_CAP * rs[j]) if slope > 0 else 0.0
            radii[k] = float(max(rs[j] - 0.5 * width, 0.25 * rs[j]))
    return radii.reshape(C.shape[:2]), exists.reshape(C.shape[:2])


def _artifact_mask(g: np.ndarray, agar: np.ndarray, cx: float, cy: float,
                   r_eq: float, spot_diam: float) -> tuple[np.ndarray, np.ndarray]:
    """Bright structures that are NOT yeast growth, at detection scale.

    Three things on these plates are bright enough to capture an ROI: the plate
    rim / meniscus, the frosted rectangular label at the bottom, and the holder
    bolts. Shape rules do not find them -- in the top-hat the label breaks into
    hundreds of fragments of 0.01x a spot's area, because the top-hat has
    already removed it as large-scale structure.

    Returns two masks, because the two jobs have very different costs:

      avoid -- used only to stop ROI CENTRING sliding onto something bright.
               A false positive here is harmless: the ROI just stays on its
               lattice position. So this one can be aggressive.

      flag  -- used to EXCLUDE a spot from the stats. A false positive here
               silently deletes a real measurement, so this one is strict.

    Texture ("bright but smooth") separates the rim and the label from healthy
    growth, because a thriving spot is bright BECAUSE it is a field of discrete
    colonies. But faint, evenly-grown spots are ALSO bright-and-smooth, and on
    respiring media (K-OAc, glycerol) that describes much of the plate: keying
    exclusion off texture flagged 20 of 48 spots on 6.1K-OAc, including
    well-grown ones, and flagged ΔZWF1 at 0.34-0.63R purely for being dim.
    Weak growth is a phenotype, not an artifact -- it is often the result being
    looked for -- so `flag` never rests on dimness. It uses position (the rim
    band) plus a size test that a single spot cannot satisfy.
    """
    from scipy import ndimage as ndi
    from skimage.morphology import disk

    H, W = g.shape
    Y, X = np.ogrid[:H, :W]
    # Anything at or beyond the usable agar circle is rim territory. This is
    # positional, so it cannot be confused with weak growth.
    rim = (X - cx) ** 2 + (Y - cy) ** 2 >= (RIM_SAFE_FRAC * r_eq) ** 2
    avoid = rim.copy()
    flag = rim.copy()

    if agar.any():
        # Local mean and standard deviation over ~a third of a spot.
        win = max(3, int(round(spot_diam / 3.0)))
        mean = ndi.uniform_filter(g, size=win, mode="nearest")
        sq2 = ndi.uniform_filter(g * g, size=win, mode="nearest")
        std = np.sqrt(np.clip(sq2 - mean * mean, 0, None))

        agar_level = float(np.median(g[agar]))
        agar_std = float(np.median(std[agar]))
        # Colony texture: how grainy the genuinely bright areas are.
        lively = agar & (mean > agar_level + 3.0)
        colony_std = float(np.median(std[lively])) if lively.any() else agar_std * 3.0

        smooth_bright = _closing(
            (mean > agar_level + 2.5)
            & (std < max(agar_std * 1.8, 0.35 * colony_std)), disk(2))
        avoid |= smooth_bright

        # Deliberately NOT folded into `flag`. A size test was tried here -- only
        # blobs larger than a couple of spot-areas count -- on the theory that a
        # single dim spot cannot be that big. It does not hold: on glycerol and
        # K-OAc neighbouring faint spots and the haze between them coalesce into
        # one large smooth region, so 6.1GLY r6c3 and r3c8 were still flagged
        # while showing plainly visible sparse colonies and nothing artifactual.
        # Every texture-derived exclusion inspected on this corpus was a false
        # positive on real, weak growth. `flag` is therefore purely positional.
        # The label rectangle is handled where it actually matters -- `avoid`
        # keeps ROI centring off it -- and anything left over is visible in the
        # per-combination preview, where a strain can be excluded by hand.

    d = disk(2)
    return _dilation(avoid, d), _dilation(flag, d)


def _rim_contaminated(g: np.ndarray, centers: np.ndarray, r: float,
                      cx: float, cy: float, r_eq: float) -> np.ndarray:
    """N_ROWS x N_COLS bool: ROIs that are BOTH near the plate edge AND actually
    contain rim/meniscus glare.

    Both conditions are needed. Position alone over-excludes: on 2.1K-OAc the
    corner ROIs at 0.87-0.90R held 0.0-0.1% rim-bright pixels -- clean, grown
    spots -- yet were dropped, which is how ΔTRR2 ended up with n=2. Brightness
    alone would risk catching a dense spot. Together they fire only on the real
    thing: the one ROI at 0.95R that was 36% glare.

    The reference is the plate's OWN colony brightness, taken from the ROIs that
    are not near the rim, so the test adapts to faint media instead of using an
    absolute gray level.
    """
    n_rows, n_cols = centers.shape[:2]
    H, W = g.shape
    d = np.hypot(centers[..., 0] - cy, centers[..., 1] - cx)
    near_rim = (d + r) > (RIM_SAFE_FRAC * r_eq)

    def patch_of(i, j):
        ccy, ccx = centers[i, j]
        y0, y1 = max(0, int(ccy - r)), min(H, int(ccy + r) + 1)
        x0, x1 = max(0, int(ccx - r)), min(W, int(ccx + r) + 1)
        if y1 <= y0 or x1 <= x0:
            return None
        ys, xs = np.ogrid[y0:y1, x0:x1]
        disk = (ys - ccy) ** 2 + (xs - ccx) ** 2 <= r * r
        return g[y0:y1, x0:x1][disk]

    interior = [patch_of(i, j) for i in range(n_rows) for j in range(n_cols)
                if not near_rim[i, j]]
    interior = [p for p in interior if p is not None and p.size]
    if not interior:
        return np.zeros((n_rows, n_cols), dtype=bool)
    colony_level = float(np.median([np.percentile(p, 99) for p in interior]))

    out = np.zeros((n_rows, n_cols), dtype=bool)
    for i in range(n_rows):
        for j in range(n_cols):
            if not near_rim[i, j]:
                continue
            p = patch_of(i, j)
            if p is None or not p.size:
                continue
            frac = float((p > colony_level + RIM_BRIGHT_MARGIN).mean())
            out[i, j] = frac > RIM_BRIGHT_FRAC
    return out


def _roi_overlaps(mask: np.ndarray, centers: np.ndarray, r: float,
                  min_frac: float = ARTIFACT_OVERLAP_FRAC) -> np.ndarray:
    """N_ROWS x N_COLS bool: does the measuring disk at each centre overlap
    `mask` by more than `min_frac` of its area?

    A fraction rather than "touches at all": a sliver of rim clipping the very
    edge of an otherwise clean spot shifts its mean by a fraction of a gray
    level, and flagging that would discard good data -- including, on these
    plates, the row's WT control, which would leave the whole row with nothing
    to normalise against.
    """
    H, W = mask.shape
    out = np.zeros(centers.shape[:2], dtype=bool)
    rr = int(np.ceil(r))
    for i in range(centers.shape[0]):
        for j in range(centers.shape[1]):
            cy, cx = centers[i, j]
            y0, y1 = max(0, int(cy) - rr), min(H, int(cy) + rr + 1)
            x0, x1 = max(0, int(cx) - rr), min(W, int(cx) + rr + 1)
            if y1 <= y0 or x1 <= x0:
                continue
            ys, xs = np.ogrid[y0:y1, x0:x1]
            disk_m = (ys - cy) ** 2 + (xs - cx) ** 2 <= r * r
            n = int(disk_m.sum())
            if n:
                hit = int((mask[y0:y1, x0:x1] & disk_m).sum())
                out[i, j] = (hit / n) > min_frac
    return out


def _parabolic_peak(line: np.ndarray, i: int) -> float:
    """Sub-sample offset of a maximum at index `i`, from a parabola through its
    two neighbours. Returns 0 at the ends, where there is nothing to fit."""
    if i <= 0 or i >= len(line) - 1:
        return 0.0
    a, b, c = float(line[i - 1]), float(line[i]), float(line[i + 1])
    denom = a - 2.0 * b + c
    if denom == 0.0:
        return 0.0
    return float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))


def _refine_centers(th_in: np.ndarray, lattice: np.ndarray,
                    s: float, disk_r: float, debug: bool = False) -> np.ndarray:
    """Centre every measuring ROI on its spot. Returns (N_ROWS, N_COLS, 2) of
    (y, x) in the DOWNSCALED coordinate frame.

    Two things this does that an intensity-weighted centroid does not:

    * It maximises the signal captured by a disk of exactly the measuring
      radius, which is the quantity actually reported, rather than locating a
      "centre of mass". Spots are rings/clumps of colonies, often denser on one
      side, and a weighted centroid is dragged toward the dense side -- which
      leaves the ROI straddling the spot edge, taking in agar on one side while
      clipping colonies on the other. That biases the mean gray DOWN, and for
      the control column it biases every ratio in the row UP.

    * Blank and very faint spots have no signal to centre on, so they take the
      MEDIAN offset of the confident spots instead of either staying on the raw
      lattice or chasing noise. The frogger prints one rigid grid, so a common
      offset is the physically meaningful correction.
    """
    from scipy.signal import fftconvolve

    # Signal captured by a measuring-radius disk centred at each pixel.
    rr = max(2, int(round(disk_r)))
    k = np.arange(-rr, rr + 1)
    disk = (k[:, None] ** 2 + k[None, :] ** 2) <= rr * rr
    cap = int(max(2, round(0.22 * s)))          # search radius, well under half a pitch
    corr = fftconvolve(th_in, disk.astype(float), mode="same")

    H, W = th_in.shape
    n_rows, n_cols = lattice.shape[:2]
    shifts = np.full((n_rows, n_cols, 2), np.nan)
    strength = np.zeros((n_rows, n_cols))
    at_edge = np.zeros((n_rows, n_cols), dtype=bool)

    for i in range(n_rows):
        for j in range(n_cols):
            ry, xx = lattice[i, j]
            cy0, cx0 = int(round(ry)), int(round(xx))
            y0, y1 = max(0, cy0 - cap), min(H, cy0 + cap + 1)
            x0, x1 = max(0, cx0 - cap), min(W, cx0 + cap + 1)
            if y1 <= y0 or x1 <= x0:
                continue
            patch = corr[y0:y1, x0:x1]
            peak = float(patch.max())
            # Signal per pixel inside the best disk, in top-hat gray units.
            strength[i, j] = peak / max(1.0, disk.sum())
            dy, dx = np.unravel_index(int(patch.argmax()), patch.shape)
            # The correlation is evaluated on the DOWNSCALED image, so an
            # integer peak quantises the centre to +/-0.5 detection px, which is
            # +/-2 px at full resolution. Interpolating the peak with a parabola
            # through its neighbours recovers the sub-pixel position for free.
            shifts[i, j] = (y0 + dy + _parabolic_peak(patch[:, dx], dy) - ry,
                            x0 + dx + _parabolic_peak(patch[dy, :], dx) - xx)
            # A peak sitting on the window boundary means the true optimum is
            # outside it -- almost always a neighbouring artifact's fringe, not
            # this spot. Measured: plate1 row 6 ΔPIM1 pinned at +cap, on the label.
            at_edge[i, j] = (dy == 0 or dy == patch.shape[0] - 1
                             or dx == 0 or dx == patch.shape[1] - 1)

    # Trust only confident spots whose optimum is strictly inside the window.
    thr = max(0.75, float(np.median(strength)) * 0.5)
    good = (strength >= thr) & ~at_edge & np.isfinite(shifts).all(axis=2)

    # The frogger prints ONE rigid grid, so the 48 offsets are not independent:
    # fit a smooth deformation (translation + linear trend in row/col) to the
    # confident spots and apply it everywhere. A per-spot free-for-all lets any
    # single ROI jump onto whatever is brightest nearby, which is exactly how a
    # blank spot ends up measuring the plate rim.
    ii, jj = np.mgrid[0:n_rows, 0:n_cols]
    A_all = np.column_stack([np.ones(ii.size), ii.ravel(), jj.ravel()])
    model = np.zeros((ii.size, 2))
    n_fit = 0
    if good.sum() >= 6:
        sel = good.ravel()
        for _ in range(3):                       # refit without outliers
            A = A_all[sel]
            b = shifts.reshape(-1, 2)[sel]
            coef, *_ = np.linalg.lstsq(A, b, rcond=None)
            pred = A_all @ coef
            resid = np.hypot(*(shifts.reshape(-1, 2) - pred).T)
            keep = good.ravel() & (resid <= max(2.0, 2.5 * np.nanmedian(
                resid[good.ravel()])))
            if keep.sum() < 6 or (keep == sel).all():
                break
            sel = keep
        model = A_all @ coef
        n_fit = int(sel.sum())
    elif good.sum() >= 1:
        model[:] = np.median(shifts.reshape(-1, 2)[good.ravel()], axis=0)
        n_fit = int(good.sum())

    # Never let the fitted correction exceed the search window.
    model = np.clip(model, -cap, cap)

    # The smooth model captures the grid as a whole, but individual pins do
    # deposit slightly off: measured against the spots' own centroids, placement
    # is a median 3.7 px out on a ~95 px radius, yet the worst spots reach 45 px,
    # which is visible and biases those readings. So confident spots get a small
    # residual correction of their own, hard-bounded well inside a spot radius so
    # it can refine a position but never relocate an ROI onto something else.
    resid = np.nan_to_num(shifts.reshape(-1, 2) - model, nan=0.0)
    fine_cap = max(1.0, FINE_ADJUST_PITCH * s)
    fine = np.where(good.ravel()[:, None],
                    np.clip(resid, -fine_cap, fine_cap), 0.0)

    centers = lattice + (model + fine).reshape(n_rows, n_cols, 2)

    if debug:
        my, mx = model.mean(axis=0)
        print(f"    ROI centring: {int(good.sum())}/{good.size} confident spots, "
              f"{n_fit} used to fit the grid deformation, "
              f"mean shift = ({my:+.1f}, {mx:+.1f}) px at detection scale")
    return centers


def _fit_1d(coords: np.ndarray, s: float) -> tuple[float, float, np.ndarray]:
    """Fit a regular 1-D lattice (offset o, spacing s) to `coords` by
    alternating index assignment and least-squares."""
    o = float(np.min(coords))
    for _ in range(12):
        idx = np.round((coords - o) / s)
        A = np.column_stack([np.ones_like(idx), idx])
        (o, s), *_ = np.linalg.lstsq(A, coords, rcond=None)
    return o, s, np.round((coords - o) / s).astype(int)


def _best_base(idx: np.ndarray, n: int, o: float, s: float,
               center: float) -> int:
    """Pick the window [base, base+n-1] of lattice indices that the n printed
    rows/columns occupy.

    Two things make this harder than "cover the detected spots":

    * A whole edge column can fail to grow. The search therefore has to reach
      windows that start well before the FIRST DETECTED spot -- the old range
      began at lo-1, i.e. at most one blank column, so a plate whose column 1
      was wiped out could not be represented at all and every strain was read
      one column across, pushing column 8 off the plate.

    * With several blank columns many windows tie on spot count, so the count
      alone cannot choose. The frogger prints its grid centred on the plate, so
      a window sitting about a full pitch off centre is wrong no matter how many
      spots it happens to cover. Measured on this corpus: legitimate offsets are
      <= 0.47 pitches (median 0.10), while the three mis-assigned plates sat at
      0.92-0.98 -- cleanly separable.
    """
    idx = np.asarray(idx)
    lo, hi = int(idx.min()), int(idx.max())

    # Every window that overlaps the detected span at all. A narrower range
    # keyed on "must contain everything" collapses when a single spurious blob
    # (a smudge, the label) lands outside the real grid: on 8.1K-OAc one false
    # detection five rows below the grid left exactly ONE candidate base, the
    # wrong one, so the top row of spots was dropped and row 6 sat on bare agar.
    scored = []
    for base in range(lo - n + 1, hi + 2):
        count = int(((idx >= base) & (idx <= base + n - 1)).sum())
        offset = abs(o + (base + (n - 1) / 2.0) * s - center) / max(s, 1e-9)
        scored.append((count, offset, base))
    if not scored:
        return lo

    # Coverage first, centring only to choose BETWEEN equally good windows.
    #
    # Centring used to be a hard filter (offset <= MAX_GRID_OFFSET_PITCH) applied
    # before ranking, which is wrong whenever a window is genuinely better rather
    # than merely tied: on 9.2GLU the correct grid matched all 14 detected spots
    # but sat exactly 0.60 pitches off centre, so floating point put it a hair
    # outside the cap and the fit fell to a window matching 8 -- every row read
    # one position low, with row 6 on bare agar. The constraint was only ever
    # needed for TIES (several blank columns make many windows tie on count), and
    # taking the most centred of the best-covering windows breaks those just as
    # well without ever discarding a strictly better fit.
    #
    # The slack of one spot keeps a single spurious blob -- a smudge, the frosted
    # label -- from tipping the choice on count alone.
    best = max(t[0] for t in scored)
    pool = [t for t in scored if t[0] >= best - GRID_COUNT_SLACK]
    pool.sort(key=lambda t: (t[1], -t[0]))
    return pool[0][2]


MAX_TILT_DEG = 20.0    # beyond this the angle estimate is not believable

# How far the centre of the fitted N_ROWS x N_COLS grid may sit from the centre
# of the plate, in spot pitches. The frogger prints a centred grid, so this is a
# real physical constraint and it is what disambiguates which columns are blank
# when an edge column fails to grow entirely.
MAX_GRID_OFFSET_PITCH = 0.6   # kept for reference; see _best_base
GRID_COUNT_SLACK = 1          # spots; windows this close to the best count tie

# How far a single confident spot may be nudged off the fitted grid, in pitches.
# Big enough to absorb a pin that deposited slightly off (worst measured ~0.11
# pitch), small enough that an ROI can never travel to a neighbour or an artifact.
FINE_ADJUST_PITCH = 0.12


def _lattice_angle(cen: np.ndarray, s0: float) -> float:
    """Tilt of the spot lattice in degrees, within +/-45.

    Taken from the directions between neighbouring spots: those displacements
    all lie along the two lattice axes, so folding them modulo 90 degrees makes
    both axes vote for the same angle. Averaging is done on 4*theta so that the
    90-degree wrap does not split the votes across the 0/90 boundary.
    """
    if len(cen) < 3:
        return 0.0
    angs = []
    for i in range(len(cen)):
        d = np.hypot(*(cen - cen[i]).T)
        for j in np.where((d > 0.6 * s0) & (d < 1.4 * s0))[0]:
            dx, dy = cen[j] - cen[i]
            angs.append(np.arctan2(dy, dx))
    if len(angs) < 2:
        return 0.0
    a4 = 4.0 * np.asarray(angs)
    deg = np.degrees(np.arctan2(np.sin(a4).mean(), np.cos(a4).mean()) / 4.0) % 90.0
    return deg - 90.0 if deg > 45.0 else deg


def _rot(pts: np.ndarray, cx: float, cy: float, deg: float) -> np.ndarray:
    """Rotate (x, y) points by `deg` about (cx, cy)."""
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    d = np.asarray(pts, float) - np.array([cx, cy])
    return np.column_stack([c * d[:, 0] - s * d[:, 1],
                            s * d[:, 0] + c * d[:, 1]]) + np.array([cx, cy])


def _fit_lattice(cen: np.ndarray, cx: float, cy: float, r_eq: float):
    """Return (spacing, centers[N_ROWS, N_COLS, (y, x)], tilt_deg) in downscaled
    coords. Falls back to a plate-centred grid at the expected spacing if too
    few spots are detected to fit reliably.

    The plate is not always photographed square to the frame. A separable fit
    (one lattice on x, one on y) cannot express rotation at all, so a tilt of a
    few degrees walks the ROIs off the spots: measured +4.6 deg on 5.2GLY, which
    over seven pitches is ~226 px of drift -- more than a spot radius. So the
    tilt is estimated first, the lattice is fitted in the straightened frame,
    and the resulting nodes are rotated back.
    """
    s0 = SPACING_FRAC * r_eq

    def grid_from(rows_y, cols_x, theta):
        pts = np.array([[x, y] for y in rows_y for x in cols_x], float)
        if theta:
            pts = _rot(pts, cx, cy, theta)
        out = np.zeros((N_ROWS, N_COLS, 2))
        for k, (x, y) in enumerate(pts):
            out[k // N_COLS, k % N_COLS] = (y, x)
        return out

    if len(cen) < 4:
        rows_y = cy - (N_ROWS - 1) / 2 * s0 + np.arange(N_ROWS) * s0
        cols_x = cx - (N_COLS - 1) / 2 * s0 + np.arange(N_COLS) * s0
        return s0, grid_from(rows_y, cols_x, 0.0), 0.0

    theta = _lattice_angle(cen, s0)
    if abs(theta) > MAX_TILT_DEG:
        theta = 0.0
    cen_r = _rot(cen, cx, cy, -theta) if theta else cen

    ox, sx, cix = _fit_1d(cen_r[:, 0], s0)
    oy, sy, ciy = _fit_1d(cen_r[:, 1], s0)
    base_j = _best_base(cix, N_COLS, ox, sx, cx)
    base_i = _best_base(ciy, N_ROWS, oy, sy, cy)
    cols_x = ox + (base_j + np.arange(N_COLS)) * sx
    rows_y = oy + (base_i + np.arange(N_ROWS)) * sy
    return (sx + sy) / 2, grid_from(rows_y, cols_x, theta), theta


def rim_flags(grid: Grid) -> np.ndarray:
    """N_ROWS x N_COLS bool: True where the measuring ROI overlaps a non-spot
    bright structure (plate rim/meniscus, the frosted label, a holder bolt) by
    more than ARTIFACT_OVERLAP_FRAC of its area.

    Flagged spots are still measured and reported -- silently dropping data is
    worse than reporting it with a warning -- but they are marked in the output
    so a value like "an empty spot reading 1.7x the control" is visible as an
    artefact rather than mistaken for growth.
    """
    if grid.artifact_flag is None:      # hand ROIs: nothing was measured off
        return np.zeros((N_ROWS, N_COLS), dtype=bool)   # the image to test
    return grid.artifact_flag.copy()


def background_gap_centers(grid: Grid) -> list[tuple[float, float]]:
    """Five agar-gap locations (full-res y, x) for background measurement,
    taken as midpoints between diagonally adjacent spots -- guaranteed to fall
    on agar, spread across the plate (protocol step 22)."""
    c = grid.centers
    def mid(i1, j1, i2, j2):
        y = (c[i1, j1, 0] + c[i2, j2, 0]) / 2
        x = (c[i1, j1, 1] + c[i2, j2, 1]) / 2
        return (y, x)
    R, C = N_ROWS - 1, N_COLS - 1
    return [mid(0, 0, 1, 1), mid(0, C - 1, 1, C), mid(R - 1, 0, R, 1),
            mid(R - 1, C - 1, R, C),
            mid(N_ROWS // 2 - 1, N_COLS // 2 - 1, N_ROWS // 2, N_COLS // 2)]


# ---------------------------------------------------------------------------
# User-specified ROIs  (calibration against hand ImageJ measurements)
# ---------------------------------------------------------------------------

ROI_COLUMNS = ["kind", "row", "col", "bg_index", "bx", "by", "width", "height"]


@dataclass
class MeasureOptions:
    """Everything that can change a measured number, in one place.

    Passed through analyze_image so the single-plate driver, the batch driver
    and the calibration harness cannot drift apart.
    """
    rois: Path | None = None              # measure at these ROIs, skip detection
    measure_diameter: float | None = None  # force ROI diameter (px)
    ball_radius: float | None = None      # force rolling-ball radius (px)
    bg_mode: str = "fiji"                 # fiji | paraboloid | rollingball | none
    bg_iters: int = BG_ITERS
    shrink: int | None = None             # None = ImageJ's 1/2/4/8 schedule
    rgb_mode: str = DEFAULT_RGB_MODE
    nudge: dict | None = None             # {(row, col): (dy, dx)} manual ROI shifts,
                                          # 1-based, in full-resolution pixels
    quant_rows: tuple | None = None       # 1-based dilution rows being scored; the
                                          # ROI is shrunk to fit inside their spots

    def resolve_ball_radius(self, largest_diameter: float) -> float:
        """--ball-radius wins; otherwise derive from the detected spot size.

        See BALL_PAD_IS_DIAMETER for why this is ambiguous and why the ground
        truth records the literal number typed into ImageJ's dialog.
        """
        if self.ball_radius is not None:
            return float(self.ball_radius)
        padded = largest_diameter + BALL_PAD
        return padded / 2.0 if BALL_PAD_IS_DIAMETER else padded


def load_roi_csv(path: Path) -> tuple[Grid, list[tuple[float, float]]]:
    """Build a Grid + background centres from a hand-recorded ROI CSV.

    Bypasses automatic detection entirely, which is what decouples "is the
    measurement maths right" from "is grid detection right" -- the two failure
    modes are indistinguishable when the program picks its own ROIs.

    Schema (one row per ROI, 48 spots + 5 background = 53 rows):

        kind,row,col,bg_index,bx,by,width,height
        spot,1,1,,1234,567,174,174
        background,,,1,2000,1500,174,174

    bx/by/width/height are ImageJ's BOUNDING BOX, copied straight out of the
    Results table (Analyze > Set Measurements > Bounding rectangle). Recording
    the box rather than a centre means no manual arithmetic stands between what
    ImageJ measured and what this reads back.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    missing = [c for c in ROI_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"{path.name} is missing required column(s): {missing}. "
            f"Expected exactly: {','.join(ROI_COLUMNS)}")

    df["kind"] = df["kind"].astype(str).str.strip().str.lower()
    spots = df[df["kind"] == "spot"]
    bgs = df[df["kind"] == "background"].sort_values("bg_index")

    if len(spots) != N_ROWS * N_COLS:
        raise ValueError(
            f"{path.name} has {len(spots)} spot ROIs, expected "
            f"{N_ROWS * N_COLS} ({N_ROWS} dilution rows x {N_COLS} strains). "
            f"Every spot must be recorded, including blank ones -- rows sitting "
            f"at background level are the most diagnostic data in the file.")
    if len(bgs) != N_BG_SAMPLES:
        raise ValueError(
            f"{path.name} has {len(bgs)} background ROIs, expected {N_BG_SAMPLES} "
            f"(protocol step 22).")

    widths = df["width"].astype(float)
    heights = df["height"].astype(float)
    if widths.nunique() != 1 or heights.nunique() != 1:
        raise ValueError(
            f"{path.name} has ROIs of differing size (widths "
            f"{sorted(widths.unique())}, heights {sorted(heights.unique())}). "
            f"Protocol step 21 requires ONE fixed selection size for every spot "
            f"and background reading on a plate -- the ROI area changes the "
            f"gray value, so mixed sizes make the numbers incomparable.")

    w = float(widths.iloc[0])
    h = float(heights.iloc[0])

    centers = np.full((N_ROWS, N_COLS, 2), np.nan)
    for _, r in spots.iterrows():
        i, j = int(r["row"]) - 1, int(r["col"]) - 1
        if not (0 <= i < N_ROWS and 0 <= j < N_COLS):
            raise ValueError(
                f"{path.name}: ROI row={r['row']} col={r['col']} is outside the "
                f"{N_ROWS}x{N_COLS} grid.")
        if not np.isnan(centers[i, j, 0]):
            raise ValueError(f"{path.name}: duplicate ROI for row={r['row']} "
                             f"col={r['col']}.")
        centers[i, j] = (float(r["by"]) + h / 2.0, float(r["bx"]) + w / 2.0)

    if np.isnan(centers).any():
        gaps = [(i + 1, j + 1) for i in range(N_ROWS) for j in range(N_COLS)
                if np.isnan(centers[i, j, 0])]
        raise ValueError(f"{path.name}: no ROI given for (row, col) {gaps}.")

    bg_centers = [(float(r["by"]) + h / 2.0, float(r["bx"]) + w / 2.0)
                  for _, r in bgs.iterrows()]

    radius = w / 2.0
    grid = Grid(centers=centers, spot_radius=radius, measure_radius=radius,
                largest_diameter=w, plate_center=(float(centers[..., 0].mean()),
                                                  float(centers[..., 1].mean())),
                plate_radius=float(np.hypot(*(centers.reshape(-1, 2)
                                              - centers.reshape(-1, 2).mean(0)).T).max()),
                plate_known=False)
    return grid, bg_centers


def export_rois(grid: Grid, bg_centers: list[tuple[float, float]],
                stem: Path) -> tuple[Path, Path]:
    """Write the auto-detected ROIs as <stem>.csv and <stem>.ijm.

    The macro drops all 53 ROIs into Fiji's ROI Manager pre-placed, so the hand
    measurement starts from the same geometry the program uses; anything the
    user nudges comes back through load_roi_csv. That closes the loop -- a
    disagreement in gray value can then only be the maths, never the placement.
    """
    r = grid.measure_radius
    d = 2 * r

    rows: list[dict[str, object]] = []
    macro = ['// Auto-generated by spotting_quant.py --export-rois',
             '// Run in Fiji with the plate image open and already 8-bit.',
             'roiManager("reset");']

    def add(kind: str, row, col, bg_index, cy: float, cx: float, name: str) -> None:
        bx, by = cx - r, cy - r
        rows.append({"kind": kind, "row": row, "col": col, "bg_index": bg_index,
                     "bx": int(round(bx)), "by": int(round(by)),
                     "width": int(round(d)), "height": int(round(d))})
        macro.append(f'makeOval({int(round(bx))}, {int(round(by))}, '
                     f'{int(round(d))}, {int(round(d))}); '
                     f'roiManager("add"); '
                     f'roiManager("select", roiManager("count")-1); '
                     f'roiManager("rename", "{name}");')

    for i in range(N_ROWS):
        for j in range(N_COLS):
            cy, cx = grid.centers[i, j]
            add("spot", i + 1, j + 1, "", cy, cx, f"spot_r{i+1}c{j+1}")
    for k, (cy, cx) in enumerate(bg_centers, start=1):
        add("background", "", "", k, cy, cx, f"bg_{k}")

    macro.append('roiManager("deselect");')
    macro.append('print("Loaded " + roiManager("count") + " ROIs.");')

    csv_path = stem.with_suffix(".csv")
    ijm_path = stem.with_suffix(".ijm")
    pd.DataFrame(rows, columns=ROI_COLUMNS).to_csv(csv_path, index=False,
                                                   encoding="utf-8-sig")
    ijm_path.write_text("\n".join(macro) + "\n", encoding="utf-8")
    return csv_path, ijm_path


# ---------------------------------------------------------------------------
# Per-spot measurement  (protocol steps 21-24)
# ---------------------------------------------------------------------------

@dataclass
class PlateMeasurement:
    """Everything measured off one plate, at every stage, so the calibration
    harness can inspect the pre-subtraction layer instead of only the result."""
    raw: np.ndarray               # N_ROWS x N_COLS mean gray, before bg subtraction
    net: np.ndarray               # raw - bg_mean; may be NEGATIVE (see below)
    bg_mean: float
    bg_samples: list[float]       # the five individual background reads
    n_px: np.ndarray              # N_ROWS x N_COLS pixel counts per ROI
    bg_n_px: list[int]
    rim_flag: np.ndarray          # N_ROWS x N_COLS bool; ROI may touch rim glare


def measure_plate(img: np.ndarray, grid: Grid,
                  bg_centers: list[tuple[float, float]]) -> PlateMeasurement:
    """Measure all 48 spots plus the five background ROIs (protocol steps 22-24).

    Spot value = mean gray over a fixed ROI minus the mean of the five agar-gap
    background ROIs. The ROI is the same size for every spot and every
    background reading on the plate, as the protocol requires (step 21).

    Net values are NOT clipped at zero. A spot reading at or below agar is
    information -- it means no growth -- and clipping turns a spread of small
    positive and negative values into a block of exact zeros, which destroys
    correlation with hand measurements in precisely the faint regime that
    matters and poisons every ratio whose denominator was clipped.
    """
    r = max(3.0, grid.measure_radius)
    d = float(round(2 * r))

    # Snap every ROI's bounding box to integer pixel offsets. ImageJ's OvalRoi
    # has integer x/y/width/height, so a selection dragged between spots keeps a
    # byte-identical mask -- and protocol step 21 is emphatic that the selection
    # area must not change between measurements on a plate, because the area
    # itself shifts the gray value. With float offsets the mask silently varies
    # by ~0.1% between spots; snapping makes every ROI exactly congruent.
    def read(cy: float, cx: float) -> tuple[float, int]:
        return _roi_mean(img, round(cx - d / 2), round(cy - d / 2), d, d)

    bg_reads = [read(cy, cx) for cy, cx in bg_centers]
    bg_samples = [m for m, _ in bg_reads]
    bg_mean = float(np.mean(bg_samples)) if bg_samples else 0.0

    raw = np.zeros((N_ROWS, N_COLS))
    n_px = np.zeros((N_ROWS, N_COLS), dtype=int)
    for i in range(N_ROWS):
        for j in range(N_COLS):
            cy, cx = grid.centers[i, j]
            raw[i, j], n_px[i, j] = read(cy, cx)

    return PlateMeasurement(raw=raw, net=raw - bg_mean, bg_mean=bg_mean,
                            bg_samples=bg_samples, n_px=n_px,
                            bg_n_px=[n for _, n in bg_reads],
                            rim_flag=rim_flags(grid))


# ---------------------------------------------------------------------------
# Interactive per-image dilution-row selection  (protocol step 20)
# ---------------------------------------------------------------------------

def choose_rows(img8: np.ndarray, grid: Grid, values: np.ndarray,
                name: str, no_prompt: bool,
                bg_centers: list[tuple[float, float]] | None = None) -> tuple[int, int]:
    """Ask the user which dilution row to quantify for EACH replicate,
    numbered 1-3 within that replicate (rep 1 = overall rows 1-3, rep 2 =
    overall rows 4-6). Returns 0-based overall row indices (rep1 in 0-2,
    rep2 in 3-5)."""
    if no_prompt:
        return 2, 5  # default: 3rd dilution of each replicate (per paper)

    _show_overlay(img8, grid, name, bg_centers)
    print(f"\n  Plate: {name}")
    print("  Background-subtracted gray value per spot")
    print("  (rows top->bottom; rep 1 = rows 1-3, rep 2 = rows 4-6; col 1 = control):")
    with np.printoptions(precision=1, suppress=True):
        print("   ", str(values).replace("\n", "\n    "))
    r1 = _ask_row("  Replicate 1 (top) -- quantify which dilution row (1-3)? ", 1, 3)
    r2 = _ask_row("  Replicate 2 (bottom) -- quantify which dilution row (1-3)? ", 1, 3)
    return r1 - 1, r2 - 1 + 3   # map rep2's 1-3 to overall rows 4-6


def _ask_row(prompt: str, lo: int, hi: int) -> int:
    while True:
        try:
            v = int(input(prompt).strip())
            if lo <= v <= hi:
                return v
        except (ValueError, EOFError):
            return lo
        print(f"    Please enter a number from {lo} to {hi}.")


def render_overlay(img: np.ndarray, grid: Grid, name: str,
                   bg_centers: list[tuple[float, float]] | None = None):
    """Build the plate figure with the grid + indices overlaid. Returns the
    Figure so callers can either show it interactively or save it as a QC
    thumbnail without a display."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 6))

    # The processed image is float and may contain negatives; pin the display
    # range so matplotlib's autoscaling cannot silently restretch the contrast
    # and make a faint plate look strong (or vice versa).
    lo, hi = float(np.percentile(img, 1)), float(np.percentile(img, 99.5))
    ax.imshow(img, cmap="gray", vmin=lo, vmax=max(hi, lo + 1e-6))

    r = grid.measure_radius
    for i in range(N_ROWS):
        for j in range(N_COLS):
            cy, cx = grid.centers[i, j]
            ax.add_patch(plt.Circle((cx, cy), r, fill=False, color="lime", lw=1))
            ax.text(cx, cy, f"{i+1},{j+1}", color="yellow", fontsize=6,
                    ha="center", va="center")
    for cy, cx in (bg_centers or []):
        ax.add_patch(plt.Circle((cx, cy), r, fill=False, color="red", lw=1, ls="--"))
    ax.set_title(f"{name}\nrow = dilution (rep1: rows 1-3, rep2: rows 4-6), "
                 f"col = strain (1 = control); red = background")
    ax.axis("off")
    fig.tight_layout()
    return fig


def _show_overlay(img: np.ndarray, grid: Grid, name: str,
                  bg_centers: list[tuple[float, float]] | None = None) -> None:
    """Pop up the plate so the user can confirm detection before picking rows."""
    import matplotlib.pyplot as plt
    render_overlay(img, grid, name, bg_centers)
    plt.show()


# ---------------------------------------------------------------------------
# Normalization + aggregation  (protocol steps 26-29)
# ---------------------------------------------------------------------------

DEFAULT_GROUP_KEYS = ["treatment"]

# Floor on the control gray value (above agar) that may act as a denominator.
#
# This is a guard against dividing by NOISE, not a test for whether a strain
# grew "enough". An earlier value of 2.0 was tuned on plates whose full signal
# range is ~28 gray, and it silently broke faint media: on glycerol the entire
# middle dilution sits at 0.5-3 gray, so a WT control reading 1.5-2.0 -- plainly
# grown, and in line with every neighbour in its row -- was rejected as "did not
# grow" and three of four replicates were nulled.
#
# What actually decides whether a denominator is usable is how it compares with
# the measurement noise on that plate, which is known: the spread of the five
# agar background reads. Callers that have it should pass `min_control`
# (see CONTROL_NOISE_MULT); this constant is only the fallback floor for callers
# that do not, and is deliberately near the noise level rather than near the
# signal level.
MIN_CONTROL_GRAY = 0.4

# A control must clear this many robust standard deviations of the plate's own
# background reads before its reciprocal is trusted.
#
# The scale is a ROBUST sigma (see bg_noise), not the max-min range of the five
# reads. The range is set entirely by its worst sample: on 1.1GLY the reads were
# 2.86, 6.04, 3.15, 3.00, 3.20 -- four tight and one stray -- giving a range of
# 3.17 and a cutoff of 6.33, which rejected controls of 3.77-5.00 that are
# plainly grown, and killed the whole Set 1 GLY experiment. The same five
# numbers give a robust sigma of ~0.22.
CONTROL_NOISE_MULT = 3.0


def bg_noise(samples) -> float:
    """Robust standard deviation of the background reads.

    Median absolute deviation, scaled to sigma. One background ROI that happens
    to land on a smudge or a fleck of growth then shifts the estimate barely at
    all, where the range would triple it.
    """
    a = np.asarray([s for s in samples if np.isfinite(s)], dtype=float)
    if a.size < 2:
        return 0.0
    mad = float(np.median(np.abs(a - np.median(a))))
    if mad > 0:
        return 1.4826 * mad
    # All reads identical to the median: fall back to the sample sd so a genuine
    # spread of exactly-tied values does not report zero noise.
    return float(np.std(a, ddof=1))


# Outlier rejection for single replicate points. Deliberately conservative: a
# criterion loose enough to tidy every group is a criterion that manufactures
# results. A plain robust-z test at 3.5 fires on 25 of 156 strain-groups in this
# corpus, including groups whose CV is already 0.02 -- trimming those is pure
# overfitting. So a point must ALSO be doing real damage: the group has to be
# genuinely variable to begin with, and dropping the point has to roughly halve
# that variability. Only ever ONE point per strain, and never below n=4.
OUTLIER_Z = 3.5           # robust z (MAD-scaled), Iglewicz-Hoaglin
OUTLIER_MIN_CV = 0.30     # the group must actually be variable
OUTLIER_CV_DROP = 0.50    # ...and removal must cut the CV at least this much
OUTLIER_MIN_N = 4
# Same idea for CONTROL spots, applied BEFORE normalizing.
CONTROL_OUTLIER_Z = 3.5


def flag_outliers(tidy: pd.DataFrame, group_keys: list[str] | None = None,
                  value: str = "relative_growth", verbose: bool = True
                  ) -> pd.DataFrame:
    """Add an `outlier` column marking single points that dominate a strain's
    spread. Points are FLAGGED, never deleted -- they stay in the tidy CSV so
    the exclusion is visible and reversible.

    Returns the frame with `outlier` added.
    """
    keys = list(group_keys or ["experiment", "strain"])
    out = tidy.copy()
    out["outlier"] = False
    if value not in out.columns:
        return out

    usable = ~out.get("excluded", False) & ~out.get("artifact", False)         & out[value].notna()
    hits = []
    for gid, g in out[usable].groupby(keys, sort=False):
        v = g[value].to_numpy(float)
        if len(v) < OUTLIER_MIN_N:
            continue
        mean = float(v.mean())
        if mean <= 0:                      # CV is meaningless around zero
            continue
        cv = float(v.std() / mean)
        if cv < OUTLIER_MIN_CV:
            continue
        med = float(np.median(v))
        mad = 1.4826 * float(np.median(np.abs(v - med)))
        if mad <= 0:
            continue
        z = np.abs((v - med) / mad)
        i = int(np.argmax(z))
        if z[i] < OUTLIER_Z:
            continue
        rest = np.delete(v, i)
        if rest.mean() <= 0:
            continue
        cv2 = float(rest.std() / rest.mean())
        if cv2 > (1.0 - OUTLIER_CV_DROP) * cv:
            continue                       # not doing enough damage to justify
        out.loc[g.index[i], "outlier"] = True
        hits.append((gid, g.iloc[i], float(v[i]), float(z[i]), cv, cv2))

    if verbose and hits:
        print(f"\n  {len(hits)} outlier point(s) excluded (one per strain at most; "
              f"robust z >= {OUTLIER_Z}, and removal cuts the CV by at least "
              f"{OUTLIER_CV_DROP:.0%}). They stay in the tidy CSV marked "
              f"`outlier`:")
        for gid, row, val, z, cv, cv2 in hits:
            who = " / ".join(str(x) for x in (gid if isinstance(gid, tuple) else (gid,)))
            rep = row.get("replicate", "?")
            print(f"      {who} {rep}: {val:.2f}  (z={z:.1f}, CV {cv:.2f} -> {cv2:.2f})")

        # A whole bad replicate shows up as many strains at once. Say so, because
        # dropping it from some strains and not others is not a coherent choice.
        if "replicate" in out.columns:
            frame = pd.DataFrame(
                [{"exp": (gid[0] if isinstance(gid, tuple) else gid),
                  "rep": row.get("replicate")} for gid, row, *_ in hits])
            n_str = (out[usable].groupby(out[usable][keys[0]])[keys[-1]]
                     .nunique().to_dict())
            for (exp, rep), k in frame.groupby(["exp", "rep"]).size().items():
                tot = n_str.get(exp, 0)
                if tot and k >= max(3, tot / 3):
                    print(f"      NOTE: {exp} {rep} is an outlier for {k} of {tot} "
                          f"strains -- that looks like a bad replicate rather than "
                          f"isolated points. Consider excluding the replicate.")
    return out


def add_relative_growth(tidy: pd.DataFrame, control_col: int = 1,
                        group_keys: list[str] | None = None,
                        min_control: float | None = None) -> pd.DataFrame:
    """Add a `relative_growth` column following protocol step 27, per group:

      * PER PLATE, average the control spots in the rows being quantified, then
        divide EVERY score on that plate by that average -- controls included,
        so the control scatters around 1.0 and averages to exactly 1.0 per plate.

    Normalising per plate rather than per replicate row is what Darren does by
    hand, and it matters. Dividing each strain by the control in its OWN row
    makes every strain on that row hostage to one control spot: WT BY grows
    poorly and erratically on respiring media, and on 8.1GLY its two quantified
    spots read 1.62 and 4.11, so the same eight strains came out ~2x apart
    depending on which row they sat in. Averaging the two controls on a plate
    damps that while still correcting plate-to-plate differences, which a single
    average over the whole combination would not. Measured across the corpus:
    median CV per row 0.32 -> per plate 0.29, and the worst cases improve far
    more (Set 8 GLY 0.46 -> 0.27, Set 7 K-OAc 0.51 -> 0.26).

    `group_keys` defines what counts as one experiment -- the set of columns
    within which a control is comparable. The default ["treatment"] is right for
    a single plate set, but a batch run MUST pass something that distinguishes
    timepoints and sets: "Glucose" recurs at every timepoint of every set in the
    corpus, and a lookup keyed on treatment alone would then match hundreds of
    rows and silently return Series instead of scalars.

    `control_col` is a parameter rather than a hardcoded 1 because the control
    is not always the left-most column -- two of the experiment trees are named
    "WT, EV, ..." and "EV, WT, ...". Getting it wrong rescales every number in
    the tree without any visible error.
    """
    keys = list(group_keys or DEFAULT_GROUP_KEYS)
    missing = [k for k in keys + ["replicate", "strain_col", "raw_growth"]
               if k not in tidy.columns]
    if missing:
        raise ValueError(f"add_relative_growth is missing column(s): {missing}.")

    tidy = tidy.copy()
    is_ctrl = tidy["strain_col"] == control_col
    if not is_ctrl.any():
        raise ValueError(
            f"No rows have strain_col == {control_col}, so nothing can be "
            f"normalized. Check control_col against the plate layout.")

    # Per-(group, PLATE) control average: the mean of the control spots in the
    # quantified rows of that plate. A merge rather than an .apply() lookup so a
    # duplicate key is a loud failure instead of a column that silently becomes
    # an object-dtype Series.
    plate_keys = keys + (["plate"] if "plate" in tidy.columns else ["replicate"])
    # An artifact-flagged control must NEVER define the normalizer. It is a spot
    # the pipeline has already judged unusable -- rim glare, unverifiable
    # placement -- and averaging it in rescales every strain on the plate. Seen
    # on Set 4 GLY: the row-1 control was rim-flagged at 69.4 gray against a
    # genuine 9.1 in row 4, so the divisor came out 39.3 and all eight strains
    # landed near 0.25, which the ratio test then called significant across the
    # board. Flagged spots are still measured and still reported; they just do
    # not get to set the scale.
    ctrl_rows = tidy.loc[is_ctrl]
    for col in ("artifact", "excluded"):
        if col in ctrl_rows.columns:
            ctrl_rows = ctrl_rows.loc[~ctrl_rows[col].astype(bool)]

    # ...and drop a control spot that is a gross outlier among the group's OWN
    # controls. Outlier rejection elsewhere runs on relative_growth, i.e. AFTER
    # normalizing -- far too late to protect the divisor. One bad control spot
    # rescales every strain on its plate: on Set 6 GLY the WT BY 02 spots read
    # 1.49, 4.81, 1.29, 1.22, and the 4.81 pushed plate 1's divisor to 3.15
    # against plate 2's 1.25, so dRHO5 -- whose RAW values are nearly identical
    # across all four replicates (10.78, 11.69, 10.51, 10.23) -- came out 3.4,
    # 3.7, 8.4, 8.2 and looked like two different phenotypes.
    if len(ctrl_rows) >= 3:
        keep_ctrl = pd.Series(True, index=ctrl_rows.index)
        for _, grp in ctrl_rows.groupby(keys, sort=False):
            v = grp["raw_growth"].to_numpy(float)
            med = float(np.median(v))
            mad = 1.4826 * float(np.median(np.abs(v - med)))
            if mad <= 0 or len(v) < 3:
                continue
            bad = np.abs(v - med) / mad >= CONTROL_OUTLIER_Z
            # never drop so many that a plate is left with no control at all
            if bad.any() and bad.sum() < len(v):
                for idx, is_bad in zip(grp.index, bad):
                    if is_bad:
                        keep_ctrl.loc[idx] = False
        dropped = ctrl_rows.loc[~keep_ctrl]
        if len(dropped):
            still = ctrl_rows.loc[keep_ctrl].groupby(plate_keys, sort=False).size()
            safe = []
            for idx, row in dropped.iterrows():
                key = tuple(row[k] for k in plate_keys)
                if still.get(key if len(key) > 1 else key[0], 0) > 0:
                    safe.append(idx)
            if safe:
                print(f"  ! {len(safe)} control spot(s) excluded from the "
                      f"normalizer as gross outliers among their own group's "
                      f"controls (robust z >= {CONTROL_OUTLIER_Z}); a bad control "
                      f"rescales every strain on its plate:")
                for idx in safe:
                    r = ctrl_rows.loc[idx]
                    who = " / ".join(str(r[k]) for k in keys)
                    print(f"      {who} {r.get('replicate','?')}: "
                          f"{r['raw_growth']:.2f}")
                ctrl_rows = ctrl_rows.drop(index=safe)

    ctrl_mean = (ctrl_rows
                 .groupby(plate_keys, sort=False)["raw_growth"].mean()
                 .rename("control_raw").reset_index())
    tidy = tidy.merge(ctrl_mean, on=plate_keys, how="left",
                      validate="many_to_one")
    # Kept for reporting: every score on a plate now divides by the same value.
    tidy["control_mean"] = tidy["control_raw"]

    # A control spot with no growth cannot normalize anything: the ratio either
    # explodes or flips sign, turning an unusable dilution into confident-looking
    # numbers. This happens for real -- on these plates the most dilute row often
    # has a control near zero -- so refuse to divide and mark the rows instead.
    floor = MIN_CONTROL_GRAY if min_control is None else float(min_control)
    is_ctrl = tidy["strain_col"] == control_col
    bad_ctrl = tidy["control_raw"] <= floor
    bad_mean = tidy["control_mean"] <= floor

    with np.errstate(divide="ignore", invalid="ignore"):
        # controls included -- one divisor for the whole plate
        rel = tidy["raw_growth"] / tidy["control_raw"]
    tidy["relative_growth"] = np.where(np.where(is_ctrl, bad_mean, bad_ctrl),
                                       np.nan, rel)
    tidy["control_ok"] = ~np.where(is_ctrl, bad_mean, bad_ctrl)

    n_bad = int((~tidy["control_ok"]).sum())
    if n_bad:
        groups = (tidy.loc[~tidy["control_ok"], keys + ["replicate"]]
                  .drop_duplicates().astype(str).agg(" / ".join, axis=1).tolist())
        print(f"  ! {n_bad} row(s) could not be normalized: the control spot "
              f"measured <= {floor:.2f} gray above agar, which is at this "
              f"plate's noise level, so the ratio would be noise over noise. "
              f"relative_growth is NaN for these; pick a less dilute row "
              f"(protocol step 20). Affected:")
        for g in groups[:6]:
            print(f"      {g}")
        if len(groups) > 6:
            print(f"      ... and {len(groups) - 6} more")
    return tidy


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class PlateResult:
    treatment: str
    plate: str                      # e.g. "Plate 1"
    image: str                      # image filename
    raw: np.ndarray                 # N_ROWS x N_COLS mean gray, pre-subtraction
    net: np.ndarray                 # N_ROWS x N_COLS, background-subtracted
    background: float
    n_bg_iters: int
    bg_spread: float                # max-min of the 5 background reads (QC)
    ball_radius: float              # radius actually used (QC / reproducibility)
    rep_rows: tuple[int, int]       # 0-based dilution rows quantified (rep1, rep2)


# ---------------------------------------------------------------------------
# Folder-tree discovery
# ---------------------------------------------------------------------------

# A plate folder is one named "Plate 1", "Plate2", "Plate 1 (Rep 1+2)", ... This
# is deliberately the same rule Data\Spotting Assays\curate.py uses, so the two
# tools always agree about what a plate is.
PLATE_RE = re.compile(r"^plate\s*\d", re.I)
TIMEPOINT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*hours?\b", re.I)
SET_RE = re.compile(r"^set\s*\d+", re.I)


@dataclass
class PlateRef:
    """One scoreable plate image, with its position in the corpus decoded.

    Every field except `image` is derived from the path and used only for
    labelling and grouping -- never for control flow -- so an unexpected folder
    name degrades a label rather than silently changing what gets measured.
    """
    image: Path
    rel: str                    # posix, relative to the walk root
    experiment: str             # first path component
    set_name: str               # "Set05" etc, "" when absent
    timepoint_h: float | None   # parsed from "NN Hours"
    treatment: str              # component directly above the plate folder
    plate: str                  # normalized, e.g. "Plate 1"
    plate_raw: str              # as on disk, e.g. "Plate 1 (Rep 1+2)"
    subset: str                 # "Replicants 1+2" etc when images nest deeper
    group: str                  # path above the plate folder = the ANOVA unit

    @property
    def replicate_stem(self) -> str:
        """Label that must be unique within a group."""
        return f"{self.plate}{' ' + self.subset if self.subset else ''}"


def _images_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def _decode(rel_dir: str, image: Path, root: Path) -> PlateRef | None:
    """Decode one image-bearing folder into a PlateRef, or None if it sits
    outside any plate folder."""
    parts = [p for p in rel_dir.split("/") if p not in (".", "")]

    idx = next((i for i in range(len(parts) - 1, -1, -1)
                if PLATE_RE.match(parts[i])), None)
    if idx is None:
        return None

    above = parts[:idx]
    plate_raw = parts[idx]
    subset = "/".join(parts[idx + 1:])

    # Normalize "Plate1", "plate 2", "Plate 1 (Rep 1+2)" -> "Plate 1" / "Plate 2"
    # so the same physical plate gets one label regardless of how it was typed.
    m = re.match(r"^plate\s*(\d+)", plate_raw, re.I)
    plate = f"Plate {int(m.group(1))}" if m else plate_raw.strip()

    timepoint = None
    for comp in reversed(above):
        m = TIMEPOINT_RE.search(comp)
        if m:
            timepoint = float(m.group(1))
            break

    return PlateRef(
        image=image, rel=(Path(rel_dir) / image.name).as_posix(),
        experiment=above[0] if above else "",
        set_name=next((c for c in above if SET_RE.match(c)), ""),
        timepoint_h=timepoint,
        treatment=above[-1] if above else "",
        plate=plate, plate_raw=plate_raw, subset=subset,
        group="/".join(above))


def discover_plates(root: Path, no_prompt: bool = False,
                    manifest: Path | None = None,
                    legacy_tuples: bool = True):
    """Find every scoreable plate image under `root`, at any nesting depth.

    The corpus is not uniform: image-bearing folders sit 2 to 7 levels deep, and
    143 of them are a level BELOW the plate folder (".../Plate 1/Replicants 3+4").
    So rather than assuming a fixed <treatment>/<plate>/ shape, this walks
    recursively and attributes each image to its nearest ancestor plate folder.

    `group` is the path above the plate folder, which makes the normalization
    unit correct at every depth without hardcoding a level count.

    Returns legacy (treatment, plate, image) tuples by default so existing
    callers keep working; pass legacy_tuples=False for the full PlateRef list.
    """
    sel: dict[str, str] = {}
    if manifest and manifest.exists():
        import json
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        entries = raw.get("selections", raw) if isinstance(raw, dict) else raw
        if isinstance(entries, dict):
            sel = {str(k).replace("\\", "/"): str(v) for k, v in entries.items()}
        print(f"  manifest: {len(sel)} pre-selected image(s) loaded.")

    refs: list[PlateRef] = []
    orphans: list[str] = []

    for dirpath, _, filenames in os.walk(root):
        folder = Path(dirpath)
        imgs = sorted(p for p in (folder / f for f in filenames)
                      if p.suffix.lower() in IMAGE_EXTS)
        if not imgs:
            continue
        rel_dir = folder.relative_to(root).as_posix()

        if _decode(rel_dir, imgs[0], root) is None:
            orphans.append(f"{rel_dir} ({len(imgs)} image(s))")
            continue

        chosen = sel.get(rel_dir)
        if chosen:
            match = next((p for p in imgs if p.name == chosen), None)
            img = match or imgs[0]
        elif len(imgs) == 1 or no_prompt:
            img = imgs[0]
            if len(imgs) > 1:
                print(f"  ! {rel_dir} has {len(imgs)} images; using {img.name}")
        else:
            img = _pick_image(imgs, rel_dir, "")

        ref = _decode(rel_dir, img, root)
        if ref is not None:
            refs.append(ref)

    if orphans:
        # Loud but not fatal: these are usually assembled figure PNGs, not plates.
        print(f"  ! {len(orphans)} image folder(s) sit outside any 'Plate N' "
              f"folder and were skipped:")
        for o in orphans[:10]:
            print(f"      {o}")
        if len(orphans) > 10:
            print(f"      ... and {len(orphans) - 10} more")

    refs.sort(key=lambda r: r.rel)
    if legacy_tuples:
        return [(r.treatment, r.plate, r.image) for r in refs]
    return refs


def resolve_strains(ref: PlateRef, root: Path, cli_strains: list[str] | None,
                    cli_control_col: int | None) -> tuple[list[str], int]:
    """Work out the strain names and control column for one plate.

    Resolution order: CLI > a strains.txt in the experiment folder > the
    experiment folder name when it is a comma-separated list of exactly N_COLS
    strains > the module default.

    Refuses to guess the control column. Two of the corpus trees are named
    "WT, EV, ..." and "EV, WT, ...", so assuming column 1 would silently rescale
    every number in one of them -- an error that produces plausible-looking
    output and no warning.
    """
    if cli_strains:
        if len(cli_strains) != N_COLS:
            raise ValueError(f"--strains has {len(cli_strains)} names, need {N_COLS}.")
        if cli_control_col is None:
            raise ValueError("--strains requires --control-col; the control is "
                             "not always column 1 in this corpus.")
        return list(cli_strains), cli_control_col

    exp_dir = root / ref.experiment if ref.experiment else root
    txt = exp_dir / "strains.txt"
    if txt.exists():
        lines = [ln.strip() for ln in txt.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        control = cli_control_col
        if lines and lines[0].lower().startswith("control:"):
            control = int(lines[0].split(":", 1)[1].strip())
            lines = lines[1:]
        if len(lines) != N_COLS:
            raise ValueError(f"{txt} lists {len(lines)} strains, need {N_COLS}.")
        if control is None:
            raise ValueError(
                f"{txt} does not declare a control column. Add a first line "
                f"'control: N' (1-based), or pass --control-col.")
        return lines, control

    parts = [p.strip() for p in ref.experiment.split(",")] if ref.experiment else []
    if len(parts) == N_COLS:
        if cli_control_col is None:
            raise ValueError(
                f"The folder name '{ref.experiment}' looks like a strain list, "
                f"but which column is the control cannot be inferred -- this "
                f"corpus has both 'WT, EV, ...' and 'EV, WT, ...' trees. Pass "
                f"--control-col, or add a strains.txt with a 'control: N' line.")
        return parts, cli_control_col

    if len(parts) > 1:
        # The folder names a strain list, but not one that fits the grid -- e.g.
        # the 14-name tree against 8 columns. Falling back to the module default
        # here would attach confident, wrong strain labels to real measurements,
        # so say so loudly rather than let it pass as a silent default.
        raise ValueError(
            f"The folder name '{ref.experiment}' lists {len(parts)} strains but "
            f"the plate grid has {N_COLS} columns, so the mapping is ambiguous. "
            f"Add a strains.txt in that folder naming the {N_COLS} columns "
            f"left-to-right with a 'control: N' first line, or pass --strains "
            f"and --control-col.")

    return list(STRAIN_NAMES), (cli_control_col or 1)


def _pick_image(imgs: list[Path], treatment: str, plate: str) -> Path:
    print(f"\n  {treatment}/{plate} contains {len(imgs)} images:")
    for i, p in enumerate(imgs, 1):
        print(f"    {i}. {p.name}")
    while True:
        try:
            v = int(input("  Which one should be scored? ").strip())
            if 1 <= v <= len(imgs):
                return imgs[v - 1]
        except (ValueError, EOFError):
            return imgs[0]
        print(f"    Enter 1-{len(imgs)}.")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def analyze_image_multi(path: Path, opts: MeasureOptions, row_sets: list,
                        label: str = "", debug: bool = False) -> dict:
    """Measure ONE plate for SEVERAL dilution-row choices in one pass.

    Detection (41s, dominated by the iterated full-resolution centring) and the
    FIJI background subtraction (10s) do not depend on which rows are scored --
    only the ROI radius does, and re-deriving that is under a second. So all
    three dilution choices can be produced for about +8s over a single one,
    instead of ~52s each. That is what lets the dilution be chosen at review
    time without waiting for a re-measure.

    Returns {row_set: (grid_copy, measurement)} plus a shared "_shared" entry
    holding (bg_centers, n_iter, spread, ball_radius).
    """
    import copy

    img8 = load_gray8(path, rgb_mode=opts.rgb_mode)
    if opts.rois is not None:
        grid, bg_centers = load_roi_csv(opts.rois)
    else:
        grid = detect_grid(img8, debug=debug, nudge=opts.nudge, quant_rows=None)
        bg_centers = background_gap_centers(grid)

    ball_radius = opts.resolve_ball_radius(grid.largest_diameter)
    if opts.bg_mode == "none":
        proc, n_iter, spread = img8.astype(np.float64), 0, 0.0
    else:
        proc, n_iter, spread = subtract_background(
            img8, ball_radius=ball_radius, bg_centers=bg_centers,
            bg_radius=grid.base_radius or grid.measure_radius,
            iters=opts.bg_iters, shrink=opts.shrink, mode=opts.bg_mode,
            fiji_path=path, debug=debug)

    small, ds = None, 1
    if opts.rois is None:
        from skimage.transform import resize
        ds = max(1, round(max(img8.shape) / DETECT_LONG_SIDE))
        small = (resize(img8.astype(float),
                        (img8.shape[0] // ds, img8.shape[1] // ds),
                        order=1, preserve_range=True) if ds > 1
                 else img8.astype(float))

    out = {"_shared": (bg_centers, n_iter, spread, ball_radius)}
    for rows in row_sets:
        g = copy.copy(grid)
        if opts.measure_diameter is not None:
            g.measure_radius = opts.measure_diameter / 2.0
        else:
            g.measure_radius, _, _ = roi_radius_for_rows(
                grid.spot_radii, grid.spot_exists, rows,
                grid.base_radius or grid.measure_radius, debug=debug)
        # The rim flag depends on the ROI size, so redo it per choice. Same
        # downscale detect_grid uses, computed once outside the loop.
        if g.plate_known and small is not None:
            g.artifact_flag = _rim_contaminated(
                small, g.centers / ds, g.measure_radius / ds,
                g.plate_center[1] / ds, g.plate_center[0] / ds,
                g.plate_radius / ds)
        out[tuple(rows)] = (g, measure_plate(proc, g, bg_centers))
    return out


def analyze_image(path: Path, opts: MeasureOptions, label: str = "",
                  debug: bool = False) -> tuple[np.ndarray, Grid,
                                                list[tuple[float, float]],
                                                PlateMeasurement, int, float, float]:
    """Load one plate image and measure it. Shared by the single-plate driver,
    the batch driver and the calibration harness so all three exercise exactly
    the same code path.

    Returns (processed_image, grid, bg_centers, measurement, n_iters, spread,
    ball_radius).
    """
    img8 = load_gray8(path, rgb_mode=opts.rgb_mode)

    if opts.rois is not None:
        grid, bg_centers = load_roi_csv(opts.rois)
    else:
        grid = detect_grid(img8, debug=debug, nudge=opts.nudge,
                           quant_rows=opts.quant_rows)
        bg_centers = background_gap_centers(grid)

    if opts.measure_diameter is not None:
        grid.measure_radius = opts.measure_diameter / 2.0

    ball_radius = opts.resolve_ball_radius(grid.largest_diameter)

    if opts.bg_mode == "none":
        proc, n_iter, spread = img8.astype(np.float64), 0, 0.0
    else:
        proc, n_iter, spread = subtract_background(
            img8, ball_radius=ball_radius, bg_centers=bg_centers,
            bg_radius=grid.measure_radius, iters=opts.bg_iters,
            shrink=opts.shrink, mode=opts.bg_mode, fiji_path=path, debug=debug)

    meas = measure_plate(proc, grid, bg_centers)
    return proc, grid, bg_centers, meas, n_iter, spread, ball_radius


def ask_control_col(strain_names: list[str], no_prompt: bool,
                    preset: int | None = None) -> int:
    """Which column is the control everything is normalised against?

    Asked once per run, after the images have been located, because getting it
    wrong silently rescales every number in the output -- and the plate layout
    is a property of the experiment, not of any one image.
    """
    if preset is not None:
        return preset
    if no_prompt:
        return 1
    print("\n  Strain columns (left to right):")
    for i, s in enumerate(strain_names, 1):
        print(f"    {i}. {s}")
    while True:
        try:
            raw = input(f"  Which column is the CONTROL? [1-{len(strain_names)}, "
                        f"default 1]: ").strip()
        except EOFError:
            return 1
        if not raw:
            return 1
        try:
            v = int(raw)
            if 1 <= v <= len(strain_names):
                print(f"    -> normalizing to '{strain_names[v - 1]}'")
                return v
        except ValueError:
            pass
        print(f"    Please enter a number from 1 to {len(strain_names)}.")


def process_root(root: Path, out_stub: str, debug: bool, no_prompt: bool,
                 opts: MeasureOptions | None = None,
                 strain_names: list[str] | None = None,
                 control_col: int | None = None) -> None:
    opts = opts or MeasureOptions()
    plates = discover_plates(root, no_prompt=no_prompt)
    if not plates:
        print(f"No treatment/plate/image folders found under {root}")
        return
    print(f"Found {len(plates)} plate image(s) across "
          f"{len({t for t, _, _ in plates})} treatment(s).")

    strain_names = strain_names or STRAIN_NAMES
    control_col = ask_control_col(strain_names, no_prompt, control_col)
    plate_results: list[PlateResult] = []
    long_rows = []  # tidy rows for normalized output + stats

    for treatment, plate, path in plates:
        label = f"{treatment} / {plate} / {path.name}"
        print(f"\n[+] {label}")
        proc, grid, bg_centers, meas, n_iter, spread, ball_r = analyze_image(
            path, opts, label=label, debug=debug)
        r1, r2 = choose_rows(proc, grid, meas.net, label, no_prompt, bg_centers)

        plate_results.append(PlateResult(
            treatment, plate, path.name, meas.raw, meas.net, meas.bg_mean,
            n_iter, spread, ball_r, (r1, r2)))
        for rep_idx, row in ((1, r1), (2, r2)):
            for col in range(N_COLS):
                long_rows.append({
                    "treatment": treatment, "plate": plate, "image": path.name,
                    "replicate": f"{plate}-rep{rep_idx}", "dilution_row": row + 1,
                    "strain_col": col + 1, "strain": strain_names[col],
                    "raw_growth": meas.net[row, col],
                    "artifact": bool(meas.rim_flag[row, col]),
                })
        n_neg = int((meas.net < 0).sum())
        print(f"    background={meas.bg_mean:.1f} (mean of {N_BG_SAMPLES}, "
              f"spread {spread:.2f}), ball r={ball_r:.0f}, iters={n_iter}, "
              f"rows quantified: {r1+1} & {r2+1}")
        if n_neg:
            print(f"    note: {n_neg}/{N_ROWS * N_COLS} spots read below agar "
                  f"(no growth); these are kept, not clipped.")
        for rep_idx, row in ((1, r1), (2, r2)):
            hit = [strain_names[c] for c in range(N_COLS) if meas.rim_flag[row, c]]
            if hit:
                print(f"    ! rep{rep_idx} (row {row+1}) ROI touches rim/label/bolt: "
                      f"{', '.join(hit)} -- excluded from the summary and stats.")
            if meas.rim_flag[row, control_col - 1]:
                print(f"    !! rep{rep_idx} CONTROL ({strain_names[control_col-1]}) "
                      f"is affected, so every ratio in that row is suspect; "
                      f"check the overlay.")

    _write_outputs(root, out_stub, plate_results, long_rows, control_col)


def _write_outputs(root, stub, plate_results, long_rows, control_col=1):
    # 1) Full table: every one of the 48 spots per plate, at BOTH stages.
    #    `stage=raw` is the mean gray before background subtraction, `stage=net`
    #    is after. Keeping both means a calibration question can be answered
    #    later without re-reading 7 MB images.
    raw_blocks = []
    for pr in plate_results:
        for stage, arr in (("raw", pr.raw), ("net", pr.net)):
            df = pd.DataFrame(arr,
                              index=[f"dil{i+1}" for i in range(N_ROWS)],
                              columns=[f"col{j+1}" for j in range(N_COLS)])
            df.insert(0, "treatment", pr.treatment)
            df.insert(1, "plate", pr.plate)
            df.insert(2, "image", pr.image)
            df.insert(3, "stage", stage)
            df.insert(4, "background", round(pr.background, 2))
            df.insert(5, "bg_spread", round(pr.bg_spread, 2))
            df.insert(6, "ball_radius", round(pr.ball_radius, 1))
            raw_blocks.append(df.reset_index().rename(columns={"index": "row"}))
    raw_path = root / f"{stub}_raw.csv"
    pd.concat(raw_blocks, ignore_index=True).to_csv(raw_path, index=False,
                                                    encoding="utf-8-sig")

    # 2) Tidy normalized table (one row per spot in the quantified rows).
    tidy = add_relative_growth(pd.DataFrame(long_rows), control_col=control_col)
    tidy_path = root / f"{stub}_normalized.csv"
    tidy.to_csv(tidy_path, index=False, encoding="utf-8-sig")

    # 3) Per-treatment, per-strain summary. Artifact-affected ROIs are kept in
    #    the tidy table (with the flag) but excluded here and from the stats:
    #    a spot whose ROI is mostly plate rim is not a measurement of growth,
    #    and averaging it in silently corrupts the strain it belongs to.
    clean = tidy[~tidy["artifact"]] if "artifact" in tidy.columns else tidy
    n_drop = len(tidy) - len(clean)
    summary = (clean.groupby(["treatment", "strain"], sort=False)["relative_growth"]
               .agg(["mean", "std", "count"]).reset_index())
    summary_path = root / f"{stub}_summary.csv"
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

    print(f"\nWrote:\n  {raw_path}\n  {tidy_path}\n  {summary_path}")
    if n_drop:
        print(f"  ({n_drop} artifact-flagged spot(s) kept in "
              f"{tidy_path.name} but excluded from the summary and stats.)")

    # 4) Stats per treatment (each treatment is its own experiment).
    ttests = []
    for treatment, sub in clean.groupby("treatment"):
        print(f"\n===== {treatment} =====")
        _run_stats(sub)
        ttests.append(_paired_ttests(sub, control_col, treatment))

    tt = pd.concat([t for t in ttests if t is not None], ignore_index=True) \
        if any(t is not None for t in ttests) else None
    if tt is not None and len(tt):
        tt_path = root / f"{stub}_paired_ttests.csv"
        tt.to_csv(tt_path, index=False, encoding="utf-8-sig")
        print(f"\nPaired t-tests vs control -> {tt_path}")
        print(tt.to_string(index=False))


def _paired_ttests(sub: pd.DataFrame, control_col: int,
                   treatment: str) -> pd.DataFrame | None:
    """Ratio paired t-test of every strain against the control.

    The pairing is already inside `relative_growth`: a strain's value is its own
    spot divided by the control spot from the SAME replicate, plate and dilution
    row, so plate-to-plate lighting, agar thickness and growth time cancel
    within the ratio. Testing those ratios against 1 is therefore the paired
    comparison on the ratio scale -- GraphPad's "ratio paired t-test", and the
    right form when the effect is multiplicative.

    An earlier version paired the strain's relative values against the CONTROL
    COLUMN's relative values. Those are not the same quantity: the strain's is
    divided by its own replicate's control, the control column's by the MEAN of
    the controls. That injected the control's whole between-replicate spread
    into every comparison -- on Set 2 K-OAc it made a strain with no growth at
    all and near-zero scatter come out "not significant".

    Mirrors src/plot_spotting.R so the table and the figure cannot disagree.
    """
    from scipy import stats as st

    if not (sub["strain_col"] == control_col).any():
        return None

    rows = []
    for col, g in sub[sub["strain_col"] != control_col].groupby("strain_col"):
        v = g["relative_growth"].to_numpy(dtype=float)
        v = v[np.isfinite(v)]
        p = float(st.ttest_1samp(v, 1.0).pvalue) if len(v) >= 2 else np.nan
        rows.append({"treatment": treatment,
                     "control": sub.loc[sub["strain_col"] == control_col,
                                        "strain"].iloc[0],
                     "strain": col, "strain_col": col, "n_pairs": len(v),
                     "mean_ratio": float(v.mean()) if len(v) else np.nan,
                     "p": p})
    if not rows:
        return None
    out = pd.DataFrame(rows)
    out["strain"] = [sub.loc[sub["strain_col"] == c, "strain"].iloc[0]
                     for c in out["strain_col"]]
    # Seven comparisons against one control is a family; report both so the
    # choice of correction is explicit rather than implied.
    finite = out["p"].notna()
    out["p_holm"] = np.nan
    if finite.any():
        from statsmodels.stats.multitest import multipletests
        out.loc[finite, "p_holm"] = multipletests(
            out.loc[finite, "p"], method="holm")[1]
    return out


def _run_stats(tidy: pd.DataFrame) -> None:
    """One-way ANOVA + Tukey HSD on relative growth by strain (steps 29-30)."""
    n_before = len(tidy)
    tidy = tidy[tidy["relative_growth"].notna()]
    dropped = n_before - len(tidy)
    if dropped:
        # These are the rows whose control did not grow (see add_relative_growth).
        # Silently feeding NaN to f_oneway returns NaN for everything, which
        # looks like a statistics failure rather than a data problem.
        print(f"  ({dropped} unnormalizable row(s) excluded from statistics.)")

    groups = [g["relative_growth"].to_numpy() for _, g in tidy.groupby("strain")]
    if len(groups) < 2 or any(len(g) < 2 for g in groups):
        print("  (Not enough replicates per strain for ANOVA.)")
        return
    from scipy.stats import f_oneway
    from statsmodels.stats.multicomp import pairwise_tukeyhsd
    F, p = f_oneway(*groups)
    print(f"  One-way ANOVA: F={F:.3f}, p={p:.3g}")
    tuk = pairwise_tukeyhsd(tidy["relative_growth"], tidy["strain"], alpha=0.05)
    print(tuk.summary())


def main(argv=None):
    try:                                    # allow Δ strain names on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Automated yeast spotting-assay quantification.")
    ap.add_argument("root", type=Path,
                    help="Root folder containing <treatment>/<plate>/<image> subfolders, "
                         "or a single image when --export-rois is given.")
    ap.add_argument("--out", default="spotting_results", help="Output filename stub.")
    ap.add_argument("--debug", action="store_true", help="Verbose + detection diagnostics.")
    ap.add_argument("--no-prompt", action="store_true",
                    help="Skip interactive row picking; use 3rd dilution of each replicate.")

    g = ap.add_argument_group("measurement (calibration knobs)")
    g.add_argument("--rois", type=Path, metavar="CSV",
                   help="Measure at these hand-recorded ROIs instead of auto-detecting.")
    g.add_argument("--export-rois", type=Path, metavar="STEM",
                   help="Write auto-detected ROIs as STEM.csv + STEM.ijm and exit. "
                        "ROOT must be a single image file.")
    g.add_argument("--measure-diameter", type=float, metavar="PX",
                   help="Force the measuring ROI diameter (px).")
    g.add_argument("--ball-radius", type=float, metavar="PX",
                   help="Rolling-ball radius (px), verbatim as typed into ImageJ's "
                        "Subtract Background dialog. Overrides the derived value.")
    g.add_argument("--bg-mode",
                   choices=("fiji", "paraboloid", "rollingball", "none"),
                   default="fiji",
                   help="'fiji' drives FIJI's own Subtract Background (sliding "
                        "paraboloid) headlessly and is the default, falling back "
                        "to the local implementation if FIJI is not installed. "
                        "'paraboloid' is the protocol's own choice (step 15, "
                        "'sliding paraboloid') and the default. 'rollingball' is "
                        "ImageJ's ball with its shrink schedule, kept for "
                        "comparison. 'none' skips background subtraction entirely, "
                        "so raw gray values can be compared with no background "
                        "code in the path.")
    g.add_argument("--bg-iters", type=int, default=BG_ITERS,
                   help=f"Rolling-ball passes (default {BG_ITERS}). See "
                        f"subtract_background on why more than one is wrong.")
    g.add_argument("--shrink", type=int, metavar="N",
                   help="Override ImageJ's 1/2/4/8 downscale schedule. --shrink 1 "
                        "disables downscaling (accurate, minutes per 24 MP image).")
    g.add_argument("--rgb-mode", choices=("unweighted", "weighted"),
                   default=DEFAULT_RGB_MODE,
                   help="RGB->gray conversion; must match ImageJ's Edit > Options > "
                        "Conversions setting used for the hand measurement.")
    ap.add_argument("--control-col", type=int, metavar="N",
                    help=f"1-based column of the control strain that everything "
                         f"is normalized against. Asked interactively if omitted "
                         f"(and assumed to be column 1 under --no-prompt).")
    ap.add_argument("--strains", metavar="NAMES",
                    help="Comma-separated strain names, left to right. Defaults "
                         "to STRAIN_NAMES in this file.")
    args = ap.parse_args(argv)

    strains = None
    if args.strains:
        strains = [s.strip() for s in args.strains.split(",")]
        if len(strains) != N_COLS:
            print(f"--strains lists {len(strains)} names but the grid has "
                  f"{N_COLS} columns.", file=sys.stderr)
            return 2
    if args.control_col is not None and not 1 <= args.control_col <= N_COLS:
        print(f"--control-col must be between 1 and {N_COLS}.", file=sys.stderr)
        return 2

    opts = MeasureOptions(
        rois=args.rois, measure_diameter=args.measure_diameter,
        ball_radius=args.ball_radius, bg_mode=args.bg_mode,
        bg_iters=args.bg_iters, shrink=args.shrink, rgb_mode=args.rgb_mode)

    if args.export_rois is not None:
        if not args.root.is_file():
            print(f"--export-rois needs a single image file, got: {args.root}",
                  file=sys.stderr)
            return 2
        img8 = load_gray8(args.root, rgb_mode=opts.rgb_mode)
        grid = detect_grid(img8, debug=args.debug)
        if opts.measure_diameter is not None:
            grid.measure_radius = opts.measure_diameter / 2.0
        csv_path, ijm_path = export_rois(grid, background_gap_centers(grid),
                                         args.export_rois)
        print(f"Wrote:\n  {csv_path}\n  {ijm_path}\n\n"
              f"Next: open the plate in Fiji, Image > Type > 8-bit, then drag "
              f"{ijm_path.name} onto Fiji and press Run to load all "
              f"{N_ROWS * N_COLS + N_BG_SAMPLES} ROIs into the ROI Manager.")
        return 0

    if not args.root.is_dir():
        print(f"Not a folder: {args.root}", file=sys.stderr)
        return 2
    process_root(args.root, args.out, args.debug, args.no_prompt, opts=opts,
                 strain_names=strains, control_col=args.control_col)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
