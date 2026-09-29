"""Where the engine keeps things.

The photo folder, `Results/`, `.spotting_cache/` and the saved answers all live
in the project folder, beside `src/` -- not inside it. Run from source that is
one level up from this file. Packaged as a single .exe there is no `src/`: the
modules are unpacked to a temporary folder that vanishes on exit, so the project
folder is wherever the .exe sits. That makes the .exe a drop-in for the .bat
files: put it in the same folder and it reads and writes the same things.
"""

from __future__ import annotations

import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

PROJECT_ROOT = (Path(sys.executable).resolve().parent if FROZEN
                else Path(__file__).resolve().parent.parent)
