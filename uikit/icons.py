"""Icons as glyphs from the Windows symbol fonts.

No image files to ship or scale: a glyph is drawn by the font engine at
whatever size and DPI it is asked for, in whatever colour the widget's
foreground is. Windows 11 has "Segoe Fluent Icons", Windows 10 "Segoe MDL2
Assets"; the two share codepoints. Anywhere else, a plain text stand-in.

    text, font = icons.get(widget, "save", size=12)
    ttk.Label(parent, text=text, font=font)
"""

from __future__ import annotations

from tkinter import font as tkfont

from . import tokens

#: name -> (codepoint in the Segoe icon fonts, plain-text fallback)
GLYPHS: dict[str, tuple[str, str]] = {
    "add": ("", "+"),
    "close": ("", "×"),
    "home": ("", "⌂"),
    "open": ("", "↗"),
    "folder": ("", "▤"),
    "save": ("", "↓"),
    "undo": ("", "↶"),
    "redo": ("", "↷"),
    "play": ("", "▶"),
    "stop": ("", "■"),
    "refresh": ("", "↻"),
    "settings": ("", "⚙"),
    "help": ("", "?"),
    "more": ("", "…"),
    "chevron_down": ("", "▾"),
    "chevron_right": ("", "▸"),
    "check": ("", "✓"),
    "error": ("", "!"),
    "warning": ("", "!"),
    "info": ("", "i"),
    "document": ("", "□"),
    "copy": ("", "⧉"),
    "grid": ("", "▦"),
    "photo": ("", "▣"),
    "chart": ("", "∷"),
    "list": ("", "≡"),
    "import": ("", "↥"),
    "flag": ("", "⚑"),
}

_family_cache: dict[str, str | None] = {}


def family(widget) -> str | None:
    """The icon font installed here, or None. Looked up once per interpreter."""
    key = str(widget.tk)
    if key not in _family_cache:
        installed = set(tkfont.families(widget))
        _family_cache[key] = next(
            (f for f in tokens.ICON_FAMILIES if f in installed), None)
    return _family_cache[key]


def get(widget, name: str, size: int = 12) -> tuple[str, tuple]:
    """(text, font) to draw icon `name` at `size` points."""
    glyph, fallback = GLYPHS[name]
    fam = family(widget)
    if fam is None:
        return fallback, (tokens.FONT_FAMILY, size)
    return glyph, (fam, size)
