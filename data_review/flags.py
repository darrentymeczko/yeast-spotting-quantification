"""What a data review decided: which plates and which spots are bad, and why.

This module is the contract between the data review and everything that reads
its decisions -- the run (`experiments.run`, for the multi-step analysis) and
the results review (`results_review`, to mark flagged spots). It is therefore
kept to the standard library and imports nothing else from this project, so any
of those can read a flags file without pulling in a window or the pipeline.

The file sits BESIDE the experiment it reviews:

    Experiment Designs/Set09.spotexp.json      the experiment
    Experiment Designs/Set09.datareview.json   its data review

A file of its own rather than a block inside the experiment, because two tools
edit them. The experiment designer saves the whole experiment from what it holds
in memory; if the flags lived in that file, saving the designer would silently
undo every flag made in the data review since the designer opened it.

Everything is keyed the way the experiment keys photos: the forward-slash path
relative to the photo folder. A spot is a cell of the DETECTED grid, numbered
from 1 as the photograph shows it (row down, column across), which is the same
convention `spotting_config.json`'s nudges use. Grid cells, not template cells:
the camera may have been turned, and the photograph is what was looked at.

Decisions, never numbers. Writes are atomic, as the review tool's are -- a
review is an afternoon of looking at plates, and a half-written JSON after a
crash would lose all of it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

KIND = "spotting_data_review"
VERSION = 1

#: A data review's file name ends in this.
SUFFIX = ".datareview.json"
#: And the experiment it belongs to ends in this.
EXPERIMENT_SUFFIX = ".spotexp.json"

#: Prefixed to every reason a downstream tool reports, so a QC column or a
#: flag in a table says where the judgement came from.
SOURCE = "data review"

SpotKey = tuple[int, int]          # (row, col), 1-based, photograph grid


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm(relpath: str) -> str:
    """One spelling per photo: forward slashes, as `experiments.intake` keys them."""
    return str(relpath).replace("\\", "/")


# ---------------------------------------------------------------------------
# Where the file is
# ---------------------------------------------------------------------------


def sidecar_for(experiment_path: Path) -> Path:
    """The data review file belonging to an experiment file."""
    path = Path(experiment_path)
    name = path.name
    if name.lower().endswith(EXPERIMENT_SUFFIX):
        stem = name[:-len(EXPERIMENT_SUFFIX)]
    else:
        stem = path.stem
    return path.with_name(stem + SUFFIX)


def is_flags_file(path: Path) -> bool:
    return Path(path).name.lower().endswith(SUFFIX)


def stem_of(path: Path) -> str:
    """`Set09` for `Set09.datareview.json`."""
    name = Path(path).name
    return name[:-len(SUFFIX)] if name.lower().endswith(SUFFIX) else Path(path).stem


def experiment_for(flags_path: Path, flags: "DataFlags | None" = None) -> Path:
    """The experiment file a data review belongs to.

    The file records it, relative to itself, so the pair can move together;
    without that, the naming rule is used.
    """
    flags_path = Path(flags_path)
    recorded = flags.experiment if flags is not None else ""
    if recorded:
        return flags_path.parent / recorded
    return flags_path.with_name(stem_of(flags_path) + EXPERIMENT_SUFFIX)


# ---------------------------------------------------------------------------
# The decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    """One judgement: this plate, or this spot, is not good data."""

    reason: str
    when: str = ""

    def to_dict(self) -> dict:
        out = {"reason": self.reason}
        if self.when:
            out["when"] = self.when
        return out


@dataclass
class DataFlags:
    """Every decision one data review holds.

    Mutable, and the only mutable thing here; the controller snapshots it for
    undo through `to_dict` / `from_dict`, as the review tool does its `Review`.
    """

    #: The experiment file, relative to this one. Written so a review opened on
    #: its own can find what it reviews.
    experiment: str = ""
    #: The experiment's own id, when it has one, so a review that has been
    #: paired with the wrong experiment can be told apart from the right one.
    experiment_id: str = ""
    plates: dict[str, Flag] = field(default_factory=dict)
    spots: dict[str, dict[SpotKey, Flag]] = field(default_factory=dict)
    #: Photos somebody has looked at and passed. Flagged photos count as looked
    #: at whether or not they are in here.
    reviewed: set[str] = field(default_factory=set)

    # -- reading -------------------------------------------------------------

    def plate_reason(self, relpath: str) -> str:
        flag = self.plates.get(norm(relpath))
        return flag.reason if flag else ""

    def spot_reason(self, relpath: str, row: int, col: int) -> str:
        """Why this one spot was flagged; "" if it was not. 1-based row, col."""
        flag = self.spots.get(norm(relpath), {}).get((int(row), int(col)))
        return flag.reason if flag else ""

    def reason_for(self, relpath: str, row: int, col: int) -> str:
        """Why a spot should not be trusted, from either kind of flag.

        A flagged plate makes every spot on it suspect, so it is reported for
        each of them. Both reasons are given when both apply.
        """
        parts = []
        plate = self.plate_reason(relpath)
        if plate:
            parts.append(f"plate: {plate}")
        spot = self.spot_reason(relpath, row, col)
        if spot:
            parts.append(spot)
        return "; ".join(parts)

    def spots_on(self, relpath: str) -> dict[SpotKey, Flag]:
        return dict(self.spots.get(norm(relpath), {}))

    def is_reviewed(self, relpath: str) -> bool:
        key = norm(relpath)
        return key in self.reviewed or key in self.plates or bool(self.spots.get(key))

    def is_flagged(self, relpath: str) -> bool:
        key = norm(relpath)
        return key in self.plates or bool(self.spots.get(key))

    @property
    def n_plates(self) -> int:
        return len(self.plates)

    @property
    def n_spots(self) -> int:
        return sum(len(v) for v in self.spots.values())

    @property
    def is_empty(self) -> bool:
        return not (self.plates or self.spots or self.reviewed)

    # -- changing ------------------------------------------------------------

    def set_plate(self, relpath: str, reason: "str | None") -> None:
        """Flag a whole plate, or clear it with None or ""."""
        key = norm(relpath)
        if reason and reason.strip():
            self.plates[key] = Flag(reason.strip(), _stamp())
        else:
            self.plates.pop(key, None)

    def set_spot(self, relpath: str, row: int, col: int,
                 reason: "str | None") -> None:
        """Flag one spot (1-based row, col), or clear it with None or ""."""
        key = norm(relpath)
        cell = (int(row), int(col))
        if reason and reason.strip():
            self.spots.setdefault(key, {})[cell] = Flag(reason.strip(), _stamp())
        else:
            bucket = self.spots.get(key)
            if bucket is not None:
                bucket.pop(cell, None)
                if not bucket:
                    self.spots.pop(key, None)

    def set_reviewed(self, relpath: str, reviewed: bool = True) -> None:
        key = norm(relpath)
        if reviewed:
            self.reviewed.add(key)
        else:
            self.reviewed.discard(key)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "kind": KIND,
            "version": VERSION,
            "experiment": self.experiment,
            "experiment_id": self.experiment_id,
            "plates": {k: f.to_dict() for k, f in sorted(self.plates.items())},
            "spots": {
                k: [{"row": r, "col": c, **f.to_dict()}
                    for (r, c), f in sorted(cells.items())]
                for k, cells in sorted(self.spots.items()) if cells
            },
            "reviewed": sorted(self.reviewed),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DataFlags":
        """Read a flags object, skipping anything malformed rather than failing.

        A stray hand edit must not make a whole review unreadable; what cannot
        be understood is dropped and the rest is kept.
        """
        if not isinstance(d, dict):
            raise ValueError("a data review must be a JSON object")
        kind = d.get("kind", KIND)
        if kind != KIND:
            raise ValueError(f"this is a {kind!r} file, not a data review")
        version = d.get("version", VERSION)
        if not isinstance(version, int) or version > VERSION:
            raise ValueError(f"data review version {version!r} is newer than "
                             f"this program understands ({VERSION})")
        out = cls(experiment=str(d.get("experiment") or ""),
                  experiment_id=str(d.get("experiment_id") or ""))
        for relpath, raw in (d.get("plates") or {}).items():
            if isinstance(raw, dict) and str(raw.get("reason", "")).strip():
                out.plates[norm(relpath)] = Flag(str(raw["reason"]).strip(),
                                                 str(raw.get("when") or ""))
        for relpath, items in (d.get("spots") or {}).items():
            for raw in items if isinstance(items, list) else ():
                try:
                    row, col = int(raw["row"]), int(raw["col"])
                    reason = str(raw["reason"]).strip()
                except (KeyError, TypeError, ValueError):
                    continue
                if row >= 1 and col >= 1 and reason:
                    out.spots.setdefault(norm(relpath), {})[(row, col)] = Flag(
                        reason, str(raw.get("when") or ""))
        reviewed = d.get("reviewed") or []
        if isinstance(reviewed, list):
            out.reviewed = {norm(r) for r in reviewed if isinstance(r, str) and r}
        return out


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def load(path: Path) -> DataFlags:
    """Read a data review, or an empty one if there is no file yet."""
    path = Path(path)
    if not path.exists():
        return DataFlags()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"{path} is not readable JSON: {exc}") from exc
    return DataFlags.from_dict(raw)


def load_for_experiment(experiment_path: "Path | None") -> DataFlags:
    """The data review beside an experiment file; empty when there is none.

    Never raises: the flags are extra information, and an unreadable file must
    not stop a run or a review from starting. Callers that need to say so to
    the person use `load` directly.
    """
    if experiment_path is None:
        return DataFlags()
    try:
        return load(sidecar_for(experiment_path))
    except (OSError, ValueError):
        return DataFlags()


def save(path: Path, flags: DataFlags) -> Path:
    """Write atomically. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(flags.to_dict(), indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".datareview-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def describe(reason: str) -> str:
    """A reason as another tool shows it: `data review: contamination`."""
    return f"{SOURCE}: {reason}" if reason else ""
