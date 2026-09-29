"""Shared widgets: the findings list, the status line, and the header.

The findings panel is the same idea as `plate_template.gui.panels.ValidationPanel`
-- everything wrong or suspicious in one place, worst first, never blocking a
save. A half-filled experiment is expected to carry warnings while it is being
typed in; only running is gated on errors.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from ..validate import Severity

#: Colours per severity, kept muted so the list reads as information rather
#: than alarm. Errors are the only thing that stops a run.
_COLOURS = {
    Severity.ERROR: "#b3261e",
    Severity.WARNING: "#8a6100",
    Severity.INFO: "#4a4a4a",
}

_PREFIX = {
    Severity.ERROR: "Error",
    Severity.WARNING: "Check",
    Severity.INFO: "Note",
}


class FindingsPanel(ttk.Frame):
    """Everything wrong or worth a look, worst first."""

    def __init__(self, master, on_select: Callable[[object], None] | None = None):
        super().__init__(master, padding=(6, 4))
        self.on_select = on_select
        self._issues: list = []
        self._line_issues: list[int] = []
        self._rendered_width = 0
        self._collapsed = False
        self._heading_text = "Findings"

        header = ttk.Frame(self)
        header.pack(fill="x")
        self.heading = ttk.Button(header, text="▾ Findings", command=self.toggle)
        self.heading.pack(fill="x")

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, pady=(2, 0))
        self.list = tk.Listbox(self.body, height=5, activestyle="none",
                               borderwidth=1, relief="solid", highlightthickness=0)
        scroll = ttk.Scrollbar(self.body, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=scroll.set)
        self.list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.list.bind("<<ListboxSelect>>", self._selected)
        self.list.bind("<Configure>", self._resized)

    def show(self, issues) -> None:
        self._issues = list(issues)
        self._render()

        errors = sum(1 for i in self._issues if i.is_error)
        warnings = sum(1 for i in self._issues
                       if i.severity is Severity.WARNING)
        bits = []
        if errors:
            bits.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            bits.append(f"{warnings} to check")
        self._heading_text = (
            "Findings" + (f" — {', '.join(bits)}" if bits else " — all clear")
        )
        self._update_heading()

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
        else:
            self.body.pack(fill="both", expand=True, pady=(2, 0))
        self._update_heading()

    def _update_heading(self) -> None:
        marker = "▸" if self._collapsed else "▾"
        self.heading.configure(text=f"{marker} {self._heading_text}")

    def _render(self, width: int | None = None) -> None:
        selected_issue = None
        selected = self.list.curselection()
        if selected and selected[0] < len(self._line_issues):
            selected_issue = self._line_issues[selected[0]]

        self.list.delete(0, "end")
        self._line_issues.clear()
        width = width or self.list.winfo_width()
        if width <= 1:
            width = max(320, self.winfo_toplevel().winfo_width() - 40)
        available = max(120, width - 18)
        font = tkfont.nametofont(self.list.cget("font"))

        for issue_index, issue in enumerate(self._issues):
            where = f"[{issue.condition}] " if getattr(issue, "condition", "") else ""
            text = f"{_PREFIX[issue.severity]}: {where}{issue.message}"
            for line in _wrap_pixels(text, font, available):
                row = self.list.size()
                self.list.insert("end", line)
                self.list.itemconfigure(row, foreground=_COLOURS[issue.severity])
                self._line_issues.append(issue_index)
                if selected_issue == issue_index:
                    self.list.selection_set(row)

    def _resized(self, event) -> None:
        if abs(event.width - self._rendered_width) < 8:
            return
        self._rendered_width = event.width
        self._render(event.width)

    def _selected(self, _event=None) -> None:
        if not self.on_select:
            return
        sel = self.list.curselection()
        if sel and sel[0] < len(self._line_issues):
            self.on_select(self._issues[self._line_issues[sel[0]]])


def _wrap_pixels(text: str, font: tkfont.Font, width: int) -> list[str]:
    """Wrap proportional-font text to an exact pixel width."""
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    line = words[0]
    for word in words[1:]:
        candidate = f"{line} {word}"
        if font.measure(candidate) <= width:
            line = candidate
        else:
            lines.extend(_split_wide_line(line, font, width))
            line = f"    {word}"
    lines.extend(_split_wide_line(line, font, width))
    return lines


def _split_wide_line(text: str, font: tkfont.Font, width: int) -> list[str]:
    """Split a single path-like word too wide to wrap at spaces."""
    if font.measure(text) <= width:
        return [text]
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + 1
        while end <= len(text) and font.measure(text[start:end]) <= width:
            end += 1
        cut = max(start + 1, end - 1)
        pieces.append(text[start:cut])
        start = cut
    return pieces


class StatusBar(ttk.Frame):
    """One line: what just happened, and what the file is."""

    def __init__(self, master):
        super().__init__(master, padding=(8, 3))
        self.message = ttk.Label(self, text="")
        self.message.pack(side="left")
        self.detail = ttk.Label(self, text="", foreground="#666")
        self.detail.pack(side="right")

    def say(self, text: str) -> None:
        self.message.configure(text=text)

    def set_detail(self, text: str) -> None:
        self.detail.configure(text=text)


class HeaderBar(ttk.Frame):
    """The experiment's name, its photo folder, and what it is bound to.

    The photo folder lives here rather than on a tab of its own. It is a
    property of the whole experiment, not a step in building one, and both
    modes need it: the time course reads the folder automatically, and the
    plate picker has nothing to show until one is chosen.
    """

    def __init__(self, master, on_rename: Callable[[], None] | None = None,
                 on_choose_photos: Callable[[], None] | None = None,
                 on_rescan: Callable[[], None] | None = None):
        super().__init__(master, padding=(8, 6))

        top = ttk.Frame(self)
        top.pack(fill="x")
        top.columnconfigure(0, weight=1)
        top.columnconfigure(2, weight=1)
        self.name = ttk.Label(top, text="Untitled", font=("", 12, "bold"))
        self.name.grid(row=0, column=0, sticky="w")
        if on_rename is not None:
            ttk.Button(top, text="Rename...", width=10,
                       command=on_rename).grid(row=0, column=1, padx=8)
        self.summary = ttk.Label(top, text="", foreground="#555", width=1,
                                 anchor="e")
        self.summary.grid(row=0, column=2, sticky="ew")

        row = ttk.Frame(self)
        row.pack(fill="x", pady=(6, 0))
        row.columnconfigure(1, weight=3)
        row.columnconfigure(2, weight=2)
        ttk.Label(row, text="Photos:").grid(row=0, column=0, sticky="w")
        self.folder = ttk.Label(row, text="none chosen", foreground="#555",
                                width=1, anchor="w")
        self.folder.grid(row=0, column=1, sticky="ew", padx=(6, 8))
        self.folder_note = ttk.Label(row, text="", foreground="#555", width=1,
                                     anchor="e")
        self.folder_note.grid(row=0, column=2, sticky="ew", padx=(0, 8))
        if on_rescan is not None:
            ttk.Button(row, text="Re-read", width=9,
                       command=on_rescan).grid(row=0, column=4, sticky="e")
        if on_choose_photos is not None:
            ttk.Button(row, text="Choose folder...", width=16,
                       command=on_choose_photos).grid(row=0, column=3, sticky="e",
                                                      padx=(0, 4))

    def show(self, name: str, summary: str, folder: str = "",
             folder_note: str = "") -> None:
        self.name.configure(text=name)
        self.summary.configure(text=summary)
        self.folder.configure(text=folder or "none chosen")
        self.folder_note.configure(text=folder_note)
