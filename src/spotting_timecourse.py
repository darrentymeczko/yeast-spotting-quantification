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
import time
from dataclasses import dataclass, replace
from functools import lru_cache
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
# Measured wall-clock for one uncached photo (full-res centring + one
# headless FIJI background subtraction). Re-check with --timing.
SECONDS_PER_MEASUREMENT = 70
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


def safe_dirname(name: str) -> str:
    """A Windows-safe folder name derived from the capture-tree folder name."""
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "timecourse"


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


def is_capture_tree(path: Path) -> bool:
    """True if this folder is itself a capture tree (has photos to score)."""
    try:
        shots, _ = discover(path)
    except OSError:
        return False
    return bool(shots)


@dataclass(frozen=True)
class Tree:
    """One capture tree to run, and what to call its results.

    `label` names the results folder, and `set_hint` is the set number its
    strain panel comes from. They differ for an extra take: `Set01/Take02` runs
    as "Set01 - Take02" but takes Set 1's panel, because "Take02" contains no
    set number of its own and would otherwise have to be typed in by hand.
    """
    path: Path
    label: str
    set_hint: "str | None" = None


def _images_below(path: Path, cap: int = 10_000) -> int:
    """How many images live anywhere under this folder."""
    n = 0
    for p in path.rglob("*"):
        if p.suffix.lower() in IMAGE_EXT and p.is_file():
            n += 1
            if n >= cap:
                break
    return n


def scan_extras(root: Path, max_depth: int = 4) -> tuple:
    """Find photo folders inside a capture tree that `discover` would skip.

    A set folder often holds a second session beside its timepoint folders --
    `Take02`, or somebody's name. `discover` walks only the timepoint folders,
    so those photos are silently left out of the analysis; on this data six of
    ten sets hid 249 photos that way, more than the 194 the run was counting.

    Returns (extras, unreadable):

    * `extras` are folders that ARE valid capture trees, as (path, n_photos).
      The scan does not descend into one once found -- its own subfolders are
      that tree's timepoints, not further takes.
    * `unreadable` are folders holding photos in a layout `discover` cannot
      read, as (path, n_photos, reason). Reported so the photos are visibly
      excluded rather than invisibly, but never offered for inclusion: working
      out which photo is plate 1 and which is plate 2 would be a guess, and
      getting it wrong silently mislabels replicates.
    """
    extras, unreadable = [], []

    def walk(d: Path, depth: int) -> bool:
        """True if anything worth reporting was found at or below `d`."""
        if depth > max_depth:
            return False
        if is_capture_tree(d):
            extras.append((d, len(discover(d)[0])))
            return True
        found = False
        try:
            subs = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return False
        for sub in subs:
            if sub.name.startswith("."):
                continue
            found |= walk(sub, depth + 1)
        if not found:
            n = _images_below(d)
            if n:
                unreadable.append((d, n, "no 'Plate N' folder, or an extra "
                                         "folder level above the timepoints"))
                return True
        return found

    try:
        subs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return [], []
    for sub in subs:
        # Timepoint folders are the tree itself; anything else is a candidate.
        if sub.name.startswith(".") or _hours(sub.name) is not None:
            continue
        walk(sub, 1)
    return extras, unreadable


def choose_extras(roots, assume=None) -> list:
    """Show the extra sessions found inside each tree and ask which to include.

    Asked immediately after the folders are chosen and before any measuring, so
    a long batch can be left alone once it starts. An included take runs as its
    OWN tree -- own results folder, own ranking -- so a photo from one session
    is never paired with a photo from another.

    `assume` of True/False answers everything without prompting, for
    --include-takes / --no-takes.
    """
    out = list(roots)
    for tree in roots:
        extras, unreadable = scan_extras(tree.path)
        if not extras and not unreadable:
            continue
        print(f"\n  {tree.label}: extra photo folder(s) beside the timepoints")
        for d, n, why in unreadable:
            print(f"    - {d.relative_to(tree.path)}  ({n} photo(s)) "
                  f"CANNOT be read: {why}")
            print(f"      Left out. Restructure it as "
                  f"<timepoint>/<medium>/Plate N/ to include it.")
        for d, n in extras:
            rel = d.relative_to(tree.path)
            # Which strain panel? A take usually inherits its parent's set, but
            # some name a set of their OWN -- Set05/Andrea/"Set 1" claims set 1
            # while sitting inside Set05. That disagreement decides which strain
            # names get attached to all eight columns, so it is never guessed
            # silently: the local claim wins (it is the more specific label) and
            # the conflict is spelled out, with the panel still shown for
            # confirmation before anything runs.
            parent_id = tree.set_hint or set_id_from_name(tree.label)
            own_id = next((set_id_from_name(part) for part in reversed(rel.parts)
                           if set_id_from_name(part)), None)
            use_id = own_id or parent_id
            if own_id and parent_id and own_id != parent_id:
                print(f"      [!] {rel} says set {own_id} but sits in set "
                      f"{parent_id}. Using set {own_id} -- check the strain "
                      f"panel it offers before accepting it.")
            if assume is None:
                ans = sb._ask(f"    Include {rel} ({n} photo(s)) as its own "
                              f"set? [y/N]: ", "n")
                take = ans.strip().lower().startswith("y")
            else:
                take = bool(assume)
                print(f"    {rel} ({n} photo(s)): "
                      f"{'included' if take else 'skipped'}")
            if take:
                label = f"{tree.label} - {'-'.join(rel.parts)}"
                out.append(Tree(d, label, use_id))
    return out


def expand_roots(paths) -> tuple:
    """Turn what the user pointed at into the list of capture trees to run.

    A path is taken as-is if it holds photos. If it does not, its immediate
    subfolders are checked instead, so pointing at the folder that CONTAINS the
    sets -- `Deletion Strains`, holding Set01..Set10 -- runs all of them. That is
    the common case when re-running everything, and it saves picking ten folders
    one at a time.

    Returns (roots, complaints). Order is preserved and duplicates are dropped,
    so overlapping selections (a parent and one of its children) run each tree
    once.
    """
    roots, seen, bad = [], set(), []
    for p in paths:
        p = Path(p)
        if not p.is_dir():
            bad.append(f"{p}: not a folder")
            continue
        if is_capture_tree(p):
            found = [p]
        else:
            found = [d for d in sorted(p.iterdir())
                     if d.is_dir() and not d.name.startswith(".")
                     and is_capture_tree(d)]
            if not found:
                bad.append(f"{p}: no photos here, and no subfolder looks like "
                           f"a capture tree (timepoint/medium/Plate N/images)")
                continue
            print(f"  {p.name}: found {len(found)} capture tree(s) inside "
                  f"-- {', '.join(d.name for d in found)}")
        for d in found:
            key = str(d.resolve()).lower()
            if key not in seen:
                seen.add(key)
                roots.append(Tree(d, d.name))
    return roots, bad


# --- native Windows multi-select folder dialog -----------------------------
# tkinter's askdirectory and .NET's FolderBrowserDialog are both single-select,
# so neither can do what this needs. The shell's own IFileOpenDialog can --
# FOS_PICKFOLDERS turns it into a folder picker and FOS_ALLOWMULTISELECT lets
# Ctrl/Shift-click pick several -- but pywin32 does not wrap that interface and
# comtypes is not installed, so it is driven through its vtable with ctypes.
# Slot numbers below are fixed by the interface definitions and never change:
#   IUnknown        0 QueryInterface  1 AddRef  2 Release
#   IModalWindow    3 Show
#   IFileDialog     9 SetOptions  10 GetOptions  17 SetTitle  12 SetFolder
#   IFileOpenDialog 27 GetResults
#   IShellItemArray 7 GetCount  8 GetItemAt
#   IShellItem      5 GetDisplayName
_CLSID_FileOpenDialog = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_IID_IFileOpenDialog = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
_FOS_PICKFOLDERS, _FOS_ALLOWMULTISELECT, _FOS_FORCEFILESYSTEM = 0x20, 0x200, 0x40
_SIGDN_FILESYSPATH = 0x80058000
_ERROR_CANCELLED = 0x800704C7


def _pick_folders_native(title: str, start: "Path | None" = None) -> list:
    """Multi-select folder picker. Raises if the shell dialog is unavailable."""
    import ctypes
    from ctypes import POINTER, byref, c_void_p, c_uint, c_ulong, c_long

    ole32 = ctypes.OleDLL("ole32")
    shell32 = ctypes.OleDLL("shell32")

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        def __init__(self, s):
            super().__init__()
            ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(self))

    def call(p, slot, *args, restype=ctypes.HRESULT, argtypes=()):
        vtbl = ctypes.cast(p, POINTER(POINTER(c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(vtbl[slot])(p, *args)

    ole32.CoInitialize(None)
    dlg = c_void_p()
    ole32.CoCreateInstance(byref(GUID(_CLSID_FileOpenDialog)), None, 1,
                           byref(GUID(_IID_IFileOpenDialog)), byref(dlg))
    try:
        opts = c_uint()
        call(dlg, 10, byref(opts), argtypes=(POINTER(c_uint),))
        call(dlg, 9, opts.value | _FOS_PICKFOLDERS | _FOS_ALLOWMULTISELECT
             | _FOS_FORCEFILESYSTEM, argtypes=(c_uint,))
        call(dlg, 17, ctypes.c_wchar_p(title), argtypes=(ctypes.c_wchar_p,))
        if start and Path(start).is_dir():
            item = c_void_p()
            try:
                shell32.SHCreateItemFromParsingName(
                    ctypes.c_wchar_p(str(start)), None,
                    byref(GUID("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")),
                    byref(item))
                call(dlg, 12, item, argtypes=(c_void_p,))
                call(item, 2, restype=c_ulong)
            except OSError:
                pass                      # a bad start folder must not stop it

        # Show returns a plain HRESULT so Cancel can be told from failure;
        # ctypes.HRESULT would turn the cancel into an exception.
        hr = call(dlg, 3, None, restype=c_long, argtypes=(c_void_p,))
        if (hr & 0xFFFFFFFF) == _ERROR_CANCELLED:
            return []
        if hr < 0:
            raise OSError(f"folder dialog failed: 0x{hr & 0xFFFFFFFF:08x}")

        arr = c_void_p()
        call(dlg, 27, byref(arr), argtypes=(POINTER(c_void_p),))
        out = []
        try:
            n = c_uint()
            call(arr, 7, byref(n), argtypes=(POINTER(c_uint),))
            for i in range(n.value):
                item = c_void_p()
                call(arr, 8, i, byref(item),
                     argtypes=(c_uint, POINTER(c_void_p)))
                try:
                    s = ctypes.c_wchar_p()
                    call(item, 5, _SIGDN_FILESYSPATH, byref(s),
                         argtypes=(c_uint, POINTER(ctypes.c_wchar_p)))
                    if s.value:
                        out.append(Path(s.value))
                        ole32.CoTaskMemFree(s)
                finally:
                    call(item, 2, restype=c_ulong)
        finally:
            call(arr, 2, restype=c_ulong)
        return out
    finally:
        call(dlg, 2, restype=c_ulong)


def pick_folders() -> list:
    """Ask for the folders to run.

    Uses the shell's multi-select folder picker so several sets can be chosen in
    one go with Ctrl or Shift-click, and reopens until Cancel so folders in
    different places can be added. Picking the folder that CONTAINS the sets is
    quicker still -- see `expand_roots`.

    Returns [] if no dialog can be shown at all, so the caller can fall back to
    typing a path.
    """
    print("\n  A folder picker will open.")
    print("    * Ctrl-click or Shift-click to select SEVERAL folders at once.")
    print("    * Or pick the one folder that CONTAINS your sets -- every "
          "capture")
    print("      tree inside it is found automatically.")
    print("    * It reopens so you can add folders from elsewhere; press "
          "Cancel when done.")

    picked, start, native = [], None, True
    while True:
        title = (f"Select time-course folder(s) -- {len(picked)} chosen so far"
                 if picked else
                 "Select time-course folder(s) (Ctrl-click for several)")
        got = []
        if native:
            try:
                got = _pick_folders_native(title, start)
            except Exception as e:
                # Never let a picker problem block the run: fall back to the
                # single-select dialog, which only needs tkinter.
                print(f"  (multi-select picker unavailable: {e}; "
                      f"falling back to one folder at a time)")
                native = False
                continue
        else:
            got = _pick_folder_tk(title, start)
        if not got:
            break
        for p in got:
            picked.append(p)
            print(f"    + {p}")
        start = str(got[-1].parent)
    return picked


def _pick_folder_tk(title: str, start) -> list:
    """Single-folder fallback picker. Returns [] on cancel or if tkinter fails."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        print(f"  (folder picker unavailable: {e})")
        return []
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
    except Exception as e:
        print(f"  (folder picker unavailable: {e})")
        return []
    try:
        d = filedialog.askdirectory(title=title, initialdir=start,
                                    mustexist=True)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return [Path(d)] if d else []


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
    t0 = time.perf_counter()
    cached = False
    try:
        ref = sb.PhotoRef(Path(path), 0, 1, "TC")
        opts = replace(sq.MeasureOptions(), quant_rows=tuple(rows))
        cache_dir_path = Path(cache_dir)

        # If the result is already cached, the photo never needs to be read —
        # skip the download entirely.
        cf = cache_dir_path / f"{sb._cache_key(ref.path, opts)}.npz"
        cached = cf.exists()
        read_path = None
        if not cached and _is_cloud_stub(ref.path):
            fd, tmp_str = tempfile.mkstemp(suffix=ref.path.suffix)
            os.close(fd)
            tmp = Path(tmp_str)
            shutil.copy2(str(ref.path), str(tmp))
            read_path = tmp

        sb.measure(ref, opts, cache_dir_path, read_path=read_path)
        return (str(path), tuple(rows), None,
                time.perf_counter() - t0, cached)
    except Exception as e:
        return (str(path), tuple(rows), f"{type(e).__name__}: {e}",
                time.perf_counter() - t0, cached)
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def measure_all(jobs, cache_dir: Path, workers: int, timing: bool = False):
    """Measure every photo, in parallel.

    Safe to parallelise: `build_jobs` emits one job per photo, and each writes
    its own cache entry keyed on that photo, so two workers never touch the same
    file. Processes rather than threads -- detect_grid is numpy work, and every
    call also spawns its own headless FIJI.

    Worker count defaults to one per physical core: each worker carries a JVM
    and a 24 MP image, and `_init_worker` pins its BLAS to a single thread so
    the pool scales on processes rather than fighting itself for threads.

    Returns (errors, elapsed) where elapsed is a list of (seconds, was_cached)
    per job -- see the --timing report.
    """
    from concurrent.futures import ProcessPoolExecutor, as_completed

    errors, elapsed = [], []
    total = len(jobs)
    t_start = time.perf_counter()
    if workers <= 1:
        for i, j in enumerate(jobs, 1):
            _, _, err, dt, cached = _measure_one(j)
            if err:
                errors.append((j[0], err))
            elapsed.append((dt, cached))
            print(f"\r    {i}/{total}", end="", flush=True)
        print()
    else:
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_init_worker) as pool:
            futs = [pool.submit(_measure_one, j) for j in jobs]
            for i, fut in enumerate(as_completed(futs), 1):
                path, rows, err, dt, cached = fut.result()
                if err:
                    errors.append((path, err))
                elapsed.append((dt, cached))
                print(f"\r    {i}/{total}  ({workers} workers)",
                      end="", flush=True)
        print()

    if timing:
        wall = time.perf_counter() - t_start
        fresh = sorted(dt for dt, c in elapsed if not c)
        hits = sum(1 for _, c in elapsed if c)
        print(f"    timing: {wall / 60:.1f} min wall-clock on {workers} "
              f"worker(s) for {total} job(s)")
        if fresh:
            mid = fresh[len(fresh) // 2]
            print(f"            {len(fresh)} measured: "
                  f"min {fresh[0]:.0f}s / median {mid:.0f}s / "
                  f"max {fresh[-1]:.0f}s each")
            print(f"            throughput {sum(fresh) / wall:.1f}x "
                  f"(serial-seconds per wall-second)")
        if hits:
            print(f"            {hits} served from cache")
    return errors, elapsed


@lru_cache(maxsize=None)
def _cached_measure(path: Path, plate: int, rows: tuple, cache_dir: Path):
    """`sb.measure` off the cache, memoized for the scoring pass.

    Scoring reads the same photo once per (candidate, row-set, shot) -- six
    times per candidate, and again for every other candidate that shares the
    photo. Each of those calls re-opens and zlib-decompresses the same .npz.
    The measurement itself is already cached on disk; this just stops the
    decompression being repeated. Read-only: nothing mutates a PlateData.
    """
    ref = sb.PhotoRef(path, 0, plate, "TC")
    opts = replace(sq.MeasureOptions(), quant_rows=rows)
    return sb.measure(ref, opts, cache_dir)


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
        try:
            pd_ = _cached_measure(shot.path, shot.plate,
                                  tuple(r + 1 for r in rows), cache_dir)
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
    """One measurement per PHOTO -- not per (photo, dilution).

    `sb.measure` computes every dilution choice in a single pass and stores them
    all in one cache entry, and `sb._cache_key` deliberately leaves `quant_rows`
    out of the key. So three jobs differing only in the row-set are three jobs
    with the SAME cache key: whichever finishes first writes the entry the other
    two would have written. Submitting them together, as this used to, handed the
    same 24 MP photo to three workers before any of them had written the cache --
    three full 41 s centrings, three JVM launches and, for a OneDrive stub, three
    downloads of the same file.

    The row-set below is therefore only a placeholder to make `opts.quant_rows`
    well-formed for the cache-key computation in `_measure_one`; all three are
    measured and cached regardless of which one is named here.

    A photo shared by several pairings is likewise measured once, which is most
    of the saving when there are technical replicates.
    """
    rows = tuple(r + 1 for r in ROW_SETS[0])
    jobs, seen = [], set()
    for c in cands:
        for shot in (c["plate1"], c["plate2"]):
            key = str(shot.path)
            if key not in seen:
                seen.add(key)
                jobs.append((key, rows, str(cache_dir)))
    return jobs


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    ap = argparse.ArgumentParser(
        description="Pick the best photo set out of a spotting time course.")
    ap.add_argument("roots", type=Path, nargs="*", metavar="FOLDER",
                    help="One or more capture trees. A folder that CONTAINS "
                         "capture trees expands to all of them, so pointing at "
                         "the folder holding Set01..Set10 runs all ten. With "
                         "none given, a folder picker opens.")
    ap.add_argument("--include-takes", action="store_true",
                    help="Include every extra session found inside a set "
                         "(Take02, a person's name) without asking. Each runs "
                         "as its own set.")
    ap.add_argument("--no-takes", action="store_true",
                    help="Skip every extra session without asking.")
    ap.add_argument("--pick", action="store_true",
                    help="Open the folder picker even when folders were given, "
                         "and add whatever you choose to them.")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel measurements (default: one per physical "
                         "core, max 8 -- each runs its own headless FIJI)")
    ap.add_argument("--estimate", action="store_true",
                    help="Report how much work it is, then stop.")
    ap.add_argument("--timing", action="store_true",
                    help="Report measured per-measurement and total wall-clock "
                         "time. Use it to compare --workers settings.")
    ap.add_argument("--figures", default="all", metavar="all|none|N",
                    help="Draw a spots-plus-graph sheet per candidate. "
                         "'all' (default) draws every candidate so they can be "
                         "compared side by side; N keeps only the best N of "
                         "each medium; 'none' skips them.")
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
    ap.add_argument("--out", type=Path, default=None,
                    help="Output CSV, or a folder to write the run into "
                         "(default: Results/Timecourse/<set> beside the code).")
    args = ap.parse_args(argv)

    given = list(args.roots)
    if args.pick or not given:
        given += pick_folders()
    if not given:
        typed = sb._ask("  Path to the time-course folder "
                        "(or the folder holding your sets): ", "").strip('" ')
        if typed:
            given = [Path(typed)]
    if not given:
        print("No folder given.", file=sys.stderr)
        return 2

    roots, bad_roots = expand_roots(given)
    for b in bad_roots:
        print(f"  ! {b}", file=sys.stderr)
    if not roots:
        print("Nothing to run.", file=sys.stderr)
        return 2

    if len(roots) > 1 and args.out and args.out.suffix.lower() == ".csv":
        print("--out names a single CSV, but this run covers "
              f"{len(roots)} folders. Give a FOLDER instead and each gets its "
              "own subfolder under it.", file=sys.stderr)
        return 2

    if len(roots) > 1 and args.from_set:
        print("--from-set names one set's strain panel, so it cannot apply to "
              f"{len(roots)} folders. Run them one at a time, or let each "
              "folder's name resolve its own panel.", file=sys.stderr)
        return 2

    # Extra sessions are found and settled BEFORE anything heavy, so every
    # question in a long batch is asked in the first minute.
    assume = True if args.include_takes else (False if args.no_takes else None)
    roots = choose_extras(roots, assume=assume)

    print(f"\n  {len(roots)} capture tree(s) to run:")
    for r in roots:
        print(f"    {r.label}")

    # Every strain-panel question is asked NOW too, for the same reason: a
    # ten-set run is hours of work, and interleaving prompts through it means
    # finding the machine waiting on question six after an hour of unattended
    # progress. --estimate skips this: it answers "how long?" without setup.
    cfgs = {}
    if not args.estimate:
        for r in roots:
            if len(roots) > 1:
                print(f"\n  --- {r.label} ---")
            try:
                cfgs[str(r.path)] = load_or_ask_config(
                    r.path, r.set_hint or args.from_set, args.main_config)
            except Exception as e:
                print(f"  ! {r.label}: {type(e).__name__}: {e}",
                      file=sys.stderr)

    rc = 0
    for i, r in enumerate(roots, 1):
        if len(roots) > 1:
            print(f"\n{'=' * 62}\n  [{i}/{len(roots)}]  {r.label}\n{'=' * 62}")
        cfg = cfgs.get(str(r.path))
        if not args.estimate and cfg is None:
            print(f"  skipped -- no strain panel for {r.label}")
            rc = rc or 1
            continue
        try:
            one = run_one(r, args, cfg, multi=len(roots) > 1)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # One bad tree must not abandon the rest of an overnight batch.
            print(f"  ! {r.label} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            one = 1
        rc = rc or one
    return rc


def run_one(tree: "Tree", args, cfg, multi: bool = False) -> int:
    """Score one capture tree. `cfg` is its already-resolved strain panel."""
    root = tree.path
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
    # useful ceiling is PHYSICAL cores, not logical ones -- hence cpu_count()//2
    # on an SMT machine. The old cap of min(4, cpus//4) dated from the
    # oversubscription incident in `_init_worker`, which the BLAS thread pinning
    # there already fixes; it left half the machine idle (measured: 4 workers on
    # a 16-logical-core box ran at ~45% total CPU). --workers 1 is the honest
    # baseline to compare any setting against; --timing reports the real cost.
    #
    # RE-MEASURED 2026-09-02, 8-core/16-thread 7840U, 8 cold photos:
    #     4 workers -> 185 s wall, median 91 s per photo, 98% of ideal scaling
    #     8 workers -> 124 s wall, median 119 s per photo, 96% of ideal
    # Per-photo time RISES with 8 -- they compete for memory bandwidth -- but
    # throughput still improves 1.49x, so 8 wins. Re-time before changing this
    # again; the naive estimate has been badly wrong here before.
    workers = args.workers or max(
        1, min(8, (multiprocessing.cpu_count() or 2) // 2))
    n_cond = len({(c["medium"], c["tp_label"]) for c in cands})
    print(f"\n  {len(shots)} photo(s), "
          f"{len({c['medium'] for c in cands})} medium/media, "
          f"{n_cond} condition-timepoints")
    print(f"  {len(cands)} photo pairing(s) x 3 dilutions = "
          f"{len(cands) * 3} candidates")
    # One measurement per photo, not per (photo, dilution): every dilution
    # choice comes out of the same pass. SECONDS_PER_MEASUREMENT is a measured
    # figure -- re-check it with --timing after any change to worker count or to
    # the measurement code, because the naive estimate has been badly wrong here
    # before (see _init_worker).
    n_todo = sum(
        1 for j in jobs
        if not (cache / f"{sb._cache_key(Path(j[0]), replace(sq.MeasureOptions(), quant_rows=j[1]))}.npz").exists())
    print(f"  {len(jobs)} measurement(s) after de-duplication, "
          f"{n_todo} not yet cached; at ~{SECONDS_PER_MEASUREMENT} s each that "
          f"is about {n_todo * SECONDS_PER_MEASUREMENT / 60 / max(workers, 1):.0f} "
          f"min on {workers} worker(s)")
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

    cache.mkdir(parents=True, exist_ok=True)

    print(f"\n  Measuring on {workers} worker(s) ...")
    errs, _ = measure_all(jobs, cache, workers, timing=args.timing)
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

    # Results live beside the code under Results/Timecourse/<set>, never inside
    # the photo tree: the photos are on OneDrive, and a run's output written
    # next to them gets synced up and is easy to mistake for part of the raw
    # capture. The main pipeline writes to Results/Spotting for the same reason,
    # and the two stay apart because they answer different questions -- this one
    # is triage, that one is the result.
    # With several trees in one run, --out names the PARENT: each still gets its
    # own subfolder, or they would overwrite each other's CSV one by one.
    if args.out and args.out.is_dir():
        outdir = (args.out / safe_dirname(tree.label) if multi else args.out)
    else:
        outdir = sb.TIMECOURSE_RESULTS / safe_dirname(tree.label)
    outdir.mkdir(parents=True, exist_ok=True)
    out = (args.out if args.out and args.out.suffix.lower() == ".csv" and not multi
           else outdir / "timecourse_candidates.csv")
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
    want = str(args.figures).strip().lower()
    if want != "none":
        n_per = None if want == "all" else max(1, int(want))
        print("\n  Drawing "
              + ("a sheet for every candidate" if n_per is None
                 else f"sheets for the best {n_per} candidate(s) per medium")
              + " ...")
        try:
            import spotting_timecourse_figures as tcf
            made = tcf.build_figures(
                df, cands, cache, cfg["strains"], cfg["control_col"],
                outdir / "figures", n_per_medium=n_per,
                exclude=cfg.get("exclude"),
                rank_note=f"ranked by {args.rank_by}")
            if made:
                print(f"  wrote {len(made)} sheet(s) to {outdir / 'figures'}")
            else:
                print("  (no sheets drawn)")
        except Exception as e:
            # The CSV is already on disk. Losing the figures must not lose the
            # run: they are a convenience drawn over data that is already saved.
            print(f"  ! figures skipped: {type(e).__name__}: {e}")

    print("\n  Every candidate is in the CSV -- the ranking is a suggestion, "
          "not a decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
