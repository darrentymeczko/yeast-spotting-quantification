"""Stage 3 of the pipeline: look at what the timecourse run produced, choose
which candidate to carry forward, and correct individual spots.

Stage 1 sets the plate format (`plate_template`), stage 2 measures
(`src/spotting_*.py`). This package touches neither. It reads what stage 2
wrote, recomputes through stage 2's own functions, and writes only to a
`chosen/` folder of its own -- so a re-run of the pipeline can never be in
conflict with a review, and a review can never be mistaken for raw output.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: True when running from the packaged .exe rather than from the source tree.
FROZEN = bool(getattr(sys, "frozen", False))

#: Repo root -- the folder holding `src/`, `Results/` and `.spotting_cache`.
#: Packaged, that is the folder the .exe sits in.
PROJECT_ROOT = (Path(sys.executable).resolve().parent if FROZEN
                else Path(__file__).resolve().parent.parent)

# The measurement engine is a folder of top-level modules rather than a package,
# and they import each other by bare name (`import spotting_batch as sb`). That
# only works with src/ on the path, which is how spotting_timecourse.py already
# arranges things for itself. Doing it here, once, means every module in this
# package can `import spotting_quant as sq` the same way the engine does -- and
# means we are importing the SAME modules the pipeline runs, not a copy.
#
# Packaged, this is also what lets an edit to `src/` take effect without a
# rebuild: the folder beside the .exe goes on the path ahead of the built-in
# copy. When there is no such folder, the built-in copy is used instead.
_SRC = PROJECT_ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

__all__ = ["PROJECT_ROOT"]
