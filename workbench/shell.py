"""The workbench's main window.

    ┌ File  Edit  View  Window  Help ─────────────────────────────────────┐
    ├────────────┬─────────────────────────────────────────────────────────┤
    │ EXPLORER   │ Home │ lab_8x6 ● × │ Set09 × │                            │  tabs
    │            │                                                         │
    │            │            the active document, or Home                │
    ├────────────┴─────────────────────────────────────────────────────────┤
    │ status                                                    environment │
    └──────────────────────────────────────────────────────────────────────┘

The shell owns the window, and only the shell touches it. A document (a tool
in a `DocumentHost`) asks for things; the shell decides how they look:

  * menus -- the menubar is File, the active document's own menus, Window,
    Help. Switching tab swaps the document's menus in and out.
  * shortcuts -- every key sequence any document asks for is bound once, on
    this window, and handed to the ACTIVE document only. Ctrl+Z in one tab
    never undoes another.
  * closing -- a tab asks its document first (unsaved changes); closing the
    window asks every document.
  * errors -- a Tk callback that raises goes to the active document's handler
    if it has one, otherwise to `workbench_error.log` and a message box.
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from uikit import tasks
from uikit import tokens as t
from uikit.dpi import px
from uikit.host import JobSpec
from uikit.theme import FONT_SMALL, FONT_STRONG, apply_theme

from . import brand
from .docs import DocumentHost
from .explorer import Explorer, reveal
from .home import HomePage
from .jobs import LOG_DIR, Job, JobRunner
from .jobs_ui import JobLog, JobsPanel
from .registry import BY_KEY, KINDS, import_configuration, kind_of, path_key
from .settings import ROOT, Settings
from .tabs import TabStrip
from .toast import ToastArea

APP_TITLE = "Spotting Quantification"
ERROR_LOG = ROOT / "workbench_error.log"
FROZEN = bool(getattr(sys, "frozen", False))


class _Home:
    """The Home tab's key in the tab strip: not a document."""

    def __repr__(self) -> str:                     # pragma: no cover - debugging
        return "<Home>"


HOME = _Home()


def job_python() -> str:
    """The interpreter for a child process: python.exe, even when the window
    itself runs under pythonw.exe (which has nowhere to print)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def program_command(*args: str) -> list[str]:
    """`spotting_app.py <args>`, however this program is being run."""
    if FROZEN:
        return [sys.executable, *args]
    return [job_python(), str(ROOT / "spotting_app.py"), *args]


class Shell:
    def __init__(self, root: tk.Tk, settings: Settings | None = None, *,
                 restore: bool = True, log_dir: Path | None = LOG_DIR) -> None:
        self.root = root
        self.settings = settings if settings is not None else Settings()
        self.documents: list[DocumentHost] = []
        self.active: DocumentHost | None = None
        self.runner = JobRunner(self._job_changed,
                                None if self.settings.read_only else log_dir)
        self._bound: set[str] = set()
        self._closed = False
        self._reporting = False
        self._explorer_job: str | None = None
        self.global_keys = {
            "<Control-w>": lambda _e: self._close_active(),
            "<Control-F4>": lambda _e: self._close_active(),
            "<Control-Tab>": lambda _e: self.step_tab(1),
            "<Control-Shift-Tab>": lambda _e: self.step_tab(-1),
        }

        apply_theme(root)
        self._styles()
        root.title(APP_TITLE)
        brand.set_window_icon(root)
        # No tearoff entry: it would sit at index 0 and shift every cascade.
        self.menubar = tk.Menu(root, tearoff=False)
        root.configure(menu=self.menubar)
        self._build_menus()
        self._build_layout()

        self.home = HomePage(self.workspace, self)
        self.tabs.add(HOME, "Home", "home", closable=False, movable=False)
        for sequence in self.global_keys:
            self.ensure_bound(sequence)

        root.protocol("WM_DELETE_WINDOW", self.exit)
        root.report_callback_exception = self._callback_error
        root.bind("<Activate>", self._window_activated, add="+")
        self._restore_window()
        self.activate(None)
        self._pump_id = root.after(100, self._pump_jobs)
        self._tick_id = root.after(1000, self._tick)
        if restore:
            root.after_idle(self._restore_session)

    # -- construction --------------------------------------------------------------

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Line.TFrame", background=t.LINE)
        style.configure("Workspace.TFrame", background=t.BG)
        style.configure("Status.TFrame", background=t.SURFACE_ALT)
        style.configure("Status.TLabel", background=t.SURFACE_ALT,
                        foreground=t.TEXT_MUTED, font=FONT_SMALL)
        style.configure("Sidebar.TFrame", background=t.SURFACE)
        style.configure("Sidebar.Muted.TLabel", background=t.SURFACE,
                        foreground=t.TEXT_MUTED)
        style.configure("Sidebar.Treeview", background=t.SURFACE,
                        fieldbackground=t.SURFACE, bordercolor=t.SURFACE,
                        borderwidth=0, lightcolor=t.SURFACE, darkcolor=t.SURFACE)

    def _line(self, master, **grid) -> None:
        line = ttk.Frame(master, style="Line.TFrame", height=1)
        line.grid(sticky="ew", **grid)

    def _build_layout(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self.paned = ttk.PanedWindow(root, orient="horizontal")
        self.paned.grid(row=2, column=0, sticky="nsew")
        # The sidebar: the Explorer above, the Jobs panel below it once there
        # has been a run.
        self.sidebar = ttk.Frame(self.paned, style="Sidebar.TFrame")
        self.jobs_panel = JobsPanel(self.sidebar, self)
        self.explorer = Explorer(self.sidebar, self)
        self.explorer.pack(side="top", fill="both", expand=True)
        self.paned.add(self.sidebar, weight=0)

        main = ttk.Frame(self.paned)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        self.paned.add(main, weight=1)

        self.tabs = TabStrip(main, on_select=self._tab_selected,
                             on_close=self._tab_closed, on_menu=self._tab_menu,
                             on_list=self._tab_list, on_reorder=self._tabs_reordered)
        self.tabs.grid(row=0, column=0, sticky="ew")
        self._line(main, row=1, column=0)
        self.workspace = ttk.Frame(main, style="Workspace.TFrame")
        self.workspace.grid(row=2, column=0, sticky="nsew")
        self.workspace.rowconfigure(0, weight=1)
        self.workspace.columnconfigure(0, weight=1)
        self.toasts = ToastArea(self.workspace)

        self._line(root, row=3, column=0)
        status = ttk.Frame(root, style="Status.TFrame", padding=(px(root, 10),
                                                                px(root, 3)))
        status.grid(row=4, column=0, sticky="ew")
        self.status_text = ttk.Label(status, style="Status.TLabel", text="Ready")
        self.status_text.pack(side="left")
        # The run in progress, if any: what it is and how far it has got.
        self.status_job = ttk.Label(status, style="Status.TLabel", text="",
                                    cursor="hand2")
        self.status_job.pack(side="left", padx=(px(root, 24), 0))
        self.status_job.bind("<ButtonRelease-1>", lambda _e: self._open_running())
        env = Path(sys.prefix).name
        ttk.Label(status, style="Status.TLabel",
                  text=f"Python {sys.version_info.major}.{sys.version_info.minor}"
                       f" ({env})   ·   {ROOT.name}").pack(side="right")

    def _build_menus(self) -> None:
        self.file_menu = tk.Menu(self.menubar, tearoff=False)
        self.new_menu = tk.Menu(self.file_menu, tearoff=False)
        self.open_menu = tk.Menu(self.file_menu, tearoff=False)
        for kind in KINDS:
            # A result set and a data review each belong to something that
            # already exists, so there is no blank one to make.
            if kind.key not in ("review", "data-review"):
                self.new_menu.add_command(
                    label=kind.title,
                    command=lambda k=kind.key: self.open_document(k))
            self.open_menu.add_command(
                label=f"{'Result set' if kind.key == 'review' else kind.title}…",
                command=lambda k=kind.key: self.ask_open(k))
        self.file_menu.add_cascade(label="New", menu=self.new_menu)
        self.file_menu.add_cascade(label="Open", menu=self.open_menu)
        self.file_menu.add_command(label="Save", accelerator="Ctrl+S",
                                   command=lambda: self.run_command("save"))
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Close tab", accelerator="Ctrl+W",
                                   command=self._close_active)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.exit)

        self.edit_menu = tk.Menu(self.menubar, tearoff=False)
        for name, key in (("Undo", "Z"), ("Redo", "Y")):
            self.edit_menu.add_command(label=name, accelerator=f"Ctrl+{key}",
                                       command=lambda n=name.lower(): self.run_command(n))

        self.window_menu = tk.Menu(self.menubar, tearoff=False)
        self._window_choice = tk.StringVar(value="home")
        self._show_explorer = tk.BooleanVar(
            value=bool(self.settings.get("explorer", True)))

        self.help_menu = tk.Menu(self.menubar, tearoff=False)
        self.help_menu.add_command(label="Read the manual",
                                   command=self.open_readme)
        self.help_menu.add_command(label="Open the project folder",
                                   command=self.open_project_folder)
        self.help_menu.add_command(label="Check the installation",
                                   command=self.run_selftest)
        self.help_menu.add_separator()
        self.help_menu.add_command(label=f"About {APP_TITLE}", command=self.about)

    # -- the menubar follows the active tab -------------------------------------------

    @staticmethod
    def _doc_menu(doc: DocumentHost | None, label: str) -> tk.Menu | None:
        if doc is None:
            return None
        return next((m for name, m in doc.menus if name == label), None)

    def _install_menus(self, doc: DocumentHost | None) -> None:
        mb = self.menubar
        mb.delete(0, "end")
        mb.add_cascade(label="File", underline=0,
                       menu=self._doc_menu(doc, "File") or self.file_menu)
        if doc is not None:
            for label, menu in doc.menus:
                if label not in ("File", "Help"):
                    mb.add_cascade(label=label, menu=menu, underline=0)
            if self._doc_menu(doc, "Edit") is None and "undo" in doc.commands:
                mb.add_cascade(label="Edit", menu=self.edit_menu, underline=0)
        mb.add_cascade(label="Window", menu=self.window_menu, underline=0)
        mb.add_cascade(label="Help", underline=0,
                       menu=self._doc_menu(doc, "Help") or self.help_menu)

    def _finish_menus(self, doc: DocumentHost) -> None:
        """The program's own items, at the foot of a document's File and Help."""
        file_menu = self._doc_menu(doc, "File")
        if file_menu is not None:
            file_menu.add_separator()
            file_menu.add_command(label=f"Exit {APP_TITLE}", command=self.exit)
        help_menu = self._doc_menu(doc, "Help")
        if help_menu is not None:
            help_menu.add_separator()
            help_menu.add_command(label=f"About {APP_TITLE}", command=self.about)

    def _sync_window_menu(self) -> None:
        menu = self.window_menu
        menu.delete(0, "end")
        menu.add_radiobutton(label="Home", value="home",
                             variable=self._window_choice,
                             command=lambda: self.activate(None))
        if self.documents:
            menu.add_separator()
        for doc in self.documents:
            mark = "  •" if doc.dirty else ""
            menu.add_radiobutton(
                label=doc.tab_label + mark,
                value=str(id(doc)), variable=self._window_choice,
                command=lambda d=doc: self.activate(d))
        menu.add_separator()
        menu.add_command(label="Next tab", accelerator="Ctrl+Tab",
                         command=lambda: self.step_tab(1))
        menu.add_command(label="Previous tab", accelerator="Ctrl+Shift+Tab",
                         command=lambda: self.step_tab(-1))
        menu.add_command(label="Close tab", accelerator="Ctrl+W",
                         command=self._close_active,
                         state="normal" if self.active else "disabled")
        menu.add_command(label="Close all tabs", command=self.close_all,
                         state="normal" if self.documents else "disabled")
        menu.add_separator()
        menu.add_checkbutton(label="Explorer", variable=self._show_explorer,
                             command=self._toggle_explorer)
        self._window_choice.set("home" if self.active is None
                                else str(id(self.active)))

    # -- shortcuts go to the active tab only -----------------------------------------

    def ensure_bound(self, sequence: str) -> None:
        """Bind `sequence` once, on this window, to the dispatcher.

        On the window rather than `bind_all`: a dialog is a window of its own,
        so typing in one never fires a document's shortcut behind it.
        """
        if sequence in self._bound:
            return
        self._bound.add(sequence)
        self.root.bind(sequence, lambda e, s=sequence: self._dispatch(s, e))

    def _dispatch(self, sequence: str, event):
        doc = self.active
        if doc is not None:
            handler = doc.keys.get(sequence)
            if handler is not None:
                # Its answer is final: "break" stops the event, None lets the
                # focused widget have it.
                return handler(event)
        handler = self.global_keys.get(sequence)
        return handler(event) if handler is not None else None

    # -- documents ----------------------------------------------------------------

    def open_document(self, kind: str, path: Path | None = None,
                      payload=None) -> DocumentHost | None:
        """Open a document of `kind` in a tab: a file, or a new one.

        A file already open is brought forward rather than opened twice --
        two tabs editing one file would each overwrite the other's save.
        """
        if path is not None:
            path = Path(path)
            existing = self.find_document(path)
            if existing is not None:
                self.activate(existing)
                return existing

        spec = BY_KEY[kind]
        label = (self.display_name(kind, path) if path is not None
                 else f"New {spec.title.lower()}")
        return self._open(kind, label, spec.icon,
                          lambda doc: spec.create(doc, path, payload), path)

    def _open(self, kind: str, label: str, icon: str, build,
              path: Path | None = None) -> DocumentHost | None:
        """Build a document with `build(host)` and give it a tab."""
        doc = DocumentHost(self, kind, label)
        if path is not None:
            doc.path = path
        doc.frame.grid(row=0, column=0, sticky="nsew")
        doc.frame.tkraise()
        self.set_status(f"Opening {label}…")
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            doc.app = build(doc)
        except Exception as exc:
            self._log_error(exc)
            for _label, menu in doc.menus:
                menu.destroy()
            doc.frame.destroy()
            self.set_status("Ready")
            messagebox.showerror(
                APP_TITLE, f"Could not open {label}.\n\n{type(exc).__name__}: "
                           f"{exc}\n\nDetails were written to {ERROR_LOG.name}.",
                parent=self.root)
            return None
        finally:
            self.root.configure(cursor="")

        self.documents.append(doc)
        self._finish_menus(doc)
        self.tabs.add(doc, doc.tab_label, icon)
        self.activate(doc)
        self.set_status("Ready")
        if doc.path is not None:
            self._remember(doc)
        self._explorer_soon()
        return doc

    def find_document(self, path: Path) -> DocumentHost | None:
        key = path_key(path)
        return next((d for d in self.documents
                     if d.path is not None and path_key(d.path) == key), None)

    def open_path(self, path: Path) -> DocumentHost | None:
        """Open whatever `path` is: a template, an experiment or a result set."""
        kind = kind_of(Path(path))
        if kind is None:
            messagebox.showerror(
                APP_TITLE, f"{path}\n\nis not a plate template, an experiment "
                           "or a time-course result folder.", parent=self.root)
            return None
        return self.open_document(kind, Path(path))

    def ask_open(self, kind: str) -> None:
        path = BY_KEY[kind].ask_open(self.root)
        if path is not None:
            self.open_document(kind, path)

    def activate(self, doc: DocumentHost | None) -> None:
        if doc is not None and doc not in self.documents:
            return                      # still being built, or already closed
        self.active = doc
        for other in self.documents:
            if other is not doc:
                other.frame.grid_remove()
        if doc is None:
            self.home.refresh()
            self.home.grid(row=0, column=0, sticky="nsew")
        else:
            self.home.grid_remove()
            doc.frame.grid(row=0, column=0, sticky="nsew")
        self.tabs.select(doc if doc is not None else HOME)
        self._install_menus(doc)
        self._sync_window_menu()
        self._sync_title()
        self._sync_commands()
        try:
            (doc.frame if doc is not None else self.home).focus_set()
        except tk.TclError:                         # pragma: no cover
            pass

    def step_tab(self, delta: int) -> str:
        order: list = [None, *self.documents]
        i = order.index(self.active) if self.active in order else 0
        self.activate(order[(i + delta) % len(order)])
        return "break"

    def close_document(self, doc: DocumentHost) -> bool:
        if doc.closed:
            return True
        if doc not in self.documents:
            return False
        # Show it first: a question about unsaved changes should be asked
        # about the document you can see.
        self.activate(doc)
        for veto in doc.vetoes:
            if not veto():
                return False
        self._dispose(doc)
        i = self.documents.index(doc)
        self.documents.remove(doc)
        self.tabs.remove(doc)
        following = (self.documents[min(i, len(self.documents) - 1)]
                     if self.documents else None)
        self.activate(following)
        # Only now, with another document's menus installed: Tk does not like a
        # menubar pointing at a menu that has gone.
        for _label, menu in doc.menus:
            menu.destroy()
        doc.frame.destroy()
        if doc.kind == "review" and not any(d.kind == "review"
                                            for d in self.documents):
            self._release_review_memory()
        self._save_session()
        self._explorer_soon()
        return True

    def close_all(self) -> bool:
        for doc in list(self.documents):
            if not self.close_document(doc):
                return False
        return True

    def _close_active(self) -> str:
        if self.active is not None:
            self.close_document(self.active)
        return "break"

    def _dispose(self, doc: DocumentHost) -> None:
        doc.closed = True
        for dispose in doc.disposers:
            try:
                dispose()
            except Exception as exc:                # pragma: no cover - defensive
                self._log_error(exc)

    @staticmethod
    def _release_review_memory() -> None:
        """The review engine caches every photo it measured, which is right
        while reviewing and wrong all afternoon after. Let it go with the
        last review tab: its worker processes end, and their memory with
        them."""
        if "results_review.background" not in sys.modules:
            return
        try:
            sys.modules["results_review.background"].release()
        except Exception:                           # pragma: no cover
            pass

    # -- what documents tell the shell ---------------------------------------------

    def document_changed(self, doc: DocumentHost) -> None:
        if doc not in self.documents:
            return
        self.tabs.update_tab(doc, label=doc.tab_label, dirty=doc.dirty)
        if doc is self.active:
            self._sync_title()
            self._sync_commands()
        self._sync_window_menu()

    def document_moved(self, doc: DocumentHost) -> None:
        """Saved somewhere new, or swapped to another file in place."""
        self.document_changed(doc)
        if doc in self.documents and doc.path is not None:
            self._remember(doc)
            self._explorer_soon()

    def _sync_title(self) -> None:
        doc = self.active
        if doc is None:
            self.root.title(APP_TITLE)
        else:
            mark = " •" if doc.dirty else ""
            self.root.title(f"{doc.tab_label}{mark} — {APP_TITLE}")

    def _sync_commands(self) -> None:
        commands = self.active.commands if self.active is not None else {}
        self.file_menu.entryconfigure("Save", state="normal" if "save" in commands else "disabled")
        for name in ("Undo", "Redo"):
            self.edit_menu.entryconfigure(name, state="normal" if name.lower() in commands else "disabled")

    def run_command(self, name: str) -> None:
        if self.active is not None and name in self.active.commands:
            self.active.commands[name]()

    # -- tabs ----------------------------------------------------------------------

    def _tab_selected(self, key) -> None:
        self.activate(None if key is HOME else key)

    def _tab_closed(self, key) -> None:
        if key is not HOME:
            self.close_document(key)

    def _tabs_reordered(self) -> None:
        self.documents[:] = [key for key in self.tabs.keys() if key is not HOME]
        self._sync_window_menu()
        self._save_session()

    def _tab_menu(self, key, x: int, y: int) -> None:
        if key is HOME:
            return
        menu = tk.Menu(self.root, tearoff=False)
        index = self.tabs.keys().index(key)
        menu.add_command(label="Move left", command=lambda: self.tabs.move(key, index - 1),
                         state="normal" if index > 1 else "disabled")
        menu.add_command(label="Move right", command=lambda: self.tabs.move(key, index + 1),
                         state="normal" if index < len(self.documents) else "disabled")
        menu.add_separator()
        menu.add_command(label="Close", command=lambda: self.close_document(key))
        menu.add_command(label="Close the others",
                         command=lambda: self._close_others(key))
        if key.path is not None:
            menu.add_separator()
            menu.add_command(label="Show in folder",
                             command=lambda: reveal(key.path))
            menu.add_command(label="Copy path",
                             command=lambda: self._copy(str(key.path)))
        menu.tk_popup(x, y)

    def _tab_list(self, x: int, y: int) -> None:
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(label="Home", command=lambda: self.activate(None))
        for doc in self.documents:
            menu.add_command(label=doc.tab_label,
                             command=lambda d=doc: self.activate(d))
        menu.tk_popup(x, y)

    def _close_others(self, keep: DocumentHost) -> None:
        for doc in list(self.documents):
            if doc is not keep and not self.close_document(doc):
                return
        self.activate(keep)

    def _copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # -- the Explorer ----------------------------------------------------------------

    def _toggle_explorer(self) -> None:
        show = bool(self._show_explorer.get())
        panes = [str(p) for p in self.paned.panes()]
        if show and str(self.sidebar) not in panes:
            self.paned.insert(0, self.sidebar, weight=0)
            self.root.after_idle(lambda: self._set_sidebar(self._sidebar_width))
        elif not show and str(self.sidebar) in panes:
            self._sidebar_width = self.paned.sashpos(0)
            self.paned.forget(self.sidebar)
        self.settings.set("explorer", show)

    def _explorer_soon(self) -> None:
        if self._explorer_job is not None:
            return

        def go() -> None:
            self._explorer_job = None
            try:
                self.explorer.refresh()
            except tk.TclError:                     # pragma: no cover - closing
                pass

        self._explorer_job = self.root.after(250, go)

    def _window_activated(self, event) -> None:
        # Files may have changed while the window was in the background.
        if event.widget is self.root:
            self._explorer_soon()

    # -- names, recents ----------------------------------------------------------------

    @staticmethod
    def display_name(kind: str, path: Path) -> str:
        path = Path(path)
        if kind == "experiment":
            name = path.name
            return name[:-len(".spotexp.json")] if name.lower().endswith(
                ".spotexp.json") else path.stem
        if kind == "data-review":
            name = path.name
            return name[:-len(".datareview.json")] if name.lower().endswith(
                ".datareview.json") else path.stem
        if kind == "review":
            try:
                from results_review.discovery import TIMECOURSE_RESULTS
                return str(path.resolve().relative_to(TIMECOURSE_RESULTS.resolve()))
            except (ValueError, OSError):
                return path.name
        return path.stem

    def open_paths(self) -> set[str]:
        return {path_key(d.path) for d in self.documents if d.path is not None}

    def recent_files(self, kind: str, limit: int) -> list[tuple[Path, datetime | None]]:
        """What was opened recently, topped up with what changed recently."""
        rows = self.settings.recent(kind)[:limit]
        seen = {path_key(p) for p, _ in rows}
        if len(rows) < limit:
            try:
                files = BY_KEY[kind].files()
            except Exception:
                files = []
            dated = []
            for p in files:
                if path_key(p) in seen:
                    continue
                dated.append((p, _modified(p)))
            dated.sort(key=lambda r: r[1] or datetime.min, reverse=True)
            rows += dated[:limit - len(rows)]
        return rows

    def last_review_set(self) -> Path | None:
        try:
            from results_review import links
            last = links.last_set()
        except Exception:
            return None
        return last if last is not None and Path(last).is_dir() else None

    def _remember(self, doc: DocumentHost) -> None:
        self.settings.add_recent(doc.kind, doc.path)
        self._save_session()

    # -- actions offered on Home ----------------------------------------------------------

    def bulk_create_experiments(self) -> None:
        from experiments.app import default_experiment_dir
        from experiments.gui.bulk import BulkCreateDialog

        out_dir = default_experiment_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        dialog = BulkCreateDialog(self.root, out_dir)
        if dialog.created:
            self.set_status(f"Created {len(dialog.created)} experiment(s) in "
                            f"{out_dir.name}")
            self.open_document("experiment", dialog.created[0])
            self._explorer_soon()

    def import_configuration(self) -> None:
        got = import_configuration(self.root)
        if got is None:
            return
        experiments, notes, path = got
        self.open_document("experiment", None, experiments[0])
        extra = (f"\n\n{len(experiments) - 1} other experiment(s) are in that file; "
                 f"use the command line to convert them all:\n"
                 f"    py -m experiments.cli migrate \"{path}\""
                 if len(experiments) > 1 else "")
        messagebox.showinfo(
            "Import", f"Imported {experiments[0].name}."
            + ("\n\n" + "\n".join(notes) if notes else "") + extra,
            parent=self.root)

    def reexport_reviews(self) -> None:
        if not messagebox.askokcancel(
                "Re-export reviewed sets",
                "Write chosen/ again for every set that has a saved review, "
                "from its review.json?\n\nIt runs in the background and can "
                "take a while; follow it in the Jobs panel.", parent=self.root):
            return
        job = self.run_job(JobSpec(
            "Re-export reviewed sets", program_command("review", "--apply"),
            cwd=str(ROOT), kind="review-apply"), owner=None)
        if job is not None:
            self.open_job(job)

    def run_selftest(self) -> None:
        job = self.run_job(JobSpec(
            "Check the installation", program_command("--selftest"),
            cwd=str(ROOT), kind="selftest"), owner=None)
        if job is not None:
            self.open_job(job)

    def open_project_folder(self) -> None:
        reveal(ROOT)

    def open_readme(self) -> None:
        readme = ROOT / "README.md"
        try:
            os.startfile(str(readme))                # type: ignore[attr-defined]
        except (OSError, AttributeError):
            reveal(readme)

    def about(self) -> None:
        messagebox.showinfo(
            f"About {APP_TITLE}",
            f"{APP_TITLE}\n\nPlate templates, experiments and their review, in "
            f"one window.\n\nProject folder:\n{ROOT}\n\n"
            f"Python {sys.version.split()[0]} ({sys.executable})",
            parent=self.root)

    # -- jobs and messages ---------------------------------------------------------------

    def run_job(self, spec: JobSpec, owner: DocumentHost | None = None) -> Job | None:
        """Start a run with its output streaming into the Jobs panel."""
        try:
            job = self.runner.start(spec, owner)
        except OSError as exc:
            if owner is not None:
                raise                       # the document reports it its own way
            messagebox.showerror(APP_TITLE, f"Could not start {spec.title}:\n\n"
                                            f"{exc}", parent=self.root)
            return None
        self.set_status(f"Started {spec.title}" if job.state == Job.RUNNING
                        else f"{spec.title} will start when the run before "
                             "it finishes")
        return job

    def open_job(self, job: Job) -> DocumentHost | None:
        """A run's log, in a tab of its own."""
        for doc in self.documents:
            if doc.kind == "job" and getattr(doc.app, "job", None) is job:
                self.activate(doc)
                return doc
        return self._open("job", job.title, "list",
                          lambda doc: JobLog(doc, job, self))

    def open_results(self, job: Job) -> None:
        """Where a finished run put its results: in Review if it can open
        them, otherwise the folder itself."""
        folder = job.result_dir
        if folder is None or not Path(folder).exists():
            messagebox.showinfo(
                APP_TITLE, "This run did not say where it wrote its results. "
                           "They are under Results/ in the project folder.",
                parent=self.root)
            return
        sets = self._result_sets(job)
        if len(sets) == 1:
            self.open_document("review", sets[0])
        elif sets:
            menu = tk.Menu(self.root, tearoff=False)
            for folder_ in sets:
                menu.add_command(
                    label=self.display_name("review", folder_),
                    command=lambda f=folder_: self.open_document("review", f))
            x, y = self.root.winfo_pointerxy()
            menu.tk_popup(x, y)
        else:
            reveal(Path(folder))

    @staticmethod
    def _result_sets(job: Job) -> list[Path]:
        if job.result_dir is None or not Path(job.result_dir).is_dir():
            return []
        try:
            from results_review import discovery
            return discovery.list_sets(Path(job.result_dir))
        except Exception:
            return []

    def _job_actions(self, job: Job) -> list:
        actions = []
        if job.state == Job.DONE and job.result_dir is not None:
            label = ("Open in Review" if self._result_sets(job)
                     else "Open the results folder")
            actions.append((label, lambda: self.open_results(job)))
        actions.append(("Show the log", lambda: self.open_job(job)))
        return actions

    def _job_changed(self, job: Job, kinds: set) -> None:
        """Called by the runner, on this thread, whenever a job moves on."""
        if not self.jobs_panel.winfo_manager() and self.runner.jobs:
            self.jobs_panel.pack(side="bottom", fill="x", before=self.explorer)
        self.jobs_panel.refresh(job if kinds != {"state"} else None)
        for doc in self.documents:
            if doc.kind == "job" and getattr(doc.app, "job", None) is job:
                doc.app.update_log()
        self._sync_status_job()
        if "state" in kinds and job.finished and job.owner is None:
            # Nobody else will say so: a run the program itself started.
            kind = {Job.DONE: "ok", Job.FAILED: "error"}.get(job.state, "info")
            verb = {Job.DONE: "finished", Job.FAILED: "did not finish",
                    Job.STOPPED: "was stopped"}[job.state]
            self.toasts.show(f"{job.title} {verb}", job.describe(), kind=kind,
                             actions=self._job_actions(job))
            self._explorer_soon()

    def _pump_jobs(self) -> None:
        try:
            self.runner.pump()
        finally:
            if not self._closed:
                self._pump_id = self.root.after(100, self._pump_jobs)

    def _tick(self) -> None:
        try:
            if self.runner.running():
                self.jobs_panel.tick()
                for doc in self.documents:
                    if doc.kind == "job":
                        doc.app.tick()
                self._sync_status_job()
        finally:
            if not self._closed:
                self._tick_id = self.root.after(1000, self._tick)

    def _sync_status_job(self) -> None:
        running = [j for j in self.runner.jobs if j.state == Job.RUNNING]
        if running:
            job = running[0]
            more = f"  (+{len(running) - 1} more)" if len(running) > 1 else ""
            self.status_job.configure(text=f"▶  {job.title}  ·  "
                                           f"{job.describe()}{more}")
        else:
            self.status_job.configure(text="")

    def _open_running(self) -> None:
        running = [j for j in self.runner.jobs if j.state == Job.RUNNING]
        if running:
            self.open_job(running[0])

    def _latest_finished(self, owner: DocumentHost) -> Job | None:
        """The run `owner` started that has just ended, if it is the one a
        notice from it is about."""
        for job in reversed(self.runner.jobs):
            if job.owner is owner and job.finished:
                return job if job.ended and job.ended > _now() - 60 else None
        return None

    def notify(self, title: str, message: str, *, kind: str = "info",
               actions=(), owner: DocumentHost | None = None) -> None:
        """A non-blocking notice. When it is about a run that document just
        finished, the run's own next steps -- Open in Review, the log -- come
        with it."""
        actions = list(actions)
        job = self._latest_finished(owner) if owner is not None else None
        if job is not None:
            actions += self._job_actions(job)
            if kind == "info" and job.state == Job.DONE:
                kind = "ok"
            self._explorer_soon()
        self.toasts.show(title, message, kind=kind, actions=actions)

    def set_status(self, text: str) -> None:
        self.status_text.configure(text=text)

    # -- errors ----------------------------------------------------------------------------

    def _log_error(self, error: BaseException) -> None:
        text = "".join(traceback.format_exception(type(error), error,
                                                  error.__traceback__))
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}")
        except OSError:
            pass
        if sys.stderr is not None:
            try:
                print(text, file=sys.stderr)
            except (OSError, ValueError):            # pragma: no cover
                pass

    def _callback_error(self, exc_type, value, tb) -> None:
        """A Tk callback raised. Never silent, never fatal."""
        if value is not None and value.__traceback__ is None:
            value = value.with_traceback(tb)
        doc = self.active
        if doc is not None and doc.error_handler is not None:
            try:
                doc.error_handler(value)
                return
            except Exception:                        # pragma: no cover
                pass
        self._log_error(value)
        if self._reporting:
            return
        self._reporting = True
        try:
            messagebox.showerror(
                APP_TITLE, f"Something went wrong:\n\n{exc_type.__name__}: "
                           f"{value}\n\nDetails were written to "
                           f"{ERROR_LOG.name}.", parent=self.root)
        finally:
            self._reporting = False

    # -- the window and the session ----------------------------------------------------------

    def _restore_window(self) -> None:
        root = self.root
        root.update_idletasks()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        geometry = self.settings.get("geometry", "")
        if not (isinstance(geometry, str) and _fits(geometry, sw, sh)):
            w, h = int(sw * 0.8), int(sh * 0.82)
            geometry = f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 3}"
        root.geometry(geometry)
        root.minsize(px(root, 900), px(root, 600))
        if self.settings.get("zoomed", False):
            try:
                root.state("zoomed")
            except tk.TclError:                      # pragma: no cover
                pass
        self._sidebar_width = self.settings.get("sidebar", px(root, 210))
        # Placed on the first real resize: a sash set while the panes are still
        # 1px wide is clamped to 0, and the Explorer starts out invisible.
        self._sidebar_bind = self.paned.bind("<Configure>", self._first_layout,
                                             add="+")
        if not self._show_explorer.get():
            self.paned.forget(self.sidebar)

    def _first_layout(self, event) -> None:
        if event.width < px(self.root, 400):
            return
        self.paned.unbind("<Configure>", self._sidebar_bind)
        self._set_sidebar(self._sidebar_width)

    def _set_sidebar(self, width) -> None:
        try:
            if str(self.sidebar) in [str(p) for p in self.paned.panes()]:
                width = max(px(self.root, 160), int(width))
                self.paned.sashpos(0, width)
        except (tk.TclError, ValueError, TypeError):
            pass

    def _remember_window(self) -> None:
        root = self.root
        try:
            zoomed = root.state() == "zoomed"
            self.settings.set("zoomed", zoomed)
            if not zoomed:
                self.settings.set("geometry", root.wm_geometry())
            if str(self.sidebar) in [str(p) for p in self.paned.panes()]:
                width = self.paned.sashpos(0)
                if width >= px(root, 160):
                    self.settings.set("sidebar", width)
        except tk.TclError:                          # pragma: no cover
            pass

    def _save_session(self) -> None:
        self.settings.set_session([(d.kind, d.path) for d in self.documents
                                   if d.path is not None])
        self.settings.set("active", path_key(self.active.path)
                          if self.active is not None and self.active.path else "")
        self.settings.save()

    def _restore_session(self) -> None:
        """Reopen what was open, one document at a time so the window stays
        responsive while a result set rebuilds."""
        pending = [(k, p) for k, p in self.settings.session()
                   if k in BY_KEY and p.exists()]
        active = self.settings.get("active", "")
        show_home = bool(self.settings.get("show_home", True))

        def next_one() -> None:
            if not pending:
                if not show_home and active:
                    doc = next((d for d in self.documents if d.path is not None
                                and path_key(d.path) == active), None)
                    if doc is not None:
                        self.activate(doc)
                        return
                self.activate(None)
                return
            kind, path = pending.pop(0)
            self.open_document(kind, path)
            self.root.after(50, next_one)

        next_one()

    def exit(self, *, force: bool = False) -> bool:
        """Close the program. Running jobs and every document are asked
        about first, unless forced."""
        running = self.runner.running()
        if running and not force:
            names = "\n".join(f"  • {j.title}" for j in running[:5])
            if not messagebox.askokcancel(
                    APP_TITLE,
                    f"{len(running)} run(s) still going:\n\n{names}\n\n"
                    "Stop them and exit? What they have finished stays; the "
                    "rest is abandoned.", icon="warning", parent=self.root):
                return False
        if not force:
            for doc in list(self.documents):
                if doc.dirty or doc.busy:
                    self.activate(doc)
                for veto in doc.vetoes:
                    if not veto():
                        return False
        if not force:
            self._remember_window()
            self._save_session()
        self._closed = True
        for job in running:
            job.stop()
        for doc in list(self.documents):
            self._dispose(doc)
        # Anything still computing for a tab is abandoned with it, rather than
        # the program lingering, windowless, until it finishes.
        tasks.shutdown()
        for timer in (self._pump_id, self._tick_id, self._explorer_job):
            if timer is not None:
                try:
                    self.root.after_cancel(timer)
                except tk.TclError:                  # pragma: no cover
                    pass
        try:
            self.root.destroy()
        except tk.TclError:                          # pragma: no cover
            pass
        return True


def _now() -> float:
    return time.time()


def _fits(geometry: str, sw: int, sh: int) -> bool:
    """A saved WxH+X+Y that is still sensible on this screen."""
    try:
        size, x, y = geometry.replace("-", "+-").split("+")[:3]
        w, h = (int(v) for v in size.split("x"))
        x, y = int(x), int(y)
    except ValueError:
        return False
    return (200 <= w <= sw * 1.05 and 200 <= h <= sh * 1.05
            and -50 <= x < sw - 100 and -50 <= y < sh - 100)


def _modified(path: Path) -> datetime | None:
    try:
        if path.is_dir():
            stamps = [p.stat().st_mtime for p in
                      (path / "review.json", path / "timecourse_candidates.csv")
                      if p.exists()]
            stamp = max(stamps) if stamps else path.stat().st_mtime
        else:
            stamp = path.stat().st_mtime
        return datetime.fromtimestamp(stamp)
    except OSError:
        return None
