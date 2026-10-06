"""Design tokens: every shared colour, font and spacing, as plain data.

One flat, light look across every tool. White surfaces sit on an off-white
ground, one blue does all the pointing, and lines are drawn thin and quiet so
that the data -- the plate, the photos, the numbers -- is the loudest thing on
screen.

No tkinter here, deliberately: the tools' own `theme.py` modules are part of
their headless cores and read these values directly. What the tools keep for
themselves are colours that MEAN something -- the plate designer's slot hues,
review's amber for a person's edit -- and those stay in their own modules.
"""

from __future__ import annotations

# --- surfaces ---------------------------------------------------------------

#: The ground everything sits on: window background, workspace, sidebar.
BG = "#F6F7F9"
#: Cards, tables, entry fields, the active tab.
SURFACE = "#FFFFFF"
#: Hover, sunken wells, header rows, the status bar.
SURFACE_ALT = "#F0F2F5"

#: Hairlines: dividers, card edges, grid lines.
LINE = "#D9DDE3"
#: Edges of things you can type in or press.
LINE_STRONG = "#C4CAD3"

# --- text -------------------------------------------------------------------

TEXT = "#1F2328"
#: Secondary text. Still 4.5:1 or better on both BG and SURFACE.
TEXT_MUTED = "#6B7280"
TEXT_DISABLED = "#A0A7B1"
#: Text on an ACCENT fill.
TEXT_ON_ACCENT = "#FFFFFF"

# --- accent -----------------------------------------------------------------

ACCENT = "#1F6FEB"
ACCENT_HOVER = "#1A5FCC"
ACCENT_PRESSED = "#1650AD"
#: Selection without focus, the selected card, soft highlights.
ACCENT_SOFT = "#E8F0FE"

# --- status -----------------------------------------------------------------
#
# The severity colours the tools were already using, reconciled to one set.

OK = "#0B7A3B"
WARNING = "#9A6400"
ERROR = "#B3261E"
INFO = "#4B5563"

# --- type -------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
#: Segoe UI ships a separate semibold face; weight "bold" alone would pick the
#: much heavier Bold, which is not the look.
FONT_FAMILY_SEMIBOLD = "Segoe UI Semibold"
MONO_FAMILY = "Consolas"

SIZE_BODY = 9
SIZE_SMALL = 8
SIZE_HEADING = 12
SIZE_TITLE = 16
SIZE_DISPLAY = 20

#: Tried in order. Fluent ships with Windows 11, MDL2 with Windows 10.
ICON_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

# --- spacing ----------------------------------------------------------------
#
# In pixels at 96 dpi; scale with `uikit.dpi.px` on a scaled display.

SPACE_XXS = 2
SPACE_XS = 4
SPACE_S = 8
SPACE_M = 12
SPACE_L = 16
SPACE_XL = 24

#: Table rows: the font's line height plus this.
ROW_PAD = 6
