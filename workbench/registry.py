"""The kinds of document the workbench opens, and how to make each one.

Four, in the order the work happens:

    1  plate        a plate template             plate_template.app.DesignerApp
    2  experiment   an experiment, set up & run   experiments.app.ExperimentApp
    3  data_review  its photos, checked by eye    data_review.app.DataReviewApp
    4  review       a time-course result set     results_review.app.ReviewApp

A data review is checked BEFORE the run's statistics are trusted, but it is
step 3 rather than part of step 2 because it is a document of its own: a
`.datareview.json` beside the experiment, so the two can be open side by side
without either overwriting the other's saves.

Every tool is imported INSIDE the functions here, never at module level, so
the workbench opens without loading a tool nobody has asked for -- and the
review tool's engine, with numpy behind it, only when a result set is opened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Kind:
    key: str
    #: "Plate template", for menus and the Home cards.
    title: str
    #: The step this is in the work, as shown on Home.
    step: int
    #: One line under the card's title.
    blurb: str
    #: A `uikit.icons` name.
    icon: str
    #: Build the tool into `host`: (host, path or None, payload or None) -> app.
    create: Callable
    #: Every file of this kind in the project, for the Explorer and Home.
    files: Callable[[], list]
    #: Ask for one to open; (parent) -> Path or None.
    ask_open: Callable
    #: Whether a path is one of these.
    recognises: Callable[[Path], bool]


# -- plate templates -------------------------------------------------------------

def _plate_create(host, path, payload):
    from plate_template.app import DesignerApp
    from plate_template.presets import blank
    from plate_template.schema import load

    template = payload if payload is not None else (load(path) if path else blank())
    return DesignerApp(host, template, path)


def _plate_dir() -> Path:
    from plate_template.app import default_template_dir
    return default_template_dir()


def _plate_files() -> list[Path]:
    folder = _plate_dir()
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.glob("*.json") if _is_plate(p)),
                  key=lambda p: p.name.lower())


def _plate_ask(parent) -> Path | None:
    from tkinter import filedialog

    start = _plate_dir()
    got = filedialog.askopenfilename(
        parent=parent, title="Open plate template",
        initialdir=str(start if start.is_dir() else Path.cwd()),
        filetypes=[("Plate template", "*.json"), ("All files", "*.*")])
    return Path(got) if got else None


def _json_kind(path: Path) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return data.get("kind", "") if isinstance(data, dict) else ""


def _is_plate(path: Path) -> bool:
    return (path.suffix.lower() == ".json"
            and not path.name.lower().endswith(".spotexp.json")
            and _json_kind(path) == "spotting_plate_template")


# -- experiments -----------------------------------------------------------------

EXPERIMENT_SUFFIX = ".spotexp.json"


def _experiment_create(host, path, payload):
    from experiments.app import ExperimentApp
    from experiments.model import Experiment
    from experiments.schema import load

    experiment = payload if payload is not None else (
        load(path) if path else Experiment())
    return ExperimentApp(host, experiment, path)


def _experiment_dir() -> Path:
    from experiments.app import default_experiment_dir
    return default_experiment_dir()


def _experiment_files() -> list[Path]:
    folder = _experiment_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"*{EXPERIMENT_SUFFIX}"), key=lambda p: p.name.lower())


def _experiment_ask(parent) -> Path | None:
    from tkinter import filedialog

    start = _experiment_dir()
    got = filedialog.askopenfilename(
        parent=parent, title="Open experiment",
        initialdir=str(start if start.is_dir() else Path.cwd()),
        filetypes=[("Experiment", "*.json"), ("All files", "*.*")])
    return Path(got) if got else None


def _is_experiment(path: Path) -> bool:
    return path.name.lower().endswith(EXPERIMENT_SUFFIX) or (
        path.suffix.lower() == ".json"
        and _json_kind(path) == "spotting_experiment")


# -- data reviews ------------------------------------------------------------------

def data_review_path(experiment: Path) -> Path:
    """The data review belonging to an experiment file (it may not exist yet)."""
    from data_review.flags import sidecar_for
    return sidecar_for(experiment)


def _data_review_create(host, path, payload):
    from data_review.app import DataReviewApp

    if path is None:
        raise ValueError("a data review is opened for an experiment")
    return DataReviewApp(host, path)


def _data_review_files() -> list[Path]:
    from data_review.flags import SUFFIX

    folder = _experiment_dir()
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"*{SUFFIX}"), key=lambda p: p.name.lower())


def _data_review_ask(parent) -> Path | None:
    """Which experiment's data to review -- asked as the experiment, not the
    review file, which may not exist until the first flag is saved."""
    from tkinter import filedialog

    start = _experiment_dir()
    got = filedialog.askopenfilename(
        parent=parent, title="Review the data of which experiment?",
        initialdir=str(start if start.is_dir() else Path.cwd()),
        filetypes=[("Experiment", "*.spotexp.json"),
                   ("Data review", "*.datareview.json"), ("All files", "*.*")])
    if not got:
        return None
    path = Path(got)
    return path if _is_data_review(path) else data_review_path(path)


def _is_data_review(path: Path) -> bool:
    return path.name.lower().endswith(".datareview.json")


# -- result sets -----------------------------------------------------------------

def _review_create(host, path, payload):
    from results_review import discovery
    from results_review.app import ReviewApp

    if path is None:
        raise ValueError("a result set is opened from its folder")
    return ReviewApp(host, discovery.load_set(path))


def _review_files() -> list[Path]:
    from results_review import discovery
    return discovery.list_sets()


def _review_ask(parent) -> Path | None:
    from results_review.app import choose_set
    return choose_set(parent)


def _is_review(path: Path) -> bool:
    return path.is_dir() and (path / "timecourse_candidates.csv").is_file()


KINDS: list[Kind] = [
    Kind("plate", "Plate template", 1,
         "Lay out the plate: which cell holds which sample, replicate and "
         "dilution, and the control on each plate.",
         "grid", _plate_create, _plate_files, _plate_ask, _is_plate),
    Kind("experiment", "Experiment", 2,
         "Say who was on the plate and where the photos are, then measure "
         "them.",
         "photo", _experiment_create, _experiment_files, _experiment_ask,
         _is_experiment),
    Kind("data-review", "Review data", 3,
         "Before the statistics: flip through every photo and flag bad "
         "plates and bad spots.",
         "flag", _data_review_create, _data_review_files, _data_review_ask,
         _is_data_review),
    Kind("review", "Review results", 4,
         "Look through what a run scored, keep the winner or choose another, "
         "and correct individual spots.",
         "chart", _review_create, _review_files, _review_ask, _is_review),
]

BY_KEY: dict[str, Kind] = {k.key: k for k in KINDS}


def path_key(path: Path) -> str:
    """One spelling per file, for "is this already open?" (Windows paths are
    case-insensitive, and the same file can be reached by several routes)."""
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def kind_of(path: Path) -> str | None:
    """Which kind of document `path` is, or None."""
    for kind in KINDS:
        try:
            if kind.recognises(path):
                return kind.key
        except OSError:
            continue
    return None


# -- importing the old pipelines' configuration ------------------------------------

def import_configuration(parent):
    """Ask for a pipeline config file and turn it into experiments.

    The same flow as the experiment designer's File > Import, for Home, where
    there is no designer open to ask. Returns (experiments, notes, path), or
    None if cancelled or the file could not be read (and was reported).
    """
    from tkinter import filedialog, messagebox

    from experiments import migrate

    chosen = filedialog.askopenfilename(
        parent=parent,
        title="Import spotting_config.json or timecourse_config.json",
        filetypes=[("Pipeline configuration", "*.json"), ("All files", "*.*")])
    if not chosen:
        return None
    path = Path(chosen)
    try:
        if path.name == "timecourse_config.json":
            experiment, notes = migrate.from_capture_tree(path.parent)
            experiments = [experiment]
        else:
            experiments, notes = migrate.from_spotting_config(path)
    except migrate.MigrationError as exc:
        messagebox.showerror("Import", str(exc), parent=parent)
        return None
    if not experiments:
        messagebox.showinfo("Import", "Nothing to import from that file.",
                            parent=parent)
        return None
    return experiments, notes, path
