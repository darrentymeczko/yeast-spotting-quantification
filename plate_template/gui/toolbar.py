"""Top toolbar: symbol buttons with hover tooltips, plus a contextual bar
carrying only the settings the active tool actually reads.

Symbols are drawn from Segoe UI Symbol rather than emoji, so they stay
monochrome and render at a predictable weight next to the text.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from .. import theme

#: key, symbol, title, one-line instruction
TOOLS: list[tuple[str, str, str, str]] = [
    (
        "assign", "●", "Place samples",
        "Drag across cells to lay down a run of samples. The first cell gets "
        "the slot number shown, the next gets the one after, and so on.",
    ),
    (
        "series", "⇩", "Add dilutions",
        "Drag from a spot you have already placed, outwards, to where the last "
        "dilution should land. The direction and number of levels come from the "
        "drag; the whole run of matching spots beside it comes along.",
    ),
    (
        "control", "◎", "Set control",
        "Click a spot. Its sample slot becomes this plate's positive control. "
        "Every plate needs one.",
    ),
    (
        "empty", "▨", "Mark empty",
        "Drag a rectangle to mark cells as deliberately blank, so they are not "
        "reported as forgotten.",
    ),
    (
        "erase", "⌫", "Erase",
        "Drag a rectangle to clear cells back to undecided.",
    ),
]

TOOL_HINTS = {key: hint for key, _sym, _title, hint in TOOLS}
TOOL_TITLES = {key: title for key, _sym, title, _hint in TOOLS}


class Tooltip:
    """Hover label. tkinter ships nothing like this, so it is 40 lines here."""

    def __init__(self, widget: tk.Misc, text: str, delay: int = 400) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._job: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        if self._window is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        # A plain Toplevel can be drawn behind its own parent; a tooltip that
        # hides behind the window it belongs to is worse than none.
        window.wm_attributes("-topmost", True)
        label = tk.Label(
            window, text=self.text, justify="left", wraplength=320,
            background="#2f2f2f", foreground="#ffffff",
            padx=9, pady=5, borderwidth=0,
        )
        label.pack()
        window.update_idletasks()
        # Keep it on screen when the button sits near the right edge.
        x = min(x, window.winfo_screenwidth() - window.winfo_width() - 8)
        window.wm_geometry(f"+{max(8, x - window.winfo_width() // 2)}+{y}")
        self._window = window

    def hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


def _styles(widget: tk.Misc) -> None:
    style = ttk.Style()
    style.configure("Tool.Toolbutton", font=("Segoe UI Symbol", 13), padding=(9, 5))
    style.configure("ToolText.Toolbutton", padding=(9, 6))


class Toolbar(ttk.Frame):
    """History, tools, view and plate actions -- all as symbol buttons."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        tool: tk.StringVar,
        show_tokens: tk.BooleanVar,
        on_tool_change: Callable[[], None],
        on_undo: Callable[[], None],
        on_redo: Callable[[], None],
        on_tokens: Callable[[], None],
        on_grid_size: Callable[[], None],
        on_add_plate: Callable[[], None],
        on_duplicate_plate: Callable[[], None],
        on_delete_plate: Callable[[], None],
    ) -> None:
        super().__init__(master, padding=(6, 4))
        _styles(self)
        self.tool = tool

        self.undo_button = self._button("↶", "Undo  (Ctrl+Z)", on_undo)
        self.redo_button = self._button("↷", "Redo  (Ctrl+Y)", on_redo)
        self._separator()

        for key, symbol, title, hint in TOOLS:
            button = ttk.Radiobutton(
                self, text=symbol, value=key, variable=tool,
                style="Tool.Toolbutton", command=on_tool_change,
            )
            button.pack(side="left", padx=1)
            Tooltip(button, f"{title}\n{hint}")

        self._separator()
        tokens = ttk.Checkbutton(
            self, text="T", variable=show_tokens, command=on_tokens,
            style="Tool.Toolbutton",
        )
        tokens.pack(side="left", padx=1)
        Tooltip(
            tokens,
            "Show tokens  (Ctrl+T)\nReplace the drawing with the literal text "
            "that gets saved -- the quickest way to check a plate against the "
            "bench.",
        )

        self._separator()
        self._button(
            "⊞", "Grid size...\nChange how many rows and columns the "
            "plate has.", on_grid_size,
        )
        self._button(
            "＋", "Add plate\nA second physical plate in the same design.",
            on_add_plate,
        )
        self._button(
            "⧉", "Duplicate this plate\nCopy the layout with every "
            "replicate number shifted up.", on_duplicate_plate,
        )
        self._button(
            "✕", "Delete this plate", on_delete_plate,
        )

    def _button(self, symbol: str, tip: str, command) -> ttk.Button:
        button = ttk.Button(
            self, text=symbol, style="Tool.Toolbutton", command=command, takefocus=False
        )
        button.pack(side="left", padx=1)
        Tooltip(button, tip)
        return button

    def _separator(self) -> None:
        ttk.Separator(self, orient="vertical").pack(
            side="left", fill="y", padx=6, pady=2
        )

    def set_history(self, can_undo: bool, can_redo: bool) -> None:
        self.undo_button.state(("!disabled",) if can_undo else ("disabled",))
        self.redo_button.state(("!disabled",) if can_redo else ("disabled",))


class ContextBar(ttk.Frame):
    """Only the settings the active tool reads, laid out in one row."""

    def __init__(self, master: tk.Misc, tool: tk.StringVar) -> None:
        super().__init__(master, padding=(8, 3))
        self.tool = tool

        self.first_slot = tk.IntVar(value=1)
        self.replicate = tk.IntVar(value=1)
        self.dilution = tk.IntVar(value=0)
        self.spacing = tk.IntVar(value=1)

        self._placing = ttk.Frame(self)
        self._spin(self._placing, "First slot", self.first_slot, 1, 99)
        self._spin(self._placing, "Replicate", self.replicate, 1, 99)
        self._spin(self._placing, "Dilution", self.dilution, 0, 20)

        # Direction and level count now come from the drag, so the only thing
        # left to configure is the gap between consecutive levels.
        self._series = ttk.Frame(self)
        self._spin(self._series, "Spacing", self.spacing, 1, 10)
        ttk.Label(
            self._series,
            text="drag outwards from a spot to set direction and levels",
            foreground=theme.MUTED,
        ).pack(side="left")

        self._idle = ttk.Label(self, text="", foreground=theme.MUTED)
        self.update_visibility()

    def _spin(self, parent, label, variable, lo, hi) -> None:
        row = ttk.Frame(parent)
        row.pack(side="left", padx=(0, 14))
        ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
        ttk.Spinbox(row, from_=lo, to=hi, textvariable=variable, width=5).pack(
            side="left"
        )

    def update_visibility(self) -> None:
        tool = self.tool.get()
        for frame in (self._placing, self._series, self._idle):
            frame.pack_forget()
        if tool == "assign":
            self._placing.pack(side="left")
        elif tool == "series":
            self._series.pack(side="left")
        else:
            self._idle.configure(text=f"{TOOL_TITLES.get(tool, '')}: no settings")
            self._idle.pack(side="left")
