"""Workbench entry point.

    py spotting_app.py                       the usual way (run_workbench.bat)
    py -m workbench [file or folder ...]     open these as well
    py -m workbench --selftest               build the window, open one of
                                             each document, close, exit 0
"""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from pathlib import Path

from uikit import dpi, tasks

APP_ID = "MartinLab.SpottingQuantification"


def _ensure_stdio() -> None:
    """Under pythonw there is no stdout or stderr; anything that writes to
    them (a stray print, a worker process) must not crash on it."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _taskbar_identity() -> None:
    """Group under this program's own taskbar icon, not python.exe's."""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workbench",
                                     description="Spotting Quantification")
    parser.add_argument("paths", nargs="*",
                        help="plate templates, experiments or result folders "
                             "to open")
    parser.add_argument("--no-restore", action="store_true",
                        help="do not reopen what was open last time")
    parser.add_argument("--selftest", action="store_true",
                        help="build the window, open a document of each kind, "
                             "and exit")
    args = parser.parse_args(argv)

    _ensure_stdio()
    # The review tool draws figures off the main thread. With a Tk loop running,
    # pyplot's default backend would be TkAgg, which must not be touched off
    # the main thread; the figures are only ever saved, so Agg is right.
    os.environ.setdefault("MPLBACKEND", "Agg")
    dpi.enable_dpi_awareness()
    tasks.favour_the_window()
    _taskbar_identity()

    from .settings import Settings
    from .shell import Shell

    root = tk.Tk()
    settings = Settings(read_only=args.selftest)
    shell = Shell(root, settings, restore=not (args.no_restore or args.selftest
                                               or args.paths))
    for path in args.paths:
        shell.open_path(Path(path))

    if args.selftest:
        root.update_idletasks()
        root.update()
        opened = [shell.open_document(kind) for kind in ("plate", "experiment")]
        root.update()
        ok = all(doc is not None for doc in opened)
        count = len(shell.documents)
        shell.exit(force=True)
        if not ok:
            print("selftest FAILED: a document did not open", file=sys.stderr)
            return 1
        print(f"selftest OK: {count} document(s) opened and closed")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
