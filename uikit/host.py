"""The window contract: a tool builds itself into a HOST, never a window.

Each tool -- plate designer, experiment designer, review window -- used to own
its Tk window outright: it set the title, built the menubar, bound keys across
the whole process, and destroyed the window when it was done. That is fine
for a program that is alone. It is not fine for a tool living as one tab among
several in a bigger window, where every one of those would trample the
neighbours.

So a tool asks its host instead:

    host.frame                 an empty container to build into
    host.window                the toplevel, for dialog parents and focus
    host.set_title(t, tab=)    the window title / the tab label
    host.set_dirty(bool)       unsaved changes, for the tab marker
    host.set_path(path)        the file it is showing, for recents and dedupe
    host.set_busy(bool)        a long job is running
    host.suggest_size(...)     a size; a tab host ignores it
    host.present()             bring it forward
    host.add_menu("Edit")      a menu, attached wherever menus go here
    host.bind_key(seq, fn)     a shortcut, live only while this tool is
    host.add_command(name, fn) "save", "undo", ... for the program's toolbar
    host.on_close(veto)        asked before closing; return False to stay
    host.on_dispose(fn)        cleanup, run once on the way out
    host.close()               close this tool
    host.run_job(spec)         start a long-running subprocess
    host.open_document(...)    ask for ANOTHER document; False = do it yourself
    host.notify(...)           tell the person something finished
    host.set_error_handler(fn) where a failing callback is reported

`StandaloneHost` is the host for a tool running on its own, and reproduces
exactly what each tool did before this contract existed. The workbench
supplies a different host. Tools accept either a host or a bare window
(`as_host`), so `ExperimentApp(tk.Toplevel(...), e)` still works.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Protocol

from .theme import apply_theme


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@dataclass
class JobSpec:
    """A long-running subprocess a tool wants started.

    `env` is the child's whole environment (None: inherit this process's).
    The `console_*`, `new_console` and `status_env` fields only apply when the
    job is shown in a console window of its own -- the standalone way. A host
    that streams the output into its own window ignores them.
    """

    title: str
    argv: list
    cwd: str | None = None
    env: dict | None = None
    #: What sort of job this is ("experiment-run"), for hosts that queue them.
    kind: str = ""
    #: Extra environment for the console presentation only.
    console_env: dict = field(default_factory=dict)
    #: Open a new console window for it (Windows).
    new_console: bool = False
    #: Name of an environment variable through which the child is told a file
    #: to write its exit code into -- for a console held open after finishing.
    status_env: str | None = None
    meta: dict = field(default_factory=dict)


class JobHandle(Protocol):
    title: str

    def poll(self) -> int | None:
        """The exit code once finished, else None."""

    def stop(self) -> None:
        """Stop it."""


class ConsoleJob:
    """A job running in a console window of its own.

    The console may be held open (waiting for Enter) after the work is done,
    so the process exiting is not when the job finished. The child writes its
    exit code to a status file first, and that is read in preference; the
    process exiting is the fallback for a child that died before it could.
    """

    def __init__(self, title: str, process, status_path: Path | None) -> None:
        self.title = title
        self.process = process
        self.status_path = status_path
        self._code: int | None = None

    def _read_status(self) -> int | None:
        if self.status_path is None:
            return None
        try:
            return int(self.status_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None              # not written yet, or mid-write

    def discard(self) -> None:
        if self.status_path is not None:
            try:
                self.status_path.unlink()
            except OSError:
                pass
            self.status_path = None

    def poll(self) -> int | None:
        if self._code is not None:
            return self._code
        code = self._read_status()
        if code is None:
            code = self.process.poll()
        if code is not None:
            self._code = code
            self.discard()
        return code

    def stop(self) -> None:
        try:
            self.process.terminate()
        except (OSError, AttributeError):
            pass


def start_console_job(spec: JobSpec) -> ConsoleJob:
    """Start `spec` the standalone way: its own console, stdio detached."""
    env = dict(os.environ if spec.env is None else spec.env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.update(spec.console_env)
    status_path: Path | None = None
    if spec.status_env:
        fd, status = tempfile.mkstemp(prefix="experiment_run_", suffix=".txt")
        os.close(fd)
        status_path = Path(status)
        env[spec.status_env] = status

    kwargs = {
        "cwd": spec.cwd,
        "env": env,
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # Below normal priority, inherited by the workers it starts: a run
        # keeping every core busy must not make the window wait its turn.
        kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS | (
            subprocess.CREATE_NEW_CONSOLE if spec.new_console else 0)
    try:
        # Looked up on the module at call time, so it can be stubbed in tests.
        process = subprocess.Popen(spec.argv, **kwargs)
    except OSError:
        if status_path is not None:
            try:
                status_path.unlink()
            except OSError:
                pass
        raise
    return ConsoleJob(spec.title, process, status_path)


# ---------------------------------------------------------------------------
# Hosts
# ---------------------------------------------------------------------------

class BaseHost:
    """What a tool may ask of the place it lives. See the module docstring."""

    standalone = True
    #: The label for the tool's own last File item.
    close_label = "Exit"

    window: tk.Misc
    frame: ttk.Frame

    def set_title(self, title: str, tab: str | None = None) -> None:
        raise NotImplementedError

    def set_dirty(self, dirty: bool) -> None:
        pass

    def set_path(self, path: Path | None) -> None:
        pass

    def set_busy(self, busy: bool) -> None:
        pass

    def suggest_size(self, width: int, height: int,
                     min_width: int | None = None,
                     min_height: int | None = None, *,
                     center: bool = True) -> None:
        pass

    def present(self) -> None:
        pass

    def add_menu(self, label: str) -> tk.Menu:
        raise NotImplementedError

    def bind_key(self, sequence: str, handler: Callable) -> None:
        raise NotImplementedError

    def add_command(self, name: str, callback: Callable) -> None:
        """Offer an action the surrounding program can trigger from its own
        controls: "save", "undo", "redo", "run". Standalone, there are none."""

    def on_close(self, callback: Callable[[], bool]) -> None:
        raise NotImplementedError

    def on_dispose(self, callback: Callable[[], None]) -> None:
        raise NotImplementedError

    def close(self) -> bool:
        raise NotImplementedError

    def run_job(self, spec: JobSpec) -> JobHandle:
        raise NotImplementedError

    def open_document(self, kind: str, path: Path | None = None,
                      payload=None) -> bool:
        """Open another document. False means "not here -- do it yourself"."""
        return False

    def notify(self, title: str, message: str, *, kind: str = "info",
               actions=()) -> None:
        raise NotImplementedError

    def set_error_handler(self, handler: Callable[[BaseException], None]) -> None:
        pass


class StandaloneHost(BaseHost):
    """A tool alone in its own window -- the way every tool used to run.

    Applies the shared theme, gives the tool a frame filling the window, and
    puts the window's own close button through the tool's veto.
    """

    standalone = True
    close_label = "Exit"

    def __init__(self, window: tk.Misc, app_title: str = "") -> None:
        self.window = window
        self.app_title = app_title
        apply_theme(window)
        self.frame = ttk.Frame(window)
        self.frame.pack(fill="both", expand=True)
        self._menubar: tk.Menu | None = None
        self._vetoes: list[Callable[[], bool]] = []
        self._disposers: list[Callable[[], None]] = []
        self._closed = False
        window.protocol("WM_DELETE_WINDOW", self.close)

    # -- presentation --------------------------------------------------------

    def set_title(self, title: str, tab: str | None = None) -> None:
        self.window.title(title)

    def set_busy(self, busy: bool) -> None:
        try:
            self.window.configure(cursor="watch" if busy else "")
        except tk.TclError:                          # pragma: no cover - closing
            pass

    def suggest_size(self, width: int, height: int,
                     min_width: int | None = None,
                     min_height: int | None = None, *,
                     center: bool = True) -> None:
        win = self.window
        if center:
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            win.geometry(f"{width}x{height}+{max(0, (sw - width) // 2)}"
                         f"+{max(0, (sh - height) // 3)}")
        else:
            win.geometry(f"{width}x{height}")
        if min_width and min_height:
            win.minsize(min_width, min_height)

    def present(self) -> None:
        self.window.deiconify()
        self.window.lift()

    # -- menus and keys ------------------------------------------------------

    def add_menu(self, label: str) -> tk.Menu:
        if self._menubar is None:
            self._menubar = tk.Menu(self.window)
            self.window.configure(menu=self._menubar)
        menu = tk.Menu(self._menubar, tearoff=False)
        self._menubar.add_cascade(label=label, menu=menu)
        return menu

    def bind_key(self, sequence: str, handler: Callable) -> None:
        # The window, not bind_all: fires from any widget in this window, and
        # from none in a dialog or in another tool's window.
        self.window.bind(sequence, handler)

    # -- lifetime ------------------------------------------------------------

    def on_close(self, callback: Callable[[], bool]) -> None:
        self._vetoes.append(callback)

    def on_dispose(self, callback: Callable[[], None]) -> None:
        self._disposers.append(callback)

    def close(self) -> bool:
        if self._closed:
            return True
        for veto in self._vetoes:
            if not veto():
                return False
        self._closed = True
        for dispose in self._disposers:
            try:
                dispose()
            except Exception:                        # pragma: no cover - defensive
                pass
        try:
            self.window.destroy()
        except tk.TclError:                          # pragma: no cover - gone
            pass
        return True

    # -- jobs and messages ---------------------------------------------------

    def run_job(self, spec: JobSpec) -> ConsoleJob:
        return start_console_job(spec)

    def notify(self, title: str, message: str, *, kind: str = "info",
               actions=()) -> None:
        show = {"error": messagebox.showerror,
                "warning": messagebox.showwarning}.get(kind, messagebox.showinfo)
        show(title, message, parent=self.window)

    def set_error_handler(self, handler: Callable[[BaseException], None]) -> None:
        # Only a real root has the hook, and only a tool that owns the root
        # may take it: a Toplevel shares its interpreter with someone else.
        if isinstance(self.window, tk.Tk):
            self.window.report_callback_exception = (
                lambda _exc, value, _tb: handler(value))


def as_host(target, app_title: str = "") -> BaseHost:
    """A host for `target`: itself if it is one, else a standalone host."""
    if isinstance(target, BaseHost):
        return target
    if isinstance(target, (tk.Tk, tk.Toplevel)):
        return StandaloneHost(target, app_title)
    raise TypeError(f"expected a host or a Tk window, not {type(target).__name__}")
