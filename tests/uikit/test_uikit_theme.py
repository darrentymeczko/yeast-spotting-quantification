"""The flat theme: applied once, complete, and not undone by a second call."""

from __future__ import annotations

import gc
from tkinter import font as tkfont
from tkinter import ttk

from uikit import icons, theme, tokens


def test_the_theme_is_in_use(tk_root):
    style = theme.apply_theme(tk_root)
    assert style.theme_use() == theme.THEME
    assert theme.is_applied(tk_root)


def test_a_second_call_keeps_what_a_tool_changed_since(tk_root):
    """Idempotent, not merely repeatable: re-applying must not reset the
    prefixed styles a tool configured after the first call."""
    style = theme.apply_theme(tk_root)
    style.configure("Probe.TButton", padding=(31, 7))
    theme.apply_theme(tk_root)
    assert str(style.lookup("Probe.TButton", "padding")) == "31 7"


def test_the_accent_button_is_the_accent(tk_root):
    style = theme.apply_theme(tk_root)
    assert style.lookup("Accent.TButton", "background").upper() == tokens.ACCENT


def test_buttons_are_drawn_with_an_outline(tk_root):
    """A theme built on clam inherits none of clam's settings. Without an
    explicit relief a button's border element draws nothing at all, and every
    button in every tool became a borderless white block."""
    style = theme.apply_theme(tk_root)
    assert str(style.lookup("TButton", "relief")) == "raised"


def test_table_rows_fit_their_text(tk_root):
    style = theme.apply_theme(tk_root)
    line = tkfont.nametofont("TkDefaultFont", root=tk_root).metrics("linespace")
    assert int(style.lookup("Treeview", "rowheight")) > line


def test_the_named_fonts_survive_garbage_collection(tk_root):
    """tkinter deletes a named font when its Font object is collected. The
    theme's own fonts once vanished that way, and every widget using them
    silently fell back to Tk's default."""
    theme.apply_theme(tk_root)
    gc.collect()
    names = set(tkfont.names(tk_root))
    for name in (theme.FONT_SMALL, theme.FONT_STRONG, theme.FONT_HEADING,
                 theme.FONT_TITLE, theme.FONT_DISPLAY, theme.FONT_MONO):
        assert name in names, name


def test_notebook_tabs_use_the_flat_underlined_element(tk_root):
    style = theme.apply_theme(tk_root)
    assert "Uikit.tab" in style.element_names()
    assert "Uikit.tab" in str(style.layout("TNotebook.Tab"))


def test_a_prefixed_notebook_inherits_the_flat_tabs(tk_root):
    """The tools name their own notebook styles; those must still get the
    theme's tab layout rather than falling back to clam's raised tabs."""
    theme.apply_theme(tk_root)
    nb = ttk.Notebook(tk_root, style="Probe.TNotebook")
    try:
        nb.add(ttk.Frame(nb), text="one")
        nb.update_idletasks()
        assert nb.cget("style") == "Probe.TNotebook"
    finally:
        nb.destroy()


def test_every_icon_has_a_glyph_and_a_fallback(tk_root):
    for name in icons.GLYPHS:
        text, font = icons.get(tk_root, name, size=11)
        assert text and font[1] == 11
