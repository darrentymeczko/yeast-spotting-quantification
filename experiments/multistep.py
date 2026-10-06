"""Headless configuration and design checks for the additional analysis."""

from dataclasses import asdict
import math

from .model import MultiStepAnalysis

SCOPES = {"Technical replicates": "technical",
          "Technical replicates + repeated measures": "repeated"}
METHODS = {"Two-step analysis": "two_step", "Full hierarchical analysis": "hierarchical"}


def to_dict(settings):
    result = asdict(settings)
    result["hours"] = list(settings.hours)
    return result


def from_dict(raw):
    if not isinstance(raw, dict):
        raise ValueError("multi_step must be an object")
    unknown = set(raw) - set(MultiStepAnalysis.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown multi_step fields: {sorted(unknown)}")
    s = MultiStepAnalysis(**raw)
    if not isinstance(s.enabled, bool):
        raise ValueError("multi_step.enabled must be true or false")
    if s.scope not in SCOPES.values() or s.method not in METHODS.values():
        raise ValueError("invalid multi_step scope or method")
    if s.dilution is not None and (type(s.dilution) is not int or s.dilution < 0):
        raise ValueError("multi_step.dilution must be a nonnegative integer")
    if not isinstance(s.hours, (list, tuple)) or any(
        type(h) not in (int, float) or not math.isfinite(h) or h < 0 for h in s.hours
    ):
        raise ValueError("multi_step.hours must contain finite nonnegative hours")
    if len(set(s.hours)) != len(s.hours):
        raise ValueError("multi_step.hours must not contain duplicates")
    s.hours = tuple(sorted(float(h) for h in s.hours))
    for name in ("plate_ids", "excluded_photos"):
        values = getattr(s, name)
        if not isinstance(values, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip()
            or any(ord(c) < 32 for c in v) for k, v in values.items()
        ):
            raise ValueError(f"multi_step.{name} requires paths and nonblank labels/reasons")
        setattr(s, name, {k: v.strip() for k, v in values.items()})
    return s


def selected_rows(e, resolution):
    """All resolved photos at preselected times, independent of candidate picks."""
    return [r for r in resolution.usable()
            if r.condition in e.condition_codes() and r.timepoint in e.multi_step.hours]


def exclusions(e, flagged_plates=None) -> dict:
    """relpath -> why, for every photo this analysis leaves out.

    Two sources: the photos excluded in this analysis's own dialog, and the
    plates flagged as bad in the data review (`flagged_plates`, relpath ->
    reason). A data-review flag always excludes here -- the analysis pools many
    technical plates and timepoints, and one bad plate would bias all of it.
    Where both apply, both reasons are kept.
    """
    out = {path: f"data review: {reason}"
           for path, reason in (flagged_plates or {}).items() if reason}
    for path, reason in e.multi_step.excluded_photos.items():
        out[path] = f"{reason}; {out[path]}" if path in out else reason
    return out


def design_errors(e, template=None, resolution=None, flagged_plates=None):
    s = e.multi_step
    if not s.enabled:
        return []
    from . import geometry
    errors = []
    names = [e.strain(slot) for slot in e.filled_slots()]
    if len(names) != len(set(names)):
        errors.append("Multi-step analysis needs a unique strain name per sample slot; distinguish separate cultures/constructs before pooling.")
    if e.output == "data":
        errors.append("Multi-step analysis needs Full output (graphs and statistics).")
    if s.dilution is None or (template is not None and not geometry.is_valid_level(template, s.dilution)):
        errors.append("Choose a valid dilution for multi-step analysis.")
    if (s.scope == "technical" and len(s.hours) < 1) or (s.scope == "repeated" and len(s.hours) < 2):
        errors.append("Select at least one hour for technical-only analysis, or at least two for repeated measures.")
    if template is not None and s.dilution is not None:
        # Matched contrasts must use an actual control with the same block ID.
        for c in e.conditions:
            for pid in geometry.plate_ids(template):
                cells = list(geometry.cells_for(template, pid, s.dilution))
                controls = {p.replicate for _, _, p in cells if p.sample_slot == e.control_for(c.code)}
                reps = {p.replicate for _, _, p in cells if p.sample_slot in e.scored_slots(c.code)}
                if reps - controls:
                    errors.append(f"{c.code}, template plate {pid}: multi-step analysis needs a control in every biological block.")
    if resolution is None:
        return errors
    excluded = exclusions(e, flagged_plates)
    rows = [r for r in selected_rows(e, resolution) if r.relpath not in excluded]
    missing = [r.relpath for r in rows if not s.plate_ids.get(r.relpath)]
    if missing:
        errors.append(f"Assign technical plate labels to {len(missing)} selected photo(s); labels must follow the same physical plate across hours.")
    keys = set()
    plate_positions = set(geometry.plate_ids(template)) if template is not None else None
    for r in rows:
        if plate_positions is not None and str(r.plate) not in plate_positions:
            errors.append(f"{r.relpath}: unknown template plate {r.plate}.")
        label = s.plate_ids.get(r.relpath)
        key = (r.condition, r.plate, label, r.timepoint)
        if label and key in keys:
            errors.append(f"{r.condition}, template plate {r.plate}, technical plate {label}: duplicate photo at {r.timepoint:g} h. Exclude the duplicate or correct its label.")
        keys.add(key)
    for c in e.conditions:
        # Conditions grow on different schedules, so a condition need not have
        # a photo at every selected hour; it only needs one at some of them.
        if not any(r.condition == c.code for r in rows):
            errors.append(f"{c.code}: no included photo at any selected hour.")
        if s.scope == "repeated":
            tracks = {}
            for r in rows:
                if r.condition == c.code:
                    tracks.setdefault((r.plate, s.plate_ids.get(r.relpath)), set()).add(r.timepoint)
            if tracks and not any(len(times) > 1 for times in tracks.values()):
                errors.append(f"{c.code}: no physical technical plate is linked across timepoints.")
        tracks_at_time = {}
        for r in rows:
            if r.condition == c.code:
                tracks_at_time.setdefault((r.plate, r.timepoint), set()).add(s.plate_ids.get(r.relpath))
        if tracks_at_time and not any(len(labels - {None}) > 1 for labels in tracks_at_time.values()):
            errors.append(f"{c.code}: include at least two technical plates for a template plate at the same selected hour.")
    unresolved = [r for r in resolution.rows if r.status == "unresolved"
                  and r.relpath not in excluded]
    if unresolved:
        errors.append(f"Resolve or explicitly exclude {len(unresolved)} unidentified photo(s) before multi-step analysis.")
    return list(dict.fromkeys(errors))
