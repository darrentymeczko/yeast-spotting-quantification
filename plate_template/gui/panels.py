"""Supporting panels: the hover inspector and the validation list."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable

from .. import theme
from ..model import CellKind, Template, cell_ref
from ..validate import Issue, Severity

_SEVERITY_TEXT = {
    Severity.ERROR: "ERROR",
    Severity.WARNING: "warn",
    Severity.INFO: "info",
}


class InspectorBar(ttk.Frame):
    """One line describing whatever the pointer is over."""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=(8, 4))
        self._label = ttk.Label(self, text="", anchor="w")
        self._label.pack(fill="x")
        #: Shown whenever nothing is hovered -- the active tool's instruction.
        self._idle = "hover a cell to inspect it"
        self.clear()

    def set_idle(self, text: str) -> None:
        self._idle = text or "hover a cell to inspect it"
        self.clear()

    def clear(self) -> None:
        self._label.configure(text=self._idle, foreground=theme.MUTED)

    def message(self, text: str, *, alarm: bool = False) -> None:
        """Report the outcome of an edit; replaced by the next hover."""
        self._label.configure(
            text=text, foreground=theme.ERROR if alarm else theme.TEXT
        )

    def show(self, template: Template, plate_id: str, cell: tuple[int, int] | None) -> None:
        if cell is None:
            self.clear()
            return
        r, c = cell
        plate = template.plate(plate_id)
        target = plate.get(r, c)
        where = f"plate {plate.id}  {cell_ref(r, c)}"

        if target.kind is CellKind.UNASSIGNED:
            text = f"{where}   unassigned"
        elif target.kind is CellKind.EMPTY:
            text = f"{where}   empty"
        else:
            p = target.placement
            assert p is not None
            bits = [
                f"sample slot {p.sample_slot}",
                f"replicate {p.replicate}",
                f"dilution {p.dilution} ({template.dilution.label(p.dilution)})",
            ]
            factor = template.dilution.factor(p.dilution)
            if factor is not None:
                bits[-1] += f", 1:{factor:g}"
            if p.sample_slot == plate.control_slot:
                bits.append("CONTROL")
            text = f"{where}   {'   '.join(bits)}   [{p.token()}]"

        self._label.configure(text=text, foreground=theme.TEXT)


class ValidationPanel(ttk.Frame):
    """Live list of what is wrong, or merely suspicious, with the design."""

    def __init__(
        self, master: tk.Misc, on_select: Callable[[Issue], None] | None = None
    ) -> None:
        super().__init__(master)
        self.on_select = on_select
        self._issues: dict[str, Issue] = {}

        self.tree = ttk.Treeview(
            self, columns=("severity", "plate", "message"), show="headings", height=4
        )
        # Column widths are pixels, but the text in them scales with the
        # display, so fixed values clip their own headers on a scaled monitor.
        # ttk's default row height does not scale either, so rows overlap.
        scale = max(1.0, self.winfo_fpixels("1i") / 96)
        ttk.Style().configure("Treeview", rowheight=int(24 * scale))
        self.tree.heading("severity", text="")
        self.tree.heading("plate", text="Plate")
        self.tree.heading("message", text="Finding")
        self.tree.column("severity", width=int(70 * scale), stretch=False, anchor="w")
        self.tree.column("plate", width=int(62 * scale), stretch=False, anchor="center")
        self.tree.column("message", width=int(600 * scale), stretch=True, anchor="w")

        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tree.tag_configure("error", foreground=theme.ERROR)
        self.tree.tag_configure("warning", foreground=theme.WARNING)
        self.tree.tag_configure("info", foreground=theme.INFO)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

    def set_issues(self, issues: list[Issue]) -> None:
        self.tree.delete(*self.tree.get_children())
        self._issues.clear()
        for issue in issues:
            item = self.tree.insert(
                "",
                "end",
                values=(
                    _SEVERITY_TEXT[issue.severity],
                    issue.plate_id or "",
                    issue.message,
                ),
                tags=(issue.severity.value,),
            )
            self._issues[item] = issue

    def _on_select(self, _event: tk.Event) -> None:
        if not self.on_select:
            return
        selection = self.tree.selection()
        if selection:
            self.on_select(self._issues[selection[0]])


class StatusBar(ttk.Frame):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, padding=(8, 3))
        self.left = ttk.Label(self, text="", anchor="w")
        self.right = ttk.Label(self, text="", anchor="e")
        self.left.pack(side="left")
        self.right.pack(side="right")

    def set(self, left: str, right: str = "", *, alarm: bool = False) -> None:
        self.left.configure(text=left)
        self.right.configure(
            text=right, foreground=theme.ERROR if alarm else theme.MUTED
        )
