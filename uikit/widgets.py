"""Small flat widgets the ttk set does not have.

    IconButton   a glyph and a label that act as one flat button, with an
                 optional drop-down menu; for command bars and link lists
    LinkLabel    text that behaves like a hyperlink

Built from classic tk widgets, because a ttk button cannot mix the icon
font with the text font, and a link needs its colour and underline to follow
the pointer.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from . import icons
from . import tokens as t
from .dpi import px

_LINK_FONTS: dict[str, tuple] = {}


def _link_fonts(widget) -> tuple:
    """(plain, underlined) fonts for links; one pair per interpreter, held so
    tkinter does not delete them."""
    key = str(widget.tk)
    if key not in _LINK_FONTS:
        base = tkfont.nametofont("TkDefaultFont", root=widget).actual()
        plain = tkfont.Font(widget, **base)
        under = tkfont.Font(widget, **base)
        under.configure(underline=True)
        _LINK_FONTS[key] = (plain, under)
    return _LINK_FONTS[key]


class IconButton(tk.Frame):
    """A flat button: an icon glyph, a label, both optional, hover-tinted.

    With `menu`, a click drops that menu below the button instead of calling
    `command`, and a small chevron says so.
    """

    def __init__(self, master, icon: str | None = None, text: str = "",
                 command: Callable | None = None, *,
                 menu: tk.Menu | None = None,
                 background: str = t.SURFACE,
                 foreground: str = t.TEXT,
                 hover: str = t.SURFACE_ALT,
                 icon_size: int = 11,
                 anchor: str = "center",
                 pad: int = 8) -> None:
        super().__init__(master, background=background, cursor="hand2")
        self.command = command
        self.menu = menu
        self._bg, self._fg, self._hover = background, foreground, hover
        self._enabled = True
        self._labels: list[tk.Label] = []

        if icon:
            glyph, font = icons.get(self, icon, icon_size)
            self._add(glyph, font, (px(self, pad), px(self, 4 if text else pad)))
        if text:
            self._add(text, "TkDefaultFont",
                      (0 if icon else px(self, pad), px(self, 4 if menu else pad)))
        if menu is not None:
            glyph, font = icons.get(self, "chevron_down", max(7, icon_size - 4))
            self._add(glyph, font, (0, px(self, pad)))
        if anchor == "w":
            for label in self._labels:
                label.pack_configure(anchor="w")

        for widget in (self, *self._labels):
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<ButtonRelease-1>", self._click, add="+")

    def _add(self, text, font, padx) -> None:
        label = tk.Label(self, text=text, font=font, background=self._bg,
                         foreground=self._fg, borderwidth=0)
        label.pack(side="left", padx=padx, pady=px(self, 5))
        self._labels.append(label)

    def _paint(self, background: str) -> None:
        self.configure(background=background)
        for label in self._labels:
            label.configure(background=background)

    def _enter(self, _event=None) -> None:
        if self._enabled:
            self._paint(self._hover)

    def _leave(self, _event=None) -> None:
        self._paint(self._bg)

    def _click(self, event) -> None:
        if not self._enabled:
            return
        # Released outside the button: the press was abandoned.
        x, y = event.x_root - self.winfo_rootx(), event.y_root - self.winfo_rooty()
        if not (0 <= x < self.winfo_width() and 0 <= y < self.winfo_height()):
            return
        if self.menu is not None:
            self.menu.tk_popup(self.winfo_rootx(),
                               self.winfo_rooty() + self.winfo_height())
        elif self.command is not None:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        colour = self._fg if enabled else t.TEXT_DISABLED
        for label in self._labels:
            label.configure(foreground=colour)
        self.configure(cursor="hand2" if enabled else "")
        if not enabled:
            self._paint(self._bg)

    def set_text(self, text: str) -> None:
        """Change the label (the text part, when there is one)."""
        for label in self._labels:
            if label.cget("font") == "TkDefaultFont":
                label.configure(text=text)
                return


class LinkLabel(tk.Label):
    """Text that reads, and behaves, as a link."""

    def __init__(self, master, text: str, command: Callable | None = None, *,
                 background: str = t.BG, foreground: str = t.ACCENT,
                 **options) -> None:
        plain, self._under = _link_fonts(master)
        super().__init__(master, text=text, background=background,
                         foreground=foreground, font=plain, cursor="hand2",
                         borderwidth=0, **options)
        self._plain = plain
        self._fg = foreground
        self.command = command
        self.bind("<Enter>", lambda _e: self.configure(font=self._under))
        self.bind("<Leave>", lambda _e: self.configure(font=self._plain))
        self.bind("<ButtonRelease-1>", self._click)

    def _click(self, _event=None) -> None:
        if self.command is not None:
            self.command()
