"""Owns the template and every mutation to it.

Undo is a stack of whole-template snapshots rather than inverse commands. A
plate is a few hundred frozen dataclasses, so a snapshot costs microseconds and
a deep stack is free -- while correct inverses for run fill, series fill, paint,
resize, plate add/delete/duplicate and control changes would be eight separate
opportunities to get it subtly wrong.

Snapshots are `schema.to_dict` output rather than deep copies: the same cost,
guaranteed free of shared references, and it exercises the serialiser on every
single edit, so a round-trip bug shows up in seconds of use instead of on the
next file load.
"""

from __future__ import annotations

from typing import Callable

from ..autofill import Plan, apply_plan
from ..model import Template
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


class EditorController:
    def __init__(
        self, template: Template, on_change: Callable[[], None] | None = None
    ) -> None:
        self.template = template
        self.on_change = on_change
        self.undo_stack = UndoStack()
        self._saved = to_dict(template)

    # -- plumbing ------------------------------------------------------------

    def snapshot(self) -> dict:
        return to_dict(self.template)

    def _sync_dilution_levels(self) -> None:
        """Keep the declared level count at least as deep as what is placed.

        Otherwise placing a fourth dilution in a three-level assay reports every
        one of those spots as out of range -- which is not a mistake by the
        person doing it, just two numbers disagreeing.
        """
        used = {p.dilution for _, _, _, p in self.template.all_placements()}
        if used:
            self.template.dilution.levels = max(
                self.template.dilution.levels, max(used) + 1
            )

    def _commit(self, before: dict, label: str) -> bool:
        """Record the edit if it actually changed anything."""
        self._sync_dilution_levels()
        if to_dict(self.template) == before:
            return False
        self.undo_stack.push(before, label)
        self._notify()
        return True

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    @property
    def dirty(self) -> bool:
        return to_dict(self.template) != self._saved

    def mark_saved(self) -> None:
        self._saved = to_dict(self.template)
        self._notify()

    def replace(self, template: Template) -> None:
        """Load a different template; history does not survive."""
        self.template = template
        self.undo_stack.clear()
        self._saved = to_dict(template)
        self._notify()

    # -- edits ---------------------------------------------------------------

    def apply(self, plate_id: str, plan: Plan, label: str) -> bool:
        before = self.snapshot()
        apply_plan(self.template.plate(plate_id), plan)
        return self._commit(before, label)

    def set_control(self, plate_id: str, slot: int) -> bool:
        before = self.snapshot()
        self.template.plate(plate_id).control_slot = slot
        return self._commit(before, f"Set control to slot {slot}")

    def resize(self, rows: int, cols: int) -> bool:
        before = self.snapshot()
        self.template.resize(rows, cols)
        return self._commit(before, f"Resize grid to {rows}x{cols}")

    def set_dilution_levels(self, levels: int) -> bool:
        before = self.snapshot()
        self.template.dilution.levels = levels
        return self._commit(before, f"Set {levels} dilution level(s)")

    def add_plate(self, plate_id: str, label: str = "") -> bool:
        before = self.snapshot()
        self.template.add_plate(plate_id, label)
        return self._commit(before, f"Add plate {plate_id}")

    def remove_plate(self, plate_id: str) -> bool:
        before = self.snapshot()
        plate = self.template.plate(plate_id)
        self.template.plates.remove(plate)
        return self._commit(before, f"Delete plate {plate_id}")

    def rename_plate(self, plate_id: str, label: str) -> bool:
        before = self.snapshot()
        self.template.plate(plate_id).label = label
        return self._commit(before, f"Rename plate {plate_id}")

    def set_name(self, name: str) -> bool:
        before = self.snapshot()
        self.template.name = name
        return self._commit(before, "Rename template")

    # -- history -------------------------------------------------------------

    def undo(self) -> str | None:
        result = self.undo_stack.undo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self.template = from_dict(snapshot)
        self._notify()
        return label

    def redo(self) -> str | None:
        result = self.undo_stack.redo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self.template = from_dict(snapshot)
        self._notify()
        return label
