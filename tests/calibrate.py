#!/usr/bin/env python3
"""
calibrate.py -- Stage-by-stage comparison of spotting_quant against hand ImageJ.

Replaces the old tests/validate.py, which compared ONLY final relative-growth
values. That is why its Pearson r of 0.61 was undiagnosable: a single number at
the end of a seven-step pipeline cannot tell you which step went wrong.

This walks the pipeline instead, comparing at every stage:

    S0   grid geometry        auto-detected centres vs the hand ROI positions
    S0b  ROI area             our pixel counts vs ImageJ's `Area`
    S1   raw gray             THE GATE -- same pixels, same coordinates
    S2   raw gray @ auto ROIs informational: what grid offset costs in gray
    S3   rolling-ball         our background subtraction vs ImageJ's
    S4   background scalar    the mean of the five agar reads
    S5   net                  raw minus background
    S6a  normalizer           our maths run over ImageJ's OWN numbers
    S6b  end-to-end           the headline relative-growth comparison

Read down the table and stop at the first FAIL: that is where divergence starts.

S1 is the gate. If reading identical pixels at identical coordinates does not
reproduce ImageJ's mean to within 0.5 gray, then nothing downstream is worth
tuning and the bug is something unglamorous -- RGB conversion mode, rounding, or
the ROI mask rule.

S6a is the stage the old validator could not express. Running OUR normalizer
over HAND net values separates "our imaging is wrong" from "our normalization is
wrong", which are otherwise indistinguishable in the final number.

Usage:
  python tests/calibrate.py --gt tests/gt/<plate_id>
  python tests/calibrate.py --gt tests/gt/<plate_id> --plot
  python tests/calibrate.py --gt tests/gt/<plate_id> --sweep ball-radius 107,150,214,300
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import spotting_quant as sq   # noqa: E402


# ---------------------------------------------------------------------------
# Gates -- the tolerance each stage must meet
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Gate:
    max_abs: float | None = None
    min_r: float | None = None
    mae: float | None = None
    informational: bool = False


GATES: dict[str, Gate] = {
    "S0  geometry (px)":      Gate(max_abs=None),        # set from ROI diameter
    "S0b ROI area (px)":      Gate(max_abs=None),        # set from ROI area (1%)
    "S1  raw gray @ GT ROIs": Gate(max_abs=0.5, min_r=0.999),
    "S2  raw gray @ auto":    Gate(informational=True),
    "S3  rolling-ball":       Gate(max_abs=1.0),
    "S4  background scalar":  Gate(max_abs=0.5),
    "S5  net":                Gate(max_abs=1.5, min_r=0.99),
    "S6a normalizer only":    Gate(max_abs=0.005),
    "S6b end-to-end":         Gate(min_r=0.95, mae=0.05),
}


# ---------------------------------------------------------------------------
# Ground-truth loading
# ---------------------------------------------------------------------------

LABEL_SPOT = re.compile(r"spot[_\s]*r(\d+)\s*c(\d+)", re.I)
LABEL_BG = re.compile(r"bg[_\s]*(\d+)", re.I)


def load_meta(gt_dir: Path) -> dict[str, str]:
    """Read gt_meta.csv (key/value) into a plain dict."""
    path = gt_dir / "gt_meta.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Copy gt_meta_TEMPLATE.csv to gt_meta.csv and "
            f"fill it in -- see tests/gt/README.md.")
    df = pd.read_csv(path, encoding="utf-8-sig")
    if "key" not in df.columns or "value" not in df.columns:
        raise ValueError(f"{path} must have 'key' and 'value' columns.")
    return {str(k).strip(): ("" if pd.isna(v) else str(v).strip())
            for k, v in zip(df["key"], df["value"])}


def _meta_float(meta: dict[str, str], key: str) -> float | None:
    v = meta.get(key, "")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _meta_bool(meta: dict[str, str], key: str) -> bool:
    return str(meta.get(key, "")).strip().upper() in ("TRUE", "1", "YES", "Y")


def load_imagej_results(path: Path) -> pd.DataFrame:
    """Parse an ImageJ Results table export into a tidy frame.

    Identifies each ROI from the `Label` column, which ImageJ writes as
    "<image title>:<roi name>" when Display Label is enabled -- so the spot
    coordinates come from the ROI names the export macro assigned, and nothing
    has to be hand-annotated after the fact.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).strip() for c in df.columns]

    if "Label" not in df.columns:
        raise ValueError(
            f"{path.name} has no 'Label' column. Enable Analyze > Set "
            f"Measurements > Display label and measure again -- without it "
            f"there is no way to tell which row is which ROI.")
    for needed in ("Mean", "Area", "BX", "BY", "Width", "Height"):
        if needed not in df.columns:
            raise ValueError(
                f"{path.name} has no '{needed}' column. Tick Area, Mean gray "
                f"value, and Bounding rectangle in Set Measurements.")

    kind, rows, cols, bgi = [], [], [], []
    for lab in df["Label"].astype(str):
        name = lab.split(":")[-1].strip()
        m_spot, m_bg = LABEL_SPOT.search(name), LABEL_BG.search(name)
        if m_spot:
            kind.append("spot")
            rows.append(int(m_spot.group(1)))
            cols.append(int(m_spot.group(2)))
            bgi.append(np.nan)
        elif m_bg:
            kind.append("background")
            rows.append(np.nan)
            cols.append(np.nan)
            bgi.append(int(m_bg.group(1)))
        else:
            raise ValueError(
                f"{path.name}: cannot identify ROI from label {lab!r}. Expected "
                f"names like 'spot_r3c5' or 'bg_2', which the --export-rois "
                f"macro assigns. Did the ROI Manager get renamed or reset?")

    out = df.copy()
    out["kind"], out["row"], out["col"], out["bg_index"] = kind, rows, cols, bgi
    return out


def gt_arrays(res: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[float], list[float]]:
    """Reshape a parsed Results frame into (means[6,8], areas[6,8], bg_means, bg_areas)."""
    means = np.full((sq.N_ROWS, sq.N_COLS), np.nan)
    areas = np.full((sq.N_ROWS, sq.N_COLS), np.nan)
    for _, r in res[res["kind"] == "spot"].iterrows():
        i, j = int(r["row"]) - 1, int(r["col"]) - 1
        means[i, j] = float(r["Mean"])
        areas[i, j] = float(r["Area"])
    bgs = res[res["kind"] == "background"].sort_values("bg_index")
    return means, areas, [float(v) for v in bgs["Mean"]], [float(v) for v in bgs["Area"]]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class StageResult:
    name: str
    n: int
    bias: float          # mean signed difference (measured - expected)
    mae: float
    max_abs: float
    r: float
    slope: float
    intercept: float
    gate: Gate
    passed: bool | None  # None = informational
    residuals: pd.DataFrame = field(default_factory=pd.DataFrame)


def compare(name: str, measured: np.ndarray, expected: np.ndarray,
            gate: Gate, labels: list[str] | None = None) -> StageResult:
    """Compare two flat arrays and score them against a gate.

    Reports slope and intercept as well as r, because together they say WHAT
    kind of error it is: slope ~1 with a non-zero intercept is an offset (a
    background problem), slope != 1 with intercept ~0 is a gain problem
    (grayscale conversion, ROI size).
    """
    m = np.asarray(measured, dtype=float).ravel()
    e = np.asarray(expected, dtype=float).ravel()
    ok = ~(np.isnan(m) | np.isnan(e))
    m, e = m[ok], e[ok]
    labs = ([labels[i] for i in np.flatnonzero(ok)] if labels
            else [str(i) for i in np.flatnonzero(ok)])

    if m.size == 0:
        return StageResult(name, 0, np.nan, np.nan, np.nan, np.nan, np.nan,
                           np.nan, gate, None)

    d = m - e
    bias, mae, mx = float(d.mean()), float(np.abs(d).mean()), float(np.abs(d).max())
    if m.size > 2 and np.std(m) > 1e-12 and np.std(e) > 1e-12:
        r = float(np.corrcoef(m, e)[0, 1])
        slope, intercept = (float(v) for v in np.polyfit(e, m, 1))
    else:
        r = slope = intercept = np.nan

    if gate.informational:
        passed = None
    else:
        passed = True
        if gate.max_abs is not None:
            passed &= mx <= gate.max_abs
        if gate.min_r is not None:
            passed &= (not np.isnan(r)) and r >= gate.min_r
        if gate.mae is not None:
            passed &= mae <= gate.mae

    resid = pd.DataFrame({"stage": name, "roi": labs, "measured": m,
                          "expected": e, "delta": d})
    resid = resid.reindex(resid["delta"].abs().sort_values(ascending=False).index)
    return StageResult(name, int(m.size), bias, mae, mx, r, slope, intercept,
                       gate, passed, resid)


def print_table(stages: list[StageResult]) -> None:
    hdr = (f"{'stage':<24} {'n':>4} {'bias':>8} {'MAE':>8} {'max|d|':>8} "
           f"{'r':>8} {'slope':>7} {'icept':>8}  verdict")
    print("\n" + hdr)
    print("-" * len(hdr))
    for s in stages:
        if s.passed is None:
            verdict = "info"
        elif s.passed:
            verdict = "PASS"
        else:
            verdict = "FAIL  <-- divergence starts here" \
                if not any(x.passed is False for x in stages[:stages.index(s)]) else "FAIL"
        print(f"{s.name:<24} {s.n:>4} {s.bias:>8.3f} {s.mae:>8.3f} "
              f"{s.max_abs:>8.3f} {s.r:>8.4f} {s.slope:>7.3f} {s.intercept:>8.3f}  {verdict}")


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def spot_labels() -> list[str]:
    return [f"r{i+1}c{j+1}" for i in range(sq.N_ROWS) for j in range(sq.N_COLS)]


def build_opts(meta: dict[str, str], overrides: dict[str, object] | None = None) -> sq.MeasureOptions:
    """Construct MeasureOptions from the recorded ImageJ settings.

    The point of gt_meta.csv is that the program is driven by what was ACTUALLY
    typed into ImageJ, rather than by our guess at what the protocol meant.
    """
    opts = sq.MeasureOptions(
        rgb_mode="weighted" if _meta_bool(meta, "rgb_weighted") else "unweighted",
        ball_radius=_meta_float(meta, "rollingball_radius_px"),
        measure_diameter=_meta_float(meta, "measure_diameter_px"),
        bg_iters=int(_meta_float(meta, "rollingball_iterations") or 1),
    )
    for k, v in (overrides or {}).items():
        setattr(opts, k, v)
    return opts


def run_ladder(gt_dir: Path, image: Path, meta: dict[str, str],
               overrides: dict[str, object] | None = None,
               debug: bool = False) -> list[StageResult]:
    roi_csv = gt_dir / "gt_rois.csv"
    if not roi_csv.exists():
        raise FileNotFoundError(
            f"{roi_csv} not found. Export ROIs with --export-rois, adjust them "
            f"in Fiji if needed, and save the final geometry here.")

    gt_grid, gt_bg_centers = sq.load_roi_csv(roi_csv)
    opts = build_opts(meta, overrides)
    opts.rois = roi_csv

    raw_res = load_imagej_results(gt_dir / "gt_measurements_raw8.csv")
    gt_raw, gt_area, gt_bg_raw, gt_bg_area = gt_arrays(raw_res)

    rb_path = gt_dir / "gt_measurements_rollingball.csv"
    have_rb = rb_path.exists()
    if have_rb:
        rb_res = load_imagej_results(rb_path)
        gt_rb, _, gt_bg_rb, _ = gt_arrays(rb_res)

    stages: list[StageResult] = []
    labels = spot_labels()

    # --- Load the image once, both stages -----------------------------------
    img8 = sq.load_gray8(image, rgb_mode=opts.rgb_mode)

    # S0: does auto-detection land where the hand ROIs are?
    auto_grid = sq.detect_grid(img8, debug=debug)
    diam = gt_grid.measure_radius * 2
    g0 = Gate(max_abs=0.15 * diam)
    offs = np.hypot(auto_grid.centers[..., 0] - gt_grid.centers[..., 0],
                    auto_grid.centers[..., 1] - gt_grid.centers[..., 1])
    stages.append(compare("S0  geometry (px)", offs, np.zeros_like(offs), g0, labels))

    # S0b: do our ROI masks contain the same number of pixels ImageJ used?
    meas_raw = sq.measure_plate(img8.astype(np.float64), gt_grid, gt_bg_centers)
    g0b = Gate(max_abs=0.01 * float(np.nanmean(gt_area)))
    stages.append(compare("S0b ROI area (px)", meas_raw.n_px, gt_area, g0b, labels))

    # S1: THE GATE -- same pixels, same coordinates, same number?
    stages.append(compare("S1  raw gray @ GT ROIs", meas_raw.raw, gt_raw,
                          GATES["S1  raw gray @ GT ROIs"], labels))

    # S2: informational -- how much gray does the grid offset actually cost?
    auto_bg = sq.background_gap_centers(auto_grid)
    if opts.measure_diameter is not None:
        auto_grid.measure_radius = opts.measure_diameter / 2.0
    meas_auto = sq.measure_plate(img8.astype(np.float64), auto_grid, auto_bg)
    stages.append(compare("S2  raw gray @ auto", meas_auto.raw, gt_raw,
                          GATES["S2  raw gray @ auto"], labels))

    if not have_rb:
        print(f"\n  ! {rb_path.name} not found -- stopping after S2. "
              f"Capture the post-Subtract-Background pass to exercise S3-S6.")
        return stages

    # S3: our rolling ball vs ImageJ's, measured at the same ROIs
    ball_r = opts.resolve_ball_radius(gt_grid.largest_diameter)
    proc, _, _ = sq.subtract_background(
        img8, ball_radius=ball_r, bg_centers=gt_bg_centers,
        bg_radius=gt_grid.measure_radius, iters=opts.bg_iters,
        shrink=opts.shrink, debug=debug)
    meas_rb = sq.measure_plate(proc, gt_grid, gt_bg_centers)
    stages.append(compare("S3  rolling-ball", meas_rb.raw, gt_rb,
                          GATES["S3  rolling-ball"], labels))

    # S4: the background scalar the whole normalization hinges on
    stages.append(compare("S4  background scalar",
                          np.array(meas_rb.bg_samples), np.array(gt_bg_rb),
                          GATES["S4  background scalar"],
                          [f"bg{i+1}" for i in range(len(gt_bg_rb))]))

    # S5: net
    gt_net = gt_rb - float(np.mean(gt_bg_rb))
    stages.append(compare("S5  net", meas_rb.net, gt_net, GATES["S5  net"], labels))

    # S6a/S6b need the hand ratios; skip cleanly when they were not supplied
    rel_path = gt_dir / "gt_relative_growth.csv"
    if not rel_path.exists():
        print(f"\n  ! {rel_path.name} not supplied -- S6a/S6b skipped. "
              f"Add it (replicate,strain,relative_growth) to check the "
              f"normalization arithmetic separately from the imaging.")
        return stages

    hand = pd.read_csv(rel_path, encoding="utf-8-sig")
    strains = [meta.get(f"strain_col_{j+1}", f"col{j+1}") for j in range(sq.N_COLS)]
    ctrl_col = int(_meta_float(meta, "control_col") or 1)
    r1 = int(_meta_float(meta, "quantified_row_rep1") or 3) - 1
    r2 = int(_meta_float(meta, "quantified_row_rep2") or 5) - 1

    def tidy_from(net: np.ndarray) -> pd.DataFrame:
        rows = []
        for rep, row in ((1, r1), (2, r2)):
            for j in range(sq.N_COLS):
                rows.append({"treatment": "gt", "replicate": f"rep{rep}",
                             "strain_col": j + 1, "strain": strains[j],
                             "raw_growth": net[row, j]})
        return sq.add_relative_growth(pd.DataFrame(rows), control_col=ctrl_col)

    def align(t: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, list[str]]:
        merged = t.merge(hand, on=["replicate", "strain"], how="inner",
                         suffixes=("_prog", "_hand"))
        if merged.empty:
            raise ValueError(
                f"{rel_path.name} shares no (replicate, strain) pairs with the "
                f"measured data. Expected replicates rep1/rep2 and strains "
                f"{strains}.")
        return (merged["relative_growth_prog"].to_numpy(),
                merged["relative_growth_hand"].to_numpy(),
                [f"{a}/{b}" for a, b in zip(merged['replicate'], merged['strain'])])

    # S6a: OUR normalizer over ImageJ's OWN net values -- isolates the maths
    m, e, labs = align(tidy_from(gt_net))
    stages.append(compare("S6a normalizer only", m, e,
                          GATES["S6a normalizer only"], labs))

    # S6b: the headline number
    m, e, labs = align(tidy_from(meas_rb.net))
    stages.append(compare("S6b end-to-end", m, e, GATES["S6b end-to-end"], labs))
    return stages


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

SWEEPABLE = {"ball-radius": ("ball_radius", float), "shrink": ("shrink", int),
             "bg-iters": ("bg_iters", int), "measure-diameter": ("measure_diameter", float),
             "rgb-mode": ("rgb_mode", str), "bg-mode": ("bg_mode", str)}


def run_sweep(gt_dir: Path, image: Path, meta: dict[str, str],
              param: str, values: list[str]) -> None:
    """Re-run the ladder across a parameter grid, reporting the stages that the
    parameter can actually move (S3, S5, S6b)."""
    if param not in SWEEPABLE:
        raise ValueError(f"--sweep parameter must be one of {sorted(SWEEPABLE)}, "
                         f"got {param!r}.")
    attr, cast = SWEEPABLE[param]
    watch = ("S3  rolling-ball", "S5  net", "S6b end-to-end")

    print(f"\nSweeping {param} over {values}")
    print(f"{param:>16} | " + " | ".join(f"{w:>22}" for w in watch))
    print("-" * (18 + 25 * len(watch)))
    for raw in values:
        val = cast(raw)
        stages = {s.name: s for s in run_ladder(gt_dir, image, meta, {attr: val})}
        cells = []
        for w in watch:
            s = stages.get(w)
            cells.append(f"{'--':>22}" if s is None
                         else f"MAE {s.mae:>7.3f} r {s.r:>6.3f}")
        print(f"{raw:>16} | " + " | ".join(cells))


# ---------------------------------------------------------------------------
# Plots
# ---------------------------------------------------------------------------

def plot_stages(stages: list[StageResult], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    show = [s for s in stages if s.n and not s.residuals.empty]
    ncol = 3
    nrow = int(np.ceil(len(show) / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.2 * ncol, 3.8 * nrow),
                             squeeze=False)
    for ax, s in zip(axes.ravel(), show):
        d = s.residuals
        ax.scatter(d["expected"], d["measured"], s=14, alpha=0.75)
        lo = float(min(d["expected"].min(), d["measured"].min()))
        hi = float(max(d["expected"].max(), d["measured"].max()))
        ax.plot([lo, hi], [lo, hi], ls="--", lw=1, color="grey")
        for _, row in d.head(5).iterrows():      # label the worst offenders
            ax.annotate(str(row["roi"]), (row["expected"], row["measured"]),
                        fontsize=6, alpha=0.8)
        verdict = "info" if s.passed is None else ("PASS" if s.passed else "FAIL")
        ax.set_title(f"{s.name}  [{verdict}]\nMAE {s.mae:.3f}  r {s.r:.4f}",
                     fontsize=9)
        ax.set_xlabel("hand ImageJ"); ax.set_ylabel("spotting_quant")
    for ax in axes.ravel()[len(show):]:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"  {out}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="Stage-by-stage calibration of spotting_quant against hand ImageJ.")
    ap.add_argument("--gt", type=Path, required=True,
                    help="Ground-truth folder (see tests/gt/README.md).")
    ap.add_argument("--image", type=Path,
                    help="Plate image. Defaults to image_relpath from gt_meta.csv.")
    # image_relpath in gt_meta.csv is relative to wherever the photographs
    # happen to live, which differs on every machine. This used to default to
    # one particular local folder, which silently did the wrong thing for
    # anybody else who ran it.
    ap.add_argument("--data-root", type=Path,
                    default=Path(os.environ.get("SPOTTING_DATA_ROOT", ".")),
                    help="Root that image_relpath in gt_meta.csv is relative "
                         "to. Defaults to $SPOTTING_DATA_ROOT, or the current "
                         "folder.")
    ap.add_argument("--sweep", nargs=2, metavar=("PARAM", "VALUES"),
                    help="Sweep a parameter, e.g. --sweep ball-radius 107,150,214")
    ap.add_argument("--plot", action="store_true", help="Save scatter plots.")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    if not args.gt.is_dir():
        print(f"Not a folder: {args.gt}", file=sys.stderr)
        return 2

    meta = load_meta(args.gt)

    image = args.image
    if image is None:
        rel = meta.get("image_relpath", "")
        if not rel:
            print("No --image given and gt_meta.csv has no image_relpath.",
                  file=sys.stderr)
            return 2
        image = args.data_root / rel
    if not image.is_file():
        print(f"Image not found: {image}", file=sys.stderr)
        return 2

    # Provenance check -- the entire reason this rewrite exists.
    want = meta.get("image_sha256", "")
    if want:
        import hashlib
        got = hashlib.sha256(image.read_bytes()).hexdigest()
        if got.lower() != want.lower():
            print(f"\n  ! IMAGE MISMATCH\n    {image}\n    sha256 {got}\n"
                  f"    gt_meta says {want}\n"
                  f"    This is not the image the ground truth was measured on. "
                  f"Calibrating against it would be meaningless.\n", file=sys.stderr)
            return 1
        print(f"Image verified against gt_meta sha256: {image.name}")
    else:
        print(f"  ! gt_meta.csv has no image_sha256 -- provenance unverified. "
              f"Fill it in; unverifiable ground truth is what caused the last "
              f"round of confusion.")

    if args.sweep:
        param, values = args.sweep[0], [v.strip() for v in args.sweep[1].split(",")]
        run_sweep(args.gt, image, meta, param, values)
        return 0

    stages = run_ladder(args.gt, image, meta, debug=args.debug)
    print_table(stages)

    failed = [s for s in stages if s.passed is False]
    if failed:
        first = failed[0]
        print(f"\nFirst failure: {first.name}")
        print("Worst offenders:")
        print(first.residuals.head(8).to_string(index=False,
                                                float_format=lambda v: f"{v:9.3f}"))
    else:
        print("\nAll gated stages passed.")

    print("\nWrote:")
    rep = args.gt / "calibration_report.csv"
    pd.DataFrame([{"stage": s.name, "n": s.n, "bias": s.bias, "mae": s.mae,
                   "max_abs": s.max_abs, "r": s.r, "slope": s.slope,
                   "intercept": s.intercept,
                   "verdict": "info" if s.passed is None else
                              ("PASS" if s.passed else "FAIL")}
                  for s in stages]).to_csv(rep, index=False, encoding="utf-8-sig")
    print(f"  {rep}")

    resid = args.gt / "residuals.csv"
    pd.concat([s.residuals for s in stages if not s.residuals.empty],
              ignore_index=True).to_csv(resid, index=False, encoding="utf-8-sig")
    print(f"  {resid}")

    if args.plot:
        plot_stages(stages, args.gt / "calibration_plots.png")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
