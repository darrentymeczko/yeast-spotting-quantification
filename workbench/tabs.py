"""The document tab strip.

Flat tabs on the white chrome: the selected one is underlined in the accent
colour, an unsaved document shows a dot where its close cross would be, and
the cross itself appears on the tab under the pointer. Middle-click closes,
dragging reorders tabs, right-click offers the tab's menu, and a chevron lists
every tab for when there are more than fit.

Built from classic tk widgets: ttk's notebook cannot draw a close button.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

from uikit import icons
from uikit import tokens as t
from uikit.dpi import px


class _Tab:
    def __init__(self, strip: "TabStrip", key, label: str, icon: str | None,
                 closable: bool, movable: bool) -> None:
        self.strip = strip
        self.key = key
        self.closable = closable
        self.movable = movable
        self.dirty = False
        self.busy = False
        self.hover = False
        bg = t.SURFACE

        self.frame = tk.Frame(strip.row, background=bg, cursor="hand2")
        inner = tk.Frame(self.frame, background=bg)
        inner.pack(side="top", fill="x")
        self.bar = tk.Frame(self.frame, background=bg, height=max(2, px(strip, 2)))
        self.bar.pack(side="bottom", fill="x")

        self.icon = None
        if icon:
            glyph, font = icons.get(strip, icon, 10)
            self.icon = tk.Label(inner, text=glyph, font=font, background=bg,
                                 foreground=t.TEXT_MUTED)
            self.icon.pack(side="left", padx=(px(strip, 12), px(strip, 6)),
                           pady=px(strip, 7))
        self.text = tk.Label(inner, text=label, background=bg,
                             foreground=t.TEXT_MUTED, font="TkDefaultFont")
        self.text.pack(side="left", padx=(0 if icon else px(strip, 12),
                                          px(strip, 6 if closable else 14)),
                       pady=px(strip, 7))
        self.close = None
        if closable:
            self._cross = icons.get(strip, "close", 7)
            self.close = tk.Label(inner, text=" ", font=self._cross[1],
                                  background=bg, foreground=t.TEXT_MUTED,
                                  width=2)
            self.close.pack(side="left", padx=(0, px(strip, 8)))
            self.close.bind("<ButtonRelease-1>", self._close_clicked)

        for widget in self._widgets():
            widget.bind("<ButtonPress-1>", self._press, add="+")
            widget.bind("<B1-Motion>", self._drag, add="+")
            widget.bind("<ButtonRelease-1>", self._select, add="+")
            widget.bind("<ButtonRelease-2>", self._middle, add="+")
            widget.bind("<Button-3>", self._menu, add="+")
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")

    def _widgets(self):
        out = [self.frame, self.text, self.text.master, self.bar]
        if self.icon is not None:
            out.append(self.icon)
        if self.close is not None:
            out.append(self.close)
        return out

    # -- events ----------------------------------------------------------------

    def _press(self, event) -> None:
        self.strip._cancel_drag()
        if self.movable and event.widget is not self.close:
            self.strip._drag_tab = self
            self.strip._drag_start = (event.x_root, event.y_root)

    def _drag(self, event) -> None:
        if self.strip._drag_tab is self:
            self.strip._drag_to(event)

    def _select(self, event) -> str | None:
        if self.close is not None and event.widget is self.close:
            return None                 # the cross acts on release
        if self.strip._drag_tab is self and self.strip._dragging:
            self.strip._drag_to(event)
            index = self.strip._drop_index
            self.strip._cancel_drag()
            if index is not None:
                self.strip.on_select(self.key)
                self.strip.move(self.key, index)
            return "break"
        self.strip._cancel_drag()
        self.strip.on_select(self.key)
        return None

    def _close_clicked(self, _event=None) -> str:
        self.strip.on_close(self.key)
        return "break"

    def _middle(self, _event=None) -> None:
        if self.closable:
            self.strip.on_close(self.key)

    def _menu(self, event) -> None:
        if self.strip.on_menu is not None:
            self.strip.on_menu(self.key, event.x_root, event.y_root)

    def _enter(self, _event=None) -> None:
        self.hover = True
        self.paint()

    def _leave(self, event=None) -> None:
        # <Leave> also fires moving between the tab's own parts.
        x, y = self.frame.winfo_pointerxy()
        inside = self.frame.winfo_containing(x, y)
        if inside is not None and str(inside).startswith(str(self.frame)):
            return
        self.hover = False
        self.paint()

    # -- drawing ---------------------------------------------------------------

    def paint(self) -> None:
        selected = self.strip.selected is self.key
        bg = t.SURFACE_ALT if (self.hover and not selected) else t.SURFACE
        fg = t.TEXT if selected else t.TEXT_MUTED
        for widget in (self.frame, self.text.master, self.text, self.icon,
                       self.close):
            if widget is not None:
                widget.configure(background=bg)
        self.bar.configure(background=t.ACCENT if selected else bg)
        self.text.configure(foreground=fg)
        if self.icon is not None:
            self.icon.configure(foreground=t.ACCENT if selected else t.TEXT_MUTED)
        if self.close is not None:
            if self.hover or (selected and not self.dirty):
                self.close.configure(text=self._cross[0], font=self._cross[1],
                                     foreground=t.TEXT_MUTED)
            elif self.dirty:
                self.close.configure(text="●", font="TkDefaultFont",
                                     foreground=t.TEXT if selected else t.TEXT_MUTED)
            else:
                self.close.configure(text=" ")

    def set_label(self, label: str) -> None:
        self.text.configure(text=label)


class TabStrip(tk.Frame):
    def __init__(self, master, *, on_select: Callable, on_close: Callable,
                 on_menu: Callable | None = None,
                 on_list: Callable | None = None,
                 on_reorder: Callable | None = None) -> None:
        super().__init__(master, background=t.SURFACE)
        self.on_select = on_select
        self.on_close = on_close
        self.on_menu = on_menu
        self.on_reorder = on_reorder
        self.selected = None
        self._tabs: dict[int, _Tab] = {}
        self._order: list = []
        self._drag_tab: _Tab | None = None
        self._drag_start = (0, 0)
        self._dragging = False
        self._drop_index: int | None = None
        self._marker = tk.Frame(self, background=t.ACCENT, width=px(self, 3))

        self.row = tk.Frame(self, background=t.SURFACE)
        self.row.pack(side="left", fill="y")
        if on_list is not None:
            glyph, font = icons.get(self, "chevron_down", 8)
            more = tk.Label(self, text=glyph, font=font, background=t.SURFACE,
                            foreground=t.TEXT_MUTED, cursor="hand2",
                            padx=px(self, 10))
            more.pack(side="right", fill="y")
            more.bind("<ButtonRelease-1>",
                      lambda e: on_list(e.widget.winfo_rootx(),
                                        e.widget.winfo_rooty()
                                        + e.widget.winfo_height()))
            more.bind("<Enter>", lambda e: e.widget.configure(
                background=t.SURFACE_ALT))
            more.bind("<Leave>", lambda e: e.widget.configure(
                background=t.SURFACE))

    def add(self, key, label: str, icon: str | None = None, *,
            closable: bool = True, movable: bool = True) -> None:
        tab = _Tab(self, key, label, icon, closable, movable)
        self._tabs[id(key)] = tab
        self._order.append(key)
        tab.frame.pack(side="left", fill="y")
        tab.paint()

    def remove(self, key) -> None:
        self._cancel_drag()
        tab = self._tabs.pop(id(key), None)
        if tab is not None:
            tab.frame.destroy()
        if key in self._order:
            self._order.remove(key)
        if self.selected is key:
            self.selected = None

    def _move_bounds(self, key) -> tuple[int, int]:
        """Fixed tabs are anchors; movable tabs cannot cross them."""
        index = self._order.index(key)
        lower, upper = 0, len(self._order) - 1
        for i, other in enumerate(self._order):
            if not self._tabs[id(other)].movable:
                if i < index:
                    lower = i + 1
                elif i > index:
                    upper = i - 1
                    break
        return lower, upper

    def move(self, key, index: int) -> bool:
        """Move an existing tab without rebuilding its document or selection."""
        tab = self._tabs.get(id(key))
        if tab is None or not tab.movable:
            return False
        lower, upper = self._move_bounds(key)
        index = max(lower, min(index, upper))
        if self._order.index(key) == index:
            return False
        self._order.remove(key)
        self._order.insert(index, key)
        # Reconfigure packing order without unmapping the tabs.
        if index:
            tab.frame.pack_configure(after=self._tabs[id(self._order[index - 1])].frame)
        else:
            tab.frame.pack_configure(before=self._tabs[id(self._order[1])].frame)
        if self.on_reorder is not None:
            self.on_reorder()
        return True

    def _cancel_drag(self) -> None:
        self._marker.place_forget()
        self._drag_tab = None
        self._dragging = False
        self._drop_index = None

    def _drag_to(self, event) -> None:
        tab = self._drag_tab
        if tab is None:
            return
        if not self._dragging:
            if max(abs(event.x_root - self._drag_start[0]),
                   abs(event.y_root - self._drag_start[1])) < px(self, 5):
                return
            self._dragging = True
        self._drop_index = None
        self._marker.place_forget()
        if not (self.winfo_rooty() <= event.y_root
                < self.winfo_rooty() + self.winfo_height()):
            return
        # Keep the layout still while dragging so unequal-width tabs never
        # shuffle back and forth under the pointer. Show the insertion edge.
        remaining = [self._tabs[id(k)] for k in self._order if k is not tab.key]
        index = sum(event.x_root >= other.frame.winfo_rootx()
                    + other.frame.winfo_width() / 2 for other in remaining)
        lower, upper = self._move_bounds(tab.key)
        index = max(lower, min(index, upper))
        self._drop_index = index
        if index == self._order.index(tab.key):
            return
        edge = (remaining[index].frame.winfo_rootx() if index < len(remaining)
                else remaining[-1].frame.winfo_rootx()
                + remaining[-1].frame.winfo_width())
        x = max(0, min(edge - self.winfo_rootx(), self.winfo_width() - px(self, 3)))
        self._marker.place(x=x, y=0, relheight=1)
        self._marker.lift()

    def select(self, key) -> None:
        self.selected = key
        for tab in self._tabs.values():
            tab.paint()

    def update_tab(self, key, *, label: str | None = None,
                   dirty: bool | None = None) -> None:
        tab = self._tabs.get(id(key))
        if tab is None:
            return
        if label is not None:
            tab.set_label(label)
        if dirty is not None:
            tab.dirty = dirty
        tab.paint()

    def keys(self) -> list:
        return list(self._order)

    def label(self, key) -> str:
        tab = self._tabs.get(id(key))
        return tab.text.cget("text") if tab is not None else ""
