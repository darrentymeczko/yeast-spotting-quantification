"""The per-spot table: the numbers behind the graph, and how to correct them.

Three things can be said about a spot, and the table keeps them distinct because
they mean different things to whoever reads the exported CSV:

  raw value     -- replaced with one measured by hand
  outlier       -- this spot is data, but it dominates the spread
  note          -- why

Pipeline/config exclusions remain visible and are preserved, but the review UI
uses one manual omit action -- outlier -- rather than presenting two controls
with the same effect on figures and statistics.

Clearing an edit restores the PIPELINE's opinion rather than the opposite of the
person's. "I have no view on this spot" and "I insist this spot is fine" are
different claims, and only the first is the correct undo.

The strain summary beside the table is the immediate feedback loop: excluding a
control spot changes the divisor for the whole plate, and the means move as soon
as it is done, before the graph is redrawn.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk
from typing import Callable

from .. import theme

#: The number the user types in is a background-subtracted grey value. Negative
#: is physically meaningless (the spot would be darker than the agar around it)
#: and is almost always a typo or a sign error, so it is refused rather than
#: normalised into a negative growth ratio.
MIN_RAW = 0.0


def _rep_no(replicate: str) -> int:
    digits = "".join(ch for ch in str(replicate) if ch.isdigit())
    return int(digits) if digits else 0


class SpotTable(ttk.Frame):
    COLUMNS = (("rep", "Replicate", 78), ("strain", "Strain", 110),
               ("raw", "Grey", 70), ("rel", "Relative", 74),
               ("flags", "Flags", 190), ("note", "Note", 180))

    def __init__(self, master, on_raw: Callable, on_outlier: Callable,
                 on_note: Callable, on_revert: Callable,
                 **kw) -> None:
        super().__init__(master, **kw)
        self.on_raw = on_raw
        self.on_outlier = on_outlier
        self.on_note = on_note
        self.on_revert = on_revert

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        hint = ("Double-click a grey value to re-measure · O toggles outlier · "
                "N adds a note · Backspace reverts")
        ttk.Label(self, text=hint, font=theme.FONT_SMALL,
                  foreground=theme.MUTED, anchor="w").grid(
            row=0, column=0, sticky="ew", padx=theme.PAD)

        # A sash, not a fixed grid. Side by side these two want more width than
        # a laptop screen has, and in a grid whichever loses gets silently
        # clipped -- the summary stops showing the SD it exists to show, or the
        # note column disappears off the edge. A sash lets the person decide
        # which they are reading, and nothing is cut off without a way back.
        self.panes = ttk.PanedWindow(self, orient="horizontal")
        # Keep both tables away from the side edges. Padding on the summary
        # Treeview itself can be absorbed by the PanedWindow while it negotiates
        # the fixed summary width, so the shared gutters belong here.
        self.panes.grid(row=1, column=0, sticky="nsew",
                        padx=(theme.PAD, 2 * theme.PAD))

        left = ttk.Frame(self.panes)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width in self.COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=44,
                             stretch=key in ("flags", "note"),
                             anchor="e" if key in ("raw", "rel") else "w")
        theme.configure_tags(self.tree)
        bar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        self.panes.add(left, weight=3)

        right = ttk.Frame(self.panes, width=theme.SUMMARY_W)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.summary = ttk.Treeview(right,
                                    columns=("strain", "n", "mean", "p"),
                                    show="headings", selectmode="none",
                                    height=8)
        # "1.000 ± 0.146" is wider than the "Mean ± SD" heading suggests.
        for key, title, width, minimum, anchor in (
                ("strain", "Strain", 70, 50, "w"),
                ("n", "n", 22, 20, "center"),
                ("mean", "Mean ± SD", 92, 78, "e"),
                ("p", "p vs +", 68, 58, "e")):
            self.summary.heading(key, text=title)
            self.summary.column(key, width=width, minwidth=minimum,
                                anchor=anchor, stretch=(key == "strain"))
        # Significance is conveyed by weight only; keep the same foreground
        # colour as every other summary row.
        self.summary.tag_configure("significant", font=theme.FONT_BOLD)
        self.summary.grid(row=0, column=0, sticky="nsew",
                          padx=(theme.PAD, 0))
        self.panes.add(right, weight=1)
        # Place the sash once, when the widget first has a real size. Left to
        # the weights alone the summary starts a few pixels too narrow and
        # clips the last digit of the SD, which is exactly the digit somebody
        # is looking at when they wonder whether an edit helped.
        self._sash_placed = False
        self.panes.bind("<Configure>", self._place_sash)

        self.message = ttk.Label(self, text="", font=theme.FONT,
                                 foreground=theme.MUTED, anchor="center",
                                 justify="center", wraplength=520)

        # Match the footer under the large candidate table on the left.  A
        # blank label using the same font and top gap as its candidate-count
        # label keeps the two bottom edges aligned even when Windows display
        # scaling changes the label height.
        self.bottom_gutter = ttk.Label(self, text="", font=theme.FONT_SMALL)
        self.bottom_gutter.grid(row=2, column=0, sticky="ew",
                                pady=(theme.GAP - 1, 0))

        self._rows: dict[str, dict] = {}
        self._editor: "tk.Entry | None" = None

        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<KeyPress-o>", self._toggle_outlier)
        self.tree.bind("<KeyPress-O>", self._toggle_outlier)
        self.tree.bind("<KeyPress-n>", self._edit_note)
        self.tree.bind("<KeyPress-N>", self._edit_note)
        self.tree.bind("<BackSpace>", self._revert)
        self.tree.bind("<Button-3>", self._context)

    # -- contents ------------------------------------------------------------

    def show_message(self, text: str, kind: str = "") -> None:
        """Replace the table with an explanation (no photos, rebuild failed...)."""
        self.panes.grid_remove()
        self.message.configure(
            text=text,
            foreground={"error": theme.ERROR, "warn": theme.WARNING}.get(
                kind, theme.MUTED))
        self.message.grid(row=1, column=0, sticky="nsew", padx=theme.PAD)

    def show(self, rows: list[dict], summary: list[dict]) -> None:
        self._cancel_edit()
        self.message.grid_remove()
        self.panes.grid()

        keep = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self._rows.clear()
        # Strain by strain, replicates in order inside it: that is how a person
        # reads four replicates of one strain against each other, which is the
        # comparison an outlier decision is actually made on.
        for row in sorted(rows, key=lambda r: (int(r["strain_col"]),
                                               _rep_no(r["replicate"]))):
            iid = f"{row['replicate']}|{row['strain_col']}"
            self._rows[iid] = row
            self.tree.insert("", "end", iid=iid, tags=theme.row_tags(row),
                             values=(row["replicate"], row["strain"],
                                     _fmt(row.get("raw_growth")),
                                     _fmt(row.get("relative_growth")),
                                     theme.flag_text(row),
                                     row.get("edit_note") or ""))
        if keep and keep[0] in self._rows:
            self.tree.selection_set(keep[0])
        elif self._rows:
            self.tree.selection_set(next(iter(self._rows)))

        self.summary.delete(*self.summary.get_children())
        heading = summary[0].get("p_heading", "p vs +") if summary else "p vs +"
        self.summary.heading("p", text=heading)
        for s in summary:
            tags = ("significant",) if s.get("significant") else ()
            self.summary.insert("", "end", tags=tags,
                                values=(s["strain"], s["n"],
                                        f"{s['mean']:.3f} ± {s['sd']:.3f}",
                                        _fmt_p(s.get("p_value"))))

    def _place_sash(self, event=None) -> None:
        """Give the summary its full width once, then leave the sash alone.

        Only once: after this the position is the person's to set, and a
        handler that kept re-centring it would undo every drag.
        """
        if self._sash_placed:
            return
        width = self.panes.winfo_width()
        if width < 400:                # not laid out yet
            return
        try:
            self.panes.sashpos(0, max(320, width - theme.SUMMARY_W))
            self._sash_placed = True
        except tk.TclError:            # pragma: no cover - not yet mapped
            pass

    def focus_table(self) -> None:
        self.tree.focus_set()

    # -- what is selected ----------------------------------------------------

    def _selected(self) -> "dict | None":
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    # -- editing -------------------------------------------------------------

    def _cancel_edit(self) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None

    def _begin_edit(self, event=None) -> str:
        """Inline entry over the grey-value cell."""
        self._cancel_edit()
        if event is not None and self.tree.identify_region(event.x, event.y) \
                != "cell":
            return "break"
        iid = (self.tree.identify_row(event.y) if event is not None
               else (self.tree.selection() or [None])[0])
        if iid is None or iid not in self._rows:
            return "break"
        if event is not None and self.tree.identify_column(event.x) != "#3":
            return "break"
        self.tree.selection_set(iid)
        box = self.tree.bbox(iid, "raw")
        if not box:
            return "break"
        row = self._rows[iid]

        entry = tk.Entry(self.tree, justify="right", font=theme.FONT,
                         relief="solid", borderwidth=1,
                         background=theme.MANUAL_SOFT, foreground=theme.MANUAL)
        entry.place(x=box[0], y=box[1], width=box[2], height=box[3])
        entry.insert(0, _fmt(row.get("raw_growth")))
        entry.select_range(0, "end")
        entry.focus_set()
        self._editor = entry

        entry.bind("<Return>", lambda _e: self._commit_edit(row))
        entry.bind("<Escape>", lambda _e: self._cancel_edit())
        entry.bind("<FocusOut>", lambda _e: self._cancel_edit())
        return "break"

    def _commit_edit(self, row: dict) -> str:
        if self._editor is None:
            return "break"
        text = self._editor.get().strip()
        self._cancel_edit()
        if not text:
            # An emptied cell means "use what was measured", not "zero".
            self.on_raw(row, None)
            return "break"
        try:
            value = float(text)
        except ValueError:
            self.bell()
            return "break"
        if value < MIN_RAW:
            self.bell()
            return "break"
        self.on_raw(row, value)
        return "break"

    # -- flags ---------------------------------------------------------------

    def _toggle_outlier(self, _event=None) -> str:
        row = self._selected()
        if row is not None:
            src = row.get("outlier_source") or ""
            if src in ("manual", "cleared"):
                self.on_outlier(row, None)
            else:
                self.on_outlier(row, not bool(row.get("outlier")))
        return "break"

    def _edit_note(self, _event=None) -> str:
        row = self._selected()
        if row is None:
            return "break"
        got = simpledialog.askstring(
            "Note",
            f"Why is {row['strain']} {row['replicate']} being changed?",
            initialvalue=row.get("edit_note") or "", parent=self)
        if got is not None:
            self.on_note(row, got)
        return "break"

    def _revert(self, _event=None) -> str:
        row = self._selected()
        if row is not None:
            self.on_revert(row)
        return "break"

    def _context(self, event) -> str:
        iid = self.tree.identify_row(event.y)
        if iid not in self._rows:
            return "break"
        self.tree.selection_set(iid)
        row = self._rows[iid]
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Re-measure…", command=self._begin_edit_selected)
        menu.add_command(
            label=("Unflag outlier" if row.get("outlier") else "Flag as outlier"),
            command=lambda: self.on_outlier(row, not bool(row.get("outlier"))))
        menu.add_separator()
        menu.add_command(label="Note…", command=self._edit_note)
        menu.add_command(label="Revert this spot",
                         command=lambda: self.on_revert(row))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _begin_edit_selected(self) -> None:
        self._begin_edit(None)


def _fmt(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return "—" if f != f else f"{f:.2f}"       # f != f catches NaN


def _fmt_p(value) -> str:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return "—"
    if p != p:
        return "—"
    if p < 0.0001:
        return "<0.0001"
    return f"{p:.4f}".rstrip("0").rstrip(".")
