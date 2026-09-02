#!/usr/bin/env python3
"""
spotting_timecourse.py -- pick the best photo set out of a time course.

A DIFFERENT pipeline from spotting_batch.py. That one takes a flat folder of
photos already chosen by eye and named  set.plateTREATMENT.JPG . This one takes
the raw capture tree, where the condition is carried by the folder names and the
same plate has been photographed at several timepoints (sometimes more than once
per timepoint), and works out which of those photos to quantify.

Expected layout:

    root/16 Hours/Glucose/Plate 1 (Rep 1+2)/*.jpg
    root/16 Hours/Glucose/Plate 2 (Rep 3+4)/*.jpg
    root/40 Hours/Glycerol/Plate 1 (Rep 1+2)/*.jpg

For every medium it tries every candidate: each timepoint, each pairing of a
plate-1 photo with a plate-2 photo, and each of the three dilution row choices.
Every candidate is scored and written out; the least variable one is proposed.

WHAT IT RANKS ON, AND WHY NOT SIGNIFICANCE

Ranking is by the spread across replicates -- the median coefficient of
variation of relative growth. That is a data-quality criterion: it does not know
which direction any effect runs, so preferring a low-variance candidate cannot
manufacture one.

The count of significant strains is reported for every candidate but is NOT used
to choose. Picking the pairing that yields the smallest p-values is selecting on
the outcome: with a dozen candidates per condition some will look significant by
chance, and the p-value printed beside them would no longer mean what it says.
The --rank-by significance switch exists because you may want to look, but it
warns, and results chosen that way must not be reported as if the candidate had
been fixed in advance.
"""

from __future__ import annotations

import argparse
import itertools
import json
import multiprocessing
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# The repository root: source lives in src/, but the photo folder and
# everything a run writes live beside it, not inside it.
PROJECT_ROOT = HERE.parent
import spotting_quant as sq        # noqa: E402
import spotting_batch as sb        # noqa: E402

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
CONFIG_NAME = "timecourse_config.json"
ROW_SETS = [(0, 3), (1, 4), (2, 5)]
ROW_NAMES = ["least", "middle", "most"]

MEDIUM_CODES = {
    "glucose": "GLU",
    "glycerol": "GLY",
    "potassium acetate": "K-OAc",
    "k-oac": "K-OAc", "koac": "K-OAc",
}


@dataclass(frozen=True)
class Shot:
    path: Path
    tp_hours: float
    tp_label: str
    medium: str
    medium_label: str
    plate: int


def _medium_code(name: str) -> str:
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key in MEDIUM_CODES:
        return MEDIUM_CODES[key]
    return re.sub(r"[^A-Za-z0-9-]+", "", name.strip()).upper()[:8] or "MEDIUM"


def _hours(name: str):
    m = re.search(r"(\d+(?:\.\d+)?)\s*h", name.strip(), re.I)
    if m:
        return float(m.group(1))
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*$", name)
    return float(m.group(1)) if m else None


def _plate_no(name: str):
    m = re.search(r"plate\s*(\d+)", name, re.I)
    return int(m.group(1)) if m else None


def discover(root: Path):
    """Walk timepoint / medium / plate and collect the photos.

    Returns (shots, complaints). Anything that does not fit the layout is
    reported rather than dropped silently: a mis-named folder would otherwise
    just vanish from the analysis without anyone noticing.
    """
    shots, bad = [], []
    for tp_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if tp_dir.name.startswith("."):
            continue
        hours = _hours(tp_dir.name)
        if hours is None:
            bad.append(f"{tp_dir.name}: no timepoint in the folder name")
            continue
        for med_dir in sorted(p for p in tp_dir.iterdir() if p.is_dir()):
            code = _medium_code(med_dir.name)
            for plate_dir in sorted(p for p in med_dir.iterdir() if p.is_dir()):
                pl = _plate_no(plate_dir.name)
                where = f"{tp_dir.name}/{med_dir.name}/{plate_dir.name}"
                if pl is None:
                    bad.append(f"{where}: no plate number in the folder name")
                    continue
                imgs = sorted(f for f in plate_dir.iterdir()
                              if f.suffix.lower() in IMAGE_EXT)
                if not imgs:
                    bad.append(f"{where}: no images")
                for f in imgs:
                    shots.append(Shot(f, hours, tp_dir.name, code,
                                      med_dir.name, pl))
    return shots, bad


def candidates(shots):
    """Every (medium, timepoint, plate-1 photo, plate-2 photo) worth scoring.

    Technical replicates multiply out: two photos of plate 1 and two of plate 2
    at one timepoint give four pairings, each a legitimate way to assemble the
    four biological replicates.
    """
    out = []
    by_key = {}
    for s in shots:
        by_key.setdefault((s.medium, s.tp_label, s.tp_hours), {}) \
              .setdefault(s.plate, []).append(s)
    for (medium, tp_label, hours), plates in sorted(
            by_key.items(), key=lambda kv: (kv[0][0], kv[0][2])):
        p1, p2 = plates.get(1, []), plates.get(2, [])
        if not p1 or not p2:
            continue
        for a, b in itertools.product(p1, p2):
            out.append({"medium": medium, "medium_label": a.medium_label,
                        "tp_label": tp_label, "hours": hours,
                        "plate1": a, "plate2": b})
    return out


def _init_worker():
    """Keep each worker single-threaded.

    Measured the hard way: 15 measurements on 6 workers took 41 minutes against
    a 3 minute estimate. Every worker runs numpy -- whose BLAS opens its own
    thread pool -- and also spawns a headless FIJI JVM, so the pool was
    competing for several times more threads than there are cores and thrashing
    memory bandwidth. Pinning each worker to one BLAS thread lets the pool scale
    on processes, which is the axis that actually helps here.
    """
    import os
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
        os.environ[var] = "1"


_CLOUD_MASK = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


def _is_cloud_stub(path: Path) -> bool:
    """True when path is a OneDrive Files-On-Demand stub not yet on this disk."""
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & _CLOUD_MASK)
    except OSError:
        return False


def _measure_one(job):
    """Worker: measure one photo at one dilution choice.

    Module level so a process pool can pickle it -- Windows has no fork, so a
    closure or a lambda would not survive the trip to the child process.

    If the photo is a OneDrive cloud stub, it is copied to a local temp file
    before measurement so that a concurrent OneDrive re-sync cannot change the
    file underneath a read. The temp file is deleted immediately after; re-runs
    hit the cache and never touch the photo at all.
    """
    path, rows, cache_dir = job
    tmp: "Path | None" = None
    try:
        ref = sb.PhotoRef(Path(path), 0, 1, "TC")
        opts = replace(sq.MeasureOptions(), quant_rows=tuple(rows))
        cache_dir_path = Path(cache_dir)

        # If the result is already cached, the photo never needs to be read —
        # skip the download entirely.
        cf = cache_dir_path / f"{sb._cache_key(ref.path, opts)}.npz"
        read_path = None
        if not cf.exists() and _is_cloud_stub(ref.path):
            fd, tmp_str = tempfile.mkstemp(suffix=ref.path.suffix)
            os.close(fd)
            tmp = Path(tmp_str)
            shutil.copy2(str(ref.path), str(tmp))
            read_path = tmp

        sb.measure(ref, opts, cache_dir_path, read_path=read_path)
        return (str(path), tuple(rows), None)
    except Exception as e:
        return (str(path), tuple(rows), f"{type(e).__name__}: {e}")
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def measure_all(jobs, cache_dir: Path, workers: int):
    """Measure every (photo, dilution) pair, in parallel.

    Safe to parallelise: each measurement is independent and writes its own
    cache entry, keyed on the photo AND the row choice, so two workers never
    touch the same file. Processes rather than threads -- detect_grid is numpy
    work, and every call also spawns its own headless FIJI.

    Worker count is kept modest by default because each one carries a JVM and a
    24 MP image; oversubscribing trades throughput for paging.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    errors = []
    total = len(jobs)
    if workers <= 1:
        for i, j in enumerate(jobs, 1):
            _, _, err = _measure_one(j)
            if err:
                errors.append((j[0], err))
            print(f"\r    {i}/{total}", end="", flush=True)
        print()
        return errors

    with ProcessPoolExecutor(max_workers=workers,
                             initializer=_init_worker) as pool:
        futs = [pool.submit(_measure_one, j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            path, rows, err = fut.result()
            if err:
                errors.append((path, err))
            print(f"\r    {i}/{total}  ({workers} workers)", end="", flush=True)
    print()
    return errors


def score_candidate(cand, rows, cache_dir: Path, strains, control_col,
                    exclude=None, p_thresh: float = 0.05):
    """Metrics for one (photo pair, dilution).

    Normalizes exactly as the main pipeline does -- per-plate control average,
    rim-flagged spots dropped, detection floor -- so the CV reported here is the
    CV the real figure would have, not an approximation of it.
    """
    from scipy import stats

    ex = set(exclude or [])
    cc = control_col - 1
    rel, ctrl_vals = {}, []
    for shot in (cand["plate1"], cand["plate2"]):
        ref = sb.PhotoRef(shot.path, 0, shot.plate, "TC")
        opts = replace(sq.MeasureOptions(),
                       quant_rows=tuple(r + 1 for r in rows))
        try:
            pd_ = sb.measure(ref, opts, cache_dir)
        except Exception:
            return None
        good = [pd_.net[r, cc] for r in rows if not pd_.rim[r, cc]]
        if not good:
            return None
        div = float(np.mean(good))
        if div <= sq.MIN_CONTROL_GRAY:
            return None
        ctrl_vals.extend(good)
        for r in rows:
            for j in range(sq.N_COLS):
                if j == cc or not strains[j] or (j + 1) in ex or pd_.rim[r, j]:
                    continue
                rel.setdefault(strains[j], []).append(float(pd_.net[r, j]) / div)

    if not ctrl_vals:
        return None
    cm = float(np.mean(ctrl_vals))
    floor = sq.MIN_CONTROL_GRAY / max(cm, 1e-9)
    cvs, n_sig, n_str = [], 0, 0
    for v in rel.values():
        v = np.asarray(v, float)
        if len(v) < 3 or v.mean() <= 0:
            continue
        n_str += 1
        cvs.append(float(v.std() / v.mean()))
        w = np.maximum(v, floor)
        if np.all(w > 0) and float(
                stats.ttest_1samp(np.log(w), 0).pvalue) < p_thresh:
            n_sig += 1
    if not cvs:
        return None
    return {"median_CV": float(np.median(cvs)),
            "control_mean": cm,
            "control_CV": float(np.std(ctrl_vals) / cm) if cm > 0 else np.nan,
            "n_strains": n_str, "n_significant": n_sig}


def set_id_from_name(name: str):
    """Set number out of a folder name like Set09, Set 9, set-10.

    Returned as the string the main config keys on, so Set09 finds sets["9"].
    """
    m = re.search(r"set[\s_-]*0*(\d+)", name, re.I)
    return str(int(m.group(1))) if m else None


def strains_from_main_config(set_id, main_cfg: Path):
    """The strain panel for one set, out of the main pipeline's config.

    The two pipelines keep separate config FILES on purpose -- the dilution
    choice means opposite things in each (a saved decision there, an output
    here) -- but the strain panel is the same experiment either way, so it is
    read from one place rather than retyped and allowed to drift.
    """
    if not main_cfg or not Path(main_cfg).exists():
        return None
    try:
        cfg = json.loads(Path(main_cfg).read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = (cfg.get("sets") or {}).get(str(set_id))
    if not entry or not entry.get("strains"):
        return None
    return {"strains": entry["strains"],
            "control_col": int(entry.get("control_col") or 1),
            "exclude": [], "from_set": str(set_id),
            "from_config": str(main_cfg)}


def load_or_ask_config(root: Path, set_id=None, main_cfg: Path = None):
    """Strain names and control column for this capture tree.

    Order of preference: a config already saved here, then the main pipeline's
    entry for the set the folder is named after (Set09 -> sets["9"]), then ask.
    """
    path = root / CONFIG_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    sid = set_id or set_id_from_name(root.name)
    if sid:
        main_cfg = main_cfg or (PROJECT_ROOT / "Spotting Assays" / "spotting_config.json")
        got = strains_from_main_config(sid, main_cfg)
        if got:
            named = [x for x in got["strains"] if x]
            print(f"\n  Folder is named for set {sid}; taking that panel "
                  f"from {Path(main_cfg).name}:")
            print(f"    {', '.join(named)}")
            print(f"    control: {got['strains'][got['control_col']-1]} "
                  f"(column {got['control_col']})")
            if sb._ask("    Use this? [Y/n]: ", "y").lower().startswith("y"):
                path.write_text(json.dumps(got, indent=2, ensure_ascii=False),
                                encoding="utf-8")
                print(f"    saved {path}")
                return got
        else:
            print(f"\n  Folder is named for set {sid}, but "
                  f"{Path(main_cfg).name} has no strains for it.")
    print("\n  Name the eight columns (blank or - for an empty column):")
    strains = []
    for i in range(1, sq.N_COLS + 1):
        v = sb._ask(f"    column {i}: ", "")
        strains.append(None if v.strip() in ("", "-") else v.strip())
    named = [i + 1 for i, s in enumerate(strains) if s]
    if not named:
        raise SystemExit("  No named columns; nothing to measure.")
    ctrl = ""
    while not ctrl.isdigit() or int(ctrl) not in named:
        ctrl = sb._ask(f"    which column is the control {named}: ",
                       str(named[0]))
    cfg = {"strains": strains, "control_col": int(ctrl), "exclude": []}
    sid2 = set_id or set_id_from_name(root.name)
    if sid2:
        cfg["from_set"] = sid2
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    print(f"    saved {path}")
    return cfg


def cloud_placeholders(shots):
    """Photos that are not actually on this disk yet.

    OneDrive's Files On-Demand leaves a stub with the real content in the cloud;
    Windows flags those with RECALL_ON_DATA_ACCESS / RECALL_ON_OPEN / OFFLINE
    and materialises them when something reads them. That matters here for two
    reasons: a run would stall while hundreds of 24 MP photos download, and a
    file read while it is still materialising can come back partial. That has
    already bitten this project once -- a OneDrive re-sync changed a photo
    underneath a finished run and the plate measured with a background spread of
    55 against a normal 0.2, which is nonsense but not obviously nonsense.

    Returns the list of shots whose files are still cloud-only.
    """
    OFFLINE = 0x1000
    RECALL_ON_OPEN = 0x40000
    RECALL_ON_DATA_ACCESS = 0x400000
    mask = OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
    out = []
    for s in shots:
        try:
            attrs = getattr(s.path.stat(), "st_file_attributes", 0)
        except OSError:
            continue
        if attrs & mask:
            out.append(s)
    return out


def build_jobs(cands, cache_dir: Path):
    """One measurement per (photo, dilution) -- deduplicated.

    A photo shared by several pairings is measured once, which is most of the
    saving when there are technical replicates.
    """
    jobs, seen = [], set()
    for c in cands:
        for rows in ROW_SETS:
            for shot in (c["plate1"], c["plate2"]):
                key = (str(shot.path), tuple(r + 1 for r in rows))
                if key not in seen:
                    seen.add(key)
                    jobs.append((str(shot.path), tuple(r + 1 for r in rows),
                                 str(cache_dir)))
    return jobs


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="Pick the best photo set out of a spotting time course.")
    ap.add_argument("root", type=Path, help="Top of the capture tree")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel measurements (default: half the cores, "
                         "max 6 -- each runs its own headless FIJI)")
    ap.add_argument("--estimate", action="store_true",
                    help="Report how much work it is, then stop.")
    ap.add_argument("--rank-by",
                    choices=["combined", "variability", "significance"],
                    default="combined",
                    help="combined (default): tight replicates AND many "
                         "significant strains, for triage. variability: spread "
                         "only. significance: hits only.")
    ap.add_argument("--from-set", default=None, metavar="N",
                    help="Take the strain panel from this set of the main "
                         "config instead of reading it off the folder name.")
    ap.add_argument("--main-config", type=Path, default=None,
                    help="Path to the main pipeline spotting_config.json.")
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="Where to keep measurements (default: a .spotting_cache "
                         "folder beside the photos). Point this at a LOCAL disk "
                         "when the photos live on OneDrive.")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    root = args.root
    if not root.is_dir():
        print(f"Not a folder: {root}", file=sys.stderr)
        return 2

    shots, bad = discover(root)
    for b in bad:
        print(f"  ! {b}")
    if not shots:
        print("No photos found. Expected timepoint/medium/Plate N/images.",
              file=sys.stderr)
        return 1

    cands = candidates(shots)
    if not cands:
        print("No condition has both a Plate 1 and a Plate 2 folder with "
              "images -- four replicates need both.", file=sys.stderr)
        return 1

    cache = args.cache_dir or (root / ".spotting_cache")
    jobs = build_jobs(cands, cache)
    # Each measurement is memory-bandwidth heavy and carries its own JVM, so the
    # useful ceiling is physical cores -- and well below even that. --workers 1
    # is the honest baseline to compare any setting against.
    workers = args.workers or max(
        1, min(4, (multiprocessing.cpu_count() or 2) // 4))
    n_cond = len({(c["medium"], c["tp_label"]) for c in cands})
    print(f"\n  {len(shots)} photo(s), "
          f"{len({c['medium'] for c in cands})} medium/media, "
          f"{n_cond} condition-timepoints")
    print(f"  {len(cands)} photo pairing(s) x 3 dilutions = "
          f"{len(cands) * 3} candidates")
    print(f"  {len(jobs)} measurement(s) after de-duplication; at ~70 s each "
          f"that is about {len(jobs) * 70 / 60 / max(workers, 1):.0f} min "
          f"on {workers} worker(s)")
    stubs = cloud_placeholders(shots)
    if stubs:
        # Count how many will actually need downloading (cached ones are free).
        opts_probe = replace(sq.MeasureOptions())
        n_download = sum(
            1 for s in stubs
            if not (cache / f"{sb._cache_key(s.path, opts_probe)}.npz").exists()
        )
        if n_download:
            print(f"\n  Note: {n_download} of {len(stubs)} cloud-only photo(s) "
                  f"will be downloaded as needed.")
            print("     Each is copied to a local temp file before measurement "
                  "so a concurrent\n     OneDrive re-sync cannot corrupt the "
                  "read. The copy is deleted immediately\n     after; re-runs "
                  "will use the cache and skip the download entirely.")
        else:
            print(f"\n  Note: {len(stubs)} photo(s) are cloud-only but all "
                  "measurements are cached -- no download needed.")

    if args.cache_dir is None and "onedrive" in str(root).lower():
        print("\n  Note: the photos are on OneDrive, so the measurement cache "
              "would be\n     written there too and synced back up. "
              "--cache-dir <local path> keeps it\n     off OneDrive; the cache "
              "is small (a few KB per measurement) but it is\n     one less "
              "thing for sync to touch.")

    if args.estimate:
        return 0

    cfg = load_or_ask_config(root, args.from_set, args.main_config)
    cache.mkdir(parents=True, exist_ok=True)

    print(f"\n  Measuring on {workers} worker(s) ...")
    errs = measure_all(jobs, cache, workers)
    for path, e in errs[:10]:
        print(f"  ! {Path(path).name}: {e}")
    if len(errs) > 10:
        print(f"  ! ... and {len(errs) - 10} more")

    print("\n  Scoring ...")
    recs = []
    for c in cands:
        for rows, nm in zip(ROW_SETS, ROW_NAMES):
            m = score_candidate(c, rows, cache, cfg["strains"],
                                cfg["control_col"], cfg.get("exclude"))
            if not m:
                continue
            recs.append({"medium": c["medium"],
                         "medium_label": c["medium_label"],
                         "timepoint": c["tp_label"], "hours": c["hours"],
                         "plate1": c["plate1"].path.name,
                         "plate2": c["plate2"].path.name,
                         "dilution": nm, **m})
    if not recs:
        print("  Nothing could be scored -- every candidate had a control at "
              "or below the noise floor.", file=sys.stderr)
        return 1

    df = pd.DataFrame(recs)
    # Combined score: rank each candidate within its medium on spread (low is
    # good) and on how many strains separate from the control (high is good),
    # then average the two ranks. Darren asked for both because this is a triage
    # list -- it decides which photo sets are worth opening first, and every
    # winner is still inspected by eye before anything is reported. Keep that in
    # mind when reading the p-values: candidates were compared on their results,
    # so the winner's p-values are optimistic as a final claim, and the run is
    # stamped to say so.
    if args.rank_by == "combined":
        df["_r_cv"] = df.groupby("medium")["median_CV"].rank(ascending=True)
        df["_r_sig"] = df.groupby("medium")["n_significant"].rank(ascending=False)
        df["rank_score"] = (df["_r_cv"] + df["_r_sig"]) / 2.0
        df = (df.sort_values(["medium", "rank_score", "median_CV"],
                             ascending=[True, True, True])
                .drop(columns=["_r_cv", "_r_sig"]).reset_index(drop=True))
    else:
        by_var = args.rank_by == "variability"
        key = "median_CV" if by_var else "n_significant"
        df = df.sort_values(["medium", key, "median_CV"],
                            ascending=[True, by_var, True]).reset_index(drop=True)
    df["ranked_by"] = args.rank_by

    out = args.out or (root / "timecourse_candidates.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"  wrote {out}  ({len(df)} scored candidates)")

    if args.rank_by != "variability":
        what = ("significance" if args.rank_by == "significance"
                else "significance as well as spread")
        print(f"\n  Note: ranked on {what}, so candidates were compared partly "
              f"on their\n     own results. Good for deciding what to look at "
              f"first; the winner's\n     p-values are optimistic as a final "
              f"claim, so confirm the chosen set\n     by eye before reporting "
              f"it.")

    print(f"\n  Best per medium (ranked by {args.rank_by}):")
    for medium, g in df.groupby("medium", sort=False):
        b = g.iloc[0]
        print(f"    {medium:>7}  {b['timepoint']:>10}  {b['dilution']:>6} "
              f"dilution   CV {b['median_CV']:.2f}   control "
              f"{b['control_mean']:.1f} (CV {b['control_CV']:.2f})   "
              f"{int(b['n_significant'])}/{int(b['n_strains'])} significant")
        print(f"               {b['plate1']}  +  {b['plate2']}")
    print("\n  Every candidate is in the CSV -- the ranking is a suggestion, "
          "not a decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
