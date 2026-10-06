"""Notices that do not interrupt: a run finishing, an export landing.

A dialog box would stop whatever the person is doing in another tab to say
something that can wait. A toast sits in the bottom-right corner of the
workspace instead, with what happened and what can be done about it ("Open in
Review"), and goes away by itself -- except an error, which stays until it has
been read.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from uikit import icons
from uikit import tokens as t
from uikit.dpi import px
from uikit.theme import FONT_STRONG
from uikit.widgets import LinkLabel

#: How long an ordinary notice stays, in milliseconds.
LINGER_MS = 12000
#: At most this many on screen; the oldest goes first.
MAX_SHOWN = 3

_KIND = {"info": ("info", t.ACCENT), "ok": ("check", t.OK),
         "warning": ("warning", t.WARNING), "error": ("error", t.ERROR)}


class Toast(tk.Frame):
    def __init__(self, area: "ToastArea", title: str, message: str, kind: str,
                 actions) -> None:
        super().__init__(area.parent, background=t.SURFACE,
                         highlightthickness=1, highlightbackground=t.LINE_STRONG)
        self.area = area
        pad = px(self, 12)
        name, colour = _KIND.get(kind, _KIND["info"])
        glyph, font = icons.get(self, name, 12)
        tk.Label(self, text=glyph, font=font, foreground=colour,
                 background=t.SURFACE).grid(row=0, column=0, rowspan=3,
                                            sticky="n", padx=(pad, px(self, 10)),
                                            pady=(pad, 0))
        tk.Label(self, text=title, font=FONT_STRONG, background=t.SURFACE,
                 foreground=t.TEXT, anchor="w", justify="left").grid(
            row=0, column=1, sticky="w", pady=(pad, 0))
        close_glyph, close_font = icons.get(self, "close", 7)
        close = tk.Label(self, text=close_glyph, font=close_font, cursor="hand2",
                         background=t.SURFACE, foreground=t.TEXT_MUTED)
        close.grid(row=0, column=2, sticky="ne", padx=(px(self, 8), pad),
                   pady=(pad, 0))
        close.bind("<ButtonRelease-1>", lambda _e: self.dismiss())
        if message:
            tk.Label(self, text=message, background=t.SURFACE,
                     foreground=t.TEXT_MUTED, justify="left", anchor="w",
                     wraplength=px(self, 300)).grid(row=1, column=1,
                                                    columnspan=2, sticky="w",
                                                    padx=(0, pad))
        row = tk.Frame(self, background=t.SURFACE)
        row.grid(row=2, column=1, columnspan=2, sticky="w",
                 pady=(px(self, 6), pad))
        for label, command in actions:
            LinkLabel(row, label, self._then(command), background=t.SURFACE).pack(
                side="left", padx=(0, px(self, 14)))
        self._timer = None
        if kind != "error":
            self._timer = self.after(LINGER_MS, self.dismiss)

    def _then(self, command: Callable) -> Callable:
        def go() -> None:
            self.dismiss()
            command()
        return go

    def dismiss(self) -> None:
        if self._timer is not None:
            try:
                self.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        self.area.remove(self)


class ToastArea:
    """Stacks toasts in the bottom-right corner of `parent`."""

    def __init__(self, parent) -> None:
        self.parent = parent
        self.toasts: list[Toast] = []

    def show(self, title: str, message: str = "", *, kind: str = "info",
             actions=()) -> Toast:
        toast = Toast(self, title, message, kind, list(actions))
        self.toasts.append(toast)
        while len(self.toasts) > MAX_SHOWN:
            self.toasts[0].dismiss()
        self._layout()
        return toast

    def remove(self, toast: Toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
        try:
            toast.destroy()
        except tk.TclError:
            pass
        self._layout()

    def _layout(self) -> None:
        margin = px(self.parent, 16)
        gap = px(self.parent, 8)
        y = -margin
        for toast in reversed(self.toasts):
            toast.update_idletasks()
            toast.place(relx=1.0, rely=1.0, x=-margin, y=y, anchor="se")
            toast.lift()
            y -= toast.winfo_reqheight() + gap
