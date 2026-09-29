"""Turning a folder of photographs into a table of known plates.

Three steps, deliberately separable so each can be tested and, in the GUI,
corrected on its own:

    scan_images   what image files exist under the root
    infer_profile what the folder layout appears to mean
    resolve       what each individual photo therefore is

The inference is structural rather than statistical. It looks for the word
"plate" to find the plate level, an hours-bearing name to find the timepoint
level, and takes what is left as the condition -- instead of scoring every
regex against every folder level and picking a winner. That is a deliberate
trade: a heuristic that guesses plate identity from cardinality would sometimes
swap plate 1 and plate 2, and `spotting_timecourse.scan_extras` already states
why that is the one thing never to guess -- "working out which photo is plate 1
and which is plate 2 would be a guess, and getting it wrong silently mislabels
replicates". So when a level cannot be identified, no rule is emitted and the
photos arrive unresolved, in front of the user, rather than confidently wrong.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from .model import QUANTIFY, TIMECOURSE, Experiment
from .profiles import (
    HOURS_PATTERNS,
    PLATE_PATTERNS,
    FacetRule,
    NamingProfile,
    apply_profile,
    builtin,
    condition_code,
)
from .validate import Issue, Severity

#: What reading a layout tries to recover, per mode.
#:
#: Deliberately NOT `validate.REQUIRED_FACETS`, which says what a *run* needs.
#: A handpicked run needs nothing from the profile, because its photographs are
#: chosen by hand -- but when reading a folder we still want everything the
#: names actually encode, both to recognise the flat `<set>.<plate><TREATMENT>`
#: layout and so a migrated experiment, whose photos were never picked in a
#: window, can still find them.
INFER_FACETS = {
    QUANTIFY: ("condition", "plate"),
    TIMECOURSE: ("condition", "plate", "timepoint"),
}

#: Mirrors `spotting_quant.IMAGE_EXTS`. Re-declared rather than imported so the
#: intake layer stays usable without numpy -- the same convention
#: `results_review/discovery.py` follows, and
#: `tests/experiments/test_matches_pipeline.py` asserts they still agree.
IMAGE_EXTS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp"}

# OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS -- from
# `spotting_timecourse._is_cloud_stub`.
_CLOUD_MASK = 0x1000 | 0x40000 | 0x400000

#: How deep below the root to look. The real corpus nests four levels
#: (timepoint/medium/plate/file) and guest sessions add two more.
MAX_DEPTH = 8


@dataclass(frozen=True)
class ImageFile:
    """One image found under the photo root."""

    #: Forward-slash relative path from the root. Forward slashes so a file
    #: keyed on one machine still matches on another, and so the key a manual
    #: override is stored under does not depend on the OS that wrote it.
    relpath: str
    parts: tuple[str, ...]
    size: int = 0
    #: A OneDrive Files-On-Demand placeholder: the name is here, the pixels are
    #: not. Reported rather than read, because reading one silently stalls.
    cloud_only: bool = False


@dataclass(frozen=True)
class PhotoRow:
    """One photo, and what the experiment believes it to be.

    This is the row the user sees and edits in the intake table.
    """

    relpath: str
    set_key: str | None = None
    condition: str | None = None
    condition_label: str = ""
    timepoint: float | None = None
    timepoint_label: str = ""
    plate: int | None = None
    #: 1-based index among the photos sharing this key -- which re-shot of the
    #: same plate this is. Counted, never parsed: in the real corpus 232 of 415
    #: files are named `_9.JPG` and the name encodes nothing.
    shot: int = 1
    #: "ok" | "unresolved" | "ignored" | "other_set"
    status: str = "unresolved"
    #: Facets that could not be read, for the ones that are unresolved.
    missing: tuple[str, ...] = ()
    cloud_only: bool = False

    @property
    def key(self) -> tuple:
        """What makes two photos alternatives for the same slot in the design.

        Two photos with the same key are re-shots of one plate and are
        interchangeable; the time course pairs one of each across plates.
        """
        return (self.set_key, self.condition, self.timepoint, self.plate)

    @property
    def group(self) -> tuple:
        """The key without the plate: everything photographed at one sitting.

        A complete group is what can actually be quantified, because a
        replicate is normalised to the control on its own plate and the design
        needs every plate present.
        """
        return (self.set_key, self.condition, self.timepoint)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


@dataclass
class Resolution:
    rows: list[PhotoRow] = field(default_factory=list)
    complaints: list[str] = field(default_factory=list)

    def usable(self) -> list[PhotoRow]:
        return [r for r in self.rows if r.is_ok]

    def unresolved(self) -> list[PhotoRow]:
        return [r for r in self.rows if r.status == "unresolved"]

    def by_key(self) -> dict[tuple, list[PhotoRow]]:
        """Re-shots of one plate, keyed by (set, condition, timepoint, plate)."""
        out: dict[tuple, list[PhotoRow]] = {}
        for row in self.usable():
            out.setdefault(row.key, []).append(row)
        return out

    def by_group(self) -> dict[tuple, dict[int, list[PhotoRow]]]:
        """Photos per sitting, then per plate: {(set, condition, tp): {plate: rows}}."""
        out: dict[tuple, dict[int, list[PhotoRow]]] = {}
        for row in self.usable():
            out.setdefault(row.group, {}).setdefault(row.plate, []).append(row)
        return out

    def conditions(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.usable():
            if row.condition:
                seen.setdefault(row.condition, None)
        return list(seen)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _is_cloud_stub(path: Path) -> bool:
    try:
        return bool(getattr(path.stat(), "st_file_attributes", 0) & _CLOUD_MASK)
    except OSError:
        return False


def scan_images(root: Path, *, max_depth: int = MAX_DEPTH) -> tuple[list[ImageFile], list[str]]:
    """Every image under `root`, at any depth, with what could not be read.

    Unlike the pipelines' walkers this makes no assumption about the shape of
    the tree -- it only finds files. Interpreting them is `resolve`'s job.
    """
    root = Path(root)
    files: list[ImageFile] = []
    complaints: list[str] = []
    if not root.is_dir():
        return files, [f"{root}: not a folder"]

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth:
            complaints.append(
                f"{_rel(folder, root)}: deeper than {max_depth} levels, not searched"
            )
            return
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            complaints.append(f"{_rel(folder, root)}: could not be read ({exc})")
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                walk(entry, depth + 1)
            elif entry.suffix.lower() in IMAGE_EXTS:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                rel = entry.relative_to(root)
                files.append(
                    ImageFile(
                        relpath=rel.as_posix(),
                        parts=rel.parts,
                        size=size,
                        cloud_only=_is_cloud_stub(entry),
                    )
                )

    walk(root, 1)
    files.sort(key=lambda f: f.relpath.lower())
    return files, complaints


def _natural_sort(values) -> list[str]:
    """Sort so set 2 comes before set 10, the way a person reads them."""
    def key(v: str):
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", v)]

    return sorted(values, key=key)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

# `spotting_timecourse.set_id_from_name`.
_SET_PATTERNS = (r"set[\s_-]*0*(\d+)",)
# Only the hours-bearing form is used for inference. The bare-number
# alternative stays available to a hand-built profile, but inferring from it
# would happily read a folder called "2024" as a timepoint.
_HOURS_STRICT = (r"(\d+(?:\.\d+)?)\s*h",)
_BARE_NUMBER = (r"^\s*(\d+(?:\.\d+)?)\s*$",)


def _texts_at(files: list[ImageFile], depth: int) -> list[str]:
    out = []
    for f in files:
        d = depth + len(f.parts) if depth < 0 else depth
        if 0 <= d < len(f.parts):
            out.append(f.parts[d])
    return out


def _hit_rate(texts: list[str], patterns: tuple[str, ...]) -> float:
    if not texts:
        return 0.0
    hits = sum(1 for t in texts if any(re.search(p, t, re.I) for p in patterns))
    return hits / len(texts)


def _best_slot(
    files: list[ImageFile],
    depths: list[int],
    patterns: tuple[str, ...],
    *,
    threshold: float = 0.9,
) -> int | None:
    """The folder level where `patterns` matches nearly every photo.

    Ties break toward the level nearest the file, which is where the more
    specific label lives -- the same reasoning `choose_extras` uses when a take
    names a set of its own.
    """
    best, best_rate = None, threshold
    for depth in depths:
        rate = _hit_rate(_texts_at(files, depth), patterns)
        if rate >= best_rate:
            best, best_rate = depth, rate
    return best


def coverage(files: list[ImageFile], profile: NamingProfile, mode: str) -> float:
    """Fraction of photos for which this profile reads every required facet."""
    if not files:
        return 0.0
    required = INFER_FACETS.get(mode, ())
    ok = 0
    for f in files:
        got = apply_profile(f.parts, profile)
        if all(got.get(facet) is not None for facet in required):
            ok += 1
    return ok / len(files)


def infer_profile(
    files: list[ImageFile], *, mode: str = TIMECOURSE
) -> tuple[NamingProfile, list[str]]:
    """Work out what this folder layout means.

    Returns the profile and a plain-language report of what it decided, which
    the GUI shows above the table: the user is being asked to confirm a reading
    of their own filing system, and cannot do that from a list of regexes.
    """
    report: list[str] = []
    if not files:
        return NamingProfile(name="Custom"), ["no images found"]

    # Work from the commonest nesting depth. A stray photo two levels up should
    # not redefine where the condition folder is; it will surface as an
    # unresolved row instead, which is the right place to deal with it.
    depth_counts = Counter(len(f.parts) for f in files)
    modal, modal_n = depth_counts.most_common(1)[0]
    pool = [f for f in files if len(f.parts) == modal]
    if len(depth_counts) > 1:
        report.append(
            f"{modal_n} of {len(files)} photos are {modal - 1} "
            f"{'folder' if modal == 2 else 'folders'} deep; the layout was read "
            f"from those"
        )

    # Directory levels only: -2 is the photo's own folder, -modal the outermost.
    dir_depths = [-d for d in range(2, modal + 1)]

    for key in ("capture_tree", "flat_lab"):
        candidate = builtin(key)
        if coverage(pool, candidate, mode) != 1.0:
            continue
        if mode == TIMECOURSE and not _timepoints_look_real(pool, candidate):
            # The layout fits, but the level the profile calls the timepoint
            # holds one constant number -- a year, a project code. Reading it
            # as a time course would invent an axis, so fall through to
            # structural inference, which applies the same test and will leave
            # the timepoint unset for the user to decide.
            continue
        report.append(f"matches the built-in {candidate.name!r} layout exactly")
        # Still look for a set level. A builtin describes one panel's layout,
        # but the user may have pointed at the folder that CONTAINS the panels
        # -- the whole `Deletion Strains` tree rather than one `SetNN` inside
        # it. Reading the set lets `resolve` see that the folder holds several
        # panels, which would otherwise merge ten strain panels into one
        # experiment without a word.
        taken = {r.depth for r in candidate.rules}
        extra = _infer_set_rule(pool, [d for d in dir_depths if d not in taken])
        if extra is not None:
            candidate.rules = candidate.rules + (extra,)
            report.append(f"{_level_name(extra.depth)} names the set")
        return candidate, report

    rules: list[FacetRule] = []
    aliases: dict[str, str] = {}

    plate_depth = _best_slot(pool, dir_depths, PLATE_PATTERNS)
    if plate_depth is not None:
        rules.append(FacetRule("plate", "segment", plate_depth, PLATE_PATTERNS, "int"))
        report.append(f"{_level_name(plate_depth)} names the plate")
    else:
        report.append(
            "no folder level names a plate (\"Plate 1\", \"Plate 2\"). Which photo "
            "is which plate has to be set by hand -- guessing it would silently "
            "mislabel replicates"
        )

    tp_depth = None
    if mode == TIMECOURSE:
        rest = [d for d in dir_depths if d != plate_depth]
        tp_depth = _best_slot(pool, rest, _HOURS_STRICT)
        if tp_depth is None:
            tp_depth = _best_slot(pool, rest, _BARE_NUMBER)
            if tp_depth is not None and len(set(_texts_at(pool, tp_depth))) < 2:
                tp_depth = None  # a single constant number is not a time course
        if tp_depth is not None:
            rules.append(
                FacetRule("timepoint", "segment", tp_depth, HOURS_PATTERNS, "hours")
            )
            report.append(f"{_level_name(tp_depth)} names the timepoint")
        else:
            report.append(
                "no folder level names a timepoint (\"40 Hours\"); a time course "
                "cannot be assembled until one is set"
            )

    used = {plate_depth, tp_depth}
    remaining = [d for d in dir_depths if d not in used]
    if remaining:
        # Nearest the photo wins: the more specific label.
        cond_depth = max(remaining)
        rules.append(FacetRule("condition", "segment", cond_depth, (r"^(.+)$",), "code"))
        labels = sorted(set(_texts_at(pool, cond_depth)))
        aliases = _alias_table(labels)
        shown = ", ".join(labels[:6]) + (" ..." if len(labels) > 6 else "")
        report.append(f"{_level_name(cond_depth)} names the condition ({shown})")
    else:
        report.append(
            "no folder level is left to name the condition; if every photo here "
            "is one medium, declare that single condition and it will be applied "
            "to all of them"
        )

    taken = {r.depth for r in rules}
    extra = _infer_set_rule(pool, [d for d in dir_depths if d not in taken])
    if extra is not None:
        rules.append(extra)
        report.append(f"{_level_name(extra.depth)} names the set")

    if not rules and modal == 1:
        report.append(
            "the photos sit directly in the folder, so everything must come "
            "from their filenames"
        )

    return NamingProfile(name="Detected", rules=tuple(rules), aliases=aliases), report


def _timepoints_look_real(files: list[ImageFile], profile: NamingProfile) -> bool:
    """Does the timepoint level actually hold timepoints?

    Either it says so ("40 Hours"), or it varies -- a bare number that never
    changes is a year or a project code, not a time course.
    """
    rule = profile.rule_for("timepoint")
    if rule is None or rule.source != "segment":
        return True
    texts = _texts_at(files, rule.depth)
    if _hit_rate(texts, _HOURS_STRICT) >= 0.9:
        return True
    return len(set(texts)) >= 2


def _infer_set_rule(files: list[ImageFile], depths: list[int]) -> FacetRule | None:
    """A folder level that names a strain panel, e.g. "Set01".

    Unlike the other facets this one is looked for everywhere, including above
    the layout a builtin profile describes: whether the chosen folder holds one
    panel or ten is the question `set_key` exists to answer.
    """
    depth = _best_slot(files, depths, _SET_PATTERNS)
    if depth is None:
        return None
    return FacetRule("set", "segment", depth, _SET_PATTERNS, "raw")


def _level_name(depth: int) -> str:
    if depth == -2:
        return "the folder each photo sits in"
    return f"the folder {-depth - 1} levels above each photo"


def _alias_table(labels: list[str]) -> dict[str, str]:
    """Seed aliases so differently-spelled folders collapse onto one code.

    Starts from the built-in medium names, then adds any label that would
    otherwise produce a code already taken by a different spelling, which is how
    "Potassium Acetate" and "k acetate" end up as the same condition.
    """
    table = dict(builtin("capture_tree").aliases)
    for label in labels:
        key = re.sub(r"\s+", " ", label.strip().lower())
        table.setdefault(key, condition_code(label, table))
    return table


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_COERCE = {
    "plate": lambda v: int(v),
    "timepoint": lambda v: float(v),
    "set": lambda v: str(v).strip(),
    "condition": lambda v: str(v).strip(),
}


def _coerce(facet: str, value):
    if value is None or value == "":
        return None
    fn = _COERCE.get(facet)
    if fn is None:
        return value
    try:
        return fn(value)
    except (TypeError, ValueError):
        return None


def _match_declared_condition(e: Experiment, facets: dict) -> None:
    """Replace a generated condition code with the experiment's own code.

    A capture-tree folder carries the human label (for example
    ``Glucose NaAsO2``).  The generic profile also generates a short fallback
    code, but that code is necessarily lossy: it truncates both ``Glycerol 37``
    and ``Glycerol NaAsO2`` to ``GLYCEROL``.  Once conditions have been declared
    in an experiment, their codes and display labels are the authority.  Match
    the complete folder label against those declarations before falling back
    to the generated code.

    Only an unambiguous exact match after case/whitespace normalisation is
    accepted.  Manual per-photo overrides are applied afterwards and therefore
    still take precedence over this automatic mapping.
    """
    parsed = facets.get("condition")
    if parsed is None or not e.conditions:
        return

    def key(value) -> str:
        return re.sub(r"\s+", " ", str(value).strip()).casefold()

    # Preserve an existing declared code, including a harmless case mismatch.
    code_matches = {c.code for c in e.conditions if key(c.code) == key(parsed)}
    if len(code_matches) == 1:
        facets["condition"] = next(iter(code_matches))
        return

    label = facets.get("condition_label")
    if not label:
        return
    label_key = key(label)
    matches = {
        c.code for c in e.conditions
        if label_key in (key(c.code), key(c.display()))
    }
    if len(matches) == 1:
        facets["condition"] = next(iter(matches))


def resolve(e: Experiment, files: list[ImageFile]) -> Resolution:
    """Decide what every photo is, under this experiment's profile and overrides.

    Precedence is fixed and total: `ignored` beats `overrides`, which beat the
    profile. Overrides are keyed on the relative path, so re-scanning after a
    few more photos arrive re-reads the layout without touching a single hand
    correction.
    """
    required = INFER_FACETS.get(e.mode, ())
    ignored = set(e.ignored)
    # An experiment with exactly one condition and no way to read one off the
    # path is not a failure: the whole folder is that condition. This is the
    # common small case -- one medium, no medium folder.
    sole_condition = (
        e.conditions[0]
        if len(e.conditions) == 1 and "condition" not in e.profile.facets()
        else None
    )

    rows: list[PhotoRow] = []
    complaints: list[str] = []
    for f in files:
        facets = apply_profile(f.parts, e.profile)
        _match_declared_condition(e, facets)
        for facet, value in e.overrides.get(f.relpath, {}).items():
            if facet.endswith("_label"):
                facets[facet] = str(value)
            elif facet in _COERCE:
                facets[facet] = _coerce(facet, value)

        if sole_condition is not None:
            facets.setdefault("condition", sole_condition.code)
            facets.setdefault("condition_label", sole_condition.display())

        missing = tuple(f_ for f_ in required if facets.get(f_) is None)
        if f.relpath in ignored:
            status = "ignored"
        elif e.set_key is not None and facets.get("set") not in (None, e.set_key):
            status = "other_set"
        elif missing:
            status = "unresolved"
        else:
            status = "ok"

        rows.append(
            PhotoRow(
                relpath=f.relpath,
                set_key=facets.get("set"),
                condition=facets.get("condition"),
                condition_label=facets.get("condition_label", "")
                or (facets.get("condition") or ""),
                timepoint=facets.get("timepoint"),
                timepoint_label=facets.get("timepoint_label", ""),
                plate=facets.get("plate"),
                status=status,
                missing=missing,
                cloud_only=f.cloud_only,
            )
        )

    rows = _number_shots(rows)
    return Resolution(rows=rows, complaints=complaints)


def _number_shots(rows: list[PhotoRow]) -> list[PhotoRow]:
    """Count the re-shots within each key, in stable path order."""
    counters: dict[tuple, int] = {}
    out: list[PhotoRow] = []
    for row in sorted(rows, key=lambda r: r.relpath.lower()):
        if not row.is_ok:
            out.append(row)
            continue
        n = counters.get(row.key, 0) + 1
        counters[row.key] = n
        out.append(replace(row, shot=n))
    return out


# ---------------------------------------------------------------------------
# Checking a resolution
# ---------------------------------------------------------------------------


def check_resolution(e: Experiment, res: Resolution, template=None) -> list[Issue]:
    """Problems with what the photos turned out to be.

    Kept apart from `validate.validate` because this one needs the disk: the
    experiment can be checked on its own merits without a folder present.
    """
    if e.mode == QUANTIFY:
        # Nothing here is the authority in handpicked mode: the photographs are
        # chosen one at a time and `validate._check_picks` judges those. All the
        # folder can say is what is physically in it.
        return _check_folder_contents(res)

    issues: list[Issue] = []
    usable = res.usable()

    if not usable:
        issues.append(
            Issue(
                Severity.ERROR,
                "no_photos_resolved",
                f"none of the {len(res.rows)} photos found could be identified; "
                f"check the naming profile or assign them by hand",
            )
        )
        return issues

    unresolved = res.unresolved()
    if unresolved:
        why = Counter(facet for row in unresolved for facet in row.missing)
        detail = ", ".join(f"{facet} ({n})" for facet, n in sorted(why.items()))
        issues.append(
            Issue(
                Severity.WARNING,
                "unresolved_photos",
                f"{len(unresolved)} of {len(res.rows)} photos could not be "
                f"identified and will be left out; missing: {detail}",
            )
        )

    cloud = [r for r in usable if r.cloud_only]
    if cloud:
        issues.append(
            Issue(
                Severity.WARNING,
                "photos_not_downloaded",
                f"{len(cloud)} photos are OneDrive placeholders that are not on "
                f"this disk yet; they will be fetched one at a time, which is "
                f"slow. Right-click the folder and choose 'Always keep on this "
                f"device' first",
            )
        )

    # The folder holds more than one strain panel. Nothing downstream would
    # notice: the photos all resolve, the conditions all look right, and ten
    # different panels quietly average together under one set of strain names.
    # `set_key` is the answer, so this is an error until one is chosen.
    if e.set_key is None:
        sets = _natural_sort({r.set_key for r in usable if r.set_key is not None})
        if len(sets) > 1:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "multiple_sets",
                    f"these photos span {len(sets)} different sets "
                    f"({', '.join(sets)}). Each set is its own strain panel and "
                    f"they must never be pooled: either point at a single set's "
                    f"folder, or choose which set this experiment is",
                )
            )

    declared = set(e.condition_codes())
    found = set(res.conditions())
    undeclared = sorted(found - declared)
    if undeclared and not declared:
        # Nothing declared yet -- this is a new experiment being set up, not a
        # mistake. Say what is there once, rather than scolding per condition.
        issues.append(
            Issue(
                Severity.INFO,
                "conditions_found",
                f"the photos hold {len(undeclared)} conditions: "
                f"{', '.join(undeclared)}",
            )
        )
        undeclared = []
    for code in undeclared:
        n = sum(1 for r in usable if r.condition == code)
        issues.append(
            Issue(
                Severity.WARNING,
                "undeclared_condition",
                f"{n} photos read as condition {code!r}, which the experiment "
                f"does not declare; add it or they will be skipped",
                code,
            )
        )
    for code in sorted(declared - found):
        issues.append(
            Issue(
                Severity.WARNING,
                "condition_without_photos",
                f"condition {code!r} is declared but no photo resolved to it",
                code,
            )
        )

    issues.extend(_check_plate_coverage(e, res, template))
    return issues


def _check_folder_contents(res: Resolution) -> list[Issue]:
    """What the photo folder physically holds, regardless of what it means."""
    issues: list[Issue] = []
    if not res.rows:
        issues.append(
            Issue(Severity.ERROR, "no_photos_found",
                  "there are no images in the photo folder")
        )
        return issues
    cloud = [r for r in res.rows if r.cloud_only]
    if cloud:
        issues.append(
            Issue(
                Severity.WARNING,
                "photos_not_downloaded",
                f"{len(cloud)} of {len(res.rows)} photos are OneDrive "
                f"placeholders that are not on this disk yet; they will be "
                f"fetched as needed, which is slow. Right-click the folder and "
                f"choose 'Always keep on this device' first",
            )
        )
    return issues


def _check_plate_coverage(e: Experiment, res: Resolution, template) -> list[Issue]:
    """Every design needs all its plates together; a lone plate cannot be scored.

    A replicate lives on one plate and is normalised to the control on that same
    plate, so a group missing a plate is not half a result -- it is no result.
    `spotting_timecourse.candidates` already drops such a group silently; here
    it is at least said out loud.
    """
    groups = res.by_group()
    if not groups:
        return []

    # Which plates a sitting must have: the template's, when one is bound.
    # Without a template, fall back to every plate number seen anywhere, which
    # is the best available statement of what a complete sitting looks like.
    if template is not None and template.plates:
        wanted = {str(p.id) for p in template.plates}
    else:
        wanted = {str(r.plate) for r in res.usable()}

    incomplete: list[str] = []
    complete = 0
    for key, plates in sorted(groups.items(), key=lambda kv: str(kv[0])):
        missing = sorted(wanted - {str(p) for p in plates})
        if missing:
            incomplete.append(f"{_key_label(key)} (no plate {', '.join(missing)})")
        else:
            complete += 1

    out: list[Issue] = []
    if complete == 0:
        plates_wanted = ", ".join(sorted(wanted))
        out.append(
            Issue(
                Severity.ERROR,
                "no_complete_group",
                f"no timepoint has every plate present ({plates_wanted}), so "
                f"nothing can be quantified: a replicate is normalised to the "
                f"control on its own plate, so a partial sitting is no result "
                f"rather than half of one",
            )
        )
    elif incomplete:
        shown = "; ".join(incomplete[:5])
        more = f" and {len(incomplete) - 5} more" if len(incomplete) > 5 else ""
        out.append(
            Issue(
                Severity.WARNING,
                "incomplete_group",
                f"{len(incomplete)} of {len(groups)} groups are missing a plate "
                f"and will be skipped: {shown}{more}",
            )
        )
    return out


def _key_label(key: tuple) -> str:
    set_key, condition, timepoint = key
    parts = [p for p in (set_key, condition) if p]
    if timepoint is not None:
        parts.append(f"{timepoint:g}h")
    return " ".join(parts) or "(unlabelled)"
