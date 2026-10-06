"""Owns a data review in progress: its flags, its undo history, the photo shown.

Deliberately free of tkinter, so everything a person can do here is testable
without a display. The two slow things -- scanning the photo folder, and the
first read of the measurement engine -- are left to the caller to put on a
thread (`load`, `check_detection`, `geometry`).

Undo is a stack of whole-flags snapshots through `to_dict`/`from_dict`, the way
`results_review` does it: the flags are a few small dicts, and the serialiser
is then exercised on every single edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import catalog, flags as flagfile
from .catalog import Photo
from .flags import DataFlags

#: Offered for a spot. Free text is accepted too; these are the usual ones.
SPOT_REASONS = ("contamination", "bubble or debris", "smear / pinning error",
                "missing spot", "glare or reflection", "merged with neighbour")
#: Offered for a whole plate.
PLATE_REASONS = ("smeared or wet plate", "out of focus", "glare / bad lighting",
                 "cracked or damaged agar", "contamination", "mislabelled plate",
                 "duplicate photo")

#: What the photo list can be narrowed to.
FILTERS = ("All photos", "Not looked at", "Flagged", "Spots not located")


class UndoStack:
    def __init__(self, limit: int = 300) -> None:
        self.limit = limit
        self._undo: list[tuple[dict, str]] = []
        self._redo: list[tuple[dict, str]] = []

    def push(self, snapshot: dict, label: str) -> None:
        self._undo.append((snapshot, label))
        del self._undo[:-self.limit]
        self._redo.clear()

    def undo(self, current: dict) -> "tuple[dict, str] | None":
        if not self._undo:
            return None
        snapshot, label = self._undo.pop()
        self._redo.append((current, label))
        return snapshot, label

    def redo(self, current: dict) -> "tuple[dict, str] | None":
        if not self._redo:
            return None
        snapshot, label = self._redo.pop()
        self._undo.append((current, label))
        return snapshot, label

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)


class DataReviewController:
    def __init__(self, flags_path: Path,
                 on_change: "Callable[[], None] | None" = None) -> None:
        self.path = Path(flags_path)
        #: Raises ValueError for a file that is not a data review at all;
        #: better than opening it and overwriting it on the first save.
        self.flags: DataFlags = flagfile.load(self.path)
        self.experiment_path = flagfile.experiment_for(self.path, self.flags)
        self.on_change = on_change
        self.undo_stack = UndoStack()
        self._saved = self.flags.to_dict()
        self.last_change = ""

        self.experiment = None
        self.template = None
        self.layout = None                   # set by `check_detection`
        self.photos: list[Photo] = []
        self.error = ""
        self.loaded = False

        self.filter = FILTERS[0]
        self.current: "Photo | None" = None
        #: relpath -> bool, filled in by `check_detection`.
        self.detected: dict[str, bool] = {}
        self._geometry: dict[str, object] = {}
        self._cells: dict[str, dict] = {}

    # -- loading (slow; call off the UI thread) ------------------------------

    def load(self) -> None:
        """Read the experiment and scan its photo folder."""
        from experiments import intake, schema
        from experiments.run import load_template

        self.loaded = False
        self.error = ""
        self._geometry.clear()
        self._cells.clear()
        self.detected = {}
        self.layout = None
        if not self.experiment_path.exists():
            self.error = (f"The experiment this data review belongs to was not "
                          f"found:\n{self.experiment_path}")
            return
        try:
            e = schema.load(self.experiment_path)
        except schema.ExperimentError as exc:
            self.error = str(exc)
            return
        root = Path(e.photo_root)
        if not root.is_dir():
            self.error = f"The experiment's photo folder was not found:\n{root}"
            self.experiment = e
            return
        files, _complaints = intake.scan_images(root)
        res = intake.resolve(e, files)
        self.experiment = e
        self.template = load_template(e)
        self.photos = catalog.photos(e, res)
        if self.template is None:
            self.error = ("The experiment has no plate template, so which spot "
                          "is which cannot be said. Choose one in the "
                          "experiment designer.")
        keep = self.current.relpath if self.current else ""
        self.current = next((p for p in self.photos if p.relpath == keep),
                            self.photos[0] if self.photos else None)
        self.loaded = True

    def check_detection(self) -> None:
        """Which photos have their spots located. Imports the engine.

        Repeatable while detection runs: photos already known to be located
        are not looked at again, and those read as "not located" are forgotten
        so their grid is read afresh next time they are shown.
        """
        from . import spots

        if self.experiment is None or self.template is None:
            return
        if self.layout is None:
            self.layout = spots.layout_for(self.experiment, self.template)
        self.detected = {p.relpath: (self.detected.get(p.relpath)
                                     or spots.is_detected(p.path, self.layout))
                         for p in self.photos}
        for key in [k for k, v in list(self._geometry.items()) if v is None]:
            self._geometry.pop(key, None)

    def reload_flags(self) -> None:
        """Re-read the flags file, when nothing here is unsaved."""
        if self.dirty:
            return
        self.flags = flagfile.load(self.path)
        self._saved = self.flags.to_dict()
        self.undo_stack = UndoStack()

    def geometry(self, photo: Photo):
        """This photo's detected grid (`spots.Detected`), or None. Cached."""
        from . import spots

        if photo.relpath in self._geometry:
            return self._geometry[photo.relpath]
        found = None
        if self.layout is not None:
            found = spots.load(photo.path, self.layout, photo.plate)
        if len(self._geometry) > 64:
            self._geometry.pop(next(iter(self._geometry)))
        self._geometry[photo.relpath] = found
        self.detected[photo.relpath] = found is not None
        return found

    def cached_geometry(self, photo: "Photo | None"):
        """The grid only if it has already been read -- never blocks."""
        return self._geometry.get(photo.relpath) if photo else None

    def has_geometry(self, photo: "Photo | None") -> bool:
        return photo is not None and photo.relpath in self._geometry

    def cells(self, photo: Photo) -> dict:
        if photo.relpath not in self._cells:
            self._cells[photo.relpath] = (
                catalog.cells(self.experiment, self.template, photo)
                if self.experiment is not None else {})
        return self._cells[photo.relpath]

    # -- what is shown -------------------------------------------------------

    def matches(self, photo: Photo, which: "str | None" = None) -> bool:
        which = which or self.filter
        if which == "Not looked at":
            return not self.flags.is_reviewed(photo.relpath)
        if which == "Flagged":
            return self.flags.is_flagged(photo.relpath)
        if which == "Spots not located":
            return not self.detected.get(photo.relpath, False)
        return True

    def visible(self) -> list[Photo]:
        return [p for p in self.photos if self.matches(p)]

    def set_filter(self, which: str) -> None:
        if which in FILTERS and which != self.filter:
            self.filter = which
            self._notify()

    def show(self, photo: "Photo | None") -> None:
        if photo is not self.current:
            self.current = photo
            self._notify()

    def find(self, relpath: str) -> "Photo | None":
        return next((p for p in self.photos if p.relpath == relpath), None)

    def step(self, delta: int) -> bool:
        """Move through the photos the filter shows. False at either end."""
        pool = self.visible()
        if not pool:
            return False
        if self.current in pool:
            i = pool.index(self.current) + delta
        else:
            # The current photo was filtered out (just passed, say): carry on
            # from where it sat in the full list.
            i = self._insertion(pool) + (delta - 1 if delta > 0 else delta)
        if not 0 <= i < len(pool):
            return False
        self.show(pool[i])
        return True

    def _insertion(self, pool: list[Photo]) -> int:
        if self.current is None or self.current not in self.photos:
            return 0
        order = {p.relpath: i for i, p in enumerate(self.photos)}
        here = order[self.current.relpath]
        return sum(1 for p in pool if order[p.relpath] < here)

    def position(self) -> tuple[int, int]:
        """(1-based index of the current photo among those shown, how many)."""
        pool = self.visible()
        if self.current in pool:
            return pool.index(self.current) + 1, len(pool)
        return 0, len(pool)

    # -- decisions -----------------------------------------------------------

    def _snapshot(self) -> dict:
        return self.flags.to_dict()

    def _commit(self, before: dict, label: str) -> bool:
        if self._snapshot() == before:
            return False
        self.undo_stack.push(before, label)
        self.last_change = label
        self._notify()
        return True

    def _spot_name(self, photo: Photo, row: int, col: int) -> str:
        cell = self.cells(photo).get((row, col))
        where = f"row {row + 1}, column {col + 1}"
        return f"{cell.strain or 'empty slot'} rep {cell.replicate} ({where})" \
            if cell else where

    def toggle_spot(self, row: int, col: int, reason: str,
                    photo: "Photo | None" = None) -> bool:
        """Flag one spot (0-based photograph cell) or, if flagged, clear it."""
        photo = photo or self.current
        if photo is None:
            return False
        if self.flags.spot_reason(photo.relpath, row + 1, col + 1):
            return self.set_spot(row, col, None, photo)
        return self.set_spot(row, col, reason or "flagged in data review", photo)

    def set_spot(self, row: int, col: int, reason: "str | None",
                 photo: "Photo | None" = None) -> bool:
        photo = photo or self.current
        if photo is None:
            return False
        before = self._snapshot()
        self.flags.set_spot(photo.relpath, row + 1, col + 1, reason)
        name = self._spot_name(photo, row, col)
        label = (f"Flag {name}: {reason.strip()}" if reason and reason.strip()
                 else f"Clear the flag on {name}")
        return self._commit(before, label)

    def set_plate(self, reason: "str | None",
                  photo: "Photo | None" = None) -> bool:
        photo = photo or self.current
        if photo is None:
            return False
        before = self._snapshot()
        self.flags.set_plate(photo.relpath, reason)
        label = (f"Flag plate {photo.title}: {reason.strip()}"
                 if reason and reason.strip()
                 else f"Clear the plate flag on {photo.title}")
        return self._commit(before, label)

    def set_reviewed(self, value: bool = True,
                     photo: "Photo | None" = None) -> bool:
        photo = photo or self.current
        if photo is None:
            return False
        before = self._snapshot()
        self.flags.set_reviewed(photo.relpath, value)
        word = "Looked at" if value else "Not looked at"
        return self._commit(before, f"{word}: {photo.title}")

    def clear_photo(self, photo: "Photo | None" = None) -> bool:
        """Forget every flag on one photo, plate and spots alike."""
        photo = photo or self.current
        if photo is None:
            return False
        before = self._snapshot()
        self.flags.set_plate(photo.relpath, None)
        self.flags.spots.pop(flagfile.norm(photo.relpath), None)
        return self._commit(before, f"Clear every flag on {photo.title}")

    # -- counts --------------------------------------------------------------

    def counts(self) -> dict:
        relpaths = {p.relpath for p in self.photos}
        return {
            "photos": len(self.photos),
            "reviewed": sum(1 for p in self.photos
                            if self.flags.is_reviewed(p.relpath)),
            "plates": sum(1 for r in self.flags.plates if r in relpaths),
            "spots": sum(len(v) for r, v in self.flags.spots.items()
                         if r in relpaths),
            "detected": sum(1 for p in self.photos
                            if self.detected.get(p.relpath)),
            # Flags for photos that are no longer part of the experiment: kept,
            # never silently thrown away, but not counted as current.
            "orphaned": len((set(self.flags.plates) | set(self.flags.spots))
                            - relpaths) if self.loaded else 0,
        }

    # -- saving and history --------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self.flags.to_dict() != self._saved

    def save(self) -> Path:
        self.flags.experiment = self.experiment_path.name
        if self.experiment is not None and self.experiment.id:
            self.flags.experiment_id = self.experiment.id
        flagfile.save(self.path, self.flags)
        self._saved = self.flags.to_dict()
        self._notify()
        return self.path

    def _restore(self, snapshot: dict) -> None:
        self.flags = DataFlags.from_dict(snapshot)
        self._notify()

    def undo(self) -> "str | None":
        got = self.undo_stack.undo(self._snapshot())
        if got is None:
            return None
        self._restore(got[0])
        return got[1]

    def redo(self) -> "str | None":
        got = self.undo_stack.redo(self._snapshot())
        if got is None:
            return None
        self._restore(got[0])
        return got[1]

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()
