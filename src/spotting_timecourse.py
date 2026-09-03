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

THE "BEST SET" AND ITS CONSENSUS

Separately from that ranking, the run aggregates which strains reach
significance across ALL candidates and in which direction. Strains that agree
often enough (CONSENSUS_MIN_FRAC) form the "core" -- on this data POS5, GTR1 and
SOD2 come out reduced in almost every candidate, while CTA1 only occasionally
does. A candidate is then scored on how much of that core it recovers in the
agreed direction, whether its control has all four replicates, how clean it is,
and how much of its significance NOTHING else supports. The single best-scoring
candidate is written out in full -- montage, graph and deck -- under best/.

The off-consensus penalty is load-bearing, not decoration. Rewarding matches
alone let the noisiest candidate win by accumulating false positives, because
every extra hit added score; it was demoted only once unsupported hits started
costing. The same caveat as above still applies: the core is derived from these
candidates, so the winner's p-values remain optimistic as a final claim and the
chosen set is still confirmed by eye.
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
# The measurement cache lives with the PROGRAM, never in the photo folders.
# Two reasons it must not go beside the photos: those folders are the raw
# capture and nothing generated belongs in them, and they sit on OneDrive, so
# every .npz would be synced up and pushed to every other machine.
#
# One shared cache serves every tree because `sb._cache_key` is keyed on the
# file NAME, size and mtime -- not the path -- so an entry stays valid when
# folders are reorganised or renamed. Verified against this dataset: 450 photos
# produce 450 distinct keys with no collisions, even though `_9.JPG` is reused
# 256 times.
TIMECOURSE_CACHE = PROJECT_ROOT / ".spotting_cache"
CONFIG_NAME = "timecourse_config.json"
ROW_SETS = [(0, 3), (1, 4), (2, 5)]
# Measured wall-clock for one uncached photo (full-res centring + one
# headless FIJI background subtraction). Re-check with --timing.
SECONDS_PER_MEASUREMENT = 70
ROW_NAMES = ["least", "middle", "most"]
# The order the media are run in, for slide and report ordering.
MEDIUM_ORDER = ["GLU", "GLY", "K-OAc"]

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

        # If the result is already cached, the photo never needs to be read --
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
    strain_sigs: dict[str, int] = {}  # strain -> +1 increased / -1 reduced
    for strain_name, v in rel.items():
        v = np.asarray(v, float)
        if len(v) < 3 or v.mean() <= 0:
            continue
        n_str += 1
        cvs.append(float(v.std() / v.mean()))
        w = np.maximum(v, floor)
        if np.all(w > 0) and float(
                stats.ttest_1samp(np.log(w), 0).pvalue) < p_thresh:
            n_sig += 1
            strain_sigs[strain_name] = 1 if float(v.mean()) > 1.0 else -1
    if not cvs:
        return None
    return {"median_CV": float(np.median(cvs)),
            "control_mean": cm,
            "control_CV": float(np.std(ctrl_vals) / cm) if cm > 0 else np.nan,
            "n_strains": n_str, "n_significant": n_sig,
            "control_n": len(ctrl_vals),
            "_strain_sigs": strain_sigs}


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
    set_cc = int(entry.get("control_col") or 1)

    # The control is chosen PER MEDIUM in the main pipeline, not per set, and
    # that choice is stored in cfg["combo"] under "<set>|<medium>". It is not a
    # formality: WT BY has a growth defect on K-OAc, so sets 1-4 name a
    # different strain as the control there and exclude WT BY outright. Reading
    # only the set-level column silently normalises K-OAc against a strain that
    # barely grew, which divides by ~0 and inflates every ratio on the plate.
    media = {}
    for key, ent in (cfg.get("combo") or {}).items():
        sid, _, medium = str(key).partition("|")
        if sid != str(set_id) or not ent or not medium:
            continue
        media[medium] = {
            "control_col": int(ent.get("control_col") or set_cc),
            "exclude": list(ent.get("exclude") or []),
        }
    return {"strains": entry["strains"],
            "control_col": set_cc,
            "exclude": [], "media": media,
            "from_set": str(set_id),
            "from_config": str(main_cfg)}


def medium_cfg(cfg: dict, medium: str):
    """(control_col, exclude) for one medium, falling back to the set default.

    Every scoring and normalising path goes through here so a per-medium
    control cannot be honoured in one place and missed in another.
    """
    m = (cfg.get("media") or {}).get(medium) or {}
    cc = int(m.get("control_col") or cfg.get("control_col") or 1)
    ex = list(m.get("exclude") if m.get("exclude") is not None
              else (cfg.get("exclude") or []))
    return cc, ex


def _report_media(cfg: dict) -> None:
    """Print the per-medium control, so a wrong one is visible before the run."""
    media = cfg.get("media") or {}
    if not media:
        return
    strains = cfg.get("strains") or []
    print("    per-medium control:")
    for medium in sorted(media, key=_medium_rank):
        cc, ex = medium_cfg(cfg, medium)
        name = strains[cc - 1] if 0 < cc <= len(strains) else "?"
        drop = (" -- excluding "
                + ", ".join(str(strains[i - 1]) if 0 < i <= len(strains)
                            else f"col{i}" for i in ex)) if ex else ""
        print(f"      {medium:>7}: {name} (column {cc}){drop}")


def load_or_ask_config(root: Path, set_id=None, main_cfg: Path = None):
    """Strain names and control column for this capture tree.

    Order of preference: a config already saved here, then the main pipeline's
    entry for the set the folder is named after (Set09 -> sets["9"]), then ask.
    """
    path = root / CONFIG_NAME
    sid = set_id or set_id_from_name(root.name)
    default_main = PROJECT_ROOT / "Spotting Assays" / "spotting_config.json"

    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        # Configs written before the per-medium control was understood have no
        # "media" block, so they would keep normalising K-OAc against WT BY.
        # Backfill from the main config rather than making the user delete and
        # re-answer: the panel is unchanged, only the per-medium control is new.
        if "media" not in saved and sid:
            got = strains_from_main_config(sid, main_cfg or default_main)
            if got and got.get("media"):
                saved["media"] = got["media"]
                path.write_text(json.dumps(saved, indent=2, ensure_ascii=False),
                                encoding="utf-8")
                print(f"  updated {path.name}: added per-medium controls "
                      f"from {Path(main_cfg or default_main).name}")
        _report_media(saved)
        return saved

    if sid:
        main_cfg = main_cfg or default_main
        got = strains_from_main_config(sid, main_cfg)
        if got:
            named = [x for x in got["strains"] if x]
            print(f"\n  Folder is named for set {sid}; taking that panel "
                  f"from {Path(main_cfg).name}:")
            print(f"    {', '.join(named)}")
            print(f"    control: {got['strains'][got['control_col']-1]} "
                  f"(column {got['control_col']})")
            _report_media(got)
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


def _aggregate_strain_significance(strain_sigs_list):
    """Count how many candidates found each strain significant, by direction.

    Returns {strain: {+1: n_increased, -1: n_reduced}}.
    """
    counts: dict[str, dict[int, int]] = {}
    for sigs in strain_sigs_list:
        for strain, direction in sigs.items():
            if strain not in counts:
                counts[strain] = {1: 0, -1: 0}
            counts[strain][direction] += 1
    return counts


CONSENSUS_MIN_FRAC = 0.5   # a strain is "core" if this fraction of candidates agree
W_ALIGN, W_COMPLETE, W_VARIANCE, W_OFF = 3.0, 1.0, 1.5, 1.0

# A candidate only gets a vote in the consensus if its control grew enough for
# the assay to resolve anything. `sq.MIN_CONTROL_GRAY` censors an individual
# spot; this is the same idea one level up, on the candidate.
#
# Why it matters: relative growth is censored at MIN_CONTROL_GRAY/control, so a
# candidate whose control read 1.6 gray cannot see a strain unless it is down
# more than 33%, while one at 25 gray resolves 2%. The dim candidate finds
# nothing -- not because nothing is there, but because it could not look. Left
# in the denominator that reads as evidence of absence. On Set02 K-OAc the 18
# undergrown 40-46 h candidates held PIM1 at 25/51 = 49%, one short of core,
# while every one of its 25 hits came from the readable timepoints (25/36 = 69%).
READABLE_CONTROL_MULT = 10.0        # control must reach 10x the spot floor
# ... but only when enough candidates survive to still be a consensus. Some
# set/medium combinations are dim THROUGHOUT (sets 5-8 on K-OAc), and gating
# those would build a "consensus" out of two candidates, which is worse than
# not gating. There the whole medium is the finding, and it is reported instead.
MIN_READABLE_CANDIDATES = 8
MIN_READABLE_FRAC = 0.30


def core_strains(consensus, n_total, min_frac: float = CONSENSUS_MIN_FRAC):
    """The strains that are significant OFTEN, and the direction they agree on.

    Returns {strain: (direction, frequency)} for strains whose dominant
    direction appears in at least `min_frac` of all candidates. This is the
    "POS5, GTR1 and SOD2 are commonly reduced" set; a strain that only
    occasionally reaches significance (CTA1) is deliberately left out, because
    matching it says more about noise than about the biology.
    """
    core = {}
    for strain, dirs in consensus.items():
        direction = max(dirs, key=lambda d: dirs[d])
        freq = dirs[direction] / max(n_total, 1)
        if dirs[direction] and freq >= min_frac:
            core[strain] = (direction, freq)
    return core


def _off_consensus_penalty(sigs, consensus, n_total, core,
                           min_frac: float = CONSENSUS_MIN_FRAC):
    """How much of this candidate's significance is unsupported by the others.

    Scaled by how RARE each unsupported hit is, not merely counted. A strain
    reaching significance in a third of candidates is a weak but real effect
    and should barely register; one that fires once in thirty is noise and
    should cost most of a point. Anything at or above the core threshold costs
    nothing.
    """
    if not sigs:
        return 0.0
    total = 0.0
    for strain, direction in sigs.items():
        if core.get(strain, (None,))[0] == direction:
            continue
        freq = consensus.get(strain, {}).get(direction, 0) / max(n_total, 1)
        total += max(0.0, 1.0 - freq / min_frac)
    return total / len(sigs)


def _best_set_score(sigs, control_n, cv_pct, core, off_frac=0.0):
    """Composite score for best-candidate selection.

    (1) Alignment (weight 3): how much of the CORE consensus this candidate
        recovers, in the agreed direction, as a fraction of the total core
        weight available. Bounded [0, 1] on purpose -- an earlier version
        summed matches without normalising, so a candidate that reached
        significance on everything simply accumulated points and beat a
        cleaner one on the strength of its false positives.
    (2) Completeness (weight 1): control replicates / 4, capped at 1. The
        control is the denominator for every ratio on the plate, so a missing
        control replicate costs more than a missing anything else.
    (3) Variance (weight 1.5): 1 - the candidate's CV percentile among all
        candidates, so the cleanest data scores 1 and the noisiest 0. Raw CV
        was useless here -- it spans too narrow a range to break ties.
    (4) Off-consensus penalty (weight 1): see `_off_consensus_penalty`. A
        candidate firing on strains nothing else agrees on is reporting noise,
        not signal, and without this term the noisiest candidate wins simply by
        accumulating false positives.
    """
    total_w = sum(f for _, f in core.values())
    matched = sum(f for s, (d, f) in core.items() if sigs.get(s) == d)
    alignment = matched / total_w if total_w else 0.0

    completeness = min(control_n / 4.0, 1.0)
    return (W_ALIGN * alignment + W_COMPLETE * completeness
            + W_VARIANCE * cv_pct - W_OFF * off_frac)


def _tc_experiment(cand, label: str) -> str:
    """What one best-candidate graph is called, e.g. "Set01 GLU 22 Hours".

    This is the `treatment` column, so R keys both the figure title and the
    figure filename on it. It carries the capture-tree label because the deck
    gathers every tree in the run: "GLU 22 Hours" alone would not say which set
    a slide came from.
    """
    return f"{label} {cand['medium']} {cand['tp_label']}"


def _medium_rank(medium: str):
    """Sort key putting media in the order they are run, not alphabetically."""
    return (MEDIUM_ORDER.index(medium) if medium in MEDIUM_ORDER
            else len(MEDIUM_ORDER), medium)


def _r_safe(s: str) -> str:
    """R's safe() from plot_spotting.R, so montage names match graph names."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


def build_tidy_for_candidate(cand, rows, cfg, cache_dir, label):
    """Tidy DataFrame for one candidate -- suitable for the R plotting script.

    Mirrors `spotting_batch.build_tidy` for a timecourse candidate: each shot
    contributes two rows of the dilution (rep 1+2 from plate 1, rep 3+4 from
    plate 2), and relative growth is normalised against the control column.
    """
    strains = cfg["strains"]
    control_col, ex_list = medium_cfg(cfg, cand["medium"])
    exclude = set(ex_list)
    experiment = _tc_experiment(cand, label)

    plates_m = []
    result_rows = []
    for shot in (cand["plate1"], cand["plate2"]):
        pd_ = _cached_measure(shot.path, shot.plate,
                              tuple(r + 1 for r in rows), cache_dir)
        plates_m.append(pd_)
        for rep_idx, row in enumerate(rows, start=1):
            rep_no = (shot.plate - 1) * 2 + rep_idx
            for col in range(sq.N_COLS):
                name = strains[col]
                if not name:
                    continue
                result_rows.append({
                    "experiment": experiment,
                    "treatment": experiment,
                    "set": "TC",
                    "plate": shot.plate,
                    "image": shot.path.name,
                    "replicate": f"rep{rep_no}",
                    "dilution_row": row + 1,
                    "dilution": ROW_NAMES[row % 3],
                    "strain_col": col + 1,
                    "strain": name,
                    "raw_growth": float(pd_.net[row, col]),
                    "artifact": bool(pd_.rim[row, col]),
                    "excluded": (col + 1) in exclude,
                    "is_control": (col + 1) == control_col,
                })

    full = pd.DataFrame(result_rows)
    if full.empty:
        return full

    keep = full[~full["excluded"]].copy()
    if keep.empty or control_col in exclude:
        return full

    # Same noise-aware floor the main pipeline uses: a fixed gray cutoff cannot
    # work across media whose signal ranges differ by an order of magnitude.
    noise = max([sq.bg_noise(p.bg_samples) for p in plates_m] or [0.0])
    min_control = max(sq.MIN_CONTROL_GRAY, sq.CONTROL_NOISE_MULT * noise)

    keep = sq.add_relative_growth(keep, control_col=control_col,
                                  group_keys=["experiment"],
                                  min_control=min_control)
    key_cols = ["experiment", "replicate", "strain_col"]
    added = [c for c in keep.columns if c not in full.columns]
    return full.merge(keep[key_cols + added], on=key_cols, how="left")


def _draw_montage_job(job):
    """Worker: draw one montage from already-cached measurements.

    Module level so a process pool can pickle it -- Windows has no fork.

    Drawing a montage costs a headless FIJI background subtraction per plate,
    measured at ~20 s each against 1.4 s to decode the photo, so a sheet is
    ~40 s of almost entirely idle CPU. Serially that is ~27 min for a 36-slide
    run. The work is per-photo and independent, exactly like `measure_all`, so
    it parallelises the same way.
    """
    (p1, pl1, p2, pl2, rows, strains, out_path, cache_dir, experiment) = job
    try:
        import spotting_montage as sm
        opts_m = replace(sq.MeasureOptions(),
                         quant_rows=tuple(r + 1 for r in rows))
        plates = [_cached_measure(Path(p), pl, tuple(r + 1 for r in rows),
                                  Path(cache_dir))
                  for p, pl in ((p1, pl1), (p2, pl2))]
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sm.build_montage(f"TC|{experiment}", plates, strains, opts_m,
                         Path(out_path), rep_label="Replicant")
        return (experiment, None)
    except Exception as e:
        return (experiment, f"{type(e).__name__}: {e}")


# How many montages share one FIJI batch inside a worker. Each plate's
# subtracted image is a 96 MB 32-bit TIFF on disk until it is consumed, so this
# trades temp space against JVM starts: 2 montages = 4 plates = ~380 MB per
# worker in flight, ~3 GB across 8. Raising it amortises the 4.3 s JVM start
# further and costs proportionally more temp space.
MONTAGES_PER_FIJI_BATCH = 2


def _run_r_job(job):
    """Worker: draw one tree's figures with plot_spotting.R.

    R is quick per call -- ~1.5 s of startup and library loading plus ~0.6 s a
    figure -- but it was run once per tree, in line, so twelve trees put 36 s of
    purely serial wall-clock in the middle of the run. Batching every tree into
    ONE R call only reaches 1.31x, because the fixed cost being amortised is
    small; running the calls side by side reaches about 5x instead.

    Output is captured rather than printed, so twelve concurrent R processes do
    not interleave their messages; the caller prints each block intact.
    """
    import contextlib
    import io

    csv_path, outdir, label = job
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            sb.run_r(Path(csv_path), Path(outdir))
        return (label, buf.getvalue(), None)
    except Exception as e:
        return (label, buf.getvalue(), f"{type(e).__name__}: {e}")


def run_r_all(jobs, workers: int):
    """Draw every tree's figures, in parallel. Returns the list of failures."""
    if not jobs:
        return []
    from concurrent.futures import ProcessPoolExecutor, as_completed

    errors = []
    t0 = time.perf_counter()
    if workers <= 1 or len(jobs) == 1:
        out = [_run_r_job(j) for j in jobs]
    else:
        out = []
        with ProcessPoolExecutor(max_workers=min(workers, len(jobs)),
                                 initializer=_init_worker) as pool:
            futs = [pool.submit(_run_r_job, j) for j in jobs]
            for fut in as_completed(futs):
                out.append(fut.result())
    for label, text, err in sorted(out, key=lambda t: t[0]):
        tail = [ln for ln in text.splitlines() if ln.strip()]
        if tail:
            print(f"  --- {label} ---")
            for ln in tail[-12:]:
                print("  " + ln)
        if err:
            errors.append((label, err))
            print(f"  ! R failed for {label}: {err}")
    print(f"    {len(jobs)} R call(s) in {time.perf_counter() - t0:.1f}s")
    return errors


def _draw_montage_chunk(chunk):
    """Worker: draw several montages, sharing FIJI runs between them.

    Every montage needs two plates background-subtracted, and each subtraction
    used to be its own headless FIJI. Starting Java is 4.3 s against 6.3 s of
    real work, so roughly half the montage pass was booting VMs. Here the
    plates of several montages go through ONE FIJI run and the results are
    handed to `build_montage` ready-made.

    Falls back to the per-plate path if FIJI is unavailable or a batch comes
    back short, so a montage is never lost to this optimisation.
    """
    import shutil
    import tifffile
    import spotting_montage as sm

    results = []
    for i in range(0, len(chunk), MONTAGES_PER_FIJI_BATCH):
        group = chunk[i:i + MONTAGES_PER_FIJI_BATCH]
        tmp = Path(tempfile.mkdtemp(prefix="tcbg_"))
        try:
            # Resolve every plate and its display radius from the CACHE, so the
            # FIJI radii are known before any image is touched.
            prepared, fiji_jobs = [], []
            for job in group:
                (p1, pl1, p2, pl2, rows, strains, out_path,
                 cache_dir, experiment) = job
                try:
                    opts_m = replace(sq.MeasureOptions(),
                                     quant_rows=tuple(r + 1 for r in rows))
                    plates = [_cached_measure(Path(p), pl,
                                              tuple(r + 1 for r in rows),
                                              Path(cache_dir))
                              for p, pl in ((p1, pl1), (p2, pl2))]
                    balls = [opts_m.resolve_ball_radius(
                        2 * float(pd_.radius) / sq.MEASURE_RADIUS_FRAC)
                        for pd_ in plates]
                    prepared.append((job, opts_m, plates, balls, strains))
                    if opts_m.bg_mode == "fiji":
                        for pd_, ball in zip(plates, balls):
                            fiji_jobs.append((pd_.ref.path, ball))
                except Exception as e:
                    results.append((job[8], f"{type(e).__name__}: {e}"))

            done = {}
            if fiji_jobs:
                try:
                    done = sq.fiji_subtract_background_batch(fiji_jobs, tmp)
                except Exception:
                    done = {}

            for job, opts_m, plates, balls, strains in prepared:
                out_path, experiment = Path(job[6]), job[8]
                try:
                    procs = []
                    for pd_, ball in zip(plates, balls):
                        tif = done.get(sq.fiji_batch_key(pd_.ref.path, ball))
                        procs.append(
                            tifffile.imread(str(tif)).astype(np.float64)
                            if tif else None)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    sm.build_montage(f"TC|{experiment}", plates, strains,
                                     opts_m, out_path, rep_label="Replicant",
                                     proc=procs)
                    results.append((experiment, None))
                except Exception as e:
                    results.append((experiment, f"{type(e).__name__}: {e}"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def draw_montages(jobs, workers: int):
    """Draw every pending montage, in parallel. Returns the list of failures."""
    if not jobs:
        return []
    from concurrent.futures import ProcessPoolExecutor, as_completed

    errors, done_n = [], 0
    total = len(jobs)
    t0 = time.perf_counter()
    # One chunk per worker, so each worker's FIJI runs are shared across the
    # montages it owns rather than restarted for every plate.
    n_chunks = max(1, min(workers, total))
    chunks = [jobs[i::n_chunks] for i in range(n_chunks)]
    chunks = [c for c in chunks if c]

    if workers <= 1:
        for c in chunks:
            for exp, err in _draw_montage_chunk(c):
                done_n += 1
                if err:
                    errors.append((exp, err))
                print(f"\r    {done_n}/{total}", end="", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_init_worker) as pool:
            futs = [pool.submit(_draw_montage_chunk, c) for c in chunks]
            for fut in as_completed(futs):
                for exp, err in fut.result():
                    done_n += 1
                    if err:
                        errors.append((exp, err))
                print(f"\r    {done_n}/{total}  ({len(chunks)} batch worker(s))",
                      end="", flush=True)
    print(f"\n    {total} montage(s) in {(time.perf_counter() - t0) / 60:.1f} min")
    for exp, err in errors[:10]:
        print(f"  ! montage failed for {exp}: {err}")
    return errors


def output_best_candidates(bests, cfg, cache, outdir, label):
    """Full pipeline output for the best candidate of EACH medium.

    One per medium, not one per set. Each medium is its own experiment -- the
    main pipeline draws a separate figure for every treatment-set combination
    -- so collapsing a set to a single winner silently discards the other two
    thirds of the work. On this data that dropped K-OAc and glycerol from eight
    of ten sets.

    All media go into ONE tidy CSV and ONE R invocation, exactly as the main
    pipeline does it: R keys its figures on `treatment` and emits one per
    medium, and the paired t-test table it writes covers them together instead
    of each run overwriting the last.

    Returns (slides, montage_jobs, r_job). The R figures and the montages
    are both drawn later, each in one parallel pass over the whole run, and
    the deck is assembled once by `build_best_deck` after they land.
    """
    outdir.mkdir(parents=True, exist_ok=True)

    # --- one tidy CSV covering every medium ---
    frames = []
    for cand, rows in bests:
        try:
            t = build_tidy_for_candidate(cand, rows, cfg, cache, label)
            if not t.empty:
                frames.append(t)
        except Exception as e:
            print(f"  ! {_tc_experiment(cand, label)}: {type(e).__name__}: {e}")
    if not frames:
        print("  ! no medium could be built -- skipping output")
        return [], [], None
    tidy = pd.concat(frames, ignore_index=True)
    tidy = sq.flag_outliers(tidy, group_keys=["experiment", "strain"])
    csv_path = outdir / "spotting_results_normalized.csv"
    tidy.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"  wrote {csv_path.name}  ({len(frames)} medium/media)")

    # --- R figures: queued, not drawn here ---
    # Deferred for the same reason as the montages. R is fast per call but was
    # run in line once per tree, so a twelve-tree run serialised 36 s of it.
    r_job = (str(csv_path), str(outdir), label)

    # --- queue one montage per medium, and the slide it belongs to ---
    # The montages are NOT drawn here. Each costs two headless FIJI
    # subtractions (~40 s) and they are independent, so they are collected
    # across every tree in the run and drawn in one parallel pass at the end;
    # doing them inline made a 36-slide run ~27 min of near-idle CPU.
    slides, jobs = [], []
    for cand, rows in bests:
        experiment = _tc_experiment(cand, label)
        safe = _r_safe(experiment)
        mont = outdir / "montages" / f"montage_{safe}.png"
        # R names its figure from the same treatment string, so the graph path
        # is derivable rather than searched for.
        # R has not run yet, so the graph cannot be checked here. `main` drops
        # any slide whose two pictures did not both land.
        graph = outdir / "figures" / f"spotting_{safe}.png"
        jobs.append((str(cand["plate1"].path), cand["plate1"].plate,
                     str(cand["plate2"].path), cand["plate2"].plate,
                     tuple(rows), cfg["strains"], str(mont), str(cache),
                     experiment))
        slides.append(((label, experiment), mont, graph))
    return slides, jobs, r_job


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
                    help="Where to keep measurements (default: .spotting_cache "
                         "inside the program folder, shared by every tree). "
                         "Nothing is ever written into the photo folders.")
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

    # A folder given to --out is CREATED, not silently ignored. Both the
    # per-tree output and the deck test `args.out.is_dir()`, so pointing at a
    # folder that does not exist yet used to drop the whole run back into the
    # default Results/Timecourse without saying so.
    if args.out and args.out.suffix.lower() != ".csv":
        args.out.mkdir(parents=True, exist_ok=True)

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
    slides, mont_jobs, r_jobs = [], [], []
    for i, r in enumerate(roots, 1):
        if len(roots) > 1:
            print(f"\n{'=' * 62}\n  [{i}/{len(roots)}]  {r.label}\n{'=' * 62}")
        cfg = cfgs.get(str(r.path))
        if not args.estimate and cfg is None:
            print(f"  skipped -- no strain panel for {r.label}")
            rc = rc or 1
            continue
        try:
            one = run_one(r, args, cfg, multi=len(roots) > 1, slides=slides,
                          mont_jobs=mont_jobs, r_jobs=r_jobs)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            # One bad tree must not abandon the rest of an overnight batch.
            print(f"  ! {r.label} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            one = 1
        rc = rc or one

    workers_out = args.workers or max(
        1, min(8, (multiprocessing.cpu_count() or 2) // 2))
    if r_jobs:
        print(f"\n{'=' * 62}\n  Drawing figures for {len(r_jobs)} tree(s) "
              f"with R on {min(workers_out, len(r_jobs))} worker(s) ...")
        run_r_all(r_jobs, workers_out)

    # Every montage in the run, drawn in one parallel pass. Each is two
    # headless FIJI subtractions and they share nothing, so this is where the
    # run's remaining wall-clock actually goes.
    if mont_jobs:
        print(f"\n{'=' * 62}\n  Drawing {len(mont_jobs)} montage(s) on "
              f"{workers_out} worker(s) ...")
        draw_montages(mont_jobs, workers_out)

    # A montage that failed leaves its slide half-built; drop those rather than
    # letting sp.build fail on a missing file.
    ready = [s for s in slides if s[1].exists() and s[2].exists()]
    if len(ready) != len(slides):
        for (lbl, exp), mont, _ in slides:
            if not mont.exists():
                print(f"  (no slide for {exp}: montage missing)")
    build_best_deck(ready, args)
    return rc


def build_best_deck(slides, args) -> "Path | None":
    """One deck for the whole run: each tree's best candidate, one slide each.

    Deliberately run-wide rather than per-set. The best set of Set01 is only
    interesting next to the best set of Set02, and a folder of thirteen
    one-slide PowerPoints has to be reassembled by hand before it can be
    looked at.

    Slides are ordered by tree label and then by medium in run order, so an
    extra take sits directly after the set it came from and each set's media
    read GLU, GLY, K-OAc rather than alphabetically.
    """
    if not slides:
        return None
    try:
        import spotting_pptx as sp
    except Exception as e:
        print(f"\n  (deck skipped -- could not import spotting_pptx: {e})")
        return None

    root = (args.out if args.out and args.out.is_dir()
            else sb.TIMECOURSE_RESULTS)
    out = root / "timecourse_best_sets.pptx"
    # The experiment already starts with the label, so the medium is what is
    # left after it -- enough to order GLU, GLY, K-OAc within each set.
    ordered = sorted(slides, key=lambda s: (
        s[0][0], _medium_rank(s[0][1][len(s[0][0]):].split()[0])))
    print(f"\n{'=' * 62}\n  Building the combined deck ...")
    try:
        sp.build(ordered, out, note=lambda label, exp: exp)
    except PermissionError:
        # Almost always the deck is open in PowerPoint, which locks it on
        # Windows. Losing a run's worth of montages to that is absurd, so the
        # deck goes to a numbered sibling and says so loudly.
        alt = None
        for i in range(2, 100):
            cand = out.with_name(f"{out.stem}_{i}{out.suffix}")
            if not cand.exists():
                alt = cand
                break
        try:
            sp.build(ordered, alt, note=lambda label, exp: exp)
        except Exception as e:
            print(f"  ! deck failed: {type(e).__name__}: {e}")
            return None
        print(f"  [!] {out.name} is LOCKED (open in PowerPoint?) and still "
              f"holds the PREVIOUS run.\n      This run was written to "
              f"{alt.name} instead -- close the old one and\n      delete it, "
              f"or you will be reading stale numbers.")
        return alt
    except Exception as e:
        print(f"  ! deck failed: {type(e).__name__}: {e}")
        return None
    print(f"  wrote {out}  ({len(ordered)} slide(s))")
    for (label, exp), _, _ in ordered:
        print(f"    {exp}")
    return out


def run_one(tree: "Tree", args, cfg, multi: bool = False, slides=None,
            mont_jobs=None, r_jobs=None) -> int:
    """Score one capture tree. `cfg` is its already-resolved strain panel.

    `slides` collects this tree's best-candidate (montage, graph) pairs for the
    run-wide PowerPoint and `mont_jobs` the montages still to be drawn; pass
    None to skip either.
    """
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

    cache = args.cache_dir or TIMECOURSE_CACHE
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
    cand_meta = []  # (cand, rows, strain_sigs, control_n) parallel to recs
    for c in cands:
        # The control column and the excluded strains are per MEDIUM: on K-OAc
        # the WT control does not grow, so several sets normalise against a
        # different strain entirely. Resolving it here means the CV and the
        # significance counts are computed against the same control the figure
        # will use.
        cc_m, ex_m = medium_cfg(cfg, c["medium"])
        for rows, nm in zip(ROW_SETS, ROW_NAMES):
            m = score_candidate(c, rows, cache, cfg["strains"], cc_m, ex_m)
            if not m:
                continue
            sigs = m.pop("_strain_sigs", {})
            ctrl_n = m.pop("control_n", 0)
            recs.append({"medium": c["medium"],
                         "medium_label": c["medium_label"],
                         "timepoint": c["tp_label"], "hours": c["hours"],
                         "plate1": c["plate1"].path.name,
                         "plate2": c["plate2"].path.name,
                         "dilution": nm, "control_n": ctrl_n, **m})
            cand_meta.append((c, rows, sigs, ctrl_n))
    if not recs:
        print("  Nothing could be scored -- every candidate had a control at "
              "or below the noise floor.", file=sys.stderr)
        return 1

    df = pd.DataFrame(recs)
    # Preserve original order index so the sorted df can look up cand_meta.
    df["_idx"] = range(len(df))

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

    # --- Cross-candidate strain significance consensus, PER MEDIUM ---
    # A strain's behaviour is a property of the strain ON THAT MEDIUM, so the
    # consensus is computed within each medium and never pooled across them.
    # Pooling actively misleads: on Set01, SOD2 is reduced in 11/24 glucose
    # candidates (46%) but only 3/29 glycerol ones (10%). Pooled that is 26%,
    # which put it under the core threshold AND had the off-consensus penalty
    # charge glucose candidates for detecting it -- enough to lose 20 Hours the
    # pick to a candidate with a worse CV.
    #
    # The CV percentile is per medium for the same reason: glycerol is noisier
    # than glucose throughout, so ranking a glucose candidate's spread against
    # glycerol's measures the medium, not the candidate. One winner is taken per
    # medium, so within-medium is the only comparison that has to be fair.
    core_by_medium, cons_by_medium, gate_by_medium = {}, {}, {}
    best_scores = [0.0] * len(df)
    floor = READABLE_CONTROL_MULT * sq.MIN_CONTROL_GRAY
    for medium, grp in df.groupby("medium", sort=False):
        pos = [df.index.get_loc(i) for i in grp.index]

        # Only candidates whose control actually resolved anything get a vote.
        vote = [p for p in pos if df["control_mean"].iloc[p] >= floor]
        enough = (len(vote) >= MIN_READABLE_CANDIDATES
                  and len(vote) >= MIN_READABLE_FRAC * len(pos))
        if not enough:
            vote = pos
        gate_by_medium[medium] = (len(vote), len(pos), enough)

        sigs_all = [cand_meta[int(df["_idx"].iloc[p])][2] for p in vote]
        cons_m = _aggregate_strain_significance(sigs_all)
        n_m = len(vote)
        core_m = core_strains(cons_m, n_m)
        cons_by_medium[medium] = (cons_m, n_m)
        core_by_medium[medium] = core_m

        cvs = grp["median_CV"]
        pct = (cvs.rank(ascending=False, pct=True) if n_m > 1
               else pd.Series([1.0] * n_m, index=cvs.index))
        for p, pv in zip(pos, pct):
            _, _, sigs, ctrl_n = cand_meta[int(df["_idx"].iloc[p])]
            off = _off_consensus_penalty(sigs, cons_m, n_m, core_m)
            best_scores[p] = _best_set_score(sigs, ctrl_n, float(pv),
                                             core_m, off)
    df["best_set_score"] = best_scores

    for medium in sorted(cons_by_medium, key=_medium_rank):
        cons_m, n_m = cons_by_medium[medium]
        core_m = core_by_medium[medium]
        n_vote, n_all, enough = gate_by_medium[medium]
        if not cons_m:
            continue
        if enough and n_vote < n_all:
            print(f"\n  {medium} strain significance "
                  f"({n_vote} of {n_all} candidates; {n_all - n_vote} had a "
                  f"control below {floor:.1f} gray and could not resolve an "
                  f"effect):")
        elif not enough:
            med_ctrl = float(df.loc[df["medium"] == medium, "control_mean"].median())
            print(f"\n  {medium} strain significance ({n_all} candidates):")
            print(f"    [!] this medium is dim THROUGHOUT (median control "
                  f"{med_ctrl:.1f} gray, floor {floor:.1f}); only {n_vote} "
                  f"candidate(s)\n        would have qualified, too few for a "
                  f"consensus, so none were excluded.\n        Treat these "
                  f"results with caution -- and check the control column is "
                  f"right\n        for this medium, since a control that does "
                  f"not grow looks exactly like this.")
        else:
            print(f"\n  {medium} strain significance ({n_m} candidates):")
        for strain, _, dirs in sorted(
                ((s, max(d.values()), d) for s, d in cons_m.items()),
                key=lambda x: x[1], reverse=True):
            parts = []
            for sign, word in ((-1, "reduced"), (1, "increased")):
                if dirs.get(sign):
                    parts.append(f"{word} {dirs[sign]}/{n_m} "
                                 f"({dirs[sign] * 100 // n_m}%)")
            mark = " <- core" if strain in core_m else ""
            print(f"    {strain:<12} {' | '.join(parts)}{mark}")
        if core_m:
            names = ", ".join(
                f"{s} {'reduced' if d < 0 else 'increased'}"
                for s, (d, _) in sorted(core_m.items(),
                                        key=lambda kv: -kv[1][1]))
            print(f"    core (>={int(CONSENSUS_MIN_FRAC * 100)}% agree): {names}")
        else:
            print(f"    (nothing reaches {int(CONSENSUS_MIN_FRAC * 100)}% -- "
                  f"this medium is picked on replicates and spread alone)")

    # One winner PER MEDIUM. Each medium is a separate experiment, so picking a
    # single winner for the whole tree would throw away the other media
    # entirely. Ties break toward the LOWER median CV -- the cleanest data, and
    # the only honest tiebreak available: alignment saturates at 1.0 once a
    # candidate recovers the whole core, so candidates routinely tie on score.
    ranked = df.sort_values(["best_set_score", "median_CV"],
                            ascending=[False, True])
    bests = []
    print("\n  Best candidate per medium:")
    for medium in sorted(df["medium"].unique(), key=_medium_rank):
        row = ranked[ranked["medium"] == medium].iloc[0]
        cand, rws, sigs, _ = cand_meta[int(row["_idx"])]
        bests.append((cand, rws))
        print(f"    {medium:>7}  {row['timepoint']:>10}  {row['dilution']:>6} "
              f"dilution   score {row['best_set_score']:.3f}   "
              f"CV {row['median_CV']:.2f}   "
              f"control {row['control_mean']:.1f} "
              f"({int(row['control_n'])}/4 reps)   "
              f"{int(row['n_significant'])}/{int(row['n_strains'])} significant")
        print(f"             {row['plate1']}  +  {row['plate2']}")
        if sigs:
            labels = {1: "increased", -1: "reduced"}
            core_m = core_by_medium.get(medium, {})
            # Core hits first: those are what the choice was actually made on.
            parts = [f"{s} ({labels.get(d, '?')})"
                     + ("" if core_m.get(s, (None,))[0] == d else " [not core]")
                     for s, d in sorted(sigs.items(),
                                        key=lambda kv: (core_m.get(kv[0], (None,))[0]
                                                        != kv[1], kv[0]))]
            print(f"             significant: {', '.join(parts)}")
    print("\n  Note: the core consensus is derived from these same candidates, "
          "so the\n     best sets' p-values are optimistic as a final claim. "
          "Confirm them by eye\n     before reporting them.")

    # Drop the internal index before writing to CSV.
    df = df.drop(columns=["_idx"]).reset_index(drop=True)

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
                rank_note=f"ranked by {args.rank_by}",
                resolve=lambda medium: medium_cfg(cfg, medium))
            if made:
                print(f"  wrote {len(made)} sheet(s) to {outdir / 'figures'}")
            else:
                print("  (no sheets drawn)")
        except Exception as e:
            # The CSV is already on disk. Losing the figures must not lose the
            # run: they are a convenience drawn over data that is already saved.
            print(f"  ! figures skipped: {type(e).__name__}: {e}")

    # Full output for each medium's winner. The slides and the montage work go
    # into the run-wide passes driven by `main`, not a deck here.
    print(f"\n  Building full output for {len(bests)} best candidate(s) ...")
    try:
        got, jobs, rj = output_best_candidates(
            bests, cfg, cache, outdir / "best", tree.label)
        if slides is not None:
            slides.extend(got)
        if mont_jobs is not None:
            mont_jobs.extend(jobs)
        if r_jobs is not None and rj is not None:
            r_jobs.append(rj)
    except Exception as e:
        print(f"  ! best-candidate output failed: {type(e).__name__}: {e}")

    print("\n  Every candidate is in the CSV -- the ranking is a suggestion, "
          "not a decision.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
