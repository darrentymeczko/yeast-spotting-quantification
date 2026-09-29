"""Owns the experiment and every mutation to it.

Undo is a stack of whole-experiment snapshots, for the same reasons
`plate_template.gui.controller` gives: an experiment is small, a snapshot costs
microseconds, and correct inverses for renaming a strain, moving a control,
adding a condition, re-inferring a profile and bulk-assigning photos would be
half a dozen separate opportunities to get it subtly wrong.

Snapshots are `schema.to_dict` output rather than deep copies: the same cost,
guaranteed free of shared references, and it exercises the serialiser on every
edit, so a round-trip bug shows up in seconds of use rather than on the next
file load.

The photo resolution is deliberately NOT part of a snapshot. It is derived from
the folder, which undo cannot put back, and re-deriving it is cheap; it is
recomputed whenever something it depends on changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import BUNDLE, REPO, intake
from ..model import PHOTO_TOPS, Condition, Experiment
from ..schema import from_dict, to_dict


class UndoStack:
    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self._undo: list[tuple[dict, str]] = []
        self._redo: list[tuple[dict, str]] = []

    def push(self, snapshot: dict, label: str) -> None:
        self._undo.append((snapshot, label))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self, current: dict) -> tuple[dict, str] | None:
        if not self._undo:
            return None
        snapshot, label = self._undo.pop()
        self._redo.append((current, label))
        return snapshot, label

    def redo(self, current: dict) -> tuple[dict, str] | None:
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

    @property
    def undo_label(self) -> str:
        return self._undo[-1][1] if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1][1] if self._redo else ""

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


class ExperimentController:
    def __init__(
        self, experiment: Experiment, on_change: Callable[[], None] | None = None
    ) -> None:
        self.experiment = experiment
        self.on_change = on_change
        self.undo_stack = UndoStack()
        self._saved = to_dict(experiment)

        #: The plate template this experiment is bound to, once loaded.
        self.template = None
        self.template_error = ""
        #: What is in the photo folder, and what it was understood to mean.
        self.files: list[intake.ImageFile] = []
        self.resolution: intake.Resolution | None = None
        self.scan_error = ""
        self.profile_report: list[str] = []

    # -- plumbing ------------------------------------------------------------

    def snapshot(self) -> dict:
        return to_dict(self.experiment)

    def _commit(self, before: dict, label: str, *, rescan: bool = False) -> bool:
        """Record the edit if it actually changed anything."""
        if to_dict(self.experiment) == before:
            return False
        self.undo_stack.push(before, label)
        if rescan:
            self.reresolve()
        self._notify()
        return True

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    @property
    def dirty(self) -> bool:
        return to_dict(self.experiment) != self._saved

    def mark_saved(self) -> None:
        self._saved = to_dict(self.experiment)
        self._notify()

    def replace(self, experiment: Experiment) -> None:
        """Load a different experiment; history does not survive."""
        self.experiment = experiment
        self.undo_stack.clear()
        self._saved = to_dict(experiment)
        self.files = []
        self.resolution = None
        self.scan_error = ""
        self.profile_report = []
        self.load_template()
        self.rescan()

    # -- the plate template --------------------------------------------------

    def load_template(self, base: Path | None = None) -> None:
        """Load the bound plate template. Never raises; errors are reported."""
        from plate_template.schema import TemplateError, load

        self.template, self.template_error = None, ""
        raw = self.experiment.template_path
        if not raw:
            return
        candidates = [Path(raw)]
        if base is not None:
            candidates.append(Path(base) / raw)
        candidates.extend((REPO / raw, BUNDLE / raw))
        for path in candidates:
            if path.exists():
                try:
                    self.template = load(path)
                except TemplateError as exc:
                    self.template_error = str(exc)
                return
        self.template_error = f"not found: {raw}"

    def bind_template(self, path: Path) -> bool:
        """Point the experiment at a plate template file."""
        from plate_template.schema import TemplateError, load

        try:
            template = load(Path(path))
        except TemplateError as exc:
            self.template_error = str(exc)
            self._notify()
            return False

        before = self.snapshot()
        self.experiment.template_id = template.id
        self.experiment.template_path = _relative_to_repo(path)
        # Grow the panel to the template's shape, so every slot has a row to
        # type a name into. Existing names keep their slots.
        slots = template.sample_slots()
        while len(self.experiment.strains) < slots:
            self.experiment.strains.append(None)
        if len(self.experiment.strains) > slots and not any(
            self.experiment.strains[slots:]
        ):
            del self.experiment.strains[slots:]
        if self.experiment.control_slot is None:
            self.experiment.control_slot = next(
                (p.control_slot for p in template.plates if p.control_slot), None
            )
        self.template, self.template_error = template, ""
        return self._commit(before, f"Use template {template.name!r}")

    # -- the panel -----------------------------------------------------------

    def set_name(self, name: str) -> bool:
        before = self.snapshot()
        self.experiment.name = name.strip() or "Untitled"
        return self._commit(before, "Rename experiment")

    def set_strain(self, slot: int, name: str) -> bool:
        before = self.snapshot()
        self.experiment.set_strain(slot, name.strip())
        return self._commit(before, f"Name slot {slot}")

    def set_control(self, slot: int) -> bool:
        before = self.snapshot()
        self.experiment.control_slot = slot
        return self._commit(before, f"Set control to slot {slot}")

    def set_mode(self, mode: str) -> bool:
        before = self.snapshot()
        self.experiment.mode = mode
        # The required facets differ by mode, so what resolves changes too.
        return self._commit(before, f"Set mode to {mode}", rescan=True)

    def set_photo_top(self, edge: str) -> bool:
        """Set which photograph edge represents the experiment's top."""
        edge = str(edge).strip().lower()
        if edge not in PHOTO_TOPS:
            raise ValueError(f"unknown photo orientation {edge!r}")
        before = self.snapshot()
        self.experiment.photo_top = edge
        return self._commit(before, f"Set experiment top to photo {edge}")

    # -- conditions ----------------------------------------------------------

    def add_condition(self, code: str, label: str = "") -> bool:
        code = code.strip()
        if not code or self.experiment.has_condition(code):
            return False
        before = self.snapshot()
        self.experiment.conditions.append(Condition(code, label.strip() or code))
        return self._commit(before, f"Add condition {code}")

    def remove_condition(self, code: str) -> bool:
        if not self.experiment.has_condition(code):
            return False
        before = self.snapshot()
        self.experiment.conditions.remove(self.experiment.condition(code))
        return self._commit(before, f"Remove condition {code}")

    def set_condition_control(self, code: str, slot: int | None) -> bool:
        before = self.snapshot()
        self.experiment.condition(code).control_slot = slot
        where = f"slot {slot}" if slot is not None else "the experiment's"
        return self._commit(before, f"{code}: control is {where}")

    def set_condition_exclude(self, code: str, slots) -> bool:
        before = self.snapshot()
        self.experiment.condition(code).exclude = tuple(sorted(set(slots)))
        return self._commit(before, f"{code}: excluded slots")

    def adopt_found_conditions(self) -> bool:
        """Declare every condition the photos turned out to hold."""
        if self.resolution is None:
            return False
        before = self.snapshot()
        labels = {
            r.condition: r.condition_label
            for r in self.resolution.usable()
            if r.condition
        }
        for code in self.resolution.conditions():
            if not self.experiment.has_condition(code):
                self.experiment.conditions.append(
                    Condition(code, labels.get(code, code))
                )
        return self._commit(before, "Add the conditions found in the photos")

    # -- photos --------------------------------------------------------------

    def set_photo_root(self, root: Path, *, infer: bool = True) -> bool:
        """Point at a photo folder, and read what its layout appears to mean."""
        before = self.snapshot()
        self.experiment.photo_root = str(Path(root))
        # Hand corrections are keyed on paths relative to the old root, so they
        # are meaningless against a different one. Dropped rather than silently
        # re-applied to whatever file now happens to sit at that path.
        self.experiment.overrides.clear()
        self.experiment.ignored.clear()
        self.rescan(infer=infer)
        return self._commit(before, f"Use photos in {Path(root).name}")

    def rescan(self, *, infer: bool = False) -> None:
        """Re-read the folder from disk, optionally re-inferring the layout."""
        self.files, self.resolution, self.scan_error = [], None, ""
        root = Path(self.experiment.photo_root)
        if not self.experiment.photo_root:
            return
        if not root.is_dir():
            self.scan_error = f"folder not found: {root}"
            return
        self.files, complaints = intake.scan_images(root)
        if complaints:
            self.scan_error = "; ".join(complaints[:3])
        if not self.files:
            self.scan_error = f"no images under {root}"
            return
        if infer or not self.experiment.profile.rules:
            profile, report = intake.infer_profile(
                self.files, mode=self.experiment.mode
            )
            self.experiment.profile = profile
            self.profile_report = report
        self.reresolve()

    def reresolve(self) -> None:
        """Re-read what each photo means, without touching the disk."""
        if self.files:
            self.resolution = intake.resolve(self.experiment, self.files)

    def set_set_key(self, key: str | None) -> bool:
        before = self.snapshot()
        self.experiment.set_key = key or None
        return self._commit(before, "Choose which set these photos are",
                            rescan=True)

    def override(self, relpath: str, facet: str, value) -> bool:
        """Correct one photo by hand. Always beats the profile."""
        before = self.snapshot()
        entry = self.experiment.overrides.setdefault(relpath, {})
        if value in (None, ""):
            entry.pop(facet, None)
            if not entry:
                self.experiment.overrides.pop(relpath, None)
        else:
            entry[facet] = value
        return self._commit(before, f"Set {facet} by hand", rescan=True)

    def override_many(self, relpaths, facet: str, value) -> bool:
        """The same correction across a selection -- one undo step, not twenty."""
        # Materialised first: callers pass generators, and the count below would
        # otherwise read 0 off an already-consumed one.
        paths = list(relpaths)
        before = self.snapshot()
        for relpath in paths:
            entry = self.experiment.overrides.setdefault(relpath, {})
            if value in (None, ""):
                entry.pop(facet, None)
                if not entry:
                    self.experiment.overrides.pop(relpath, None)
            else:
                entry[facet] = value
        return self._commit(before, f"Set {facet} on {len(paths)} photo(s)",
                            rescan=True)

    def set_ignored(self, relpaths, ignored: bool) -> bool:
        paths = list(relpaths)
        before = self.snapshot()
        current = set(self.experiment.ignored)
        if ignored:
            current.update(paths)
        else:
            current.difference_update(paths)
        self.experiment.ignored = sorted(current)
        verb = "Leave out" if ignored else "Include"
        return self._commit(before, f"{verb} {len(paths)} photo(s)", rescan=True)

    # -- picked photographs (QUANTIFY mode) ----------------------------------

    def candidates(self) -> list[str]:
        """Every image under the photo folder, as relative paths.

        Not filtered by the naming profile: in handpicked mode the user is
        choosing from what is physically there, and a folder whose filenames
        encode nothing is the normal case, not a fault.
        """
        return [f.relpath for f in self.files]

    def plate_ids(self) -> list[str]:
        """The plate slots of the bound template, in the order it declares."""
        if self.template is None:
            return []
        return [str(p.id) for p in self.template.plates]

    def plate_label(self, plate_id: str) -> str:
        if self.template is None:
            return f"Plate {plate_id}"
        for plate in self.template.plates:
            if str(plate.id) == str(plate_id):
                return plate.label or f"Plate {plate.id}"
        return f"Plate {plate_id}"

    def slots(self) -> list[tuple[str, str]]:
        """(condition, plate) pairs needing a photograph, in reading order."""
        return [(c.code, plate_id)
                for c in self.experiment.conditions
                for plate_id in self.plate_ids()]

    def dilution_levels(self) -> list[tuple[int, str]]:
        """Every dilution level the template declares, as (index, display name).

        Read from the template rather than assumed, so a design with six levels
        offers six. Levels are identified by index; the name is for reading.
        """
        from .. import geometry

        return geometry.levels(self.template)

    def dilution_display(self, code: str, plate_id: str) -> str:
        """How one plate's chosen level should read in a table, or "-"."""
        from .. import geometry

        index = geometry.resolve_level(
            self.template, self.experiment.dilution_for(code, plate_id))
        if index is None:
            return "-"
        return geometry.level_display(self.template, index)

    def dilution_index(self, code: str, plate_id: str) -> int | None:
        from .. import geometry

        return geometry.resolve_level(
            self.template, self.experiment.dilution_for(code, plate_id))

    def photo_path(self, relpath: str) -> Path:
        return Path(self.experiment.photo_root) / relpath

    def set_pick(self, code: str, plate_id: str, relpath: str | None) -> bool:
        before = self.snapshot()
        self.experiment.set_pick(code, plate_id, relpath)
        what = Path(relpath).name if relpath else "nothing"
        return self._commit(before, f"{code} plate {plate_id}: use {what}")

    def set_plate_dilution(self, code: str, plate_id: str, index: int | None) -> bool:
        """Choose a plate's dilution level, stored as its 0-based index.

        The index rather than the name: a template may rename its levels or
        declare more than there are words for, and a stored name would then mean
        a different row -- or nothing at all.
        """
        from .. import geometry

        before = self.snapshot()
        self.experiment.set_dilution_for(code, plate_id, index)
        what = (geometry.level_display(self.template, index)
                if index is not None else "nothing")
        return self._commit(before,
                            f"{code} plate {plate_id}: score dilution {what}")


    # -- findings ------------------------------------------------------------

    def issues(self) -> list:
        """Everything wrong with the experiment as it stands."""
        from ..validate import validate

        found = list(validate(self.experiment, self.template))
        if self.resolution is not None:
            found += intake.check_resolution(
                self.experiment, self.resolution, self.template
            )
        return found

    # -- history -------------------------------------------------------------

    def undo(self) -> str | None:
        result = self.undo_stack.undo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self.experiment = from_dict(snapshot)
        self.reresolve()
        self._notify()
        return label

    def redo(self) -> str | None:
        result = self.undo_stack.redo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self.experiment = from_dict(snapshot)
        self.reresolve()
        self._notify()
        return label


def _relative_to_repo(path: Path) -> str:
    """Store a template path relative to the repo when it lives inside it.

    A relative path survives the whole folder being moved or copied to another
    machine, which is how these tools actually travel.
    """
    resolved = Path(path).resolve()
    # BUNDLE too: the shipped templates live there, and when packaged that is a
    # temporary folder, so an absolute path to it would be dead on the next run.
    for root in (REPO, BUNDLE):
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    return str(path)
