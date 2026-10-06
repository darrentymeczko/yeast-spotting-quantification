"""Data review: look at every photograph before the statistics are run.

Between setting an experiment up and running it, flip through every plate it
imported and say which are not good data -- a whole plate (smeared, cracked,
out of focus) or single spots (a contaminant, a bubble, a pinning error). The
location of every spot comes from the program's own detection, so it has to be
run first; it fills the same measurement cache the run reads, so detecting
first costs the run nothing.

What the flags do, by design, differs between the two analyses:

* the additional multi-step analysis EXCLUDES them. It pools many technical
  plates and timepoints, and one bad plate would quietly bias all of it;
* the normal endpoint analysis only MARKS them. The results review shows each
  flagged spot and why, and the person decides whether to omit it there.

Decisions are stored beside the experiment (`flags.py`), never in it, and are
never applied to the photographs.

    flags.py       the file format: the contract other tools read (stdlib only)
    catalog.py     which photos there are, and what is in each grid cell
    spots.py       where detection put the spots (reads the measurement cache)
    controller.py  the review in progress: flags, undo, the photo shown
    cli.py         `detect`: locate the spots on every photo, as a job
    app.py         the window
"""

from __future__ import annotations

import sys
from pathlib import Path

#: True when packaged as the single .exe.
FROZEN = bool(getattr(sys, "frozen", False))

#: The project folder: `Experiment Designs/`, `.spotting_cache/`.
PROJECT_ROOT = (Path(sys.executable).resolve().parent if FROZEN
                else Path(__file__).resolve().parent.parent)

__all__ = ["FROZEN", "PROJECT_ROOT"]
