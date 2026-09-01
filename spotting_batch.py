#!/usr/bin/env python3
"""
spotting_batch.py -- interactive batch workflow over a flat folder of spotting
assay photos whose metadata lives in the FILENAME.

    <set>.<plate><TREATMENT>.JPG        e.g.  1.2GLU.JPG
     |     |       |
     |     |       +-- treatment: GLU, GLY, K-OAc, ...
     |     +---------- plate 1 (biological replicates 1+2)
     |                 plate 2 (biological replicates 3+4)
     +---------------- set

A "treatment-set combination" (e.g. set 1 on K-OAc) is one experiment: its two
plates carry four biological replicates of the same strain panel. Each SET has
its own strain panel, so combinations are never pooled across sets.

Run order, which mirrors how the decisions actually depend on each other:

    1. discover photos and group them into treatment-set combinations
    2. per SET: name the 8 columns (a column may be empty) and pick the control
    3. measure every plate once (cached on disk, so re-runs are instant)
    4. per combination: show its plates side by side, pick the dilution to score
    5. normalize, export tidy data + summaries, and draw the Prism-style figures

Steps 2 and 4 are remembered in spotting_config.json, so a second run only asks
about things it has not seen. Delete that file, or pass --reask, to start over.

Usage:
    python spotting_batch.py "Spotting Assays" [options]
    (or just double-click run_spotting.bat)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import spotting_quant as sq   # noqa: E402

# <set>.<plate><treatment>; the treatment may contain hyphens ("K-OAc").
NAME_RE = re.compile(r"^\s*(\d+)\s*\.\s*(\d+)\s*(.+?)\s*$")

# The lab spots three dilutions per replicate, twice down the plate:
#   rows 1,4 = least dilute   rows 2,5 = middle   rows 3,6 = most dilute
DILUTIONS = {"least": (0, 3), "middle": (1, 4), "most": (2, 5)}
DILUTION_ORDER = ["least", "middle", "most"]

CONFIG_NAME = "spotting_config.json"
CACHE_DIR = ".spotting_cache"

# Long edge, in pixels, of each plate in the dilution-choice preview. At 6000 px
# wide a spot is ~194 px across, so this keeps a spot ~65 px -- enough to see
# individual colonies in a sparse spot. Raise it with --preview-size if a
# borderline plate needs more; the cost is only redraw time.
PREVIEW_PX = 2000


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PhotoRef:
    path: Path
    set_id: str
    plate: int
    treatment: str

    @property
    def combo(self) -> str:
        """Key for one experiment: this set on this medium."""
        return f"{self.set_id}|{self.treatment}"

    @property
    def label(self) -> str:
        return f"Set {self.set_id} {self.treatment} plate {self.plate}"


def parse_name(stem: str) -> tuple[str, int, str] | None:
    m = NAME_RE.match(stem)
    if not m:
        return None
    set_id, plate, treatment = m.group(1), int(m.group(2)), m.group(3).strip()
    return (set_id, plate, treatment) if treatment else None


def discover(folder: Path) -> tuple[dict[str, list[PhotoRef]], list[str]]:
    """Group images into {combo: [PhotoRef, ...]}. Also returns the names that
    did not parse, so a typo'd file is reported rather than silently ignored."""
    combos: dict[str, list[PhotoRef]] = {}
    skipped: list[str] = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in sq.IMAGE_EXTS:
            continue
        parsed = parse_name(p.stem)
        if parsed is None:
            skipped.append(p.name)
            continue
        ref = PhotoRef(p, *parsed)
        combos.setdefault(ref.combo, []).append(ref)
    for refs in combos.values():
        refs.sort(key=lambda r: r.plate)
    return combos, skipped


def sort_combos(keys) -> list[str]:
    """Treatment first, then set numerically, so the run reads tidily."""
    def key(k: str):
        s, t = k.split("|", 1)
        return (t.lower(), int(s) if s.isdigit() else 0, s)
    return sorted(keys, key=key)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def new_config() -> dict:
    return {"version": 2, "sets": {}, "combo": {}, "nudge": {}}


def load_config(path: Path) -> dict:
    if not path.exists():
        return new_config()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("sets", {})
    cfg.setdefault("combo", {})
    cfg.setdefault("nudge", {})

    # v1 kept the per-combination control and dilution in two parallel maps and
    # had no notion of excluded strains. Fold them into one entry per
    # combination so a config written before that existed still loads.
    old_ctrl = cfg.pop("combo_control", None) or {}
    old_dil = cfg.pop("dilution", None) or {}
    for combo, dil in old_dil.items():
        entry = cfg["combo"].setdefault(combo, {})
        entry.setdefault("dilution", dil)
        entry.setdefault("exclude", [])
        if combo in old_ctrl:
            entry.setdefault("control_col", old_ctrl[combo])

    # A migrated entry with no recorded control is incomplete; drop it so the
    # combination is asked about again rather than failing later on a KeyError.
    cfg["combo"] = {c: e for c, e in cfg["combo"].items()
                    if e.get("control_col") and e.get("dilution")}
    cfg["version"] = 2
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str | None = None) -> str:
    try:
        raw = input(prompt).strip()
    except EOFError:
        return default or ""
    return raw or (default or "")


def prompt_set(set_id: str, treatments: list[str], existing: dict | None) -> dict:
    """Name the eight columns for one set, and choose its control column."""
    if existing:
        shown = [s if s else "(empty)" for s in existing["strains"]]
        print(f"\n  Set {set_id} is already configured:")
        for i, s in enumerate(shown, 1):
            mark = "  <- control" if i == existing["control_col"] else ""
            print(f"      {i}. {s}{mark}")
        if _ask("    Keep this? [Y/n]: ", "y").lower().startswith("y"):
            return existing

    print(f"\n  === Set {set_id} ===")
    print(f"  Treatments in this set: {', '.join(treatments)}")
    print(f"  Name each of the {sq.N_COLS} columns, left to right.")
    print("  Press Enter (or type '-') if a column holds no strain.")
    strains: list[str | None] = []
    for i in range(1, sq.N_COLS + 1):
        raw = _ask(f"    Column {i}: ")
        strains.append(None if raw in ("", "-", "empty", "none") else raw)

    filled = [i for i, s in enumerate(strains, 1) if s]
    if not filled:
        print("    ! Every column was left empty; please name at least one.")
        return prompt_set(set_id, treatments, None)

    while True:
        opts = ", ".join(f"{i}={strains[i-1]}" for i in filled)
        raw = _ask(f"    Which column is the CONTROL? ({opts}) "
                   f"[default {filled[0]}]: ", str(filled[0]))
        try:
            ctrl = int(raw)
            if ctrl in filled:
                break
        except ValueError:
            pass
        print(f"      Enter one of: {filled}")

    # The control is NOT locked in here. It is re-offered per treatment-set
    # combination with this as the default, because whether a strain is a valid
    # positive control depends on the medium: the WT BY used here has an
    # inherent growth defect on respiring media (K-OAc), so those plates need a
    # different reference even though the same panel was spotted.
    return {"strains": strains, "control_col": ctrl}


def _parse_cols(raw: str, valid: list[int]) -> list[int] | None:
    """Parse '1' / '1,3' / '1 3' into column numbers, or None if invalid."""
    if not raw.strip():
        return []
    out = []
    for tok in re.split(r"[,\s]+", raw.strip()):
        try:
            v = int(tok)
        except ValueError:
            return None
        if v not in valid:
            return None
        out.append(v)
    return sorted(set(out))


def dilution_variability(combo_key: str, refs: list, opts: "sq.MeasureOptions",
                         cfg: dict, cache_dir: Path, control_col: int,
                         strains: list, exclude: list | None) -> dict:
    """Median across-replicate CV of relative growth, for each dilution choice.

    Costs nothing measurable: step 2 already measured every plate at all three
    row choices, so this is arithmetic on cached arrays. It normalizes the same
    way the real pipeline does -- per-plate control average, detection floor --
    so the number shown is the one the figures would actually have.

    Returns {choice: cv}, missing any choice that cannot be scored (a control at
    or below the noise floor).
    """
    ex = set(exclude or [])
    cc = control_col - 1
    out = {}
    for di, choice in enumerate(DILUTION_ORDER):
        rows = DILUTIONS[choice]
        rel = {}
        ok = True
        for ref in refs:
            try:
                pd_ = measure(ref, replace(opts, nudge=plate_nudges(cfg, ref),
                                           quant_rows=(rows[0] + 1, rows[1] + 1)),
                              cache_dir)
            except Exception:
                ok = False
                break
            ctrl = [pd_.net[r, cc] for r in rows
                    if not pd_.rim[r, cc]]
            if not ctrl:
                continue
            div = float(np.mean(ctrl))
            if div <= sq.MIN_CONTROL_GRAY:
                continue
            for r in rows:
                for j in range(sq.N_COLS):
                    if j == cc or not strains[j] or (j + 1) in ex:
                        continue
                    if pd_.rim[r, j]:
                        continue
                    rel.setdefault(strains[j], []).append(float(pd_.net[r, j]) / div)
        if not ok:
            continue
        cvs = [float(np.std(v) / np.mean(v))
               for v in rel.values() if len(v) >= 3 and np.mean(v) > 0]
        if cvs:
            out[choice] = float(np.median(cvs))
    return out


def format_dilution_advice(cv: dict) -> list:
    """Lines describing the variability of each dilution choice."""
    if not cv:
        return []
    best = min(cv, key=cv.get)
    lines = ["    Spread across replicates at each dilution "
             "(median CV of relative growth, lower is better):"]
    for i, choice in enumerate(DILUTION_ORDER, start=1):
        if choice in cv:
            mark = "   <-- least variable" if choice == best else ""
            lines.append(f"      {i} = {choice:<6} {cv[choice]:.2f}{mark}")
        else:
            lines.append(f"      {i} = {choice:<6}   -- (control at the noise "
                         f"floor; cannot be scored)")
    lines.append("    This measures consistency only. It cannot tell you whether a "
                 "row is")
    lines.append("    saturated, so judge growth from the picture as well.")
    return lines


def prompt_combo(combo: str, cfg_set: dict, plates: list[int],
                 existing: dict | None, show, advice: list | None = None) -> dict:
    """Everything decided while looking at one combination's plates: which
    strains to drop, which column is the positive control, and the dilution.

    `advice` is printed first -- before the "already configured" shortcut -- so a
    saved dilution choice can still be reconsidered against the measured spread.

    All three are asked here rather than once per set because all three depend
    on what the plates actually look like on THIS medium.
    """
    set_id, treatment = combo.split("|", 1)
    for line in (advice or []):
        print(line)

    strains = cfg_set["strains"]
    named = [i for i, s in enumerate(strains, 1) if s]

    if existing:
        ex = existing.get("exclude", [])
        dil = existing["dilution"]
        desc = (dil.get("choice") if dil["mode"] == "combo"
                else ", ".join(f"plate {k}: {v}" for k, v in dil["choices"].items()))
        print(f"    Already set -> control: {strains[existing['control_col']-1]}"
              f" (col {existing['control_col']}); "
              f"excluded: {', '.join(strains[c-1] for c in ex) if ex else 'none'}; "
              f"dilution: {desc}")
        if _ask("    Keep this? [Y/n]: ", "y").lower().startswith("y"):
            return existing

    show()

    # --- 1. exclusions --------------------------------------------------
    print(f"\n    Columns in set {set_id}:")
    for i in named:
        star = "   <- set default control" if i == cfg_set["control_col"] else ""
        print(f"       {i}. {strains[i-1]}{star}")
    while True:
        raw = _ask("    Exclude any strains from this experiment? "
                   "(e.g. 1 or 1,3 — Enter for none): ")
        ex = _parse_cols(raw, named)
        if ex is None:
            print(f"      Enter column numbers from {named}, or just press Enter.")
            continue
        remaining = [i for i in named if i not in ex]
        if len(remaining) < 2:
            print("      That would leave fewer than 2 strains; try again.")
            continue
        break
    if ex:
        print(f"      excluding: {', '.join(f'{i}={strains[i-1]}' for i in ex)}")

    # --- 2. positive control --------------------------------------------
    default_ctrl = cfg_set["control_col"]
    if default_ctrl not in remaining:
        default_ctrl = remaining[0]
        print(f"      (set default control was excluded; "
              f"suggesting {default_ctrl}={strains[default_ctrl-1]})")
    while True:
        opts = ", ".join(f"{i}={strains[i-1]}" for i in remaining)
        raw = _ask(f"    Which column is the POSITIVE CONTROL? ({opts}) "
                   f"[default {default_ctrl}]: ", str(default_ctrl))
        try:
            ctrl = int(raw)
            if ctrl in remaining:
                break
        except ValueError:
            pass
        print(f"      Enter one of: {remaining}")
    print(f"      control: {strains[ctrl-1]} (everything is normalized to this)")

    # --- 3. dilution -----------------------------------------------------
    print(f"    Dilutions:  1 = least dilute (rows 1 & 4)")
    print(f"                2 = middle       (rows 2 & 5)")
    print(f"                3 = most dilute  (rows 3 & 6)")
    raw = _ask("    Choice for BOTH plates [1/2/3], or 'p' to set each plate "
               "separately: ", "3")
    if raw.lower().startswith("p"):
        choices = {}
        for pl in plates:
            while True:
                r = _ask(f"      Plate {pl} dilution [1/2/3]: ", "3")
                if r in ("1", "2", "3"):
                    choices[str(pl)] = DILUTION_ORDER[int(r) - 1]
                    break
                print("        Enter 1, 2 or 3.")
        dil = {"mode": "plate", "choices": choices}
    else:
        while raw not in ("1", "2", "3"):
            raw = _ask("      Enter 1, 2 or 3: ", "3")
        dil = {"mode": "combo", "choice": DILUTION_ORDER[int(raw) - 1]}

    return {"exclude": ex, "control_col": ctrl, "dilution": dil}


# ---------------------------------------------------------------------------
# Measurement (cached)
# ---------------------------------------------------------------------------

@dataclass
class PlateData:
    ref: PhotoRef
    net: np.ndarray            # N_ROWS x N_COLS
    rim: np.ndarray            # N_ROWS x N_COLS bool
    bg_mean: float
    spread: float
    centers: np.ndarray        # grid centres in ORIGINAL image pixels
    radius: float              # measuring radius in ORIGINAL image pixels
    bg_samples: np.ndarray     # the five individual agar reads
    quant_rows: tuple          # dilution rows the ROI was sized to fit ((), if none)
    plate_center: tuple        # agar disc centre, ORIGINAL image pixels (y, x)
    plate_radius: float        # agar disc radius, ORIGINAL image pixels


def _cache_key(path: Path, opts: sq.MeasureOptions) -> str:
    st = path.stat()
    # The trailing version tag invalidates the cache whenever the measurement or
    # flagging logic changes. Bump it, or stale .npz files keep serving numbers
    # (and artifact flags) produced by code that no longer exists.
    #   v3: artifact flag became positional only -- dim spots are no longer
    #       excluded, so every cached rim_flag from v2 is wrong.
    #   v21: grid base selection no longer applies the centring cap as a hard
    #       filter before ranking -- it broke on 9.2GLU, where the correct grid
    #       matched all 14 detected spots but sat at exactly 0.60 pitches and
    #       was discarded, putting every row one position low. Six of the
    #       twelve set 9/10 plates were affected.
    #   v20: the transition-width correction is capped, so diffuse (undergrown)
    #       spot edges are no longer over-corrected inward.
    #   v19: the measuring ROI is shrunk to fit inside the smallest resolved spot
    #       in the scored rows, so it depends on the dilution choice.
    #   v18: ROIs whose placement cannot be verified against their own outline are
    #       flagged, when the spot has real signal.
    #   v17: centring uses brightness first and texture as a rescue, so faint
    #       rim-adjacent spots are no longer parked on the rim.
    #   v16: wider centring search (0.30 pitch) so genuinely displaced corner
    #       spots can be reached; patch enlarged so the window is fully covered.
    #   v15: full-res centring is restricted to agar (masked correlation), so
    #       corner ROIs are no longer pulled by meniscus glare or the black table.
    #   v14: per-spot centring corrections are accepted on evidence, not on
    #       agreement with an affine lattice model.
    #   v13: measuring ROI is sized from the measured growth footprint.
    #   v12: the full-res centring disc is now sized from the measured spot,
    #       not from the (smaller) measuring ROI, and runs in two passes.
    #   v11: ROI centres are now refined at FULL resolution
    #       (sq.refine_centers_fullres). Detection ran at a 4x downscale, so
    #       every cached centre is off by ~20px -- a quarter of the ROI radius.
    #   v10: the spot diameter that sizes the paraboloid is now measured at full
    #       resolution (sq.measure_spot_diameter). ball_radius=None does not
    #       encode the DERIVED radius, so entries written under the old
    #       estimator would otherwise still be served.
    #   v9: FORMAT change -- plate_center/plate_radius are now stored, so the
    #       montage can mask the plate rim out of the corners of each block.
    #   v4: FORMAT change -- the thumbnail is gone and `centers`/`radius` are now
    #       in original-image pixels, not thumbnail pixels. Reading a v3 file
    #       under v4 code would silently scale every ROI by ~0.1.
    #   v5: grid detection became tilt-aware, and the column window may now start
    #       before the first DETECTED spot -- three plates had every strain read
    #       one column across, so their cached geometry is wrong.
    #   v6: the rim flag now needs evidence of actual glare inside the ROI, not
    #       just proximity to the edge, so every cached rim_flag is stale.
    #   v7: FORMAT change -- the five individual background reads are stored, so
    #       the usable-control cutoff can use a robust sigma instead of the
    #       outlier-driven max-min range.
    #   v8: background subtraction now runs through FIJI itself by default, and
    #       it flattens better than the local paraboloid (spread 0.08 vs 0.32),
    #       so every cached measurement differs.
    nudges = sorted((k, tuple(v)) for k, v in (opts.nudge or {}).items())
    sig = (f"{path.name}|{st.st_size}|{int(st.st_mtime)}|{opts.bg_mode}|"
           f"{opts.ball_radius}|{opts.bg_iters}|{opts.rgb_mode}|{nudges}|v22")
    return hashlib.sha1(sig.encode()).hexdigest()[:16]


def quant_rows_for(cfg: dict, combo_key: str, ref: "PhotoRef") -> tuple:
    """1-based dilution rows that will be scored on this plate, if already known.

    Each plate carries two replicates (rows 1-3 and 4-6) and one dilution is
    scored from each, so this returns the two rows the ROI must fit inside.
    Empty when the combination has not been configured yet -- step 2 then
    measures at full ROI size and step 3 re-measures once the choice is made.
    """
    entry = (cfg.get("combo") or {}).get(combo_key)
    if not entry or not entry.get("dilution"):
        return ()
    try:
        lo, hi = rows_for(entry["dilution"], ref.plate)
    except (KeyError, TypeError):
        return ()
    return (lo + 1, hi + 1)


def plate_nudges(cfg: dict, ref: "PhotoRef") -> dict:
    """Manual ROI shifts for one plate, from the config.

    Stored as {"<filename>": {"row,col": [dy, dx]}} so it is obvious what was
    overridden and by how much. Detection places 48 ROIs per plate correctly in
    almost every case, but a faint spot near the rim can defeat every automatic
    estimator at once -- those are flagged, and this is how a human corrects one
    when they can see what the algorithm cannot. Shifts are full-resolution
    pixels, +dy down, +dx right.
    """
    entry = (cfg.get("nudge") or {}).get(ref.path.name)
    if not entry:
        return {}
    out = {}
    for key, val in entry.items():
        try:
            r, c = (int(t) for t in str(key).replace(" ", "").split(","))
            dy, dx = (float(v) for v in val)
        except (ValueError, TypeError):
            print(f"    (ignoring malformed nudge {key!r} for {ref.path.name})")
            continue
        out[(r, c)] = (dy, dx)
    return out


def _rowset_key(rows) -> str:
    return "r" + "_".join(str(int(v)) for v in rows) if rows else "rNONE"


ALL_ROW_SETS = [(lo + 1, hi + 1) for lo, hi in DILUTIONS.values()]


def measure(ref: PhotoRef, opts: sq.MeasureOptions, cache_dir: Path,
            debug: bool = False, precompute: bool = True) -> PlateData:
    """Measure one plate, returning the result for `opts.quant_rows`.

    Every dilution choice is measured and cached in the same pass. Detection --
    the iterated full-resolution centring -- is 41 s per plate and does not
    depend on which rows are scored; only the ROI radius does, and re-deriving
    that is under a second. Measured on 9.1GLU: all three choices in one pass
    took 53.0 s against 53.8 s for a single one. So the dilution can be changed
    at review time with no re-measure, which is the whole point.
    """
    cf = cache_dir / f"{_cache_key(ref.path, opts)}.npz"
    want = tuple(opts.quant_rows or ())
    if cf.exists():
        try:
            z = np.load(cf)
            key = _rowset_key(want)
            if f"{key}_net" in z:
                return PlateData(
                    ref, z[f"{key}_net"], z[f"{key}_rim"].astype(bool),
                    float(z[f"{key}_bg_mean"]), float(z[f"{key}_spread"]),
                    z["centers"], float(z[f"{key}_radius"]),
                    z[f"{key}_bg_samples"], want,
                    tuple(z["plate_center"]), float(z["plate_radius"]))
        except Exception:
            try:
                cf.unlink()                 # corrupt cache: just re-measure
            except OSError:
                pass

    row_sets = list(dict.fromkeys(ALL_ROW_SETS + ([want] if want else [])))
    if not precompute:
        row_sets = [want] if want else [ALL_ROW_SETS[1]]

    res = sq.analyze_image_multi(ref.path, opts, row_sets,
                                 label=ref.label, debug=debug)
    _, _, spread, _ = res["_shared"]

    # Only geometry and per-choice measurements are cached, at full resolution.
    # An earlier version cached a 620 px thumbnail and drew previews from it --
    # at that size a spot is ~20 px across, far too coarse to see whether a faint
    # spot carries sparse micro-colonies, which is the whole point of the
    # preview. Previews are rendered from the original file instead.
    store, out = {}, None
    for rows in row_sets:
        g, m = res[tuple(rows)]
        k = _rowset_key(tuple(rows))
        store[f"{k}_net"] = m.net
        store[f"{k}_rim"] = m.rim_flag
        store[f"{k}_bg_mean"] = m.bg_mean
        store[f"{k}_spread"] = spread
        store[f"{k}_radius"] = g.measure_radius
        store[f"{k}_bg_samples"] = np.asarray(m.bg_samples, dtype=float)
        d = PlateData(ref, m.net, m.rim_flag, m.bg_mean, spread,
                      g.centers, g.measure_radius,
                      np.asarray(m.bg_samples, dtype=float), tuple(rows),
                      tuple(g.plate_center), float(g.plate_radius))
        if tuple(rows) == want or out is None:
            out = d
            geom = g

    # Caching is purely an optimisation. A failure here (read-only folder, a
    # path over Windows' 260-character limit, a full disk) must never cost the
    # measurement itself -- that would throw away ~50 s of work per plate and,
    # worse, silently drop the plate from the analysis.
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cf, centers=geom.centers,
                            plate_center=np.asarray(geom.plate_center, float),
                            plate_radius=float(geom.plate_radius), **store)
    except Exception as e:
        if not measure._warned:
            print(f"\n    (note: measurements could not be cached -- {e}. "
                  f"Analysis continues, but re-runs will be slow.)")
            measure._warned = True
    return out


measure._warned = False


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def make_preview(plates: list[PlateData], combo: str, strains: list[str | None],
                 preview_dir: Path | None = None, preview_px: int = PREVIEW_PX,
                 rgb_mode: str = sq.DEFAULT_RGB_MODE):
    """Return a callable that pops up all plates of a combination side by side,
    with each row labelled by its dilution so the choice is made by looking at
    the plates rather than from memory.

    The image is re-read from the original file here rather than from a cached
    thumbnail, so the operator sees enough detail to tell a genuinely blank spot
    from one carrying sparse micro-colonies -- the distinction that decides
    which dilution is worth scoring.
    """
    def show():
        try:
            import matplotlib
            import matplotlib.pyplot as plt
            from skimage.transform import resize
        except Exception:
                return
        # Under a headless backend a window cannot be shown, so write the
        # preview to a file instead of emitting a warning and nothing useful.
        gui = matplotlib.get_backend().lower() not in {
            "agg", "pdf", "ps", "svg", "cairo", "template"}
        try:
            n = len(plates)
            # Sized so each plate gets ~preview_px across on screen at 100 dpi.
            side = max(4.0, preview_px / 150.0)
            fig, axes = plt.subplots(1, n, figsize=(side * n, side * 1.05))
            axes = np.atleast_1d(axes)
            for ax, pd_ in zip(axes, plates):
                img = sq.load_gray8(pd_.ref.path, rgb_mode=rgb_mode)
                s = min(1.0, preview_px / max(img.shape))
                if s < 1.0:
                    img = resize(img.astype(float),
                                 (round(img.shape[0] * s), round(img.shape[1] * s)),
                                 order=1, preserve_range=True,
                                 anti_aliasing=True).astype(np.uint8)
                cen, rad = pd_.centers * s, pd_.radius * s
                ax.imshow(img, cmap="gray")
                for i in range(sq.N_ROWS):
                    for j in range(sq.N_COLS):
                        cy, cx = cen[i, j]
                        col = "red" if pd_.rim[i, j] else "lime"
                        ax.add_patch(plt.Circle((cx, cy), rad, fill=False,
                                                color=col, lw=1.0))
                    tag = DILUTION_ORDER[i % 3]
                    ax.text(cen[i, 0][1] - rad * 2.3, cen[i, 0][0],
                            f"{i+1}\n{tag}", color="yellow", fontsize=9,
                            ha="center", va="center", fontweight="bold")
                ax.set_title(f"plate {pd_.ref.plate}   "
                             f"(rows 1-3 = reps 1+2, rows 4-6 = reps 3+4)",
                             fontsize=9)
                ax.axis("off")
            set_id, treatment = combo.split("|", 1)
            fig.suptitle(f"Set {set_id} — {treatment}", fontsize=13)
            fig.tight_layout()
            d = preview_dir or Path.cwd()
            d.mkdir(parents=True, exist_ok=True)
            png = d / f"preview_set{set_id}_{treatment.replace('/', '-')}.png"
            # Always written, GUI or not: the on-screen window cannot be zoomed
            # once the prompt takes over, and a borderline spot often needs a
            # closer look than the window gives.
            fig.savefig(png, dpi=150, bbox_inches="tight")
            print(f"    full-resolution preview: {png}")
            if gui:
                plt.show(block=False)
                plt.pause(0.6)
            else:
                plt.close(fig)
        except Exception as e:
            print(f"    (preview unavailable: {e})")
    return show


def close_preview() -> None:
    try:
        import matplotlib.pyplot as plt
        plt.close("all")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def rows_for(dil: dict, plate: int) -> tuple[int, int]:
    choice = (dil["choice"] if dil["mode"] == "combo"
              else dil["choices"][str(plate)])
    return DILUTIONS[choice]


def build_tidy(combo: str, plates: list[PlateData], strains: list[str | None],
               control_col: int, dil: dict,
               exclude: list[int] | None = None) -> pd.DataFrame:
    """One tidy frame for one treatment-set combination.

    `experiment` is what the downstream normalizer and the R script treat as one
    graph, so it carries both the set and the treatment: different sets hold
    different strain panels and must never share a control.

    Excluded strains are KEPT as rows, marked `excluded`, with no relative
    growth. Their raw measurements are still real data -- "the WT does not grow
    on K-OAc" is a result worth having in the file -- but they take no part in
    the normalization, summary, statistics or figures.
    """
    set_id, treatment = combo.split("|", 1)
    experiment = f"Set {set_id} {treatment}"
    drop = set(exclude or ())
    rows = []
    for pd_ in plates:
        r_lo, r_hi = rows_for(dil, pd_.ref.plate)
        for rep_idx, row in enumerate((r_lo, r_hi), start=1):
            # Plate 1 carries replicates 1+2, plate 2 carries 3+4.
            rep_no = (pd_.ref.plate - 1) * 2 + rep_idx
            for col in range(sq.N_COLS):
                name = strains[col]
                if not name:
                    continue          # column holds no strain
                rows.append({
                    "experiment": experiment, "treatment": treatment,
                    "set": set_id, "plate": pd_.ref.plate,
                    "image": pd_.ref.path.name,
                    "replicate": f"rep{rep_no}", "dilution_row": row + 1,
                    "dilution": DILUTION_ORDER[row % 3],
                    "strain_col": col + 1, "strain": name,
                    "raw_growth": float(pd_.net[row, col]),
                    "artifact": bool(pd_.rim[row, col]),
                    "excluded": (col + 1) in drop,
                    # Carried into the CSV so the R script normalizes against
                    # the control that was actually chosen, rather than
                    # guessing it from the column order.
                    "is_control": (col + 1) == control_col,
                })
    full = pd.DataFrame(rows)
    if full.empty:
        return full

    keep = full[~full["excluded"]].copy()
    if control_col in drop or keep.empty:
        print(f"    ! {experiment}: the control column was excluded; "
              f"skipping normalization for this combination.")
        return full

    # Whether a control can serve as a denominator depends on this plate's own
    # measurement noise, which we measured: the spread of its five agar reads.
    # A fixed gray cutoff cannot work across media whose whole signal range
    # differs by an order of magnitude (glucose ~28 gray, glycerol middle row
    # ~0.5-3), and using one nulled visibly-grown glycerol controls.
    noise = max([sq.bg_noise(p.bg_samples) for p in plates] or [0.0])
    min_control = max(sq.MIN_CONTROL_GRAY, sq.CONTROL_NOISE_MULT * noise)

    keep = sq.add_relative_growth(keep, control_col=control_col,
                                  group_keys=["experiment"],
                                  min_control=min_control)
    key = ["experiment", "replicate", "strain_col"]
    added = [c for c in keep.columns if c not in full.columns]
    return full.merge(keep[key + added], on=key, how="left")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, nargs="?", default=HERE / "Spotting Assays",
                    help="Folder of <set>.<plate><TREATMENT> images.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Where results go (default: <folder>/Results).")
    ap.add_argument("--config", type=Path, default=None,
                    help=f"Config file (default: <folder>/{CONFIG_NAME}).")
    ap.add_argument("--reask", action="store_true",
                    help="Re-ask every strain, control and dilution question.")
    ap.add_argument("--reask-combos", "--reask-dilutions", dest="reask_combos",
                    action="store_true",
                    help="Keep the per-set strain panels, but re-ask the "
                         "per-combination questions (exclusions, positive "
                         "control, dilution).")
    ap.add_argument("--no-graphs", action="store_true",
                    help="Skip the R figures; still writes all CSVs.")
    ap.add_argument("--no-precompute", action="store_true",
                    help="Measure only the configured dilution instead of all "
                         "three. Saves nothing on a first run (the shared "
                         "detection dominates) but avoids storing the extra "
                         "arrays; changing a dilution then costs a re-measure.")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Do not ask about the graphs/montages; draw both.")
    ap.add_argument("--keep-outliers", action="store_true",
                    help="Keep every replicate point. By default a single point "
                         "that dominates a strain's spread is flagged and left "
                         "out of the summary, stats and figures (it stays in the "
                         "tidy CSV, marked).")
    ap.add_argument("--no-pptx", action="store_true",
                    help="Skip the PowerPoint deck of montages + graphs.")
    ap.add_argument("--no-montages", action="store_true",
                    help="Skip the spot montages (the pictures of the plates).")
    ap.add_argument("--label", default="Replicant",
                    help="Row label on the montages (default 'Replicant').")
    ap.add_argument("--preview-size", type=int, default=PREVIEW_PX, metavar="PX",
                    help=f"Long edge of each plate in the dilution preview "
                         f"(default {PREVIEW_PX}). Larger shows finer colonies.")
    ap.add_argument("--ball-radius", type=float, default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    folder: Path = args.folder
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2
    outdir = args.out or folder / "Results"
    cfg_path = args.config or folder / CONFIG_NAME

    combos, skipped = discover(folder)
    if not combos:
        print(f"No images named <set>.<plate><TREATMENT> found in {folder}",
              file=sys.stderr)
        return 2
    if skipped:
        print(f"! Ignored {len(skipped)} file(s) whose names did not parse: "
              f"{', '.join(skipped[:6])}{' ...' if len(skipped) > 6 else ''}")

    keys = sort_combos(combos)
    sets = sorted({combos[k][0].set_id for k in keys},
                  key=lambda s: int(s) if s.isdigit() else 0)
    treatments = sorted({combos[k][0].treatment for k in keys})
    print(f"\nFound {sum(len(v) for v in combos.values())} photo(s): "
          f"{len(keys)} treatment-set combination(s), "
          f"{len(sets)} set(s), treatments: {', '.join(treatments)}")
    for k in keys:
        pl = ", ".join(f"plate {r.plate}" for r in combos[k])
        s, t = k.split("|", 1)
        print(f"    Set {s:>2} {t:<8} : {pl}")

    cfg = new_config() if args.reask else load_config(cfg_path)
    if args.reask_combos:
        cfg["combo"] = {}

    # --- step 2: strains + control, per set --------------------------------
    print("\n" + "=" * 62)
    print("STEP 1 of 3 — strains and control column, per set")
    print("=" * 62)
    for s in sets:
        tr = sorted({combos[k][0].treatment for k in keys
                     if combos[k][0].set_id == s})
        cfg["sets"][s] = prompt_set(s, tr, cfg["sets"].get(s))
        save_config(cfg_path, cfg)

    # --- step 3: measure everything ----------------------------------------
    opts = sq.MeasureOptions(ball_radius=args.ball_radius)
    cache_dir = folder / CACHE_DIR
    total = sum(len(v) for v in combos.values())
    print("\n" + "=" * 62)
    print(f"STEP 2 of 3 — measuring {total} plate(s)")
    print("=" * 62)
    print("  (first run is slow; results are cached so re-runs are instant)")
    data: dict[str, list[PlateData]] = {}
    done = 0
    for k in keys:
        lst = []
        for ref in combos[k]:
            done += 1
            print(f"  [{done}/{total}] {ref.path.name} ", end="", flush=True)
            try:
                pdta = measure(ref, replace(opts,
                                            nudge=plate_nudges(cfg, ref),
                                            quant_rows=quant_rows_for(cfg, k, ref)),
                               cache_dir, debug=args.debug,
                               precompute=not args.no_precompute)
            except Exception as e:
                print(f"FAILED: {e}")
                continue
            flags = int(pdta.rim.sum())
            print(f"bg={pdta.bg_mean:.1f} spread={pdta.spread:.2f}"
                  + (f" [{flags} artifact ROI(s)]" if flags else ""))
            lst.append(pdta)
        if lst:
            data[k] = lst

    # --- step 4: dilution per combination ----------------------------------
    print("\n" + "=" * 62)
    print("STEP 3 of 3 — choose the dilution to score, per combination")
    print("=" * 62)
    frames = []
    for k in keys:
        if k not in data:
            continue
        s, t = k.split("|", 1)
        cfg_set = cfg["sets"][s]
        print(f"\n  --- Set {s} on {t} "
              f"({len(data[k])} plate(s), {2*len(data[k])} replicates) ---")

        show = make_preview(data[k], k, cfg_set["strains"],
                            preview_dir=outdir / "previews",
                            preview_px=args.preview_size,
                            rgb_mode=opts.rgb_mode)
        prev = cfg["combo"].get(k) or {}
        adv = []
        try:
            cv = dilution_variability(
                k, [d.ref for d in data[k]], opts, cfg, cache_dir,
                prev.get("control_col") or cfg_set.get("control_col") or 1,
                cfg_set["strains"], prev.get("exclude"))
            adv = format_dilution_advice(cv)
        except Exception as e:
            if args.debug:
                print(f"    (dilution advice unavailable: {e})")
        entry = prompt_combo(k, cfg_set, [d.ref.plate for d in data[k]],
                             cfg["combo"].get(k), show, advice=adv)
        cfg["combo"][k] = entry
        close_preview()
        save_config(cfg_path, cfg)

        # The ROI is sized to fit inside the spots of the rows actually scored,
        # so the plates must be measured with that choice known. Step 2 used the
        # previously configured dilution; if this run changed it, re-measure now.
        plates = []
        for pdta in data[k]:
            want = quant_rows_for(cfg, k, pdta.ref)
            if want and want != pdta.quant_rows:
                print(f"    switching {pdta.ref.path.name} to rows {list(want)} ...",
                      end="", flush=True)
                try:
                    pdta = measure(pdta.ref,
                                   replace(opts, nudge=plate_nudges(cfg, pdta.ref),
                                           quant_rows=want),
                                   cache_dir, debug=args.debug,
                                   precompute=not args.no_precompute)
                    print(" done")
                except Exception as e:
                    print(f" FAILED: {e}")
            plates.append(pdta)
        data[k] = plates

        # Report the ROI actually used. One size per plate, applied to all 48
        # spots on it; it is the largest circle that still fits inside every
        # spot of the scored rows, so it varies between plates and with the
        # dilution chosen.
        for pdta in plates:
            rows = ", ".join(str(r) for r in pdta.quant_rows) or "n/a"
            flagged = int(pdta.rim.sum())
            print(f"    ROI  plate {pdta.ref.plate}: {2 * pdta.radius:.0f} px diameter "
                  f"-- sized from scored rows {rows}, applied to all 48 spots"
                  + (f"; {flagged} flagged" if flagged else ""))

        frames.append(build_tidy(k, plates, cfg_set["strains"],
                                 entry["control_col"], entry["dilution"],
                                 entry.get("exclude")))

    tidy = pd.concat([f for f in frames if not f.empty], ignore_index=True) \
        if frames else pd.DataFrame()
    if tidy.empty:
        print("\nNothing to export.", file=sys.stderr)
        return 1

    # --- step 5: export -----------------------------------------------------
    outdir.mkdir(parents=True, exist_ok=True)
    # The R script keys its figures on `treatment`; each set-treatment pair is
    # its own experiment here, so that column carries the combined label.
    out = tidy.copy()
    out["treatment_medium"] = out["treatment"]
    out["treatment"] = out["experiment"]

    # Flag outliers BEFORE writing the CSV. The R script reads that file, so a
    # column added afterwards reaches neither the figures nor the stats -- the
    # flagging silently did nothing but change the summary table.
    if not args.keep_outliers:
        out = sq.flag_outliers(out, group_keys=["experiment", "strain"])
    else:
        out["outlier"] = False

    norm_path = outdir / "spotting_results_normalized.csv"
    out.to_csv(norm_path, index=False, encoding="utf-8-sig")

    clean = out[~out["artifact"] & ~out["excluded"] & ~out["outlier"]]
    summary = (clean.groupby(["experiment", "strain"], sort=False)["relative_growth"]
               .agg(["mean", "std", "count"]).reset_index())
    summary.to_csv(outdir / "spotting_results_summary.csv", index=False,
                   encoding="utf-8-sig")

    n_flag = int(out["artifact"].sum())
    n_excl = int(out["excluded"].sum())
    print(f"\nWrote:\n  {norm_path}\n  {outdir / 'spotting_results_summary.csv'}")
    if n_flag:
        print(f"  ({n_flag} artifact-flagged spot(s) kept in the tidy file but "
              f"excluded from the summary, stats and figures.)")
    if n_excl:
        dropped = (out.loc[out["excluded"], ["experiment", "strain"]]
                   .drop_duplicates().groupby("experiment")["strain"]
                   .apply(lambda s: ", ".join(sorted(s))))
        print(f"  ({n_excl} spot(s) from strains you excluded are kept in the "
              f"tidy file with no relative growth:)")
        for exp, strains_ in dropped.items():
            print(f"      {exp}: {strains_}")
    print(f"  config: {cfg_path}")

    # The R graphs take about a minute, so they always run (--no-graphs
    # skips them). The montages are the slow step -- they re-run the FIJI
    # background subtraction on every original 24 MP photo -- so only those
    # are asked about. CSVs and graphs are on disk by then, so saying no
    # costs only the spot pictures.
    n_plates = sum(len(v) for v in data.values())
    if not args.no_graphs:
        run_r(norm_path, outdir)

    if args.no_montages:
        want_mont = False
    elif args.yes:
        want_mont = True
    else:
        mins = max(1, round(n_plates * 25 / 60))
        want_mont = _ask(f"  Draw the spot montages (~{mins} min for "
                         f"{n_plates} plates)? [Y/n]: ", "y").lower().startswith("y")
    if want_mont:
        make_montages(data, cfg, opts, outdir / "montages", args.label)
    elif not args.no_montages:
        print("  (montages skipped -- run  python spotting_montage.py  later "
              "to draw them)")

    # Assemble the deck last: it only collects PNGs the two steps above wrote,
    # so it costs a second and needs no measurement.
    write_condition_matrix(outdir)

    if not args.no_pptx:
        make_pptx(outdir)
    return 0


CONDITION_ORDER = ["GLU", "GLY", "K-OAc"]


def _stars(p) -> str:
    """Significance marks, matching R/plot_spotting.R exactly."""
    if p is None or not np.isfinite(p):
        return ""
    if p < 1e-4:
        return "****"
    if p < 1e-3:
        return "***"
    if p < 1e-2:
        return "**"
    if p < 0.05:
        return "*"
    return "ns"


def write_condition_matrix(outdir: Path) -> None:
    """Every strain's growth relative to its control, across the conditions.

    One row per (set, strain), one block of columns per medium: the geometric
    mean ratio, its Holm-adjusted p, n, and a display cell combining the ratio
    with the significance mark ("0.77 *").

    Built from the R script's own output rather than recomputed here. The
    figures come from that file, so a strain can never be starred in the table
    and unstarred in the plot -- and spotting_quant's Python t-test is NOT the
    same test (raw-scale, uncensored), so recomputing would have produced a
    second, quietly different set of p-values.
    """
    src = outdir / "figures" / "spotting_paired_ttests.csv"
    if not src.exists():
        return                      # no R run, nothing to summarise -- stay silent

    try:
        t = pd.read_csv(src)
        if t.empty:
            return
        parts = t["treatment"].astype(str).str.extract(r"^Set\s+(\S+)\s+(.*)$")
        t["set"] = parts[0]
        t["medium"] = parts[1]

        rows = {}
        controls = {}
        for _, r in t.iterrows():
            key = (r["set"], r["group2"])
            rows.setdefault(key, {})[r["medium"]] = r
            controls[(r["set"], r["medium"])] = r["group1"]
            # the control itself: 1.00 by definition, no test against itself
            ck = (r["set"], r["group1"])
            rows.setdefault(ck, {}).setdefault(r["medium"], None)

        media = [m for m in CONDITION_ORDER if m in set(t["medium"])]
        media += sorted(set(t["medium"]) - set(media))

        def set_key(k):
            try:
                return (0, int(k[0]), str(k[1]))
            except ValueError:
                return (1, 0, str(k[1]))

        out = []
        for (set_id, strain) in sorted(rows, key=set_key):
            is_ctrl = any(controls.get((set_id, m)) == strain for m in media)
            rec = {"set": set_id, "strain": strain,
                   "is_control": is_ctrl,
                   "control": next((controls[(set_id, m)] for m in media
                                    if (set_id, m) in controls), "")}
            for m in media:
                r = rows[(set_id, strain)].get(m)
                if is_ctrl:
                    rec[m] = "1.00"
                    rec[f"{m} ratio"] = 1.0
                    rec[f"{m} p_adj"] = np.nan
                    rec[f"{m} n"] = np.nan
                elif r is None:
                    rec[m] = ""
                    rec[f"{m} ratio"] = np.nan
                    rec[f"{m} p_adj"] = np.nan
                    rec[f"{m} n"] = np.nan
                else:
                    mark = _stars(r["p_adj"])
                    rec[m] = (f"{r['mean_ratio']:.2f}"
                              + (f" {mark}" if mark and mark != "ns" else ""))
                    rec[f"{m} ratio"] = float(r["mean_ratio"])
                    rec[f"{m} p_adj"] = float(r["p_adj"])
                    rec[f"{m} n"] = int(r["n"])
            out.append(rec)

        df = pd.DataFrame(out)
        csv_path = outdir / "spotting_relative_growth_by_condition.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        made = [csv_path]
        try:
            xlsx = outdir / "spotting_relative_growth_by_condition.xlsx"
            df.to_excel(xlsx, index=False)
            made.append(xlsx)
        except Exception:
            pass                    # openpyxl missing: the CSV is enough
        for m in made:
            print(f"  {m}")
    except Exception as e:
        print(f"  (condition summary skipped: {e})")

def make_pptx(outdir: Path) -> None:
    """Collect the montages and graphs into a PowerPoint deck.

    One slide per combination in the layout Darren supplied: a blank widescreen
    slide, montage left, graph right, no text. Purely an assembly step -- if a
    combination is missing either picture it is skipped and said so.
    """
    try:
        import spotting_pptx as sp
    except Exception as e:                       # pragma: no cover
        print("\n  (deck skipped -- could not import spotting_pptx: "
              f"{e})")
        return
    try:
        pairs = sp.find_pairs(outdir)
        if not pairs:
            print("  (no slide has both a montage and a graph -- deck skipped)")
            return
        print("\nBuilding the PowerPoint deck ...")
        out = sp.build(pairs, outdir / "spotting_figures.pptx")
        print(f"  wrote {out}  ({len(pairs)} slide(s))")
    except Exception as e:
        print(f"  ! deck failed: {e}")


def make_montages(data: dict, cfg: dict, opts: "sq.MeasureOptions",
                  outdir: Path, label: str) -> None:
    """Draw the spot montages -- the picture of the plates themselves.

    These used to live only in spotting_montage.py and had to be run by hand,
    which meant a finished run silently left the previous run's montages in
    place, looking current. Same trap as the Rscript working-directory bug.
    A failure here must not lose the run: the CSVs and figures are already
    written by this point.
    """
    try:
        import spotting_montage as sm
    except Exception as e:                       # pragma: no cover
        print("\n  (montages skipped -- spotting_montage did not import: "
              f"{e})")
        return

    print("\nDrawing spot montages ...")
    made = 0
    for k, plates in data.items():
        set_id, treatment = k.split("|", 1)
        entry = cfg.get("sets", {}).get(set_id)
        if not entry:
            continue
        safe = f"montage_set{set_id}_{treatment.replace('/', '-')}.png"
        try:
            sm.build_montage(k, plates, entry["strains"], opts,
                             outdir / safe, rep_label=label)
            made += 1
        except Exception as e:
            print(f"  ! montage failed for set {set_id} {treatment}: {e}")
    if made:
        print(f"  wrote {made} montage(s) to {outdir}")


def run_r(csv_path: Path, outdir: Path) -> None:
    """Draw the figures with R/plot_spotting.R if R is available."""
    import shutil
    import subprocess

    # Absolute paths, always. Rscript does not necessarily start in the working
    # directory it was launched from -- an Rprofile that calls setwd() moves it
    # -- and a relative path then resolves against somewhere else entirely. That
    # happened here: R reported the CSV missing from 'C:/Users/darre/Workplace'
    # and silently drew no figures, leaving the previous run's figures in place
    # and looking current.
    csv_path = Path(csv_path).resolve()
    outdir = Path(outdir).resolve()

    rscript = shutil.which("Rscript")
    if not rscript:
        cands = sorted(Path("C:/Program Files/R").glob("R-*/bin/Rscript.exe"),
                       reverse=True) if Path("C:/Program Files/R").exists() else []
        rscript = str(cands[0]) if cands else None
    script = HERE / "R" / "plot_spotting.R"
    if not rscript or not script.exists():
        print("\n  (R not found — CSVs written; run R/plot_spotting.R yourself "
              "to draw the figures.)")
        return

    print(f"\nDrawing figures with {rscript} ...")
    try:
        # R prints UTF-8; without saying so the strain names come back as
        # mojibake on a cp1252 console.
        r = subprocess.run([rscript, str(script), str(csv_path),
                            "--outdir", str(outdir / "figures")],
                           capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace")
        tail = [ln for ln in (r.stdout or "").splitlines()
                if ln.strip() and "S3 guide" not in ln]
        for ln in tail[-25:]:
            print("  " + ln)
        if r.returncode != 0:
            print("  ! R exited non-zero; stderr tail:")
            for ln in (r.stderr or "").splitlines()[-12:]:
                print("    " + ln)
    except Exception as e:
        print(f"  ! could not run R: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
