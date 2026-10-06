"""Colours and geometry for the grid canvas.

Three visual channels:

    sample slot  ->  hue     (which sample this is)
    replicate    ->  shade   (same hue, stepped lighter to darker)
    dilution     ->  dimming (greyed and sunk toward the background)

Size is deliberately constant; shrinking spots broke the grid's rhythm. Because
shade and dimming both move lightness, the dilution fade spends part of its
budget removing chroma, which keeps "a dim spot" from reading as "a spot from a
different replicate".

Nothing is encoded by colour alone: the sample number is printed on the least
dilute spot of each series, and the replicate number on the spot where the
replicate changes.
"""

from __future__ import annotations

import colorsys

from uikit import tokens

# --- geometry ---------------------------------------------------------------

PAD = 10          # outer breathing room
HEADER = 26       # gutter for the 1-based row/column numbers
MIN_PITCH = 14    # below this the grid is unreadable; skip the redraw
SPOT_FRACTION = 0.40     # spot radius as a fraction of the cell pitch, always
LABEL_INSET = 0.04       # replicate number's offset from the cell's top-left
LABEL_SIZE = 0.18        # ...and its height, kept clear of the spot's arc

# --- palette ----------------------------------------------------------------
#
# Neutrals and the accent are the shared ones (`uikit.tokens`), so the grid
# sits in the same window chrome as every other tool. The colours below that
# carry MEANING on the plate -- empty, undecided, the control ring -- are this
# tool's own.

BG = tokens.BG
CELL_BG = tokens.SURFACE
EMPTY_BG = "#e2e2df"
UNASSIGNED_BG = "#f6f3ec"
GRID_LINE = tokens.LINE
TEXT = tokens.TEXT
MUTED = tokens.TEXT_MUTED
ACCENT = tokens.ACCENT
CONTROL_RING = "#2c2f35"
#: One fixed ink for every replicate number. The replicate is identified by the
#: digit, not by a colour, so nothing about this mark varies between replicates.
REPLICATE_LABEL = "#5a5e66"
FLASH = "#e8bf5a"

ERROR = tokens.ERROR
WARNING = tokens.WARNING
INFO = tokens.INFO

#: Hand-picked hues rather than an even sweep: an even sweep lands squarely in
#: the muddy yellow-greens and the electric magentas, which is what made the
#: first palette look like a set of highlighter pens. Spaced by eye, then
#: rendered at one restrained saturation so no sample shouts over its
#: neighbours.
_HUES = (210, 30, 145, 330, 265, 185, 55, 0, 100, 300, 165, 240)
_SATURATION = 0.44

#: Lightness per biological replicate, ordered light to dark. A sample keeps
#: one hue across its whole dilution series and changes shade only when the
#: replicate changes, so a replicate block reads as one band of colour.
_REPLICATE_LIGHTNESS = (0.78, 0.66, 0.54, 0.44, 0.36)

#: How far each dilution level dims toward the background, and the ceiling so
#: the deepest level never disappears entirely.
DILUTION_FADE = 0.30
DILUTION_FADE_MAX = 0.78

#: Share of the fade spent removing chroma rather than lightening. Without it a
#: faded light replicate looks like a solid dark one; greying the spot as it
#: fades keeps "dim" distinct from "different replicate".
DILUTION_DESATURATE = 0.55

#: Continues the hue sequence past the curated list without a visible seam.
_GOLDEN_DEGREES = 137.50776405003785


def _hex(r: float, g: float, b: float) -> str:
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def _rgb(colour: str) -> tuple[float, float, float]:
    c = colour.lstrip("#")
    return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def blend(colour: str, toward: str, amount: float) -> str:
    a, b = _rgb(colour), _rgb(toward)
    amount = min(max(amount, 0.0), 1.0)
    return _hex(*(x + (y - x) * amount for x, y in zip(a, b)))


#: The hover outline: the accent, softened so it reads as "pointing at", not
#: as "selected".
HOVER = blend(ACCENT, CELL_BG, 0.55)


def luminance(colour: str) -> float:
    r, g, b = _rgb(colour)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def slot_hue(slot: int) -> float:
    index = slot - 1
    if 0 <= index < len(_HUES):
        return float(_HUES[index])
    return (_HUES[0] + (index - len(_HUES) + 1) * _GOLDEN_DEGREES) % 360


def replicate_lightness(replicate: int) -> float:
    return _REPLICATE_LIGHTNESS[(replicate - 1) % len(_REPLICATE_LIGHTNESS)]


def slot_colour(slot: int, replicate: int = 1) -> str:
    """Hue from the sample slot, shade from the replicate."""
    return _hex(
        *colorsys.hls_to_rgb(
            slot_hue(slot) / 360.0, replicate_lightness(replicate), _SATURATION
        )
    )


def _fade(dilution: int) -> float:
    if dilution <= 0:
        return 0.0
    return min(DILUTION_FADE * dilution, DILUTION_FADE_MAX)


def _greyed(colour: str, amount: float) -> str:
    """Pull chroma out while holding apparent lightness."""
    level = luminance(colour)
    return blend(colour, _hex(level, level, level), amount)


def spot_fill(slot: int, replicate: int, dilution: int = 0) -> str:
    """Hue from the sample, shade from the replicate, dimmed by dilution.

    Grown spots read brighter than agar, so a more dilute spot sinks toward the
    background rather than darkening.
    """
    base = slot_colour(slot, replicate)
    amount = _fade(dilution)
    if amount == 0.0:
        return base
    return blend(_greyed(base, amount * DILUTION_DESATURATE), CELL_BG, amount)


def spot_edge(slot: int, replicate: int, dilution: int = 0) -> str:
    """Fades with the fill, but more slowly, so the boundary survives."""
    base = blend(slot_colour(slot, replicate), "#101216", 0.30)
    return blend(base, CELL_BG, _fade(dilution) * 0.62)


def spot_ink(slot: int, replicate: int, dilution: int = 0) -> str:
    """Digit colour, taken from whatever the fill actually ended up being."""
    fill = spot_fill(slot, replicate, dilution)
    if luminance(fill) < 0.50:
        return blend(fill, "#ffffff", 0.88)
    return blend(fill, "#101216", 0.70)


def spot_radius(pitch: float) -> float:
    return pitch * SPOT_FRACTION


