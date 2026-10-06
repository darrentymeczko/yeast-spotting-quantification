"""Plate Template Designer.

Design a spotting-assay plate layout -- grid size, which cells hold which
sample slot, replicate and dilution level, and which slot is the control on
each plate -- and save it as a reusable, strain-agnostic JSON template.

Keep this module import-light; the GUI is reached via `plate_template.app`.
"""

from .model import KIND, SCHEMA_VERSION

__version__ = "0.1.0"

__all__ = ["KIND", "SCHEMA_VERSION", "__version__"]
