"""Conservative treatment suggestions from lab photo names (no image reads).

These are reviewable labels, not biological annotations. In particular R1,
1a, 1.1 and camera counters never establish a plate or replicate identity.
"""

from __future__ import annotations

import re

HOURS = r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)(?![a-z])"
PLATE = r"(?<!\w)(?:plate\s*|p)(\d+)(?!\d)"
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = re.compile(
    rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?\b|"
    rf"\b\d{{1,2}}\s+{_MONTH}(?:\s+\d{{4}})?\b|"
    r"\b\d{1,4}[-/]\d{1,2}[-/]\d{1,4}\b", re.I)
_SESSION = re.compile(r"^(?:(?:next|same)\s+)?(?:day\s*\d*|morning|afternoon|evening|later|late|overnight|new folder|\d{1,4}\s*[ap]m)(?:\s.*)?$", re.I)
_CAMERA = re.compile(r"^(?:_?\d+(?:[_.]\d+)*|(?:img|dsc|image|image_g)[ _-]*\d+)(?:\s*\(\d+\))?$", re.I)
_KNOWN = re.compile(
    r"(?<![a-z0-9])(?:glucose|glu|glycerol|gly|galactose|gal|sgal|sd|ypd|"
    r"raffinose|dextrose|potassium\s+acetate|k[- ]?oac|h2o2|naaso2?|"
    r"azc|diamide|radicicol|rad|mg[- ]?132|znso4|untreated|control|"
    r"met[+-]|m[+-])(?![a-z])", re.I)
_TEMP = re.compile(r"(?<![\w.])(\d{2}(?:\.\d+)?)\s*(?:°\s*)?(?:c\b|degrees?\b)", re.I)
_BARE_TEMP = re.compile(r"(?<![\w.])(25|30|37|42)(?![\w.]|\s*(?:[ap]m|%|[munµμ]m))", re.I)


def _clean(text: str) -> str:
    text = text.replace("_", " ")
    text = re.sub(HOURS, " ", text, flags=re.I)
    text = re.sub(PLATE, " ", text, flags=re.I)
    text = re.sub(r"(?<!\w)(?:rep(?:licate)?\s*|r)\d+(?:\s*[+,]\s*\d+)*", " ", text, flags=re.I)
    text = re.sub(r"\b(?:next day|next morning|morning|afternoon|evening|later|late|retake|again)\b", " ", text, flags=re.I)
    text = re.sub(r"\b\d{1,4}\s*[ap]m\b", " ", text, flags=re.I)
    text = re.sub(r"\(\s*\d+\s*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" .,()[]")


def treatment_label(parts: tuple[str, ...]) -> str:
    """Combine meaningful nearby labels, retaining temperature and dosage.

    Folder order does not define meaning: both 30C/Glucose and Glucose/30C
    describe the same suggestion. Dates and session words do not imply elapsed
    hours. Unknown filename text is retained for review, never spell-corrected.
    """
    labels: dict[str, str] = {}
    temperatures: set[str] = set()
    unknown_folders: list[str] = []
    for i, part in enumerate(parts):
        is_file = i == len(parts) - 1
        text = part.rsplit(".", 1)[0] if is_file else part
        if _DATE.search(text):
            if not is_file:
                continue
            text = _DATE.sub(" ", text)
        if _SESSION.fullmatch(text.strip()):
            continue
        if _CAMERA.fullmatch(text.strip()) and text.strip() not in ("25", "30", "37", "42"):
            continue
        clean = _clean(text)
        # A bare numeric directory is not an elapsed time. In this corpus these
        # four standalone numbers commonly label incubation temperature.
        temps = list(_TEMP.finditer(clean)) + list(_BARE_TEMP.finditer(clean))
        for match in temps:
            temperatures.add(f"{float(match.group(1)):g}C")
        clean = _TEMP.sub(" ", clean)
        clean = _BARE_TEMP.sub(" ", clean)
        clean = re.sub(r"(?:^|\s)\d{1,2}[a-z]?$", " ", clean, flags=re.I)
        clean = re.sub(r"\s+", " ", clean).strip(" .,()[]")
        clean = re.sub(r"^[- ]+|\s+-+$", "", clean)
        if not clean or re.fullmatch(r"[\d\s.,+]+", clean):
            continue
        # Arbitrary project/owner folders are not conditions. An arbitrary
        # filename can be a treatment, so retain it if it isn't a camera ID.
        if _KNOWN.search(clean) or (is_file and not re.fullmatch(r"\d+[a-z]", clean, re.I)):
            labels.setdefault(clean.casefold(), clean)
        elif not is_file and not re.fullmatch(r"(?:tp|set[\s_-]*\d+|r\d+)", clean, re.I):
            unknown_folders.append(clean)
    if not labels and unknown_folders:
        label = unknown_folders[-1]
        labels[label.casefold()] = label
    values = [labels[k] for k in sorted(labels)]
    return " + ".join(values + sorted(temperatures))
