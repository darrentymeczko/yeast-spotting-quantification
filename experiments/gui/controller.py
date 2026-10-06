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

from dataclasses import replace
from contextlib import contextmanager
from copy import deepcopy
import re
from pathlib import Path
from typing import Callable

from .. import BUNDLE, REPO, intake
from ..model import (
    DATA_OUTPUT,
    OUTPUTS,
    P_ADJUST_METHODS,
    PHOTO_TOPS,
    POSTHOC_METHODS,
    STATISTICAL_TESTS,
    Condition,
    Experiment,
    StrainGroup,
)
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
        self._template_base: Path | None = None
        #: What is in the photo folder, and what it was understood to mean.
        self.files: list[intake.ImageFile] = []
        self.resolution: intake.Resolution | None = None
        self.scan_error = ""
        self.profile_report: list[str] = []
        self.active_group: str | None = None
        self._transaction_depth = 0
        #: relpath -> reason: plates the data review flagged as bad. Kept up
        #: to date by the window, which knows where the review is saved; the
        #: multi-step checks ask nothing of a plate it will leave out anyway.
        self.review_plates: dict[str, str] = {}

    @property
    def panel_experiment(self) -> Experiment:
        """The panel currently edited, not a filter on which panels will run."""
        groups = self.experiment.strain_groups
        if not groups:
            self.active_group = None
            return self.experiment
        if self.active_group not in groups:
            self.active_group = next(iter(groups))
        return self.experiment.for_group(self.active_group)

    def select_group(self, key: str) -> None:
        if key not in self.experiment.strain_groups:
            raise ValueError("Choose a defined strain group.")
        self.active_group = key
        self._notify()

    def add_strain_group(self, key: str, *, first_key: str | None = None) -> bool:
        key = key.strip()
        first_key = first_key.strip() if first_key else None
        names = [key] + ([first_key] if first_key else [])
        if any(not n or "|" in n or any(ord(c) < 32 for c in n) for n in names):
            raise ValueError("Use a group name without | or control characters.")
        e = self.experiment
        if key in e.strain_groups or first_key == key:
            raise ValueError("That strain group already exists; choose a different name.")
        if not e.strain_groups and not first_key:
            raise ValueError("Name the existing strain group first.")
        before = self.snapshot()
        if not e.strain_groups:
            e.strain_groups[first_key] = StrainGroup(
                list(e.strains), e.control_slot, deepcopy(e.picks),
                {c.code: deepcopy(c) for c in e.conditions})
        slots = self.template.sample_slots() if self.template else e.slot_count()
        e.strain_groups[key] = StrainGroup(
            [None] * slots, conditions={c.code: Condition(c.code, c.label) for c in e.conditions})
        e.set_key = None
        self.active_group = key
        return self._commit(before, f"Add strain group {key}", rescan=True)

    def _panel_condition(self, code: str):
        panel = self.panel_experiment
        if not self.experiment.strain_groups:
            return self.experiment.condition(code)
        group = self.experiment.strain_groups[self.active_group]
        return group.conditions.setdefault(code, deepcopy(panel.condition(code)))

    # -- plumbing ------------------------------------------------------------

    def snapshot(self) -> dict:
        return to_dict(self.experiment)

    def _commit(self, before: dict, label: str, *, rescan: bool = False) -> bool:
        """Record the edit if it actually changed anything."""
        if to_dict(self.experiment) == before:
            return False
        if rescan:
            self.reresolve()
        if self._transaction_depth:
            return True
        self.undo_stack.push(before, label)
        self._notify()
        return True

    @contextmanager
    def transaction(self, label: str):
        """A compound user action has one history entry and one refresh."""
        before = self.snapshot()
        self._transaction_depth += 1
        try:
            yield
        except Exception:
            self._restore(before)
            raise
        finally:
            self._transaction_depth -= 1
        self._commit(before, label, rescan=True)

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

        self._template_base = base
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
        for group in self.experiment.strain_groups.values():
            while len(group.strains) < slots:
                group.strains.append(None)
            if not any(group.strains[slots:]):
                del group.strains[slots:]
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
        self.panel_experiment.set_strain(slot, name.strip())
        return self._commit(before, f"Name slot {slot}")

    def set_control(self, slot: int) -> bool:
        before = self.snapshot()
        self.panel_experiment  # Ensure the active group is still valid after undo.
        if self.experiment.strain_groups:
            self.experiment.strain_groups[self.active_group].control_slot = slot
        else:
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

    def set_output(self, output: str) -> bool:
        """Full graphs and statistics, or only the per-spot data CSV."""
        if output not in OUTPUTS:
            raise ValueError(f"unknown output {output!r}")
        before = self.snapshot()
        self.experiment.output = output
        what = "data CSV only" if output == DATA_OUTPUT else "full results"
        return self._commit(before, f"Output {what}")

    def set_statistics(self, **changes) -> bool:
        """Change which tests the run's figures report.

        Accepts any `Statistics` field. Dunnett only compares with a reference,
        so comparing every pair moves an ANOVA's post-hoc test to Tukey HSD,
        as the review tool does.
        """
        stats = replace(self.experiment.statistics, **changes)
        if stats.test not in STATISTICAL_TESTS:
            raise ValueError("the test must be 't_test' or 'anova'")
        if stats.p_adjust not in P_ADJUST_METHODS:
            raise ValueError(f"unknown correction {stats.p_adjust!r}")
        if stats.posthoc not in POSTHOC_METHODS:
            raise ValueError(f"unknown post-hoc test {stats.posthoc!r}")
        if not 0 < float(stats.alpha) < 1:
            raise ValueError("the p cutoff must be between 0 and 1")
        if stats.all_pairs and stats.posthoc == "dunnett":
            if changes.get("posthoc") == "dunnett":
                raise ValueError("Dunnett's test compares strains with a "
                                 "reference; use Tukey HSD for every pair")
            stats = replace(stats, posthoc="tukey")
        before = self.snapshot()
        self.experiment.statistics = replace(
            stats, alpha=float(stats.alpha),
            extra_references=tuple(dict.fromkeys(stats.extra_references)))
        return self._commit(before, "Change the statistics")

    # -- conditions ----------------------------------------------------------

    def set_multi_step(self, settings) -> bool:
        from ..multistep import from_dict, to_dict
        checked = from_dict(to_dict(settings))
        before = self.snapshot()
        self.experiment.multi_step = checked
        return self._commit(before, "Change multi-step analysis")

    @staticmethod
    def _name_key(name: str) -> str:
        return " ".join(name.split()).casefold()

    def condition_name(self, code: str | None) -> str:
        if code and self.experiment.has_condition(code):
            return self.experiment.condition(code).display()
        return next((r.condition_label for r in self.found_condition_rows()
                     if r.condition == code), code or "")

    def _ensure_named_condition(self, name: str) -> str:
        """Find or create a treatment; identifiers never come from user input."""
        from ..profiles import condition_code, MEDIUM_ALIASES

        name = name.strip()
        if not name:
            raise ValueError("Enter a treatment name.")
        e = self.experiment
        matches = [c for c in e.conditions if self._name_key(c.display()) == self._name_key(name)]
        if len(matches) > 1:
            raise ValueError("Several treatments have this name. Give them distinct names in Conditions first.")
        if matches:
            return matches[0].code
        found = {r.condition for r in self.found_condition_rows()
                 if self._name_key(r.condition_label) == self._name_key(name)}
        if len(found) > 1:
            raise ValueError("Several detected treatments have this name. Add them in Conditions and give them distinct names first.")
        if len(found) == 1 and not e.has_condition(next(iter(found))):
            code = next(iter(found))
        else:
            reserved = set(e.condition_codes()) | set(e.profile.aliases.values()) | set(e.picks)
            reserved.update(r.condition for r in self.found_condition_rows())
            reserved.update(v.get("condition") for v in e.overrides.values())
            used = {str(v).casefold() for v in reserved if v}
            base = condition_code(name, MEDIUM_ALIASES)
            code, suffix = base, 2
            while code.casefold() in used:
                code, suffix = f"{base}-{suffix}", suffix + 1
        e.conditions.append(Condition(code, name))
        e.profile.aliases[self._name_key(name)] = code
        return code

    def add_named_condition(self, name: str) -> str:
        before = self.snapshot()
        code = self._ensure_named_condition(name)
        self._commit(before, f"Add treatment {name.strip()}", rescan=True)
        return code

    def set_condition_name(self, code: str, name: str) -> bool:
        name = name.strip()
        if not name:
            raise ValueError("Enter a treatment name.")
        e = self.experiment
        if any(c.code != code and self._name_key(c.display()) == self._name_key(name)
               for c in e.conditions):
            raise ValueError("Another treatment already has that name.")
        before = self.snapshot()
        condition = e.condition(code)
        if condition.display() == name:
            return False
        spellings = [condition.display(), name]
        spellings += [r.condition_label for r in self.found_condition_rows() if r.condition == code]
        for spelling in spellings:
            e.profile.aliases[self._name_key(spelling)] = code
        condition.label = name
        return self._commit(before, f"Rename treatment to {name}", rescan=True)

    def assign_treatment(self, relpaths, name: str) -> bool:
        """Resolve a name and assign photos in one undoable operation."""
        paths = list(relpaths)
        if not paths:
            return False
        before = self.snapshot()
        code = self._ensure_named_condition(name) if name.strip() else None
        for path in paths:
            entry = self.experiment.overrides.setdefault(path, {})
            entry.pop("condition_label", None)
            if code is None:
                entry.pop("condition", None)
                if not entry:
                    self.experiment.overrides.pop(path, None)
            else:
                entry["condition"] = code
        return self._commit(before, f"Set treatment on {len(paths)} photo(s)", rescan=True)

    def add_condition(self, code: str, label: str = "") -> bool:
        code = code.strip()
        if not code or self.experiment.has_condition(code):
            return False
        before = self.snapshot()
        self.experiment.conditions.append(Condition(code, label.strip() or code))
        return self._commit(before, f"Add condition {code}", rescan=True)

    def remove_condition(self, code: str) -> bool:
        if not self.experiment.has_condition(code):
            return False
        before = self.snapshot()
        self.experiment.conditions.remove(self.experiment.condition(code))
        self.experiment.picks.pop(code, None)
        for group in self.experiment.strain_groups.values():
            group.conditions.pop(code, None)
            group.picks.pop(code, None)
        return self._commit(before, f"Remove condition {code}", rescan=True)

    def set_condition_control(self, code: str, slot: int | None) -> bool:
        before = self.snapshot()
        self._panel_condition(code).control_slot = slot
        where = f"slot {slot}" if slot is not None else "the experiment's"
        return self._commit(before, f"{code}: control is {where}")

    def set_condition_exclude(self, code: str, slots) -> bool:
        before = self.snapshot()
        self._panel_condition(code).exclude = tuple(sorted(set(slots)))
        return self._commit(before, f"{code}: excluded slots")

    def adopt_found_conditions(self) -> bool:
        """Declare every condition the photos turned out to hold."""
        if self.resolution is None:
            return False
        before = self.snapshot()
        labels = {
            r.condition: r.condition_label
            for r in self.found_condition_rows()
            if r.condition
        }
        for code in labels:
            if not self.experiment.has_condition(code):
                self.experiment.conditions.append(
                    Condition(code, labels.get(code, code))
                )
        return self._commit(before, "Add the conditions found in the photos", rescan=True)

    def found_condition_rows(self):
        """Treatments can be known while plate/timepoint are still unknown."""
        return [r for r in self.resolution.rows
                if r.condition and r.status not in ("ignored", "other_set")] if self.resolution else []

    def rename_condition(self, old: str, code: str, label: str) -> bool:
        code, label = code.strip(), label.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", code):
            raise ValueError("Use letters, numbers, hyphens or underscores for the result code.")
        if any(c.code.casefold() == code.casefold() and c.code != old
               for c in self.experiment.conditions):
            raise ValueError("That code already belongs to another condition.")
        before = self.snapshot()
        e = self.experiment
        condition = e.condition(old)
        # Preserve the original spelling -> code link even when both displayed
        # fields change, and migrate every reference in one undoable operation.
        spellings = [condition.label, condition.code]
        spellings += [r.condition_label for r in self.found_condition_rows() if r.condition == old]
        e.profile.aliases = {k: code if v == old else v for k, v in e.profile.aliases.items()}
        for spelling in spellings:
            if spelling:
                e.profile.aliases[re.sub(r"\s+", " ", spelling.strip().lower())] = code
        for entry in e.overrides.values():
            if entry.get("condition") == old:
                entry["condition"] = code
        if old in e.picks and old != code:
            e.picks[code] = e.picks.pop(old)
        for group in e.strain_groups.values():
            if old != code:
                if old in group.picks:
                    group.picks[code] = group.picks.pop(old)
                if old in group.conditions:
                    group.conditions[code] = group.conditions.pop(old)
                    group.conditions[code].code = code
        condition.code, condition.label = code, label or code
        return self._commit(before, f"Rename condition {old}", rescan=True)

    def set_profile(self, profile) -> bool:
        profile.check()
        before = self.snapshot()
        self.experiment.profile = profile
        self.profile_report = ["Organization edited by hand. Review the photo table below."]
        return self._commit(before, "Change data organization", rescan=True)

    def detect_organization(self) -> bool:
        before = self.snapshot()
        profile, report = intake.infer_profile(self.files, mode=self.experiment.mode)
        # Retain user code corrections across an explicit re-detection.
        profile.aliases.update(self.experiment.profile.aliases)
        self.experiment.profile = profile
        self.profile_report = report
        return self._commit(before, "Detect data organization", rescan=True)

    # -- photos --------------------------------------------------------------

    def set_photo_root(self, root: Path, *, infer: bool = True,
                       adopt_conditions: bool = False) -> bool:
        """Point at a photo folder, and read what its layout appears to mean."""
        before = self.snapshot()
        if str(Path(root)) == self.experiment.photo_root:
            with self.transaction("Read photo folder"):
                self.rescan()
                if adopt_conditions:
                    self.adopt_found_conditions()
            return self.snapshot() != before
        self.experiment.photo_root = str(Path(root))
        # Hand corrections are keyed on paths relative to the old root, so they
        # are meaningless against a different one. Dropped rather than silently
        # re-applied to whatever file now happens to sit at that path.
        self.experiment.overrides.clear()
        self.experiment.ignored.clear()
        self.experiment.picks.clear()
        for group in self.experiment.strain_groups.values():
            group.picks.clear()
        self.experiment.multi_step.plate_ids.clear()
        self.experiment.multi_step.excluded_photos.clear()
        self._scan(infer=infer)
        if adopt_conditions:
            for row in self.found_condition_rows():
                if not self.experiment.has_condition(row.condition):
                    self.experiment.conditions.append(Condition(row.condition, row.condition_label))
        return self._commit(before, f"Use photos in {Path(root).name}")

    def rescan(self, *, infer: bool | None = None) -> None:
        """Refresh disk contents; any inferred configuration is undoable.

        None detects an initial layout; False preserves even an empty profile.
        """
        before = self.snapshot()
        self._scan(infer=infer)
        self._commit(before, "Detect data organization while reading photos")

    def _scan(self, *, infer: bool | None = None) -> None:
        self.files, self.resolution, self.scan_error = [], None, ""
        self.profile_report = []
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
        if infer or (infer is None and not self.experiment.profile.rules and not self.experiment.profile.name.startswith("Manual")):
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
        else:
            self.resolution = None

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
        if self.experiment.strain_groups:
            key = self.panel_experiment.set_key
            return [r.relpath for r in self.resolution.rows
                    if r.set_key == key and r.status != "ignored"] if self.resolution else []
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
            self.template, self.panel_experiment.dilution_for(code, plate_id))
        if index is None:
            return "-"
        return geometry.level_display(self.template, index)

    def dilution_index(self, code: str, plate_id: str) -> int | None:
        from .. import geometry

        return geometry.resolve_level(
            self.template, self.panel_experiment.dilution_for(code, plate_id))

    def photo_path(self, relpath: str) -> Path:
        return Path(self.experiment.photo_root) / relpath

    def set_pick(self, code: str, plate_id: str, relpath: str | None) -> bool:
        before = self.snapshot()
        if self.experiment.strain_groups and relpath and relpath not in self.candidates():
            raise ValueError("Assign this photo to the selected strain group in Data first.")
        self.panel_experiment.set_pick(code, plate_id, relpath)
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
        panel = self.panel_experiment
        panel.set_dilution_for(code, plate_id, index)
        self._panel_condition(code).dilution = panel.condition(code).dilution
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
                self.experiment, self.resolution, self.template,
                self.review_plates,
            )
        return found

    # -- history -------------------------------------------------------------

    def undo(self) -> str | None:
        result = self.undo_stack.undo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self._restore(snapshot)
        return label

    def redo(self) -> str | None:
        result = self.undo_stack.redo(self.snapshot())
        if result is None:
            return None
        snapshot, label = result
        self._restore(snapshot)
        return label

    def _restore(self, snapshot: dict) -> None:
        old_root = self.experiment.photo_root
        old_template = (self.experiment.template_path, self.experiment.template_id)
        self.experiment = from_dict(snapshot)
        if old_template != (self.experiment.template_path, self.experiment.template_id):
            self.load_template(self._template_base)
        self.profile_report = []
        if self.experiment.photo_root != old_root:
            # History must not silently replace the restored naming rules.
            self._scan(infer=False)
        else:
            self.reresolve()
        self._notify()


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
