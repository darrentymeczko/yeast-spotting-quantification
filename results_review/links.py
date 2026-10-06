"""Find the capture tree a results folder came from.

Nothing in `Results/Timecourse/<set>/` records where the photos were. The
measurement cache is keyed on file NAME, SIZE and MTIME rather than path
(spotting_batch.py:490) -- which is what lets the photos be reorganised without
invalidating the cache, but also means the real file has to be in hand before a
cached measurement can be looked up. So the link has to be stored.

It is stored twice, on purpose:

* in the set's own `review.json`, so a review folder is self-describing and
  moving it keeps the link, and
* in a per-user `links.json`, so the FIRST time a set is opened the folder can
  usually be found without asking at all.

Browsing needs none of this. Only rebuilding a candidate's per-spot data does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import PROJECT_ROOT

#: Remembered folders, beside the code rather than in the results tree: it is a
#: property of this machine, not of the experiment.
LINKS_FILE = PROJECT_ROOT / ".results_review_links.json"

CONFIG_NAME = "timecourse_config.json"

#: Where capture trees have been found before. Tried in order, and only as a
#: guess -- a guess is always checked against the set's own folder name before
#: it is offered, and the person can always point somewhere else.
SEARCH_HINTS = [
    Path.home() / "OneDrive - The University of Western Ontario" / "Vault"
    / "2.  Projects" / "Martin Lab" / "Data" / "Spotting Assays",
    PROJECT_ROOT / "Spotting Assays",
]


def _load() -> dict:
    """`{"photos": {label: root}, "last_set": path}`.

    An older flat `{label: root}` file is read as the photos map, so upgrading
    does not lose the folders somebody has already located by hand.
    """
    try:
        data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"photos": {}}
    if not isinstance(data, dict):
        return {"photos": {}}
    if "photos" not in data:
        return {"photos": {k: v for k, v in data.items()
                           if isinstance(v, str)}}
    if not isinstance(data.get("photos"), dict):
        data["photos"] = {}
    return data


def _save(data: dict) -> None:
    try:
        LINKS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass          # a remembered path is a convenience; never fail a run for it


def remembered(label: str) -> "Path | None":
    """The folder last used for this set, if it is still there."""
    raw = _load()["photos"].get(label)
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def remember(label: str, root: Path) -> None:
    data = _load()
    data["photos"][str(label)] = str(Path(root))
    _save(data)


def forget(label: str) -> None:
    data = _load()
    if data["photos"].pop(str(label), None) is not None:
        _save(data)


# --- which set was open last ------------------------------------------------
# Double-clicking the launcher should land where you left off. Reviewing a set
# takes more than one sitting, and choosing the same folder out of a list of
# fifteen every time is the kind of small friction that stops a tool being used.


def last_set() -> "Path | None":
    """The results folder opened most recently, if it is still a results folder."""
    raw = _load().get("last_set")
    if not raw:
        return None
    p = Path(raw)
    return p if (p / "timecourse_candidates.csv").exists() else None


def remember_set(results_dir: Path) -> None:
    data = _load()
    data["last_set"] = str(Path(results_dir))
    _save(data)


# --- how the sheet is shown -------------------------------------------------
# A way of looking, not a fact about any one set: it carries across sets and
# sittings, and the PowerPoint export lays its slides out the same way.

SHEET_VIEWS = ("fit", "aligned")


def sheet_view() -> tuple[str, bool]:
    """(view, rotated) the sheet was last shown in; ("fit", False) at first."""
    raw = _load().get("sheet_view")
    raw = raw if isinstance(raw, dict) else {}
    view = raw.get("view")
    return (view if view in SHEET_VIEWS else SHEET_VIEWS[0],
            raw.get("rotate") is True)


def remember_sheet_view(view: str, rotate: bool) -> None:
    if view not in SHEET_VIEWS or sheet_view() == (view, bool(rotate)):
        return
    data = _load()
    data["sheet_view"] = {"view": view, "rotate": bool(rotate)}
    _save(data)


# ---------------------------------------------------------------------------


def is_capture_tree(path: Path) -> bool:
    """True if this looks like the tree `spotting_timecourse.discover` walks.

    Checked structurally -- a timepoint folder holding a medium folder holding a
    plate folder with images -- rather than by importing `discover`, which would
    drag the whole measurement stack into the browser. Loose on purpose: the
    authoritative answer comes from `discover` itself at rebuild time, and this
    only has to be good enough to stop somebody linking their Desktop.
    """
    path = Path(path)
    if not path.is_dir():
        return False
    if (path / CONFIG_NAME).exists():
        return True
    try:
        for tp in path.iterdir():
            if not tp.is_dir():
                continue
            for med in tp.iterdir():
                if not med.is_dir():
                    continue
                for plate in med.iterdir():
                    if plate.is_dir() and any(
                            f.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif",
                                                 ".tiff")
                            for f in plate.iterdir() if f.is_file()):
                        return True
    except OSError:
        return False
    return False


#: How deep `spotting_timecourse.scan_extras` looks for an extra session.
_EXTRA_DEPTH = 4


def _extra_label(root: Path, sub: Path) -> str:
    """Rebuild the results label `spotting_timecourse` gives an extra session.

    Mirrors spotting_timecourse.py:342 -- `f"{tree.label} - {'-'.join(rel.parts)}"`.
    """
    rel = sub.relative_to(root)
    return f"{root.name} - {'-'.join(rel.parts)}"


def _find_extra(root: Path, label: str) -> "Path | None":
    """The sub-session folder inside `root` whose results label is `label`.

    Found by WALKING and rebuilding each label, not by parsing the label apart.
    Parsing is genuinely ambiguous: the parts are joined with "-", and a part may
    itself contain " - ". `Set05 - Emily-Set 1 - Practice` is
    `Set05/Emily/Set 1 - Practice`, while `Set05 - Andrea-Set 1` is
    `Set05/Andrea/Set 1` -- no split rule gets both right, but generating the
    label from each candidate folder and comparing gets both right by
    construction.
    """

    def walk(d: Path, depth: int) -> "Path | None":
        if depth > _EXTRA_DEPTH:
            return None
        try:
            subs = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return None
        for sub in subs:
            if sub.name.startswith("."):
                continue
            if _extra_label(root, sub) == label and is_capture_tree(sub):
                return sub
            got = walk(sub, depth + 1)
            if got is not None:
                return got
        return None

    return walk(root, 1)


def _search(hint: Path, label: str) -> "Path | None":
    """Look for `label`'s capture tree directly under `hint`.

    A plain set is a folder of that name. An extra session is a folder INSIDE
    its set, and is never resolved to the parent set instead: those are
    different photographs, and quietly linking the wrong ones would produce a
    confident, wrong answer rather than an error.
    """
    p = hint / label
    if is_capture_tree(p):
        return p
    if " - " not in label:
        return None
    head = label.split(" - ", 1)[0]
    root = hint / head
    return _find_extra(root, label) if root.is_dir() else None


def guess(label: str) -> "Path | None":
    """Best guess at the capture tree for a results folder, or None.

    Order: what was used last, then the known data roots. Every hit is checked
    with `is_capture_tree`, so a folder that merely has the right name is not
    offered.
    """
    got = remembered(label)
    if got and is_capture_tree(got):
        return got

    for hint in SEARCH_HINTS:
        if not hint.is_dir():
            continue
        got = _search(hint, label)
        if got is not None:
            return got
        # One level down: the sets usually sit in a project folder such as
        # "Deletion Strains" rather than loose in the data root.
        try:
            subs = sorted(d for d in hint.iterdir() if d.is_dir())
        except OSError:
            continue
        for sub in subs:
            got = _search(sub, label)
            if got is not None:
                return got
    return None


def from_run_info(results_dir: "Path | None") -> "Path | None":
    """The photo folder `experiment.json` recorded, if it is still there.

    Better than any guess: the experiment layer knows exactly which folder it
    read, including layouts `is_capture_tree` would not recognise, so this is
    checked only for existence rather than for shape.
    """
    if results_dir is None:
        return None
    from .discovery import load_run_info

    info = load_run_info(results_dir)
    if info is None or not info.photo_root:
        return None
    p = Path(info.photo_root)
    return p if p.is_dir() else None


def resolve(label: str, stored: str = "", results_dir: "Path | None" = None
            ) -> "Path | None":
    """The capture tree for this set: what was recorded, else a guess.

    Order of authority, most specific first:

    1. what the review itself recorded -- it travels with the results,
    2. what the run recorded in `experiment.json` -- the folder actually read,
    3. a guess from the known data roots, for a set never reviewed or run
       through the experiment layer.

    `SEARCH_HINTS` is therefore now the last resort rather than the main
    mechanism, which matters because it is a literal path to one person's
    OneDrive folder.
    """
    if stored:
        p = Path(stored)
        from .discovery import load_run_info
        info = load_run_info(results_dir) if results_dir is not None else None
        recorded = info is not None and "resolved_photos" in info.pipeline_config
        if p.is_dir() and (recorded or is_capture_tree(p)):
            return p
    recorded = from_run_info(results_dir)
    if recorded is not None:
        return recorded
    return guess(label)


def load_config(root: Path) -> dict:
    """The strain panel for a capture tree (`timecourse_config.json`).

    The same file `spotting_timecourse.load_or_ask_config` writes, read rather
    than re-asked, so the names cannot drift between the two tools.
    """
    path = Path(root) / CONFIG_NAME
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing -- run the timecourse pipeline on this folder "
            f"once so it writes the strain panel, or point at the folder that "
            f"has it.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg.get("strains"):
        raise ValueError(f"{path} names no strains.")
    return cfg


def describe(root: "Path | None") -> str:
    """Short, readable form of a linked folder for the header bar."""
    if root is None:
        return "not linked"
    p = Path(root)
    try:
        return str(p.relative_to(Path.home()))
    except ValueError:
        parts = p.parts
        return str(Path(*parts[-3:])) if len(parts) > 3 else str(p)


def photos_available(root: "Path | None") -> tuple[bool, str]:
    """Whether the photos are actually readable, not merely listed.

    OneDrive lists online-only files with their real size but reading one blocks
    on a download -- or fails outright when offline. A placeholder reports zero
    blocks on disk, which is cheap to check and is the difference between "this
    will take a moment" and "this cannot work".
    """
    if root is None:
        return False, "no capture folder is linked"
    try:
        for tp in sorted(Path(root).iterdir()):
            if not tp.is_dir():
                continue
            for f in tp.rglob("*"):
                if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".tif",
                                            ".tiff"):
                    continue
                st = f.stat()
                blocks = getattr(st, "st_blocks", None)
                if blocks == 0 and st.st_size > 0:
                    return False, (f"{f.name} is online-only; make the folder "
                                   f"available offline in OneDrive first")
                return True, ""
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    return False, "no photos found under the linked folder"


def default_start_dir() -> Path:
    """Where a 'locate the photos' dialog should open."""
    for hint in SEARCH_HINTS:
        if hint.is_dir():
            return hint
    return Path(os.path.expanduser("~"))
