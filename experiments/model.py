"""Data model for an experiment: the layer that binds a plate template to reality.

`plate_template` describes geometry and is deliberately strain-agnostic -- its
`sample_slot` means "the Nth sample in the panel" and its docstring defers
binding those slots to strain names to "a separate layer". This is that layer.

Each strain group is one panel, spotted across one or more conditions, with a
positive control that every strain is relativised to. An experiment can hold
multiple named groups, but each is quantified and reviewed independently.
Condition stays an axis inside each group, because the panel is the thing
that was physically spotted: the same eight strains go onto glucose, glycerol
and potassium acetate, and only the control may differ between them. That
difference is not cosmetic -- the WT BY used here has a growth defect on
respiring media, so normalising K-OAc against it would divide by a strain that
barely grew (see `spotting_timecourse.py:868`).

Together with the naming profile, an experiment says everything the pipelines
need that cannot be read off a photograph.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .profiles import NamingProfile

# Version 2 adds strain groups; version 3 adds optional multi-step settings.
# Older designers must refuse newer files rather than silently omit analyses.
SCHEMA_VERSION = 3
KIND = "spotting_experiment"

#: Handpicked photos the user has already chosen, quantified once.
QUANTIFY = "quantify"
#: Every combination of timepoint and technical replicate, scored and ranked.
TIMECOURSE = "timecourse"
MODES = (QUANTIFY, TIMECOURSE)

#: Which edge of each photograph corresponds to the plate template's top edge.
#: Stored on the experiment because camera orientation is part of interpreting
#: every measured row and column, not a display preference.
PHOTO_TOPS = ("top", "right", "bottom", "left")

#: Graphs, charts and statistics, as the pipelines have always produced.
FULL_OUTPUT = "full"
#: Only the per-spot CSV of raw and normalised grey values; nothing is drawn.
DATA_OUTPUT = "data"
OUTPUTS = (FULL_OUTPUT, DATA_OUTPUT)

#: Which dilution level to score, for `QUANTIFY` mode. Mirrors the shape already
#: stored in `spotting_config.json`: one choice for the whole condition, or one
#: per plate when the two plates grew differently.
#:
#: "plate" is the normal case rather than the exception. Every spot is
#: normalised to the control on ITS OWN plate, so two plates of the same
#: treatment that grew to different densities can each be scored at whichever
#: dilution is actually readable on it without the two becoming incomparable.
DILUTION_MODES = ("condition", "plate")

#: The statistics the review tool offers, repeated here so the designer can set
#: them before a run without importing the plotting stack. The values are the
#: keyword arguments `spotting_batch.run_plots` takes.
STATISTICAL_TESTS = ("t_test", "anova")
#: Multiple-comparison corrections for the ratio t-tests.
P_ADJUST_METHODS = ("none", "holm", "bonferroni", "sidak")
#: The pairwise test that follows an ANOVA. Mirrors
#: `spotting_plots.POSTHOC_METHODS`.
POSTHOC_METHODS = ("dunnett", "tukey", "holm", "bonferroni", "sidak", "none")


@dataclass
class Statistics:
    """Which tests the figures report, chosen before the run.

    A time course also counts each candidate's significant strains with this
    test, so its ranking and the figures agree on which strains differ.

    The defaults are the pipeline's own: ratio paired t-tests of every strain
    against the control, uncorrected, at p < 0.05. The review tool can still
    change any of this afterwards; the run seeds it with these.
    """

    test: str = "t_test"
    p_adjust: str = "none"
    #: Only read after an ANOVA.
    posthoc: str = "dunnett"
    alpha: float = 0.05
    #: Compare every strain with every other, instead of with the references.
    all_pairs: bool = False
    #: Strain names every other strain is ALSO compared with, besides the
    #: control. Ignored when `all_pairs`.
    extra_references: tuple[str, ...] = ()

    def plot_kwargs(self) -> dict:
        """The keyword arguments `spotting_batch.run_plots` takes, as chosen."""
        return {"statistical_test": self.test,
                "p_adjust": self.p_adjust if self.test == "t_test" else "none",
                "alpha": float(self.alpha),
                "posthoc": self.posthoc if self.test == "anova" else "none",
                "extra_references": tuple(self.extra_references),
                "all_pairs": bool(self.all_pairs)}

    def review_dict(self) -> dict:
        """The `statistics` block of a results folder's `review.json`."""
        return {"test": self.test, "p_adjust": self.p_adjust,
                "alpha": float(self.alpha), "posthoc": self.posthoc,
                "extra_references": list(self.extra_references),
                "all_pairs": bool(self.all_pairs)}


@dataclass
class MultiStepAnalysis:
    """Optional comparison analysis; physical plate labels persist across time.

    Labels are local to a strain group, condition and template plate position.
    The template's replicate numbers identify matched biological blocks.
    """

    enabled: bool = False
    scope: str = "technical"
    method: str = "two_step"
    dilution: int | None = None
    hours: tuple[float, ...] = ()
    plate_ids: dict[str, str] = field(default_factory=dict)
    excluded_photos: dict[str, str] = field(default_factory=dict)


@dataclass
class Condition:
    """One medium or treatment the panel was spotted on.

    `code` is canonical and short ("K-OAc"); it names result folders and files,
    so it must stay stable. `label` is whatever the user actually wrote
    ("Potassium Acetate") and is what figures show.
    """

    code: str
    label: str = ""
    #: Overrides the experiment's control for this condition only. None means
    #: "use the experiment's".
    control_slot: int | None = None
    #: Sample slots to drop for this condition -- a strain that failed to grow
    #: here for a reason unrelated to the question being asked.
    exclude: tuple[int, ...] = ()
    #: QUANTIFY mode only: which dilution level to score. None means "not chosen
    #: yet", which validation reports rather than guessing.
    dilution: dict | None = None

    def display(self) -> str:
        return self.label or self.code


@dataclass
class StrainGroup:
    """A separately labelled panel sharing the experiment's plate geometry."""

    strains: list[str | None] = field(default_factory=list)
    control_slot: int | None = None
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    conditions: dict[str, Condition] = field(default_factory=dict)


@dataclass
class Experiment:
    """One or more strain panels, their treatments and shared photo folder."""

    name: str = "Untitled"
    #: The template this panel was spotted on. `template_id` is the authority --
    #: `template_path` is a convenience that may go stale if files move.
    template_id: str = ""
    template_path: str = ""
    #: Index 0 is sample slot 1. None marks a slot that was deliberately left
    #: empty, which the existing configs already express as a null column.
    strains: list[str | None] = field(default_factory=list)
    #: 1-based sample slot of the positive control. Every strain is divided by
    #: the mean of this slot's spots ON ITS OWN PLATE.
    control_slot: int | None = None
    conditions: list[Condition] = field(default_factory=list)
    mode: str = TIMECOURSE
    #: Where the experiment/template's top edge appears in each photograph.
    photo_top: str = "top"
    #: Absolute path to the folder the photos live under. Everything else is
    #: stored relative to it, so moving the tree only means re-pointing this.
    photo_root: str = ""
    #: When the photo folder holds several panels (the flat layout keeps all ten
    #: sets in one directory), only photos whose `set` facet equals this belong
    #: to this experiment. None means "every photo under the root".
    set_key: str | None = None
    #: Explicit panels; every included photo must name one when this is used.
    strain_groups: dict[str, StrainGroup] = field(default_factory=dict)
    #: How a photo's path says what it is. Both modes use this to suggest
    #: conditions; TIMECOURSE also requires plate and timepoint identities.
    #: QUANTIFY still chooses photos by eye and does not require a profile.
    profile: NamingProfile = field(default_factory=NamingProfile)
    #: QUANTIFY only: condition code -> plate id -> relpath. The photograph the
    #: user picked for each plate of each condition, stated outright rather than
    #: derived from a filename.
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    #: relpath -> {facet: value}. Hand corrections, which always beat the
    #: profile, so re-scanning after adding photos never clobbers them.
    overrides: dict[str, dict] = field(default_factory=dict)
    #: relpaths the user positively excluded -- a blurred shot, a test frame.
    ignored: list[str] = field(default_factory=list)
    #: The statistical tests the run's figures report.
    statistics: Statistics = field(default_factory=Statistics)
    multi_step: MultiStepAnalysis = field(default_factory=MultiStepAnalysis)
    #: FULL_OUTPUT draws every graph and chart; DATA_OUTPUT only writes the
    #: per-spot CSV (raw and normalised grey values).
    output: str = FULL_OUTPUT
    id: str = ""
    revision: int = 1
    description: str = ""
    created: str = ""
    schema_version: int = SCHEMA_VERSION

    # -- panel -------------------------------------------------------------

    def for_group(self, key: str) -> Experiment:
        """Single-panel view for existing measurement and review contracts.

        Lists/picks reference the panel; condition views carry the shared names
        and group-specific controls, exclusions and dilution choices.
        """
        group = self.strain_groups[key]
        return replace(self, name=f"{self.name} - {key}", set_key=key,
                       strains=group.strains, control_slot=group.control_slot,
                       picks=group.picks, strain_groups={},
                       conditions=[replace(group.conditions.get(c.code, c),
                                           code=c.code, label=c.label)
                                   for c in self.conditions])

    @property
    def is_timecourse(self) -> bool:
        return self.mode == TIMECOURSE

    def strain(self, slot: int) -> str | None:
        """The strain in a 1-based sample slot, or None if empty/out of range."""
        if 1 <= slot <= len(self.strains):
            return self.strains[slot - 1]
        return None

    def filled_slots(self) -> list[int]:
        """1-based slots that actually carry a strain."""
        return [i for i, s in enumerate(self.strains, 1) if s]

    def slot_count(self) -> int:
        return len(self.strains)

    def set_strain(self, slot: int, name: str | None) -> None:
        """Name a 1-based slot, growing the panel if it does not reach that far."""
        if slot < 1:
            raise ValueError(f"sample slot is 1-based, got {slot}")
        while len(self.strains) < slot:
            self.strains.append(None)
        self.strains[slot - 1] = name or None

    # -- conditions --------------------------------------------------------

    def condition(self, code: str) -> Condition:
        for c in self.conditions:
            if c.code == code:
                return c
        raise KeyError(f"no condition with code {code!r}")

    def has_condition(self, code: str) -> bool:
        return any(c.code == code for c in self.conditions)

    def condition_codes(self) -> list[str]:
        return [c.code for c in self.conditions]

    def control_for(self, code: str) -> int | None:
        """The control slot in force for one condition.

        The per-condition override is the whole reason this method exists; see
        the module docstring.
        """
        try:
            override = self.condition(code).control_slot
        except KeyError:
            return self.control_slot
        return override if override is not None else self.control_slot

    def exclude_for(self, code: str) -> tuple[int, ...]:
        try:
            return self.condition(code).exclude
        except KeyError:
            return ()

    # -- picked photos (QUANTIFY mode) ---------------------------------------

    def pick(self, code: str, plate_id: str) -> str | None:
        """The photo chosen for one plate of one condition, relative to the root."""
        return self.picks.get(code, {}).get(str(plate_id))

    def set_pick(self, code: str, plate_id: str, relpath: str | None) -> None:
        slots = self.picks.setdefault(code, {})
        if relpath:
            slots[str(plate_id)] = relpath
        else:
            slots.pop(str(plate_id), None)
            if not slots:
                self.picks.pop(code, None)

    def picked_plates(self, code: str) -> dict[str, str]:
        return dict(self.picks.get(code, {}))

    def missing_picks(self, plate_ids) -> list[tuple[str, str]]:
        """(condition, plate) pairs still without a photograph."""
        return [
            (c.code, str(p))
            for c in self.conditions
            for p in plate_ids
            if not self.pick(c.code, p)
        ]

    # -- dilution (QUANTIFY mode) --------------------------------------------

    def dilution_for(self, code: str, plate_id: str):
        """The dilution level to score on one plate of one condition, as stored.

        Reads both stored shapes: a single choice for the whole condition, which
        is what migrated `spotting_config.json` entries carry, and one choice per
        plate, which is what the picker writes.

        The value comes back exactly as it was saved -- a 0-based level index,
        or one of the old level names ("middle"). Turning either into an index
        needs the plate template, which is what names the levels, so that is
        `geometry.resolve_level`'s job rather than the model's.
        """
        try:
            dilution = self.condition(code).dilution
        except KeyError:
            return None
        if not dilution:
            return None
        if dilution.get("mode") == "plate":
            return (dilution.get("choices") or {}).get(str(plate_id))
        return dilution.get("choice")

    def set_dilution_for(self, code: str, plate_id: str, level) -> None:
        """Set one plate's dilution, converting a condition-wide choice if needed.

        A condition-wide choice is expanded to a per-plate one across the plates
        already picked, so setting a single plate never silently changes the
        others that were relying on the shared value.
        """
        condition = self.condition(code)
        dilution = condition.dilution or {}
        if dilution.get("mode") == "plate":
            choices = dict(dilution.get("choices") or {})
        else:
            shared = dilution.get("choice")
            choices = ({str(p): shared for p in self.picked_plates(code)}
                       if shared is not None and shared != "" else {})
        # `level` may legitimately be 0 -- the least dilute row -- so only None
        # and "" clear a choice. `if level:` would silently refuse level 0.
        if level is None or level == "":
            choices.pop(str(plate_id), None)
        else:
            choices[str(plate_id)] = level
        condition.dilution = {"mode": "plate", "choices": choices} if choices else None

    def scored_slots(self, code: str) -> list[int]:
        """Slots that contribute to the result for one condition.

        Filled, and not excluded. The control stays in -- it is the divisor and
        is reported at a relative growth of 1.
        """
        dropped = set(self.exclude_for(code))
        return [s for s in self.filled_slots() if s not in dropped]
