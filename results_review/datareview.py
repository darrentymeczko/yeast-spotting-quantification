"""What the data review flagged, as it applies to a result set.

The data review (`data_review/`) is done on the photographs before the run. In
the endpoint analysis its flags are MARKERS, never exclusions: a flagged spot
stays in the candidate, the graph and the statistics until the person reviewing
the results omits it themselves. This module only finds the flags that touch
what is on screen, so they can be shown.

The flags are read live from the file beside the experiment when the run said
where that is, so flags made after the run still show; otherwise from the copy
the run recorded. Stdlib only, like `discovery`: listing candidates must not
need the measurement stack.
"""

from __future__ import annotations

from pathlib import Path

from data_review import flags as flagfile
from data_review.flags import DataFlags

from .discovery import DILUTION_ORDER, SetRun
from .model import Candidate

#: The lab's classic layout, mirrored from `spotting_batch.DILUTIONS` (level d
#: is rows d and d + 3, all eight columns) for runs that recorded no layout.
_CLASSIC_ROWS = {0: (0, 3), 1: (1, 4), 2: (2, 5)}
_CLASSIC_COLS = 8


def load_flags(run: SetRun) -> "DataFlags | None":
    """The data review that applies to this result set, or None if there is none."""
    info = run.experiment
    if info is None:
        return None
    if info.data_review_file:
        path = Path(info.data_review_file)
        try:
            if path.exists():
                return flagfile.load(path)
        except (OSError, ValueError):
            pass
    if info.data_review_snapshot:
        try:
            return DataFlags.from_dict(info.data_review_snapshot)
        except ValueError:
            return None
    return None


def _records(run: SetRun) -> list:
    cfg = run.experiment.pipeline_config if run.experiment else {}
    records = cfg.get("resolved_photos") if isinstance(cfg, dict) else None
    return records if isinstance(records, list) else []


def _plates(run: SetRun, cand: Candidate) -> list[int]:
    """The template plate number behind each of a candidate's photos, in order."""
    layout = run.experiment.dilution_layout if run.experiment else {}
    numbers = sorted({int(no) for lv in (layout.get("levels") or [])
                      for no in (lv.get("plates") or {})})
    return numbers or list(range(1, len(cand.photos) + 1))


def candidate_photos(run: SetRun, cand: Candidate) -> list[tuple[int, "str | None"]]:
    """(plate, relpath) for each of a candidate's photos.

    The candidates CSV names photos by FILE NAME only, and on this project most
    are called `_9.JPG`, so the path is looked up in the photo assignments the
    run recorded: same condition, same timepoint, same plate, same name. None
    where that does not identify exactly one photo.
    """
    records = _records(run)
    out = []
    for plate, name in zip(_plates(run, cand), cand.photos):
        hits = []
        for r in records:
            try:
                label = r.get("timepoint_label") or f"{float(r['timepoint']):g} Hours"
                if (r.get("condition") == cand.medium and label == cand.timepoint
                        and int(r.get("plate")) == plate
                        and Path(str(r.get("relpath", ""))).name == name):
                    hits.append(str(r["relpath"]))
            except (KeyError, TypeError, ValueError):
                continue
        out.append((plate, hits[0] if len(hits) == 1 else None))
    return out


def level_cells(run: SetRun, cand: Candidate, plate: int) -> list[tuple[int, int]]:
    """0-based photograph (row, col) of every spot scored on one plate."""
    layout = run.experiment.dilution_layout if run.experiment else {}
    want = str(cand.dilution).strip().lower()
    for lv in layout.get("levels") or []:
        if str(lv.get("name", "")).strip().lower() == want:
            cells = (lv.get("plates") or {}).get(str(plate)) or []
            return [(int(c[0]), int(c[1])) for c in cells]
    if not layout and want in DILUTION_ORDER:
        rows = _CLASSIC_ROWS[DILUTION_ORDER.index(want)]
        return [(r, c) for r in rows for c in range(_CLASSIC_COLS)]
    return []


def candidate_flags(run: SetRun, cand: Candidate,
                    flags: "DataFlags | None") -> tuple[int, int]:
    """(flagged plates, flagged spots) among what this candidate scores."""
    if flags is None or flags.is_empty:
        return 0, 0
    plates = spots = 0
    for plate, relpath in candidate_photos(run, cand):
        if relpath is None:
            continue
        if flags.plate_reason(relpath):
            plates += 1
        on = flags.spots_on(relpath)
        if on:
            spots += sum(1 for r, c in level_cells(run, cand, plate)
                         if (r + 1, c + 1) in on)
    return plates, spots


def describe(plates: int, spots: int) -> str:
    bits = []
    if plates:
        bits.append(f"{plates} flagged plate{'s' if plates != 1 else ''}")
    if spots:
        bits.append(f"{spots} flagged spot{'s' if spots != 1 else ''}")
    return " and ".join(bits)
