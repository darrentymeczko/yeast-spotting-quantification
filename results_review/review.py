"""Load and save `Results/Timecourse/<set>/review.json`.

The file holds decisions, never derived numbers: which candidate was chosen per
medium, and what was changed about individual spots. Everything else is
regenerated from the photos through the pipeline's own functions, so a review
stays meaningful after a re-measure instead of freezing a copy of numbers that
may no longer be what the engine produces.

Writes are atomic. A review is hand work that can represent an afternoon of
looking at plates, and a half-written JSON after a crash would lose all of it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .discovery import SetRun
from .model import Review


def load(path: Path) -> Review:
    """Read a review, or return an empty one if there is none yet."""
    path = Path(path)
    if not path.exists():
        return Review()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"{path} is not readable JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a review object.")
    return Review.from_dict(data)


def save(path: Path, review: Review) -> Path:
    """Write a review atomically. Returns the path written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(review.to_dict(), indent=2, ensure_ascii=False) + "\n"

    # Write beside the target so the replace is on one filesystem, then
    # os.replace, which is atomic on Windows and POSIX alike.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".review-",
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


def load_for(run: SetRun) -> Review:
    return load(run.review_path)


def save_for(run: SetRun, review: Review) -> Path:
    return save(run.review_path, review)


def with_defaults(run: SetRun, review: Review) -> Review:
    """Fill in the pipeline's own pick for any medium not yet decided.

    Opening a set must show the same candidate the run exported to `best/`, so
    the starting point is the pipeline's answer and every difference from it is
    something the person actually chose. Media already decided are untouched.
    """
    for medium in run.media:
        if medium in review.picks:
            continue
        best = run.pipeline_best(medium)
        if best is not None:
            review.set_pick(medium, best)
    return review


def is_default(run: SetRun, review: Review, medium: str) -> bool:
    """True when this medium's pick is still the pipeline's own."""
    pick = review.pick(medium)
    best = run.pipeline_best(medium)
    return bool(pick and best and pick.matches(best))


def chosen_candidate(run: SetRun, review: Review, medium: str):
    """The Candidate this review selects for a medium, falling back to the
    pipeline's pick.

    Returns None when the review names a candidate that is no longer in the CSV
    -- which happens if the timecourse is re-run with different photos. That is
    reported to the person rather than silently re-defaulted: a pick that can no
    longer be honoured is a thing they need to know about.
    """
    pick = review.pick(medium)
    if pick is None:
        return run.pipeline_best(medium)
    return run.find(medium, pick.timepoint, pick.plate1, pick.plate2,
                    pick.dilution, pick.extra_plates)


def stale_picks(run: SetRun, review: Review) -> list[str]:
    """Media whose recorded pick no longer matches any scored candidate."""
    return [m for m in sorted(review.picks)
            if m in run.media and chosen_candidate(run, review, m) is None]
