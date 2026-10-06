"""The Explorer: every plate template, experiment and result set in the project.

One group per kind of document, in the order the work happens. Double-click
(or Enter) opens a file in a tab, or brings its tab forward if it is already
open; open documents are listed in bold. The right-click menu offers Open,
Show in folder and Copy path. It is read again whenever the window comes back
into focus, when a document is saved somewhere new, and on Refresh.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING

from uikit import icons
from uikit import tokens as t
from uikit.dpi import px
from uikit.theme import FONT_SMALL, FONT_STRONG

from .registry import KINDS, path_key

if TYPE_CHECKING:                                  # pragma: no cover
    from .shell import Shell


class Explorer(ttk.Frame):
    def __init__(self, master, shell: "Shell") -> None:
        super().__init__(master, style="Sidebar.TFrame")
        self.shell = shell
        self._paths: dict[str, tuple[str, Path]] = {}

        head = ttk.Frame(self, style="Sidebar.TFrame")
        head.pack(fill="x", padx=(px(self, 12), px(self, 4)),
                  pady=(px(self, 8), px(self, 2)))
        ttk.Label(head, text="EXPLORER", font=FONT_SMALL,
                  style="Sidebar.Muted.TLabel").pack(side="left")
        glyph, font = icons.get(self, "refresh", 9)
        refresh = tk.Label(head, text=glyph, font=font, background=t.SURFACE,
                           foreground=t.TEXT_MUTED, cursor="hand2",
                           padx=px(self, 6))
        refresh.pack(side="right")
        refresh.bind("<ButtonRelease-1>", lambda _e: self.refresh())

        area = ttk.Frame(self, style="Sidebar.TFrame")
        area.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(area, show="tree", style="Sidebar.Treeview",
                                 selectmode="browse")
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.tree.yview)

        def scrolled(first, last) -> None:
            # Only there when there is something to scroll to.
            scroll.set(first, last)
            if float(first) <= 0.0 and float(last) >= 1.0:
                scroll.pack_forget()
            elif not scroll.winfo_manager():
                scroll.pack(side="right", fill="y", before=self.tree)

        self.tree.configure(yscrollcommand=scrolled)
        self.tree.pack(side="left", fill="both", expand=True,
                       padx=(px(self, 4), 0))
        self.tree.tag_configure("group", font=FONT_SMALL, foreground=t.TEXT_MUTED)
        self.tree.tag_configure("open", font=FONT_STRONG)
        self.tree.tag_configure("empty", foreground=t.TEXT_DISABLED)

        self.tree.bind("<Double-Button-1>", self._activate)
        self.tree.bind("<Return>", self._activate)
        self.tree.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Open", command=self._open_selected)
        self.menu.add_command(label="Show in folder", command=self._reveal_selected)
        self.menu.add_command(label="Copy path", command=self._copy_selected)

        for kind in KINDS:
            self.tree.insert("", "end", iid=f"group:{kind.key}",
                             text=f"{kind.step}  {kind.title.upper()}",
                             open=False, tags=("group",))
        self.refresh()

    # -- contents ----------------------------------------------------------------

    def refresh(self) -> None:
        open_paths = self.shell.open_paths()
        selected = self.tree.selection()
        self._paths.clear()
        for kind in KINDS:
            group = f"group:{kind.key}"
            self.tree.delete(*self.tree.get_children(group))
            try:
                files = kind.files()
            except Exception:                    # a moved folder must not break it
                files = []
            if not files:
                self.tree.insert(group, "end", text="none yet", tags=("empty",))
                continue
            for path in files:
                iid = f"{kind.key}:{path}"
                self._paths[iid] = (kind.key, path)
                tags = ("open",) if path_key(path) in open_paths else ()
                self.tree.insert(group, "end", iid=iid,
                                 text=self.shell.display_name(kind.key, path),
                                 tags=tags)
        keep = [iid for iid in selected if self.tree.exists(iid)]
        if keep:
            self.tree.selection_set(keep)

    # -- acting on a row ------------------------------------------------------------

    def _selected(self) -> tuple[str, Path] | None:
        sel = self.tree.selection()
        return self._paths.get(sel[0]) if sel else None

    def _activate(self, _event=None) -> str:
        self._open_selected()
        return "break"

    def _open_selected(self) -> None:
        got = self._selected()
        if got is not None:
            self.shell.open_document(*got)

    def _reveal_selected(self) -> None:
        got = self._selected()
        if got is not None:
            reveal(got[1])

    def _copy_selected(self) -> None:
        got = self._selected()
        if got is not None:
            self.clipboard_clear()
            self.clipboard_append(str(got[1]))

    def _menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row or row not in self._paths:
            return
        self.tree.selection_set(row)
        self.menu.tk_popup(event.x_root, event.y_root)


def reveal(path: Path) -> None:
    """Show `path` in the file manager, selected where the platform allows."""
    path = Path(path)
    try:
        if sys.platform == "win32":
            if path.is_dir():
                os.startfile(str(path))                  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])
    except OSError:
        pass
