"""Checks an experiment against the things that make a run wrong or impossible.

Errors mean the experiment cannot drive a run. Warnings mean something looks
like a slip but might be deliberate -- a half-filled panel is expected while it
is being typed in, which is why saving is never blocked on them.

Validity is derived, never stored: nothing here writes to the experiment.

The checks that matter most are the quiet ones. A control slot that was left
empty, or excluded by the very condition it is meant to normalise, does not
crash anything -- it silently divides every strain by nothing, or by a strain
that was never meant to be the reference. Those are errors here for the same
reason `plate_template.validate` treats a duplicate placement as one: the result
still looks like a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import geometry
from .model import QUANTIFY, TIMECOURSE, Experiment
from .profiles import NamingProfile


#: Mirrors the `len(v) < 3` floor in `spotting_timecourse.score_candidate`.
#: `tests/experiments/test_bridge_matches_pipeline.py` asserts they agree.
TIMECOURSE_MIN_REPLICATES = 3


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    #: Condition code this is about, when it is about one.
    condition: str | None = None
    #: 1-based sample slots involved, so the panel editor can highlight them.
    slots: tuple[int, ...] = ()

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


#: Facets a profile must be able to read, per mode.
#:
#: A time course cannot be assembled without knowing when each photo was taken
#: and which plate it is, and there are far too many photos to say so by hand.
#:
#: Handpicked quantification requires NOTHING of the profile. Its photos are
#: chosen one at a time against a preview, so which plate a photo is is stated
#: outright in `Experiment.picks` rather than guessed from its name -- which
#: means a folder whose names encode nothing at all works perfectly well.
REQUIRED_FACETS = {
    QUANTIFY: (),
    TIMECOURSE: ("condition", "plate", "timepoint"),
}


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------


def _check_panel(e: Experiment, out: list[Issue]) -> None:
    filled = e.filled_slots()
    if not filled:
        out.append(
            Issue(
                Severity.ERROR,
                "no_strains",
                "no sample slot has been given a strain name",
            )
        )
        return

    blank = [s for s in range(1, e.slot_count() + 1) if not e.strain(s)]
    if blank:
        n = len(blank)
        out.append(
            Issue(
                Severity.WARNING,
                "empty_slots",
                f"sample {_plural(n, 'slot')} "
                f"{', '.join(str(s) for s in blank)} "
                f"{_plural(n, 'has', 'have')} no strain; "
                f"{_plural(n, 'it', 'they')} will be skipped",
                slots=tuple(blank),
            )
        )

    seen: dict[str, list[int]] = {}
    for slot in filled:
        seen.setdefault(e.strain(slot), []).append(slot)
    for name, slots in seen.items():
        if len(slots) > 1:
            out.append(
                Issue(
                    Severity.WARNING,
                    "duplicate_strain",
                    f"{name!r} is named in slots "
                    f"{', '.join(str(s) for s in slots)}; if that is deliberate "
                    f"they will be reported as separate samples, not pooled",
                    slots=tuple(slots),
                )
            )


# ---------------------------------------------------------------------------
# Controls -- every strain is divided by this, so a slip here poisons everything
# ---------------------------------------------------------------------------


def _check_controls(e: Experiment, out: list[Issue]) -> None:
    if e.control_slot is None:
        out.append(
            Issue(
                Severity.ERROR,
                "no_control",
                "no sample slot is designated as the positive control; every "
                "strain is reported relative to one",
            )
        )
    elif not e.strain(e.control_slot):
        out.append(
            Issue(
                Severity.ERROR,
                "control_slot_empty",
                f"slot {e.control_slot} is the control but has no strain name",
                slots=(e.control_slot,),
            )
        )

    for c in e.conditions:
        control = e.control_for(c.code)
        if control is None:
            continue
        if not e.strain(control):
            out.append(
                Issue(
                    Severity.ERROR,
                    "control_slot_empty",
                    f"{c.display()}: slot {control} is its control but has no "
                    f"strain name",
                    c.code,
                    (control,),
                )
            )
        if control in c.exclude:
            out.append(
                Issue(
                    Severity.ERROR,
                    "control_excluded",
                    f"{c.display()}: slot {control} is both its control and "
                    f"excluded from it; there would be nothing to normalise to",
                    c.code,
                    (control,),
                )
            )

    designated = {e.control_for(c.code) for c in e.conditions}
    designated.discard(None)
    if len(designated) > 1:
        names = ", ".join(
            f"{c.display()}: slot {e.control_for(c.code)}" for c in e.conditions
        )
        out.append(
            Issue(
                Severity.INFO,
                "control_differs_between_conditions",
                f"conditions use different controls ({names}); intended when a "
                f"strain does not grow on one medium, a slip otherwise",
            )
        )


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def _check_conditions(e: Experiment, out: list[Issue]) -> None:
    if not e.conditions:
        out.append(
            Issue(
                Severity.ERROR,
                "no_conditions",
                "the experiment has no conditions; add at least one medium or "
                "treatment the panel was spotted on",
            )
        )
        return

    panel = set(range(1, e.slot_count() + 1))
    for c in e.conditions:
        outside = sorted(set(c.exclude) - panel)
        if outside:
            out.append(
                Issue(
                    Severity.ERROR,
                    "exclude_out_of_range",
                    f"{c.display()}: excluded {_plural(len(outside), 'slot')} "
                    f"{', '.join(str(s) for s in outside)} "
                    f"{_plural(len(outside), 'is', 'are')} outside the panel "
                    f"(1..{e.slot_count()})",
                    c.code,
                    tuple(outside),
                )
            )

        scored = e.scored_slots(c.code)
        control = e.control_for(c.code)
        if not [s for s in scored if s != control]:
            out.append(
                Issue(
                    Severity.WARNING,
                    "nothing_to_compare",
                    f"{c.display()}: every strain except the control is empty "
                    f"or excluded, so there is nothing to test",
                    c.code,
                )
            )


def _names_identify_photos(profile) -> bool:
    """True when a photograph's own filename says which plate of which condition.

    The flat lab layout does: `4.1GLU.JPG` is plate 1 of set 4 on glucose, and
    exactly one file carries that name. A folder-based layout does not, however
    completely it describes the tree.
    """
    from_filename = {rule.facet for rule in profile.rules if rule.source == "stem"}
    return {"condition", "plate"} <= from_filename


def _check_picks(e: Experiment, template, out: list[Issue]) -> None:
    """Handpicked mode: one photo, and one dilution level, per condition and plate.

    Both are per plate rather than per condition. Every spot is normalised to
    the control on its own plate, so two plates of one treatment that grew to
    different densities can each be scored at whichever dilution is readable on
    it -- and they routinely are.
    """
    if e.mode != QUANTIFY or not e.conditions:
        return
    plate_ids = ([str(p.id) for p in template.plates]
                 if template is not None and template.plates else [])
    if not plate_ids:
        return          # `_check_template` has already said the template is missing

    # Nothing picked at all, but the FILENAMES say which photo is which: this is
    # a migrated `spotting_config.json`, whose photos were never chosen in a
    # window because the flat `<set>.<plate><TREATMENT>` layout names them.
    # `run.to_photo_refs` falls back the same way, so this is a note about how
    # the run will work, not a fault. One pick means the picker is being used,
    # and from then on the missing ones are errors.
    #
    # Reading it from the FOLDERS does not count. A capture tree maps many
    # photographs onto the same condition and plate -- one per timepoint -- so
    # there would be nothing to identify a single plate with.
    if not e.picks and _names_identify_photos(e.profile):
        out.append(
            Issue(
                Severity.WARNING,
                "photos_read_from_filenames",
                "no photographs have been chosen for the plates, so they will "
                "be matched by filename instead. Choose one on the Plates tab "
                "to take over from that",
            )
        )
        return

    for c in e.conditions:
        for plate_id in plate_ids:
            if not e.pick(c.code, plate_id):
                out.append(
                    Issue(
                        Severity.ERROR,
                        "no_photo_for_plate",
                        f"{c.display()}: no photograph chosen for plate "
                        f"{plate_id}",
                        c.code,
                    )
                )
            else:
                index = geometry.resolve_level(
                    template, e.dilution_for(c.code, plate_id))
                if index is None:
                    out.append(
                        Issue(
                            Severity.ERROR,
                            "no_dilution_choice",
                            f"{c.display()} plate {plate_id}: no dilution level "
                            f"chosen. One level is scored per plate and it "
                            f"cannot be picked for you",
                            c.code,
                        )
                    )
                elif not geometry.is_valid_level(template, index):
                    out.append(
                        Issue(
                            Severity.ERROR,
                            "dilution_out_of_range",
                            f"{c.display()} plate {plate_id}: level "
                            f"{index + 1} was chosen but the template declares "
                            f"only {geometry.level_count(template)}",
                            c.code,
                        )
                    )
                elif not geometry.rows_for(template, plate_id, index):
                    out.append(
                        Issue(
                            Severity.ERROR,
                            "dilution_not_on_plate",
                            f"{c.display()} plate {plate_id}: nothing is spotted "
                            f"at {geometry.level_display(template, index)} on "
                            f"that plate",
                            c.code,
                        )
                    )

    root = Path(e.photo_root) if e.photo_root else None
    for code, slots in sorted(e.picks.items()):
        if not e.has_condition(code):
            out.append(
                Issue(
                    Severity.WARNING,
                    "pick_for_unknown_condition",
                    f"photographs are chosen for {code!r}, which is no longer "
                    f"one of this experiment's conditions",
                    code,
                )
            )
            continue
        for plate_id, relpath in sorted(slots.items()):
            if plate_ids and plate_id not in plate_ids:
                out.append(
                    Issue(
                        Severity.WARNING,
                        "pick_for_unknown_plate",
                        f"{code}: a photograph is chosen for plate {plate_id}, "
                        f"which the template does not have",
                        code,
                    )
                )
            elif root is not None and not (root / relpath).exists():
                out.append(
                    Issue(
                        Severity.ERROR,
                        "picked_photo_missing",
                        f"{code} plate {plate_id}: {relpath} is no longer in the "
                        f"photo folder",
                        code,
                    )
                )


# ---------------------------------------------------------------------------
# The template binding
# ---------------------------------------------------------------------------


def _check_template(e: Experiment, template, out: list[Issue]) -> None:
    if not e.template_id and template is None:
        out.append(
            Issue(
                Severity.ERROR,
                "no_template",
                "no plate template is bound; the experiment does not know the "
                "plate layout its photos were spotted in",
            )
        )
        return
    if template is None:
        out.append(
            Issue(
                Severity.ERROR,
                "template_not_found",
                f"the plate template could not be loaded from "
                f"{e.template_path or e.template_id!r}",
            )
        )
        return

    if e.template_id and template.id and template.id != e.template_id:
        out.append(
            Issue(
                Severity.ERROR,
                "template_mismatch",
                f"{e.template_path or 'the template file'} is template "
                f"{template.id!r}, but this experiment was built on "
                f"{e.template_id!r}",
            )
        )
        return

    slots = template.sample_slots()
    if slots and e.slot_count() and slots != e.slot_count():
        out.append(
            Issue(
                Severity.ERROR,
                "template_slot_mismatch",
                f"the template has {slots} sample "
                f"{_plural(slots, 'slot')} but the panel names "
                f"{e.slot_count()}; strains would be attached to the wrong spots",
            )
        )

    if e.control_slot is not None and slots and e.control_slot > slots:
        out.append(
            Issue(
                Severity.ERROR,
                "control_not_in_template",
                f"slot {e.control_slot} is the control but the template only "
                f"has {slots} {_plural(slots, 'slot')}",
                slots=(e.control_slot,),
            )
        )

    # The engine derives the expected spot pitch from the grid it is told to
    # look for, so any size is measurable. The only floor is that a lattice
    # needs two rows and two columns to be fitted at all, and to leave an agar
    # gap between diagonally adjacent spots for the background reads.
    if not geometry.grid_supported(template):
        out.append(
            Issue(
                Severity.ERROR,
                "grid_too_small",
                f"the template is {template.rows}x{template.cols}; the plate "
                f"finder needs at least "
                f"{geometry.MIN_ROWS}x{geometry.MIN_COLS} to fit a lattice and "
                f"to find agar between the spots to measure the background",
            )
        )

    # The time course scores every dilution level the template declares, but it
    # still assembles each candidate from exactly two plates -- one photograph
    # of plate 1 paired with one of plate 2. A design with another plate count
    # would silently score nothing, so it is refused here instead.
    # Scoring a candidate takes a CV and a one-sample t-test per strain, which
    # need at least three replicates (`spotting_timecourse.score_candidate`
    # skips a strain with fewer). Below that every candidate scores as nothing
    # and the run ends blaming the noise floor, so the real reason is given here.
    replicates = len(template.replicates())
    if e.mode == TIMECOURSE and 0 < replicates < TIMECOURSE_MIN_REPLICATES:
        out.append(
            Issue(
                Severity.ERROR,
                "timecourse_too_few_replicates",
                f"the time course ranks candidates on each strain's spread and "
                f"significance, which need at least "
                f"{TIMECOURSE_MIN_REPLICATES} biological replicates; this "
                f"design has {replicates}. Handpicked quantification still "
                f"reports it",
            )
        )

    # The time course takes a photograph of every plate the design has and
    # combines them, so any plate count works. It does match photos to plates by
    # NUMBER, though, so a template whose plate ids are not numbers cannot run.
    if e.mode == TIMECOURSE:
        bad = [p for p in geometry.plate_ids(template) if not str(p).isdigit()]
        if bad:
            out.append(
                Issue(
                    Severity.ERROR,
                    "timecourse_plate_ids_not_numbers",
                    f"plate {', '.join(bad)} cannot be matched to a photograph: "
                    f"the time course reads plate numbers off the photo folders, "
                    f"so the template's plate ids must be numbers",
                )
            )

    for plate in template.plates:
        if not plate.scorable_dilutions():
            out.append(
                Issue(
                    Severity.WARNING,
                    "plate_not_scorable",
                    f"template plate {plate.id!r} has no dilution level where "
                    f"every sample and the control are all present, so it "
                    f"cannot be quantified as designed",
                )
            )


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


def _check_photos(e: Experiment, out: list[Issue]) -> None:
    if not e.photo_root:
        out.append(
            Issue(
                Severity.ERROR,
                "no_photo_root",
                "no photo folder has been chosen",
            )
        )

    profile: NamingProfile = e.profile
    have = profile.facets()
    missing = [f for f in REQUIRED_FACETS.get(e.mode, ()) if f not in have]
    if missing:
        why = (
            "this is needed to assemble a time course"
            if e.mode == TIMECOURSE
            else "this is needed to know what each photo is"
        )
        out.append(
            Issue(
                Severity.ERROR,
                "profile_incomplete",
                f"the naming profile cannot read "
                f"{', '.join(missing)} out of the photo paths; {why}",
            )
        )

    try:
        profile.check()
    except Exception as exc:  # ProfileError, but never let one escape validate
        out.append(
            Issue(Severity.ERROR, "profile_invalid", f"naming profile: {exc}")
        )


# ---------------------------------------------------------------------------


def _summary(e: Experiment) -> Issue:
    filled = len(e.filled_slots())
    conditions = len(e.conditions)
    mode = "time course" if e.is_timecourse else "handpicked quantification"
    return Issue(
        Severity.INFO,
        "summary",
        f"{filled} {_plural(filled, 'strain')}, "
        f"{conditions} {_plural(conditions, 'condition')}, "
        f"control slot {e.control_slot if e.control_slot is not None else '-'}, "
        f"{mode}",
    )


def validate(e: Experiment, template=None) -> list[Issue]:
    """Every problem with this experiment, worst first.

    `template` is the `plate_template.model.Template` the experiment is bound
    to, or None when it could not be loaded. It is passed in rather than read
    from disk so this stays pure and testable.
    """
    issues: list[Issue] = []
    _check_panel(e, issues)
    _check_controls(e, issues)
    _check_conditions(e, issues)
    _check_template(e, template, issues)
    _check_photos(e, issues)
    _check_picks(e, template, issues)
    issues.append(_summary(e))
    issues.sort(key=lambda i: (_RANK[i.severity], i.condition or "", i.code))
    return issues


def blocking(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity is Severity.ERROR]
