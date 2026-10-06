"""Read what a timecourse run left behind, without opening a photo.

Deliberately stdlib-only. Listing 197 sheets and telling you which one the
pipeline picked is a filename problem, not a measurement problem, and making it
import numpy / pandas / matplotlib would mean the browser could not open on a
machine that can only look at results. `rebuild.py` and `export.py` pull in the
engine; nothing above them needs to.

The price is two small constants and one regex mirrored from the engine rather
than imported. `tests/results_review/test_discovery.py` asserts they still agree
with `spotting_batch` / `spotting_timecourse_figures` whenever those can be
imported, so drift is caught rather than assumed away.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import PROJECT_ROOT
from .model import DILUTION_ORDER, Candidate

# Re-exported: this module is where the rest of the package (and the tests) ask
# about the pipeline's orderings, so the dilution order lives here alongside the
# medium order even though `model` is what defines it.
__all__ = ["DILUTION_ORDER", "EXPERIMENT_JSON", "MEDIUM_ORDER", "RunInfo",
           "SetRun", "candidate_id", "list_sets", "load_candidates", "load_set",
           "load_run_info", "medium_rank", "missing_sheets", "safe_name",
           "sheet_name", "sheet_path", "sort_prefix", "TIMECOURSE_RESULTS"]

#: Mirrors `spotting_timecourse.MEDIUM_ORDER` -- the order the media are run in,
#: which is the order the tabs should appear in. Alphabetical would put K-OAc
#: first, which is not how anyone reads these.
MEDIUM_ORDER = ["GLU", "GLY", "K-OAc"]

#: Mirrors `spotting_batch.TIMECOURSE_RESULTS`.
TIMECOURSE_RESULTS = PROJECT_ROOT / "Results" / "Timecourse"

CANDIDATES_CSV = "timecourse_candidates.csv"
REVIEW_JSON = "review.json"

#: Written by `experiments.run` beside the results. Purely additive: every file
#: this module already reads is still written exactly as before, so a results
#: folder produced by the console pipeline has none of this and still opens.
EXPERIMENT_JSON = "experiment.json"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(s) -> str:
    """Mirror `spotting_timecourse_figures.safe_name` for graph lookup."""
    return _UNSAFE.sub("_", str(s)).strip("_")


def medium_rank(medium: str) -> tuple[int, str]:
    """Sort key putting media in the order they are run, not alphabetically."""
    return ((MEDIUM_ORDER.index(medium) if medium in MEDIUM_ORDER
             else len(MEDIUM_ORDER)), medium)


def candidate_id(cand: Candidate) -> str:
    """Mirrors `spotting_timecourse_figures.candidate_id`.

    Both photo stems are in the name because several pairings share a timepoint
    and medium whenever there are technical replicates.
    """
    stems = "-".join(Path(p).stem for p in cand.photos)
    return safe_name(f"{cand.medium}_{cand.timepoint}_{stems}_{cand.dilution}")


def sort_prefix(cand: Candidate) -> str:
    """Mirrors `spotting_timecourse_figures._sort_prefix`.

    Timepoint ascending, then dilution least->most, so sorting by name walks the
    time course in order instead of putting '19 Hours' before '9 Hours'.
    """
    h = ("______" if cand.hours is None or math.isnan(cand.hours)
         else f"{float(cand.hours):06.1f}")
    # Two digits past ten levels, exactly as the pipeline pads it.
    d = (f"{cand.dilution_rank:02d}" if cand.level_count > 10
         else f"{cand.dilution_rank}")
    return f"{h}h_d{d}_"


def sheet_name(cand: Candidate) -> str:
    return f"{sort_prefix(cand)}{candidate_id(cand)}.png"


def sheet_path(figures_dir: Path, cand: Candidate) -> Path:
    """Where `spotting_timecourse_figures` wrote this candidate's comparison sheet."""
    return Path(figures_dir) / safe_name(cand.medium) / sheet_name(cand)


# ---------------------------------------------------------------------------


@dataclass
class RunInfo:
    """What `experiment.json` says produced this results folder.

    A resolved snapshot taken at run time, not a live reference: the experiment
    file it came from may have been edited since, and a results folder has to
    keep saying what actually made it.

    Everything here is optional. A results folder from the console pipeline has
    no `experiment.json`, and one written by a newer version may carry keys this
    does not know about, so a missing or odd field degrades to None rather than
    stopping the review from opening.
    """

    name: str = ""
    written: str = ""
    photo_root: str = ""
    #: 1-based sample slot -> strain name; empty slots are absent.
    strains: dict[int, str] = field(default_factory=dict)
    #: medium code -> the 1-based slot used as its positive control.
    controls: dict[str, int] = field(default_factory=dict)
    #: The exact `cfg` dict the run was driven with, shaped like a
    #: `timecourse_config.json`. Usable in place of that file, which an
    #: experiment-layer photo folder need not have.
    pipeline_config: dict = field(default_factory=dict)
    #: The dilution levels the run scored, as `DilutionLayout.to_dict` wrote
    #: them. Empty for a classic run.
    dilution_layout: dict = field(default_factory=dict)
    cache_key_version: str = ""
    #: Where the experiment's data review is saved, and what it held when the
    #: run was made (`data_review.flags.DataFlags.to_dict`). Either may be empty.
    data_review_file: str = ""
    data_review_snapshot: dict = field(default_factory=dict)

    @property
    def level_names(self) -> list:
        return [str(lv.get("name", "")) for lv in
                (self.dilution_layout.get("levels") or [])]

    def strain(self, strain_col: int) -> "str | None":
        """The strain in a 1-based column, or None if it was empty."""
        return self.strains.get(int(strain_col))

    def control_for(self, medium: str) -> "int | None":
        return self.controls.get(medium)


def load_run_info(results_dir: Path) -> "RunInfo | None":
    """Read `experiment.json`, or None when there is not a usable one.

    Never raises. This is extra context for the display, and failing to read it
    must not make an otherwise-reviewable results folder unopenable.
    """
    path = Path(results_dir) / EXPERIMENT_JSON
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    strains: dict[int, str] = {}
    for i, name in enumerate(raw.get("strains") or [], 1):
        if isinstance(name, str) and name.strip():
            strains[i] = name.strip()

    controls: dict[str, int] = {}
    for medium, slot in (raw.get("controls") or {}).items():
        if isinstance(slot, int):
            controls[str(medium)] = slot

    experiment = raw.get("experiment") or {}
    cfg = raw.get("pipeline_config")
    layout = raw.get("dilution_layout")
    review = raw.get("data_review") if isinstance(raw.get("data_review"), dict) else {}
    snapshot = review.get("flags")
    return RunInfo(
        data_review_file=str(review.get("file") or ""),
        data_review_snapshot=snapshot if isinstance(snapshot, dict) else {},
        dilution_layout=layout if isinstance(layout, dict) else {},
        name=str(experiment.get("name") or ""),
        written=str(raw.get("written") or ""),
        photo_root=str(raw.get("photo_root") or ""),
        strains=strains,
        controls=controls,
        pipeline_config=cfg if isinstance(cfg, dict) and cfg.get("strains") else {},
        cache_key_version=str(raw.get("cache_key_version") or ""),
    )


@dataclass
class SetRun:
    """One `Results/Timecourse/<set>/` folder, as far as the browser cares."""

    results_dir: Path
    label: str
    candidates: list[Candidate] = field(default_factory=list)
    #: Present only for a folder produced through the experiment layer.
    experiment: "RunInfo | None" = None

    # -- layout --------------------------------------------------------------

    @property
    def figures_dir(self) -> Path:
        return self.results_dir / "figures"

    @property
    def best_dir(self) -> Path:
        return self.results_dir / "best"

    @property
    def chosen_dir(self) -> Path:
        return self.results_dir / "chosen"

    @property
    def review_path(self) -> Path:
        return self.results_dir / REVIEW_JSON

    @property
    def candidates_csv(self) -> Path:
        return self.results_dir / CANDIDATES_CSV

    # -- contents ------------------------------------------------------------

    @property
    def media(self) -> list[str]:
        seen = {c.medium for c in self.candidates}
        return sorted(seen, key=medium_rank)

    def medium_label(self, medium: str) -> str:
        """What the experiment calls a medium ("Glucose + 37C"), for display.

        The code ("GLUCOSE3") stays the key -- it names the figure folders and
        the review's picks -- but it is generated, and nobody wrote it. A
        console-pipeline run has no labels, so its codes are shown as they are.
        """
        return next((c.medium_label for c in self.candidates
                     if c.medium == medium and c.medium_label), medium)

    def for_medium(self, medium: str) -> list[Candidate]:
        """This medium's candidates, in the CSV's own (rank_score) order."""
        return [c for c in self.candidates if c.medium == medium]

    def find(self, medium: str, timepoint: str, plate1: str, plate2: str,
             dilution: str, extra_plates=()) -> "Candidate | None":
        want = (medium, timepoint, plate1, plate2, str(dilution).strip().lower())
        if extra_plates:
            want += (tuple(extra_plates),)
        for c in self.candidates:
            if c.key == want:
                return c
        return None

    def sheet(self, cand: Candidate) -> Path:
        return sheet_path(self.figures_dir, cand)

    def pipeline_best(self, medium: str) -> "Candidate | None":
        """The candidate the run actually put in `best/`.

        NOT the first row of the medium. The CSV is sorted by `rank_score`
        (spotting_timecourse.py:1884), but the winner written to `best/` is the
        highest `best_set_score`, ties broken toward the lower median CV
        (spotting_timecourse.py:1986). Those disagree on real data, and taking
        the first row would quietly show a different default from the one the
        pipeline exported.
        """
        pool = self.for_medium(medium)
        if not pool:
            return None

        def key(c: Candidate):
            score = -math.inf if math.isnan(c.best_set_score) else c.best_set_score
            cv = math.inf if math.isnan(c.median_cv) else c.median_cv
            return (-score, cv, c.csv_order)

        return min(pool, key=key)


def load_candidates(csv_path: Path) -> list[Candidate]:
    """Every scored candidate, in the order the pipeline wrote them."""
    csv_path = Path(csv_path)
    # utf-8-sig: the pipeline writes a BOM, and without this the first column
    # comes back named "﻿medium" and every lookup on it misses.
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [Candidate.from_row(row, order=i)
                for i, row in enumerate(csv.DictReader(fh))]


def load_set(results_dir: Path) -> SetRun:
    """Load one results folder. Raises if it is not one."""
    results_dir = Path(results_dir).resolve()
    run = SetRun(results_dir=results_dir, label=results_dir.name)
    if not run.candidates_csv.exists():
        raise FileNotFoundError(
            f"{results_dir} has no {CANDIDATES_CSV} -- this is not a timecourse "
            f"results folder. Expected something like "
            f"{TIMECOURSE_RESULTS / 'Set01'}.")
    run.candidates = load_candidates(run.candidates_csv)
    run.experiment = load_run_info(results_dir)
    if run.experiment and run.experiment.name:
        run.label = run.experiment.name
    names = run.experiment.level_names if run.experiment is not None else []
    if names:
        # The recorded order is the only source of it: a design's own level
        # names carry no order, and the sheet filenames were written from it.
        order = {n.strip().lower(): i for i, n in enumerate(names)}
        run.candidates = [
            replace(c, level_index=order.get(c.dilution.strip().lower(), -1),
                    level_count=len(names))
            for c in run.candidates
        ]
    return run


def list_sets(root: "Path | None" = None) -> list[Path]:
    """Every timecourse results folder under `root`, newest run last.

    Sorted by name so `Set01`, `Set01 - Take02`, `Set02` group the way the
    folders read; an extra take sits next to the set it came from.
    """
    root = Path(root or TIMECOURSE_RESULTS)
    if not root.is_dir():
        return []
    return sorted({p.parent for p in root.rglob(CANDIDATES_CSV)},
                  key=lambda p: str(p.relative_to(root)).lower())


def missing_sheets(run: SetRun) -> list[Candidate]:
    """Candidates whose sheet was never drawn (a run with `--figures none|N`).

    Reported rather than shown as a broken image: "no sheet was drawn for this"
    is a fact about the run, and guessing at a different filename would hide it.
    """
    return [c for c in run.candidates if not run.sheet(c).exists()]
