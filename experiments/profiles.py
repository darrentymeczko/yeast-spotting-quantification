"""Naming profiles: how a photo's location tells you what it is.

Both existing pipelines hardcode one layout. `spotting_batch` demands
`<set>.<plate><TREATMENT>.JPG` in a flat folder; `spotting_timecourse.discover`
demands exactly `<N Hours>/<Medium>/Plate N/`. Anything else is unreadable, and
another lab's photos are unreadable by construction.

A profile makes that layout data instead of code. It is a list of rules, each
saying: take this part of the path, match this regex, and read the capture as
this facet. The two builtin profiles reproduce the current behaviour exactly --
their regexes are the ones already in `spotting_timecourse._hours`, `_plate_no`,
`_medium_code` and `spotting_batch.NAME_RE`, decomposed one facet at a time, so
existing data needs no configuration and produces the same answers.

Four facets are parsed:

    set         which panel this photo belongs to (flat layouts only; a capture
                tree says it with the folder you point at)
    condition   the medium/treatment, canonicalised to a short code
    timepoint   hours, for a time course
    plate       which plate slot of the template this photo is

A fifth, the technical-replicate index, is deliberately NOT parsed. In the real
corpus 232 of 415 files are literally named `_9.JPG` and the rest carry Windows
de-duplication suffixes: the filename holds no information at all. Which re-shot
a photo is follows from how many photos share its key, so it is counted at
resolve time rather than read out of a name that does not encode it.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The facets a rule may produce. `plate` and `condition` are always required;
#: `timepoint` is required in timecourse mode; `set` is optional and only used
#: to pick this experiment's photos out of a folder holding several panels.
FACETS = ("set", "condition", "timepoint", "plate")

#: How a captured string becomes a value. `code` is the condition
#: canonicaliser -- see `condition_code`.
TRANSFORMS = ("raw", "int", "hours", "code")

#: Where a rule reads its text from. `segment` indexes the path parts (negative
#: counts from the file upwards, which is what makes a rule survive an extra
#: folder level above it); `stem` is the filename without its extension.
SOURCES = ("segment", "stem")


class ProfileError(ValueError):
    """A naming profile is malformed. Messages name the offending rule."""


@dataclass(frozen=True)
class FacetRule:
    """One instruction: read `facet` out of one piece of the path.

    `patterns` are tried in order and the first that matches wins, which is how
    a single facet tolerates more than one way of writing it -- "40 Hours" and
    a bare "40" are the same timepoint, and `spotting_timecourse._hours` already
    accepts both.
    """

    facet: str
    source: str = "segment"
    #: Only meaningful when `source` is "segment". Negative counts from the
    #: file: -1 is the filename, -2 its folder, and so on.
    depth: int = -1
    patterns: tuple[str, ...] = ()
    transform: str = "raw"

    def check(self) -> None:
        where = f"rule for {self.facet!r}"
        if self.facet not in FACETS:
            raise ProfileError(
                f"{where}: unknown facet; expected one of {', '.join(FACETS)}"
            )
        if self.source not in SOURCES:
            raise ProfileError(
                f"{where}: unknown source {self.source!r}; "
                f"expected one of {', '.join(SOURCES)}"
            )
        if self.transform not in TRANSFORMS:
            raise ProfileError(
                f"{where}: unknown transform {self.transform!r}; "
                f"expected one of {', '.join(TRANSFORMS)}"
            )
        if not self.patterns:
            raise ProfileError(f"{where}: has no patterns")
        for pat in self.patterns:
            try:
                compiled = re.compile(pat, re.I)
            except re.error as exc:
                raise ProfileError(f"{where}: {pat!r} is not a regex: {exc}") from exc
            if compiled.groups < 1:
                raise ProfileError(
                    f"{where}: {pat!r} has no capture group; a pattern must "
                    f"capture the value it reads"
                )


@dataclass
class NamingProfile:
    """A complete description of one folder layout."""

    name: str = "Custom"
    rules: tuple[FacetRule, ...] = ()
    #: Lower-cased, whitespace-collapsed condition text -> canonical code.
    #: The user-editable generalisation of `spotting_timecourse.MEDIUM_CODES`;
    #: it is what collapses "Potassium Acetate", "k acetate" and "K-OAc" onto
    #: one condition without touching code.
    aliases: dict[str, str] = field(default_factory=dict)

    def check(self) -> None:
        for rule in self.rules:
            rule.check()

    def rule_for(self, facet: str) -> FacetRule | None:
        for rule in self.rules:
            if rule.facet == facet:
                return rule
        return None

    def facets(self) -> set[str]:
        return {rule.facet for rule in self.rules}


# ---------------------------------------------------------------------------
# Applying a profile
# ---------------------------------------------------------------------------


def condition_code(name: str, aliases: dict[str, str]) -> str:
    """Canonical short code for a condition, e.g. "Potassium Acetate" -> "K-OAc".

    Mirrors `spotting_timecourse._medium_code` exactly, with the alias table
    supplied rather than hardcoded, so a capture tree read through the builtin
    profile produces the same codes the pipeline has always produced -- and
    therefore the same result filenames.
    """
    key = re.sub(r"\s+", " ", name.strip().lower())
    if key in aliases:
        return aliases[key]
    return re.sub(r"[^A-Za-z0-9-]+", "", name.strip()).upper()[:8] or "CONDITION"


def _text_for(parts: tuple[str, ...], rule: FacetRule) -> str | None:
    """The piece of the path this rule reads, or None if the path is too short."""
    if rule.source == "stem":
        stem = parts[-1] if parts else ""
        return stem.rsplit(".", 1)[0] if "." in stem else stem
    depth = rule.depth
    if depth < 0:
        depth += len(parts)
    if not 0 <= depth < len(parts):
        return None
    return parts[depth]


def _transform(raw: str, rule: FacetRule, aliases: dict[str, str]):
    text = raw.strip()
    if rule.transform == "raw":
        return text or None
    if rule.transform == "int":
        try:
            return int(text)
        except ValueError:
            return None
    if rule.transform == "hours":
        try:
            return float(text)
        except ValueError:
            return None
    if rule.transform == "code":
        return condition_code(raw, aliases)
    raise ProfileError(f"rule for {rule.facet!r}: unknown transform {rule.transform!r}")


#: Facets whose label is the whole path segment rather than just the captured
#: text. Only `timepoint` qualifies, and for a concrete reason: the pipeline
#: names result folders and figure files after the timepoint folder verbatim
#: ("40 Hours", not "40"), so shortening the label here would rename every
#: output. Every other facet labels from its capture, because a filename that
#: packs several facets into one string ("7.2K-OAc") is not a label for any of
#: them.
_LABEL_FROM_SEGMENT = ("timepoint",)


def apply_rule(parts: tuple[str, ...], rule: FacetRule, aliases: dict[str, str]):
    """Read one facet, returning (value, label) or (None, label) on a miss.

    The label comes back alongside the value because a condition carries both:
    the code that names result files and the words the user actually wrote,
    which is what the figures show.
    """
    text = _text_for(parts, rule)
    if text is None:
        return None, None
    for pat in rule.patterns:
        m = re.search(pat, text, re.I)
        if m is not None:
            label = text if rule.facet in _LABEL_FROM_SEGMENT else m.group(1)
            return _transform(m.group(1), rule, aliases), label
    return None, text


def apply_profile(parts: tuple[str, ...], profile: NamingProfile) -> dict:
    """Read every facet this profile knows about out of one relative path.

    Returns a dict of facet -> value for those that matched, each alongside a
    `<facet>_label` holding the text it was read from. The labels are not
    decoration: the pipeline names result folders and figure files after the
    words the user actually wrote ("40 Hours", "Potassium Acetate"), not after
    the parsed value, so both have to survive.

    Facets that did not match are simply absent; deciding whether that is fatal
    belongs to the caller, which knows the processing mode.
    """
    out: dict = {}
    for rule in profile.rules:
        value, text = apply_rule(parts, rule, profile.aliases)
        if value is None:
            continue
        out[rule.facet] = value
        if text is not None:
            out[f"{rule.facet}_label"] = text.strip()
    return out


# ---------------------------------------------------------------------------
# Builtin profiles -- the two layouts the pipelines already understand
# ---------------------------------------------------------------------------

#: Seeded from `spotting_timecourse.MEDIUM_CODES`. Users extend this per
#: profile rather than editing code.
MEDIUM_ALIASES = {
    "glucose": "GLU",
    "glycerol": "GLY",
    "potassium acetate": "K-OAc",
    "k-oac": "K-OAc",
    "koac": "K-OAc",
}

# `spotting_timecourse._hours`: "40 Hours", "40h", or a bare "40".
HOURS_PATTERNS = (r"(\d+(?:\.\d+)?)\s*h", r"^\s*(\d+(?:\.\d+)?)\s*$")
# `spotting_timecourse._plate_no`.
PLATE_PATTERNS = (r"plate\s*(\d+)",)

# `spotting_batch.NAME_RE` = ^\s*(\d+)\s*\.\s*(\d+)\s*(.+?)\s*$ , decomposed
# one facet at a time. `tests/experiments/test_profiles.py` asserts the three
# together agree with `parse_name` on every name in the corpus.
_FLAT_SET = (r"^\s*(\d+)\s*\.",)
_FLAT_PLATE = (r"^\s*\d+\s*\.\s*(\d+)",)
_FLAT_CONDITION = (r"^\s*\d+\s*\.\s*\d+\s*(.+?)\s*$",)


def capture_tree() -> NamingProfile:
    """`<N Hours>/<Medium>/Plate N (…)/*.jpg` -- the raw capture layout.

    Depths are negative so the rules still read correctly when the tree sits
    below extra folders, which is exactly the case that defeats
    `spotting_timecourse.discover` today: `Set05/Andrea/Set 1/40 Hours/...`
    has a level the fixed walk does not expect.
    """
    return NamingProfile(
        name="Capture tree",
        rules=(
            FacetRule("timepoint", "segment", -4, HOURS_PATTERNS, "hours"),
            FacetRule("condition", "segment", -3, (r"^(.+)$",), "code"),
            FacetRule("plate", "segment", -2, PLATE_PATTERNS, "int"),
        ),
        aliases=dict(MEDIUM_ALIASES),
    )


def flat_lab() -> NamingProfile:
    """`<set>.<plate><TREATMENT>.JPG` in one folder -- the handpicked layout."""
    return NamingProfile(
        name="Flat filenames",
        rules=(
            FacetRule("set", "stem", -1, _FLAT_SET, "raw"),
            FacetRule("plate", "stem", -1, _FLAT_PLATE, "int"),
            FacetRule("condition", "stem", -1, _FLAT_CONDITION, "code"),
        ),
        aliases=dict(MEDIUM_ALIASES),
    )


BUILTINS = {"capture_tree": capture_tree, "flat_lab": flat_lab}


def builtin(key: str) -> NamingProfile:
    try:
        return BUILTINS[key]()
    except KeyError:
        raise ProfileError(
            f"unknown builtin profile {key!r}; "
            f"expected one of {', '.join(sorted(BUILTINS))}"
        ) from None


def list_builtins() -> list[tuple[str, str]]:
    return [(key, make().name) for key, make in sorted(BUILTINS.items())]


# ---------------------------------------------------------------------------
# Serialisation -- profiles are saved so one lab's layout is described once
# ---------------------------------------------------------------------------


def rule_to_dict(rule: FacetRule) -> dict:
    return {
        "facet": rule.facet,
        "source": rule.source,
        "depth": rule.depth,
        "patterns": list(rule.patterns),
        "transform": rule.transform,
    }


def rule_from_dict(d: dict, path: str) -> FacetRule:
    if not isinstance(d, dict):
        raise ProfileError(f"{path}: expected an object, got {type(d).__name__}")
    try:
        rule = FacetRule(
            facet=str(d["facet"]),
            source=str(d.get("source", "segment")),
            depth=int(d.get("depth", -1)),
            patterns=tuple(str(p) for p in d.get("patterns", ())),
            transform=str(d.get("transform", "raw")),
        )
    except KeyError as exc:
        raise ProfileError(f"{path}: missing required key {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    try:
        rule.check()
    except ProfileError as exc:
        # Re-raised with the JSON path, so a bad profile says where it is bad
        # rather than only what is bad.
        raise ProfileError(f"{path}: {exc}") from exc
    return rule


def profile_to_dict(p: NamingProfile) -> dict:
    return {
        "name": p.name,
        "rules": [rule_to_dict(r) for r in p.rules],
        "aliases": dict(p.aliases),
    }


def profile_from_dict(d: dict, path: str = "$") -> NamingProfile:
    if not isinstance(d, dict):
        raise ProfileError(f"{path}: expected an object, got {type(d).__name__}")
    raw_rules = d.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ProfileError(f"{path}.rules: expected a list")
    aliases = d.get("aliases", {}) or {}
    if not isinstance(aliases, dict):
        raise ProfileError(f"{path}.aliases: expected an object")
    return NamingProfile(
        name=str(d.get("name", "Custom")),
        rules=tuple(
            rule_from_dict(r, f"{path}.rules[{i}]") for i, r in enumerate(raw_rules)
        ),
        # Keys are normalised on the way in so a hand-written profile with
        # "Potassium Acetate" as a key still matches; `condition_code` looks up
        # the lower-cased, whitespace-collapsed form.
        aliases={
            re.sub(r"\s+", " ", str(k).strip().lower()): str(v)
            for k, v in aliases.items()
        },
    )
