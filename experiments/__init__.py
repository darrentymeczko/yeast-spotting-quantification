"""The experiment layer: binds a plate template to strains, conditions and photos.

Only the two constants are imported here. Everything else stays behind an
explicit module import so this package is cheap to import and so the headless
core never drags in tkinter -- the same rule `plate_template` follows, and
`tests/experiments/test_no_gui_imports.py` enforces it.
"""

import sys
from pathlib import Path

from .model import KIND, SCHEMA_VERSION

#: True when packaged as the single .exe, where the modules are unpacked to a
#: temporary folder that vanishes on exit.
FROZEN = bool(getattr(sys, "frozen", False))

#: Where the shipped, read-only files live: `plate_template/templates/`, `src/`.
#: The source tree, or the folder the .exe unpacks itself into.
BUNDLE = Path(__file__).resolve().parents[1]

#: The user's project folder: `Plate Templates/`, `Experiment Designs/`, the
#: photos. The same as BUNDLE from source; the folder the .exe sits in when
#: packaged, so an experiment written by one build opens in the other.
REPO = Path(sys.executable).resolve().parent if FROZEN else BUNDLE

__all__ = ["KIND", "SCHEMA_VERSION", "FROZEN", "BUNDLE", "REPO"]
