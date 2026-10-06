"""The host that puts a tool in a workbench tab.

Everything a tool asks of its host (`uikit.host.BaseHost`) lands here and is
turned into something the shell shows: the title becomes the tab's label, the
tool's menus are swapped into the menubar while its tab is active, its
shortcuts fire only while its tab is active, and closing it closes the tab --
never the window.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from uikit.host import BaseHost, JobSpec

if TYPE_CHECKING:                                  # pragma: no cover
    from .shell import Shell


class DocumentHost(BaseHost):
    standalone = False
    close_label = "Close tab"

    def __init__(self, shell: "Shell", kind: str, label: str) -> None:
        self.shell = shell
        self.kind = kind
        self.window = shell.root
        self.frame = ttk.Frame(shell.workspace)
        #: Set by the shell once the tool is built.
        self.app = None
        self.title = label
        self.tab_label = label
        self.dirty = False
        self.busy = False
        self.path: Path | None = None
        #: (label, menu), in the order the tool added them.
        self.menus: list[tuple[str, tk.Menu]] = []
        self.keys: dict[str, Callable] = {}
        #: name -> callback, for the shell's shared menus ("save", "undo", ...).
        self.commands: dict[str, Callable] = {}
        self.vetoes: list[Callable[[], bool]] = []
        self.disposers: list[Callable[[], None]] = []
        self.error_handler: Callable[[BaseException], None] | None = None
        self.closed = False

    # -- presentation --------------------------------------------------------

    def set_title(self, title: str, tab: str | None = None) -> None:
        self.title = title
        if tab:
            self.tab_label = tab
        self.shell.document_changed(self)

    def set_dirty(self, dirty: bool) -> None:
        dirty = bool(dirty)
        if dirty != self.dirty:
            self.dirty = dirty
            self.shell.document_changed(self)

    def set_path(self, path: Path | None) -> None:
        path = Path(path) if path else None
        if path != self.path:
            self.path = path
            self.shell.document_moved(self)

    def set_busy(self, busy: bool) -> None:
        self.busy = bool(busy)
        try:
            self.frame.configure(cursor="watch" if busy else "")
        except tk.TclError:                          # pragma: no cover - closing
            pass
        self.shell.document_changed(self)

    def present(self) -> None:
        self.shell.activate(self)

    # -- menus, keys, commands ------------------------------------------------

    def add_menu(self, label: str) -> tk.Menu:
        # A child of the menubar itself: Windows draws a cascade natively only
        # when its menu is.
        menu = tk.Menu(self.shell.menubar, tearoff=False)
        self.menus.append((label, menu))
        return menu

    def bind_key(self, sequence: str, handler: Callable) -> None:
        self.keys[sequence] = handler
        self.shell.ensure_bound(sequence)

    def add_command(self, name: str, callback: Callable) -> None:
        self.commands[name] = callback
        self.shell.document_changed(self)

    # -- lifetime ------------------------------------------------------------

    def on_close(self, callback: Callable[[], bool]) -> None:
        self.vetoes.append(callback)

    def on_dispose(self, callback: Callable[[], None]) -> None:
        self.disposers.append(callback)

    def close(self) -> bool:
        return self.shell.close_document(self)

    # -- the rest of the program --------------------------------------------

    def run_job(self, spec: JobSpec):
        return self.shell.run_job(spec, owner=self)

    def open_document(self, kind: str, path: Path | None = None,
                      payload=None) -> bool:
        self.shell.open_document(kind, path, payload)
        return True

    def notify(self, title: str, message: str, *, kind: str = "info",
               actions=()) -> None:
        self.shell.notify(title, message, kind=kind, actions=actions, owner=self)

    def set_error_handler(self, handler: Callable[[BaseException], None]) -> None:
        self.error_handler = handler
