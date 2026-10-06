"""Plain-language choices for the underlying naming rules."""

from ..profiles import FacetRule, apply_rule, HOURS_PATTERNS, PLATE_PATTERNS, flat_lab
from ..names import HOURS, PLATE

FORMATS = {
    "condition": {"Use the whole name": (r"^(.+)$",)},
    "timepoint": {
        "Hours written as 16h or 16 Hours": (HOURS,),
        "Numbers are hours (16, 40, ...)": (r"^\s*(\d+(?:\.\d+)?)\s*$",),
    },
    "plate": {
        "Plate 1, Plate 2 or P1, P2": (PLATE,),
        "Just a plate number (1, 2, ...)": (r"^\s*(\d+)\s*$",),
        "R1, R2 are my plate numbers": (r"^\s*R\s*(\d+)\s*$",),
    },
    "set": {
        "Set 1, Set 2 or Set01, Set02": (r"set[\s_-]*0*(\d+)",),
        "Use the whole name": (r"^(.+)$",),
    },
}
TRANSFORMS = {"condition": "code", "plate": "int", "timepoint": "hours", "set": "raw"}


def location_choices(files, current=None):
    """Label folder positions with actual names, never signed indexes."""
    choices = {"Assign in the photo list": None,
               "Photo filename": ("stem", -1),
               "Look through folders and filenames": ("path", -1)}
    depths = list(range(-2, -max((len(f.parts) for f in files), default=1) - 1, -1))
    if current and current.source == "segment" and current.depth not in depths:
        depths.append(current.depth)
    for depth in depths:
        names = []
        for file in files:
            index = depth if depth >= 0 else len(file.parts) + depth
            if 0 <= index < len(file.parts):
                name = file.parts[index]
                if name not in names:
                    names.append(name)
            if len(names) == 3:
                break
        if depth == -2:
            label = "Folder containing the photo"
        elif depth < -2:
            n = -depth - 2
            label = "One folder above that" if n == 1 else f"{n} folders above the photo's folder"
        elif depth == -1:
            label = "Photo filename (including extension)"
        else:
            label = f"Folder {depth + 1} below the chosen data folder"
        if names:
            label = ", ".join(n[:35] for n in names) + " — " + label.lower()
        choices[label] = ("segment", depth)
    return choices


def reading_choices(files, facet, current, aliases):
    """One choice states both where to look and how to interpret the name.

    Only offer presets that read at least one supplied photo. Always retain an
    exact saved rule, even if it currently matches nothing, so a UI refresh or
    applying another row cannot change a custom interpretation.
    """
    locations = location_choices(files, current)
    choices = {}

    def location_text(rule):
        if rule.source == "stem":
            return "photo filenames"
        if rule.source == "path":
            return "folders and photo filenames"
        location = next(label for label, value in locations.items()
                        if value == (rule.source, rule.depth))
        if " — " in location:
            names, position = location.split(" — ", 1)
            return f"‘{names}’ folders ({position})"
        return location.lower()

    def describe(rule, format_name=None):
        if rule.source == "treatment":
            return "Suggest treatments from folders and filenames"
        location = location_text(rule)
        what = {"condition": "treatment names", "timepoint": "hours",
                "plate": "plate numbers", "set": "strain groups"}[facet]
        if format_name and format_name.startswith("Numbers are hours"):
            return f"Use numbers in {location} as hours"
        if format_name and format_name.startswith("Just a plate number"):
            return f"Use numbers in {location} as plate numbers"
        if format_name and format_name.startswith("R1, R2"):
            return f"Use R1, R2 in {location} as plate numbers"
        if facet == "set" and format_name == "Use the whole name":
            return f"Use full names in {location} as strain groups"
        return f"Read {what} from {location}"

    if current is not None:
        format_name = next((name for name, patterns in FORMATS[facet].items()
                            if current.patterns == patterns), None)
        label = describe(current, format_name)
        known = format_name is not None or current.source == "treatment"
        known |= facet == "timepoint" and current.patterns == HOURS_PATTERNS
        known |= facet == "plate" and current.patterns == PLATE_PATTERNS
        flat = flat_lab().rule_for(facet)
        if flat and current.source == "stem" and current.patterns == flat.patterns:
            label += " (set.plateTreatment names)"
            known = True
        if not known:
            label += " (saved custom reading)"
        choices[label] = current
    if facet == "condition" and (current is None or current.source != "treatment"):
        choices["Suggest treatments from folders and filenames"] = FacetRule(
            facet, "treatment", -1, (r"^(.+)$",), "code")

    for location in locations.values():
        if location is None:
            continue
        source, depth = location
        # A whole path is not a treatment name or a strain group name.
        if source == "path" and facet in ("condition", "set"):
            continue
        for format_name, patterns in FORMATS[facet].items():
            rule = FacetRule(facet, source, depth, patterns, TRANSFORMS[facet])
            if not any(apply_rule(f.parts, rule, aliases)[0] is not None for f in files):
                continue
            label = describe(rule, format_name)
            # The saved interpretation wins when it already describes this
            # choice, including older built-in patterns with the same purpose.
            choices.setdefault(label, rule)
    choices["All photos belong to one strain group" if facet == "set"
            else "Assign individually in the photo list"] = None
    return choices


def example_text(files, rule, aliases):
    if rule is None:
        return "Not read from names. Assign in the photo list if needed."
    found = []
    missing = 0
    for file in files:
        value, label = apply_rule(file.parts, rule, aliases)
        if value is None:
            missing += 1
        else:
            text = (label or str(value)) if rule.facet == "condition" else str(value)
            if rule.facet == "timepoint":
                text = f"{float(value):g} hours"
            if text not in found and len(found) < 3:
                found.append(text)
    if not files:
        return "Choose a data folder to see examples."
    if not found:
        return "No matches. Choose another reading or assign individual photos."
    text = "Reads: " + ", ".join(found)
    if missing:
        text += f" · {missing} photo(s) need a manual assignment"
    return text
