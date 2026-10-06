"""The flat ttk theme every tool runs under.

`apply_theme(root)` is called once, before a tool builds its widgets. It
creates a theme called "spotting" on top of ttk's "clam" (the one built-in
theme whose colours can all be set), points the named fonts at Segoe UI, and
fills the option database so the classic tk widgets -- Canvas, Text, Listbox --
match the ttk ones.

Everything here is process-wide by nature: a Tk interpreter has one style
database. That is why the tools must never configure an UNPREFIXED style such
as "Treeview" themselves -- two tools in one window would fight over it. A tool
that wants something different names its own style ("Plates.TNotebook.Tab"),
which inherits everything set here and overrides only what it says.

Style names provided beyond the stock ones:

    Accent.TButton       the one primary action in a view
    Ghost.TButton        a borderless text button
    Muted.TLabel         secondary text
    Small.TLabel         small secondary text
    Heading.TLabel       a section heading
    Title.TLabel         a page title
    Surface.TFrame       a white panel
    Surface.TLabel       ...and a label on one
    Card.TFrame          a white panel with a hairline edge
    Thin.Horizontal.TProgressbar
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from . import tokens as t
from .dpi import px

THEME = "spotting"

#: Tcl variable marking an interpreter as themed, so a second call is free and
#: cannot undo a tool's own (prefixed) style changes made since the first.
_MARK = "::uikit_theme_applied"

#: Named fonts this module creates, beyond Tk's own.
FONT_SMALL = "UikitSmall"
FONT_STRONG = "UikitStrong"
FONT_HEADING = "UikitHeading"
FONT_TITLE = "UikitTitle"
FONT_DISPLAY = "UikitDisplay"
FONT_MONO = "UikitMono"

#: PhotoImages backing the image elements, and the named fonts created here.
#: tkinter deletes both from Tk the moment Python drops its last reference --
#: the element then draws as nothing and the font silently reverts to Tk's
#: default -- so they are held for the life of the process.
_IMAGES: list = []
_FONTS: list = []


def is_applied(widget) -> bool:
    try:
        return bool(int(widget.tk.call("info", "exists", _MARK)))
    except tk.TclError:                              # pragma: no cover - defensive
        return False


def apply_theme(widget) -> ttk.Style:
    """Theme this interpreter. Idempotent: later calls return at once."""
    style = ttk.Style(widget)
    if is_applied(widget):
        return style
    _fonts(widget)
    if THEME not in style.theme_names():
        style.theme_create(THEME, parent="clam")
    style.theme_use(THEME)
    _configure(style, widget)
    _options(widget)
    try:
        widget.winfo_toplevel().configure(background=t.BG)
    except tk.TclError:                              # pragma: no cover - defensive
        pass
    widget.tk.call("set", _MARK, 1)
    return style


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

def _semibold(widget) -> tuple[str, str]:
    """(family, weight) for semibold text, falling back to bold."""
    if t.FONT_FAMILY_SEMIBOLD in tkfont.families(widget):
        return t.FONT_FAMILY_SEMIBOLD, "normal"
    return t.FONT_FAMILY, "bold"


def _named(widget, name: str, **options) -> None:
    if name in tkfont.names(widget):
        tkfont.nametofont(name, root=widget).configure(**options)
    else:
        _FONTS.append(tkfont.Font(widget, name=name, **options))


def _fonts(widget) -> None:
    body = {"family": t.FONT_FAMILY, "size": t.SIZE_BODY, "weight": "normal"}
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        _named(widget, name, **body)
    semi, weight = _semibold(widget)
    _named(widget, "TkHeadingFont", family=semi, size=t.SIZE_BODY, weight=weight)
    _named(widget, FONT_SMALL, family=t.FONT_FAMILY, size=t.SIZE_SMALL)
    _named(widget, FONT_STRONG, family=semi, size=t.SIZE_BODY, weight=weight)
    _named(widget, FONT_HEADING, family=semi, size=t.SIZE_HEADING, weight=weight)
    _named(widget, FONT_TITLE, family=semi, size=t.SIZE_TITLE, weight=weight)
    _named(widget, FONT_DISPLAY, family=semi, size=t.SIZE_DISPLAY, weight=weight)
    _named(widget, FONT_MONO, family=t.MONO_FAMILY, size=t.SIZE_BODY)


def row_height(widget) -> int:
    """Table row height: the body font's line height plus a little air."""
    line = tkfont.nametofont("TkDefaultFont", root=widget).metrics("linespace")
    return line + px(widget, t.ROW_PAD)


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

def _flat(colour: str) -> dict:
    """Clam draws a bevel from lightcolor/darkcolor; equal to the fill, it
    is gone and only the one-pixel bordercolor outline is left."""
    return {"lightcolor": colour, "darkcolor": colour}


def _state_flat(*pairs) -> dict:
    """The same state map for background, lightcolor and darkcolor."""
    spec = list(pairs)
    return {"background": spec, "lightcolor": spec, "darkcolor": spec}


def _configure(style: ttk.Style, w) -> None:
    """Every setting the theme needs, stated in full.

    A theme created with `parent="clam"` borrows clam's ELEMENTS but none of
    its style settings -- not even a button's relief -- so anything clam would
    have set, and this look relies on, is set again here.
    """
    def s(n: float) -> int:
        return px(w, n)

    style.configure(
        ".",
        background=t.BG, foreground=t.TEXT, font="TkDefaultFont",
        bordercolor=t.LINE_STRONG, troughcolor=t.SURFACE_ALT,
        fieldbackground=t.SURFACE, insertcolor=t.TEXT,
        selectbackground=t.ACCENT, selectforeground=t.TEXT_ON_ACCENT,
        focuscolor=t.ACCENT, arrowcolor=t.TEXT_MUTED,
        **_flat(t.BG),
    )
    style.map(".", foreground=[("disabled", t.TEXT_DISABLED)])

    # -- frames and labels ---------------------------------------------------
    style.configure("TFrame", background=t.BG)
    style.configure("Surface.TFrame", background=t.SURFACE)
    style.configure("Card.TFrame", background=t.SURFACE, bordercolor=t.LINE,
                    relief="solid", borderwidth=1)
    style.configure("TLabel", background=t.BG, foreground=t.TEXT)
    style.configure("Muted.TLabel", foreground=t.TEXT_MUTED)
    style.configure("Small.TLabel", foreground=t.TEXT_MUTED, font=FONT_SMALL)
    style.configure("Heading.TLabel", font=FONT_HEADING)
    style.configure("Title.TLabel", font=FONT_TITLE)
    style.configure("Surface.TLabel", background=t.SURFACE)
    style.configure("TLabelframe", background=t.BG, bordercolor=t.LINE,
                    relief="solid", borderwidth=1, labeloutside=True,
                    labelmargins=(0, 0, 0, s(4)), **_flat(t.BG))
    style.configure("TLabelframe.Label", background=t.BG, foreground=t.TEXT,
                    font=FONT_STRONG)

    # -- buttons -------------------------------------------------------------
    pad = (s(10), s(4))
    style.configure("TButton", background=t.SURFACE, foreground=t.TEXT,
                    bordercolor=t.LINE_STRONG, padding=pad, anchor="center",
                    relief="raised", width=-11,
                    focusthickness=1, focuscolor=t.ACCENT, **_flat(t.SURFACE))
    style.map("TButton",
              bordercolor=[("disabled", t.LINE), ("focus", t.ACCENT)],
              **_state_flat(("disabled", t.SURFACE_ALT),
                            ("pressed", t.LINE),
                            ("active", t.SURFACE_ALT)))

    style.configure("Accent.TButton", background=t.ACCENT,
                    foreground=t.TEXT_ON_ACCENT, bordercolor=t.ACCENT,
                    focuscolor=t.TEXT_ON_ACCENT, **_flat(t.ACCENT))
    style.map("Accent.TButton",
              foreground=[("disabled", t.TEXT_ON_ACCENT)],
              bordercolor=[("disabled", t.TEXT_DISABLED),
                           ("pressed", t.ACCENT_PRESSED),
                           ("active", t.ACCENT_HOVER)],
              **_state_flat(("disabled", t.TEXT_DISABLED),
                            ("pressed", t.ACCENT_PRESSED),
                            ("active", t.ACCENT_HOVER)))

    style.configure("Ghost.TButton", background=t.BG, bordercolor=t.BG,
                    foreground=t.ACCENT, relief="flat", **_flat(t.BG))
    style.map("Ghost.TButton",
              bordercolor=[("active", t.SURFACE_ALT)],
              **_state_flat(("pressed", t.LINE), ("active", t.SURFACE_ALT)))

    # Toggle-style buttons: the plate designer's tools, review's media tabs.
    style.configure("Toolbutton", background=t.BG, bordercolor=t.BG,
                    padding=(s(8), s(4)), relief="flat", anchor="center",
                    **_flat(t.BG))
    style.map("Toolbutton",
              foreground=[("disabled", t.TEXT_DISABLED), ("selected", t.ACCENT)],
              bordercolor=[("selected", t.ACCENT_SOFT),
                           ("active", t.SURFACE_ALT)],
              **_state_flat(("selected", t.ACCENT_SOFT),
                            ("pressed", t.LINE),
                            ("active", t.SURFACE_ALT)))

    style.configure("TMenubutton", background=t.SURFACE, bordercolor=t.LINE_STRONG,
                    padding=pad, arrowcolor=t.TEXT_MUTED, relief="raised",
                    width=-11, **_flat(t.SURFACE))
    style.map("TMenubutton",
              **_state_flat(("pressed", t.LINE), ("active", t.SURFACE_ALT)))

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=t.BG, indicatorbackground=t.SURFACE,
                        indicatorforeground=t.TEXT_ON_ACCENT,
                        upperbordercolor=t.LINE_STRONG,
                        lowerbordercolor=t.LINE_STRONG,
                        indicatorsize=s(13),
                        indicatormargin=(0, 0, s(6), 0), padding=s(2))
        style.map(name,
                  background=[("active", t.BG)],
                  indicatorbackground=[("disabled", t.SURFACE_ALT),
                                       ("selected", t.ACCENT),
                                       ("pressed", t.ACCENT_SOFT)],
                  upperbordercolor=[("selected", t.ACCENT),
                                    ("active", t.ACCENT)],
                  lowerbordercolor=[("selected", t.ACCENT),
                                    ("active", t.ACCENT)])
    # A radio button reads best as an accent dot in a white ring; filled
    # solid, clam's large dot left only a ring that looked unselected.
    style.configure("TRadiobutton", indicatorforeground=t.ACCENT)
    style.map("TRadiobutton",
              indicatorbackground=[("disabled", t.SURFACE_ALT),
                                   ("pressed", t.ACCENT_SOFT)])

    # -- fields --------------------------------------------------------------
    field = {"fieldbackground": t.SURFACE, "bordercolor": t.LINE_STRONG,
             "padding": (s(5), s(3)), "insertwidth": 1, **_flat(t.SURFACE)}
    style.configure("TEntry", **field)
    style.map("TEntry",
              fieldbackground=[("disabled", t.SURFACE_ALT),
                               ("readonly", t.SURFACE_ALT)],
              bordercolor=[("focus", t.ACCENT)],
              lightcolor=[("focus", t.SURFACE)])

    style.configure("TCombobox", background=t.SURFACE, arrowcolor=t.TEXT_MUTED,
                    arrowsize=s(12), **field)
    style.map("TCombobox",
              fieldbackground=[("disabled", t.SURFACE_ALT),
                               ("readonly", t.SURFACE)],
              selectbackground=[("readonly", "focus", t.ACCENT_SOFT)],
              selectforeground=[("readonly", "focus", t.TEXT)],
              bordercolor=[("focus", t.ACCENT), ("active", t.TEXT_MUTED)],
              **_state_flat(("pressed", t.SURFACE_ALT),
                            ("active", t.SURFACE)))

    style.configure("ComboboxPopdownFrame", relief="solid", borderwidth=1,
                    bordercolor=t.LINE_STRONG)

    style.configure("TSpinbox", background=t.SURFACE, arrowcolor=t.TEXT_MUTED,
                    arrowsize=s(10),
                    **{**field, "padding": (s(5), s(2), s(12), s(2))})
    style.map("TSpinbox",
              fieldbackground=[("disabled", t.SURFACE_ALT),
                               ("readonly", t.SURFACE_ALT)],
              bordercolor=[("focus", t.ACCENT)],
              **_state_flat(("pressed", t.SURFACE_ALT),
                            ("active", t.SURFACE)))

    # -- tables --------------------------------------------------------------
    style.configure("Treeview", background=t.SURFACE, fieldbackground=t.SURFACE,
                    foreground=t.TEXT, bordercolor=t.LINE,
                    rowheight=row_height(w), **_flat(t.SURFACE))
    style.map("Treeview",
              background=[("selected", "focus", t.ACCENT),
                          ("selected", t.ACCENT_SOFT)],
              foreground=[("selected", "focus", t.TEXT_ON_ACCENT),
                          ("selected", t.TEXT),
                          ("disabled", t.TEXT_DISABLED)])
    style.configure("Heading", background=t.SURFACE_ALT,
                    foreground=t.TEXT_MUTED, font="TkHeadingFont",
                    bordercolor=t.LINE, relief="flat", padding=(s(6), s(3)),
                    **_flat(t.SURFACE_ALT))
    style.map("Heading", **_state_flat(("active", t.LINE)))

    # -- tabs ----------------------------------------------------------------
    _tab_element(style, w)
    style.configure("TNotebook", background=t.BG, bordercolor=t.LINE,
                    tabmargins=(0, 0, 0, 0), **_flat(t.BG))
    style.configure("TNotebook.Tab", foreground=t.TEXT_MUTED,
                    padding=(s(14), s(7)), font="TkDefaultFont")
    style.map("TNotebook.Tab",
              foreground=[("selected", t.TEXT), ("active", t.TEXT)],
              expand=[("selected", (0, 0, 0, 0))])

    # -- panes, scrollbars, progress, separators -----------------------------
    style.configure("TPanedwindow", background=t.BG)
    style.configure("Sash", sashthickness=s(6), gripcount=0,
                    background=t.BG, bordercolor=t.BG, **_flat(t.BG))

    thumb, thumb_hover = "#D0D4DA", "#AEB4BE"
    for orient in ("Vertical", "Horizontal"):
        sticky = "ns" if orient == "Vertical" else "ew"
        style.layout(f"{orient}.TScrollbar", [
            (f"{orient}.Scrollbar.trough", {"sticky": sticky, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1",
                                               "sticky": "nswe"})]})])
        style.configure(f"{orient}.TScrollbar", troughcolor=t.SURFACE,
                        background=thumb, bordercolor=t.SURFACE,
                        arrowsize=s(11), gripcount=0, **_flat(thumb))
        style.map(f"{orient}.TScrollbar",
                  **_state_flat(("pressed", thumb_hover), ("active", thumb_hover)))

    style.configure("TProgressbar", background=t.ACCENT, troughcolor=t.SURFACE_ALT,
                    bordercolor=t.SURFACE_ALT, **_flat(t.ACCENT))
    style.configure("Thin.Horizontal.TProgressbar", thickness=s(4))
    style.configure("TSeparator", background=t.LINE)
    style.configure("TScale", troughcolor=t.SURFACE_ALT, background=t.SURFACE,
                    bordercolor=t.LINE_STRONG, **_flat(t.SURFACE))


def _tab_element(style: ttk.Style, w) -> None:
    """Notebook tabs as flat labels, the selected one underlined in accent.

    Clam draws tabs as raised boxes with no way to colour just one edge, so
    the tab's border is replaced with an image element: a plain block that
    stretches, plus a two-pixel accent bar along the bottom when selected.
    """
    line = max(2, px(w, 2))
    size = line + 6

    def block(fill: str, bar: str | None) -> tk.PhotoImage:
        img = tk.PhotoImage(master=w, width=size, height=size)
        img.put(fill, to=(0, 0, size, size))
        if bar:
            img.put(bar, to=(0, size - line, size, size))
        _IMAGES.append(img)
        return img

    plain = block(t.BG, None)
    hover = block(t.SURFACE_ALT, None)
    chosen = block(t.BG, t.ACCENT)
    border = (2, 2, 2, line + 1)
    if "Uikit.tab" not in style.element_names():
        style.element_create("Uikit.tab", "image", plain,
                             ("selected", chosen), ("active", hover),
                             border=border, sticky="nsew")
    style.layout("TNotebook.Tab", [
        ("Uikit.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])


# ---------------------------------------------------------------------------
# The classic tk widgets, through the option database
# ---------------------------------------------------------------------------

def _options(w) -> None:
    add = w.option_add
    add("*Toplevel.background", t.BG)
    add("*Frame.background", t.BG)
    add("*Label.background", t.BG)
    add("*Label.foreground", t.TEXT)
    add("*Canvas.background", t.BG)
    add("*Canvas.highlightThickness", 0)
    for cls in ("Listbox", "Text", "Entry"):
        add(f"*{cls}.background", t.SURFACE)
        add(f"*{cls}.foreground", t.TEXT)
        add(f"*{cls}.relief", "flat")
        add(f"*{cls}.borderWidth", 0)
        add(f"*{cls}.highlightThickness", 1)
        add(f"*{cls}.highlightBackground", t.LINE_STRONG)
        add(f"*{cls}.highlightColor", t.ACCENT)
        add(f"*{cls}.selectBackground", t.ACCENT)
        add(f"*{cls}.selectForeground", t.TEXT_ON_ACCENT)
        add(f"*{cls}.insertBackground", t.TEXT)
        add(f"*{cls}.font", "TkDefaultFont")
    # The drop-down list of a ttk Combobox is a classic Listbox.
    add("*TCombobox*Listbox.background", t.SURFACE)
    add("*TCombobox*Listbox.foreground", t.TEXT)
    add("*TCombobox*Listbox.selectBackground", t.ACCENT)
    add("*TCombobox*Listbox.selectForeground", t.TEXT_ON_ACCENT)
    add("*TCombobox*Listbox.font", "TkDefaultFont")
    add("*TCombobox*Listbox.highlightThickness", 0)
