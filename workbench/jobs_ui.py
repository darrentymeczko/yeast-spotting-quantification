"""What the person sees of a job: a row in the Jobs panel, and its log tab.

    JobsPanel   the bottom of the sidebar: one row per run, newest first, with
                its state, a thin progress bar and how far it has got. Click a
                row to open its log. Hidden while there has been no run.
    JobLog      a tab showing a run's output as it arrives -- the counter line
                rewritten in place, as a console would -- with Stop, and the
                way to its results once it has finished.
"""

from __future__ import annotations

import itertools
import os
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from uikit import icons
from uikit import tokens as t
from uikit.dpi import px
from uikit.theme import FONT_MONO, FONT_SMALL, FONT_STRONG
from uikit.widgets import IconButton, LinkLabel

from .jobs import Job

if TYPE_CHECKING:                                  # pragma: no cover
    from .shell import Shell

#: Rows shown in the panel; older runs stay reachable from the Window menu.
PANEL_ROWS = 6

#: Lines a log tab shows: the latest ones. The log file keeps everything.
VIEW_LINES = 5000
#: How far past VIEW_LINES the view may grow before it is trimmed back.
VIEW_SLACK = 500

_STATE_ICON = {Job.QUEUED: ("more", t.TEXT_MUTED),
               Job.RUNNING: ("play", t.ACCENT),
               Job.DONE: ("check", t.OK),
               Job.FAILED: ("error", t.ERROR),
               Job.STOPPED: ("stop", t.TEXT_MUTED)}


class _Row(tk.Frame):
    def __init__(self, master, job: Job, on_open: Callable[[Job], None]) -> None:
        super().__init__(master, background=t.SURFACE, cursor="hand2")
        self.job = job
        pad = px(self, 12)
        self.icon = tk.Label(self, background=t.SURFACE)
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n",
                       padx=(pad, px(self, 8)), pady=(px(self, 4), 0))
        self.title = tk.Label(self, text=job.title, background=t.SURFACE,
                              foreground=t.TEXT, anchor="w")
        self.title.grid(row=0, column=1, sticky="ew", padx=(0, pad))
        self.detail = tk.Label(self, background=t.SURFACE, font=FONT_SMALL,
                               foreground=t.TEXT_MUTED, anchor="w")
        self.detail.grid(row=1, column=1, sticky="ew", padx=(0, pad))
        self.bar = ttk.Progressbar(self, style="Thin.Horizontal.TProgressbar",
                                   maximum=1.0)
        self.bar.grid(row=2, column=1, sticky="ew", padx=(0, pad),
                      pady=(px(self, 2), px(self, 6)))
        self.columnconfigure(1, weight=1)
        for widget in (self, self.icon, self.title, self.detail):
            widget.bind("<ButtonRelease-1>", lambda _e: on_open(job))
            widget.bind("<Enter>", lambda _e: self._paint(t.SURFACE_ALT))
            widget.bind("<Leave>", lambda _e: self._paint(t.SURFACE))
        self.update_row()

    def _paint(self, bg: str) -> None:
        for widget in (self, self.icon, self.title, self.detail):
            widget.configure(background=bg)

    def update_row(self) -> None:
        job = self.job
        name, colour = _STATE_ICON[job.state]
        glyph, font = icons.get(self, name, 9)
        self.icon.configure(text=glyph, font=font, foreground=colour)
        self.detail.configure(text=job.describe())
        if job.state == Job.RUNNING:
            self.bar.grid()
            if job.progress is not None:
                done, total = job.progress
                if str(self.bar.cget("mode")) != "determinate":
                    self.bar.stop()
                    self.bar.configure(mode="determinate")
                self.bar.configure(value=done / total)
            elif str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(40)
        else:
            self.bar.stop()
            self.bar.grid_remove()


class JobsPanel(ttk.Frame):
    def __init__(self, master, shell: "Shell") -> None:
        super().__init__(master, style="Sidebar.TFrame")
        self.shell = shell
        self._rows: dict[int, _Row] = {}
        ttk.Frame(self, style="Line.TFrame", height=1).pack(fill="x")
        head = ttk.Frame(self, style="Sidebar.TFrame")
        head.pack(fill="x", padx=px(self, 12), pady=(px(self, 8), px(self, 4)))
        ttk.Label(head, text="JOBS", font=FONT_SMALL,
                  style="Sidebar.Muted.TLabel").pack(side="left")
        self.list = ttk.Frame(self, style="Sidebar.TFrame")
        self.list.pack(fill="x", pady=(0, px(self, 6)))

    def refresh(self, job: Job | None = None) -> None:
        """Bring the rows up to date: one row, or all of them."""
        jobs = list(reversed(self.shell.runner.jobs))[:PANEL_ROWS]
        wanted = {id(j) for j in jobs}
        if job is not None and id(job) in self._rows and set(self._rows) == wanted:
            self._rows[id(job)].update_row()
            return
        for row in self._rows.values():
            row.destroy()
        self._rows = {}
        for j in jobs:
            row = _Row(self.list, j, self.shell.open_job)
            row.pack(fill="x")
            self._rows[id(j)] = row

    def tick(self) -> None:
        """Once a second: elapsed times move on while nothing is printed."""
        for row in self._rows.values():
            if row.job.state == Job.RUNNING:
                row.detail.configure(text=row.job.describe())


class JobLog(ttk.Frame):
    """A run's output, live, in a tab of its own."""

    def __init__(self, host, job: Job, shell: "Shell") -> None:
        super().__init__(host.frame)
        self.pack(fill="both", expand=True)
        self.host = host
        self.job = job
        self.shell = shell
        self._shown = 0               # job.committed when the widget last caught up

        head = ttk.Frame(self, padding=(px(self, 16), px(self, 12),
                                        px(self, 16), px(self, 8)))
        head.pack(fill="x")
        head.columnconfigure(1, weight=1)
        self.icon = tk.Label(head, background=t.BG)
        self.icon.grid(row=0, column=0, rowspan=2, sticky="n",
                       padx=(0, px(self, 10)))
        ttk.Label(head, text=job.title, font=FONT_STRONG).grid(
            row=0, column=1, sticky="w")
        self.detail = ttk.Label(head, style="Muted.TLabel")
        self.detail.grid(row=1, column=1, sticky="w")
        self.bar = ttk.Progressbar(head, maximum=1.0)
        self.bar.grid(row=2, column=0, columnspan=3, sticky="ew",
                      pady=(px(self, 8), 0))

        buttons = ttk.Frame(head)
        buttons.grid(row=0, column=2, rowspan=2, sticky="e")
        self.results_button = ttk.Button(buttons, text="Open the results",
                                         style="Accent.TButton",
                                         command=lambda: shell.open_results(job))
        self.stop_button = ttk.Button(buttons, text="Stop", command=self._stop)
        self.stop_button.pack(side="right")
        self.log_link = LinkLabel(buttons, "Log file", self._open_log)
        self.log_link.pack(side="right", padx=(0, px(self, 12)))

        area = ttk.Frame(self, padding=(px(self, 16), 0, px(self, 16),
                                        px(self, 16)))
        area.pack(fill="both", expand=True)
        self.text = tk.Text(area, wrap="none", font=FONT_MONO, state="disabled",
                            background=t.SURFACE, foreground=t.TEXT,
                            highlightthickness=1, highlightbackground=t.LINE,
                            padx=px(self, 10), pady=px(self, 8))
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        # The line being rewritten in place is always the last thing in the
        # widget, and is tagged so it can be found and replaced.
        self.text.tag_configure("live", foreground=t.ACCENT)

        host.set_title(f"{job.title} — log", tab=job.title)
        self.update_log(full=True)

    def _stop(self) -> None:
        from tkinter import messagebox

        if messagebox.askokcancel(
                "Stop", f"Stop {self.job.title}?\n\nWhatever it has finished "
                        "stays; the rest is abandoned.", parent=self):
            self.job.stop()

    def _open_log(self) -> None:
        if self.job.log_path is not None and self.job.log_path.exists():
            try:
                os.startfile(str(self.job.log_path))   # type: ignore[attr-defined]
            except (OSError, AttributeError):
                pass

    def update_log(self, full: bool = False) -> None:
        job = self.job
        name, colour = _STATE_ICON[job.state]
        glyph, font = icons.get(self, name, 14)
        self.icon.configure(text=glyph, font=font, foreground=colour)
        self.detail.configure(text=job.describe()
                              + (f"  ·  {job.phase}" if job.active and job.phase else ""))
        if job.progress is not None and job.state == Job.RUNNING:
            done, total = job.progress
            if str(self.bar.cget("mode")) != "determinate":
                self.bar.stop()
                self.bar.configure(mode="determinate")
            self.bar.configure(value=done / total)
        elif job.state == Job.RUNNING:
            if str(self.bar.cget("mode")) != "indeterminate":
                self.bar.configure(mode="indeterminate")
                self.bar.start(40)
        else:
            self.bar.stop()
            self.bar.configure(mode="determinate",
                               value=1.0 if job.state == Job.DONE else 0.0)
        if job.finished:
            self.stop_button.pack_forget()
            if (job.state == Job.DONE and job.result_dir is not None
                    and not self.results_button.winfo_manager()):
                self.results_button.pack(side="right")
        self._append_output(full)

    def _append_output(self, full: bool) -> None:
        """Add only what is new. This runs up to ten times a second while a
        run prints, so it must never copy, or redraw, the whole output."""
        text = self.text
        job = self.job
        lines = job.lines
        at_bottom = text.yview()[1] >= 0.999
        text.configure(state="normal")
        fresh = job.committed - self._shown
        if full or fresh < 0 or fresh > min(len(lines), VIEW_LINES):
            # From scratch, or so much is new that the rest would be trimmed.
            text.delete("1.0", "end")
            fresh = min(len(lines), VIEW_LINES)
        new = list(itertools.islice(lines, len(lines) - fresh, None)) if fresh else []
        # Take the old live line off, add what was committed since, then the
        # new live line -- the same text a console would be showing.
        old = text.tag_ranges("live")
        if old:
            text.delete(old[0], old[1])
        if new:
            text.insert("end-1c", "\n".join(new) + "\n")
        if job.live:
            text.insert("end-1c", job.live, ("live",))
        self._shown = job.committed
        # Keep the view to the latest lines; the log file has all of them.
        # Trimmed in steps, so not on every update once it is full.
        shown = int(text.index("end-1c").split(".")[0])
        if shown > VIEW_LINES + VIEW_SLACK:
            text.delete("1.0", f"{shown - VIEW_LINES + 1}.0")
        text.configure(state="disabled")
        if at_bottom or full:
            text.see("end")

    def tick(self) -> None:
        if self.job.state == Job.RUNNING:
            self.detail.configure(text=self.job.describe()
                                  + (f"  ·  {self.job.phase}" if self.job.phase else ""))
