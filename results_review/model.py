"""What a review is made of.

Three things, kept deliberately separate:

* `Candidate` -- one scored row of `timecourse_candidates.csv`. Read-only; it
  is what the pipeline decided, and nothing here rewrites it.
* `SpotEdit` -- one correction to one spot. A decision, not a value: it says
  what the person changed and why, so the numbers can always be regenerated
  from the photos plus these.
* `Review` -- the pick per medium plus those edits, i.e. the entire state this
  tool owns. Round-trips through `review.json`.

`Candidate` holds photo FILE NAMES, not paths, because that is all the
candidates CSV records. Resolving them to files on disk needs the capture tree,
which is `links.py`'s job and is not required to browse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

#: Mirrors `spotting_batch.DILUTION_ORDER`: least -> most dilute. The index is
#: the `d0/d1/d2` token in a sheet filename, and the order spots are compared in.
DILUTION_ORDER = ["least", "middle", "most"]
# The keys of `spotting_plots.POSTHOC_METHODS`, repeated so this module stays
# free of the plotting stack's imports.
POSTHOC_METHODS = ("dunnett", "tukey", "holm", "bonferroni", "sidak", "none")

#: Identifies one spot within a medium's frame. Stable across a re-measure --
#: which is the point: re-running the pipeline must not silently strand a
#: correction. (Photo names are not in the key: a spot is "replicate 3, column
#: 8", and it stays that spot if a different pairing is chosen.)
SpotKey = tuple[str, int]     # (replicate, strain_col)


def _f(v, default=math.nan) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Candidate:
    """One scored (medium, timepoint, photo pairing, dilution).

    The four identity fields plus the medium are exactly what
    `spotting_timecourse_figures._match_candidate` keys on, so a Candidate can
    always be turned back into the pipeline's own candidate dict.
    """

    medium: str
    medium_label: str
    timepoint: str
    hours: float
    plate1: str
    plate2: str
    dilution: str

    # --- what the pipeline thought of it ---
    control_n: int = 0
    median_cv: float = math.nan
    control_mean: float = math.nan
    control_cv: float = math.nan
    n_strains: int = 0
    n_significant: int = 0
    rank_score: float = math.nan
    ranked_by: str = ""
    best_set_score: float = math.nan

    #: Position in the CSV's own within-medium order (which is `rank_score`
    #: order). Kept so "sort as the pipeline printed it" needs no re-derivation.
    csv_order: int = 0

    #: This candidate's dilution level, least -> most dilute, and how many levels
    #: the design has. Set by `discovery.load_set` from the run's recorded
    #: layout, since a design's level names ("neat", "1:10", ...) say nothing
    #: about their order. -1 / 0 mean "not recorded": the classic names are
    #: then ranked by DILUTION_ORDER, as they always were.
    level_index: int = -1
    level_count: int = 0

    #: Photos of plates 3, 4, ... for a design with more than two plates. A
    #: one-plate design leaves `plate2` empty. `photos` is the whole list.
    extra_plates: tuple = ()

    @property
    def photos(self) -> tuple:
        """Every plate's photo, in plate order -- one, two, or any number."""
        return tuple(p for p in (self.plate1, self.plate2, *self.extra_plates) if p)

    @property
    def key(self) -> tuple:
        """Identity. Enough to find the sheet, the candidate dict, and the row.

        The five-field form for one or two plates, as it always was; further
        plates are appended so candidates that differ only there stay distinct.
        """
        base = (self.medium, self.timepoint, self.plate1, self.plate2,
                self.dilution)
        return base + ((tuple(self.extra_plates),) if self.extra_plates else ())

    @property
    def dilution_rank(self) -> int:
        """Position in least -> most order, for sorting a time course properly.

        Also the `d<N>` token in the comparison sheet's filename, so it has to
        match what the pipeline wrote: the recorded level index when the run
        recorded its layout, else the classic order.
        """
        if self.level_index >= 0:
            return self.level_index
        try:
            return DILUTION_ORDER.index(self.dilution)
        except ValueError:
            return len(DILUTION_ORDER)

    @property
    def label(self) -> str:
        return f"{self.timepoint} · {self.dilution}"

    @property
    def detail(self) -> str:
        """The pairing, for telling technical replicates apart in a list."""
        return " + ".join(self.photos)

    @classmethod
    def from_row(cls, row: dict, order: int = 0) -> "Candidate":
        """Build from one row of `timecourse_candidates.csv`.

        The CSV has a `plate<k>` column per plate of the design; plates past the
        second go into `extra_plates`.
        """
        extra, k = [], 3
        while str(row.get(f"plate{k}", "") or "").strip():
            extra.append(str(row[f"plate{k}"]))
            k += 1
        return cls(
            extra_plates=tuple(extra),
            medium=str(row.get("medium", "")),
            medium_label=str(row.get("medium_label", "") or row.get("medium", "")),
            timepoint=str(row.get("timepoint", "")),
            hours=_f(row.get("hours")),
            plate1=str(row.get("plate1", "")),
            plate2=str(row.get("plate2", "")),
            dilution=str(row.get("dilution", "")).strip().lower(),
            control_n=_i(row.get("control_n")),
            median_cv=_f(row.get("median_CV")),
            control_mean=_f(row.get("control_mean")),
            control_cv=_f(row.get("control_CV")),
            n_strains=_i(row.get("n_strains")),
            n_significant=_i(row.get("n_significant")),
            rank_score=_f(row.get("rank_score")),
            ranked_by=str(row.get("ranked_by", "")),
            best_set_score=_f(row.get("best_set_score")),
            csv_order=order,
        )


@dataclass(frozen=True)
class SpotEdit:
    """One person's correction to one spot.

    Every field except the key is optional and means "leave this alone" when
    None, so an edit that only adds a note does not also assert an exclusion.
    `raw_growth` carries a hand-measured grey value; relative growth is never
    stored, because it is derived and storing it would let the two disagree.
    """

    replicate: str
    strain_col: int
    excluded: "bool | None" = None
    outlier: "bool | None" = None
    raw_growth: "float | None" = None
    note: str = ""

    @property
    def key(self) -> SpotKey:
        return (self.replicate, int(self.strain_col))

    @property
    def is_empty(self) -> bool:
        """True when this edit no longer says anything and can be dropped."""
        return (self.excluded is None and self.outlier is None
                and self.raw_growth is None and not self.note.strip())

    def to_dict(self) -> dict:
        out: dict = {"replicate": self.replicate, "strain_col": self.strain_col}
        if self.excluded is not None:
            out["excluded"] = bool(self.excluded)
        if self.outlier is not None:
            out["outlier"] = bool(self.outlier)
        if self.raw_growth is not None:
            out["raw_growth"] = float(self.raw_growth)
        if self.note.strip():
            out["note"] = self.note.strip()
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "SpotEdit":
        return cls(
            replicate=str(d.get("replicate", "")),
            strain_col=_i(d.get("strain_col")),
            excluded=None if d.get("excluded") is None else bool(d["excluded"]),
            outlier=None if d.get("outlier") is None else bool(d["outlier"]),
            raw_growth=(None if d.get("raw_growth") is None
                        else _f(d["raw_growth"])),
            note=str(d.get("note", "") or ""),
        )


@dataclass(frozen=True)
class Pick:
    """The candidate chosen for one medium, and why."""

    timepoint: str
    hours: float
    plate1: str
    plate2: str
    dilution: str
    reason: str = ""
    extra_plates: tuple = ()

    def matches(self, cand: Candidate) -> bool:
        return (cand.timepoint == self.timepoint
                and cand.plate1 == self.plate1
                and cand.plate2 == self.plate2
                and tuple(cand.extra_plates) == tuple(self.extra_plates)
                and cand.dilution == self.dilution)

    def to_dict(self) -> dict:
        d = {"timepoint": self.timepoint, "hours": self.hours,
             "plate1": self.plate1, "plate2": self.plate2,
             "dilution": self.dilution}
        if self.extra_plates:
            d["extra_plates"] = list(self.extra_plates)
        if self.reason.strip():
            d["reason"] = self.reason.strip()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Pick":
        return cls(timepoint=str(d.get("timepoint", "")), hours=_f(d.get("hours")),
                   plate1=str(d.get("plate1", "")), plate2=str(d.get("plate2", "")),
                   dilution=str(d.get("dilution", "")).strip().lower(),
                   reason=str(d.get("reason", "") or ""),
                   extra_plates=tuple(str(p) for p in d.get("extra_plates") or ()))

    @classmethod
    def of(cls, cand: Candidate, reason: str = "") -> "Pick":
        return cls(cand.timepoint, cand.hours, cand.plate1, cand.plate2,
                   cand.dilution, reason, tuple(cand.extra_plates))


@dataclass
class Review:
    """Everything this tool owns for one set: the picks and the corrections.

    Mutable, and the only mutable thing here -- the controller snapshots it for
    undo the way `plate_template` snapshots its template.
    """

    version: int = 1
    capture_root: str = ""
    statistical_test: str = "t_test"
    p_adjust: str = "none"
    alpha: float = 0.05
    # The test that follows an ANOVA to say which pairs differ.
    posthoc: str = "dunnett"
    # Strains every other strain is ALSO compared with, besides each medium's
    # control (which is always a reference). Ignored when `all_pairs`.
    extra_references: list[str] = field(default_factory=list)
    all_pairs: bool = False
    picks: dict[str, Pick] = field(default_factory=dict)
    edits: dict[str, dict[SpotKey, SpotEdit]] = field(default_factory=dict)

    # -- picks ---------------------------------------------------------------

    def pick(self, medium: str) -> "Pick | None":
        return self.picks.get(medium)

    def set_pick(self, medium: str, cand: Candidate, reason: str = "") -> None:
        self.picks[medium] = Pick.of(cand, reason)

    # -- edits ---------------------------------------------------------------

    def edits_for(self, medium: str) -> dict[SpotKey, SpotEdit]:
        return self.edits.get(medium, {})

    def edit(self, medium: str, key: SpotKey) -> "SpotEdit | None":
        return self.edits.get(medium, {}).get(key)

    def set_edit(self, medium: str, edit: SpotEdit) -> None:
        """Store an edit, or drop it once it says nothing."""
        bucket = self.edits.setdefault(medium, {})
        if edit.is_empty:
            bucket.pop(edit.key, None)
            if not bucket:
                self.edits.pop(medium, None)
        else:
            bucket[edit.key] = edit

    def amend(self, medium: str, replicate: str, strain_col: int,
              **changes) -> SpotEdit:
        """Change one field of one spot's edit, creating it if needed.

        Pass a field as None to clear that assertion -- `amend(..., excluded=None)`
        means "stop saying anything about exclusion", which is how an edit is
        undone back to the pipeline's own opinion rather than to the opposite one.
        """
        key: SpotKey = (replicate, int(strain_col))
        cur = self.edit(medium, key) or SpotEdit(replicate, int(strain_col))
        new = replace(cur, **changes)
        self.set_edit(medium, new)
        return new

    def clear_medium(self, medium: str) -> None:
        self.edits.pop(medium, None)

    @property
    def n_edits(self) -> int:
        return sum(len(v) for v in self.edits.values())

    # -- statistics ----------------------------------------------------------

    def statistics_kwargs(self) -> dict:
        """The keyword arguments `spotting_batch.run_plots` takes, as chosen."""
        return {"statistical_test": self.statistical_test,
                "p_adjust": self.p_adjust, "alpha": self.alpha,
                "posthoc": (self.posthoc if self.statistical_test == "anova"
                            else "none"),
                "extra_references": tuple(self.extra_references),
                "all_pairs": self.all_pairs}

    def comparisons_text(self) -> str:
        """The comparison set in a few words, for a button or a summary row."""
        if self.all_pairs:
            return "all pairs"
        if not self.extra_references:
            return "vs control"
        return "vs control + " + ", ".join(self.extra_references)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "capture_root": self.capture_root,
            "statistics": {"test": self.statistical_test,
                           "p_adjust": self.p_adjust,
                           "alpha": self.alpha,
                           "posthoc": self.posthoc,
                           "extra_references": list(self.extra_references),
                           "all_pairs": self.all_pairs},
            "chosen": {m: p.to_dict() for m, p in sorted(self.picks.items())},
            "edits": {
                m: [e.to_dict() for e in sorted(b.values(),
                                                key=lambda e: (e.replicate,
                                                               e.strain_col))]
                for m, b in sorted(self.edits.items()) if b
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Review":
        statistics = d.get("statistics") or {}
        statistical_test = str(statistics.get("test", "t_test"))
        if statistical_test not in {"t_test", "anova"}:
            statistical_test = "t_test"
        p_adjust = str(statistics.get("p_adjust", "none"))
        if p_adjust not in {"none", "holm", "bonferroni", "sidak"}:
            p_adjust = "none"
        alpha = _f(statistics.get("alpha"), 0.05)
        if not math.isfinite(alpha) or not 0 < alpha < 1:
            alpha = 0.05
        posthoc = str(statistics.get("posthoc", "dunnett"))
        if posthoc not in POSTHOC_METHODS:
            posthoc = "dunnett"
        refs = statistics.get("extra_references") or []
        extra_references = (list(dict.fromkeys(str(r) for r in refs if str(r)))
                            if isinstance(refs, list) else [])
        all_pairs = statistics.get("all_pairs") is True
        if all_pairs and posthoc == "dunnett":
            posthoc = "tukey"
        picks = {str(m): Pick.from_dict(v)
                 for m, v in (d.get("chosen") or {}).items()}
        edits: dict[str, dict[SpotKey, SpotEdit]] = {}
        for medium, items in (d.get("edits") or {}).items():
            bucket = {}
            for item in items or ():
                e = SpotEdit.from_dict(item)
                if not e.is_empty:
                    bucket[e.key] = e
            if bucket:
                edits[str(medium)] = bucket
        return cls(version=_i(d.get("version"), 1),
                   capture_root=str(d.get("capture_root", "") or ""),
                   statistical_test=statistical_test,
                   p_adjust=p_adjust,
                   alpha=alpha,
                   posthoc=posthoc,
                   extra_references=extra_references,
                   all_pairs=all_pairs,
                   picks=picks, edits=edits)
