"""The bars around the edge, and the candidate list.

The candidate list is the heart of the window: it is where the disagreement
with the pipeline happens. So it shows the numbers the pipeline ranked on
(`best_set_score`, median CV, how many strains separated) rather than just a
name, and it marks the pipeline's own pick separately from the current one --
those are the same thing until the moment they are not, and that moment is the
whole point.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .. import theme
from ..model import Candidate

def _num(v, default):
    return default if v is None or (isinstance(v, float) and math.isnan(v)) else v


#: What each column sorts on, and which direction it starts in when first
#: clicked. "Better first" is not the same direction for every column -- a high
#: score is good and a low CV is good -- so each says which way it means, rather
#: than making somebody click twice to find out.
SORT_KEYS = {
    "mark": (lambda c: c.csv_order, False),
    "when": (lambda c: (_num(c.hours, math.inf), c.dilution_rank), False),
    "dil": (lambda c: (c.dilution_rank, _num(c.hours, math.inf)), False),
    "pair": (lambda c: (c.plate1, c.plate2, _num(c.hours, math.inf)), False),
    "score": (lambda c: _num(c.best_set_score, -math.inf), True),
    "cv": (lambda c: _num(c.median_cv, math.inf), False),
    "sig": (lambda c: (c.n_significant, -_num(c.median_cv, math.inf)), True),
}

#: Opening order: the pipeline's own ranking, best first.
DEFAULT_SORT = "score"


def _fmt(v, spec=".3f", dash="—"):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return dash
    return format(v, spec)


class HeaderBar(ttk.Frame):
    """Which set is open, and whether its photos can be reached.

    The two arrows step through the sets in order. Ten sets get reviewed in one
    sitting and they are compared against each other, so moving between them
    has to be one click, not a dialog.
    """

    def __init__(self, master, on_open_set: Callable[[], None],
                 on_link: Callable[[], None],
                 on_step_set: Callable[[int], None], **kw) -> None:
        super().__init__(master, **kw)
        self.columnconfigure(4, weight=1)

        self.prev = ttk.Button(self, text="◀", width=3,
                               command=lambda: on_step_set(-1))
        self.prev.grid(row=0, column=0, sticky="w", padx=(theme.PAD, 0))
        self.next = ttk.Button(self, text="▶", width=3,
                               command=lambda: on_step_set(1))
        self.next.grid(row=0, column=1, sticky="w", padx=(2, theme.GAP))

        self.set_label = ttk.Label(self, text="—", font=theme.FONT_HEAD)
        self.set_label.grid(row=0, column=2, sticky="w")

        self.position = ttk.Label(self, text="", font=theme.FONT_SMALL,
                                  foreground=theme.MUTED)
        self.position.grid(row=0, column=3, sticky="w", padx=(theme.GAP, 0))

        self.photos = ttk.Label(self, text="", font=theme.FONT_SMALL,
                                foreground=theme.MUTED)
        self.photos.grid(row=0, column=4, sticky="w", padx=(theme.PAD, 0))

        # The photo/set actions live in the options row below this header,
        # beside the statistics controls they affect, rather than crowding the
        # top edge.  Keep the callbacks accepted here for API compatibility.
        self.on_link = on_link
        self.on_open_set = on_open_set

    def show(self, label: str, photos: str, ok: bool,
             index: int = -1, total: int = 0) -> None:
        self.set_label.configure(text=label)
        self.photos.configure(
            text=f"photos: {photos}",
            foreground=theme.MUTED if ok else theme.WARNING)
        if total and index >= 0:
            self.position.configure(text=f"{index + 1} of {total}")
            self.prev.state(["!disabled"] if index > 0 else ["disabled"])
            self.next.state(["!disabled"] if index < total - 1
                            else ["disabled"])
        else:
            # A results folder opened from somewhere else is not part of the
            # sequence, so stepping through it would mean nothing.
            self.position.configure(text="")
            self.prev.state(["disabled"])
            self.next.state(["disabled"])


class StatusBar(ttk.Frame):
    """One line of what just happened, and whether anything is unsaved.

    Carries the progress bar too. Redrawing/exporting figures and the two
    background-subtraction passes per medium can take long enough that a window
    which merely sits there is indistinguishable from one that has hung.
    """

    def __init__(self, master, **kw) -> None:
        super().__init__(master, **kw)
        self.columnconfigure(1, weight=1)

        self.bar = ttk.Progressbar(self, mode="indeterminate", length=120)
        # Not gridded until there is something to wait for: a progress bar
        # sitting still at zero reads as a job that is stuck.
        self.message = ttk.Label(self, text="", font=theme.FONT_SMALL,
                                 anchor="w")
        self.message.grid(row=0, column=1, sticky="ew", padx=theme.PAD, pady=2)
        self.state = ttk.Label(self, text="", font=theme.FONT_SMALL,
                               anchor="e")
        self.state.grid(row=0, column=2, sticky="e", padx=theme.PAD)

    def say(self, text: str, kind: str = "") -> None:
        colour = {"error": theme.ERROR, "warn": theme.WARNING,
                  "ok": theme.OK}.get(kind, theme.TEXT)
        self.message.configure(text=text, foreground=colour)

    def busy(self, on: bool) -> None:
        """Show or hide the moving bar."""
        if on:
            self.bar.grid(row=0, column=0, sticky="w", padx=(theme.PAD, 0))
            self.bar.start(12)
        else:
            self.bar.stop()
            self.bar.grid_remove()

    def show_state(self, text: str, dirty: bool) -> None:
        self.state.configure(text=text,
                             foreground=theme.MANUAL if dirty else theme.MUTED)


class CandidateList(ttk.Frame):
    """Every candidate for the current medium, with the numbers it was ranked on."""

    COLUMNS = (("mark", "", 36), ("when", "Timepoint", 78),
               ("dil", "Dilution", 62), ("pair", "Photos", 130),
               ("score", "Score", 52), ("cv", "CV", 50), ("sig", "Sig", 44))

    def __init__(self, master, on_select: Callable[[Candidate], None],
                 on_choose: Callable[[Candidate], None], **kw) -> None:
        super().__init__(master, **kw)
        self.on_select = on_select
        self.on_choose = on_choose
        self._by_iid: dict[str, Candidate] = {}
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.sort_key = DEFAULT_SORT
        self.sort_desc = SORT_KEYS[DEFAULT_SORT][1]

        self.tree = ttk.Treeview(self, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width in self.COLUMNS:
            # Sorting lives on the headers, where the columns are. A dropdown
            # naming the same orderings in words is a second place to learn and
            # a second thing to keep in step with the columns themselves.
            self.tree.heading(key, text=title,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=theme.px(width), stretch=(key == "pair"),
                             anchor="w" if key in ("when", "dil", "pair")
                             else "center")
        bar = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure("default", foreground=theme.DEFAULT_MARK)
        self.tree.tag_configure("datareview", foreground=theme.REVIEWED_BAD)
        self.tree.tag_configure("chosen", background=theme.CHOSEN_SOFT,
                                foreground=theme.CHOSEN, font=theme.FONT_BOLD)
        self.tree.tag_configure("nosheet", foreground=theme.MUTED)

        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Double-1>", self._activated)
        self.tree.bind("<Return>", self._activated)

        self.count = ttk.Label(self, text="", font=theme.FONT_SMALL,
                               foreground=theme.MUTED, anchor="w")
        self.count.grid(row=1, column=0, columnspan=2, sticky="ew",
                        pady=(theme.GAP - 1, 0))

        self._candidates: list[Candidate] = []
        self._default: "Candidate | None" = None
        self._chosen: "Candidate | None" = None
        self._has_sheet = set()
        self._flagged: dict = {}

    # -- contents ------------------------------------------------------------

    def show(self, candidates: list[Candidate], default: "Candidate | None",
             chosen: "Candidate | None", has_sheet, flagged=None) -> None:
        """`flagged`: candidate key -> (plates, spots) the data review flagged
        among what that candidate scores. Marked ⚑; nothing is hidden."""
        self._candidates = list(candidates)
        self._default = default
        self._chosen = chosen
        self._has_sheet = set(has_sheet)
        self._flagged = dict(flagged or {})
        self._repopulate()

    def sort_by(self, column: str) -> None:
        """Clicking a header sorts by it; clicking it again reverses.

        A fresh column starts in the direction that puts the interesting end
        first, which differs per column -- highest score, but LOWEST spread.
        """
        if column not in SORT_KEYS:
            return
        if column == self.sort_key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = column
            self.sort_desc = SORT_KEYS[column][1]
        self._repopulate()

    def _headings(self) -> None:
        """Mark the sorted column with the direction it is sorted in."""
        for key, title, _w in self.COLUMNS:
            arrow = ("  ▼" if self.sort_desc else "  ▲") \
                if key == self.sort_key else ""
            self.tree.heading(key, text=f"{title}{arrow}")

    def _repopulate(self) -> None:
        key, _default_desc = SORT_KEYS.get(self.sort_key,
                                           SORT_KEYS[DEFAULT_SORT])
        # csv_order as the final tiebreak, so equal rows keep a stable order
        # instead of shuffling every time the list is rebuilt.
        ordered = sorted(self._candidates, key=lambda c: (key(c), c.csv_order),
                         reverse=self.sort_desc)
        self._headings()
        self.tree.delete(*self.tree.get_children())
        self._by_iid.clear()
        select = None
        for i, c in enumerate(ordered):
            tags = []
            mark = ""
            if self._default is not None and c.key == self._default.key:
                mark = "★"
                tags.append("default")
            if self._chosen is not None and c.key == self._chosen.key:
                mark = ("★●" if mark else "●")
                tags.append("chosen")
            if c.key not in self._has_sheet:
                tags.append("nosheet")
            if c.key in self._flagged:
                mark += "⚑"
                # Before "chosen", so the chosen row keeps its own colours.
                at = tags.index("chosen") if "chosen" in tags else len(tags)
                tags.insert(at, "datareview")
            iid = str(i)
            self._by_iid[iid] = c
            self.tree.insert(
                "", "end", iid=iid, tags=tuple(tags),
                values=(mark, c.timepoint, c.dilution, c.detail,
                        _fmt(c.best_set_score, ".2f"), _fmt(c.median_cv, ".3f"),
                        f"{c.n_significant}/{c.n_strains}"))
            if self._chosen is not None and c.key == self._chosen.key:
                select = iid
        n_missing = len(self._candidates) - len(
            [c for c in self._candidates if c.key in self._has_sheet])
        note = f"{len(self._candidates)} candidates"
        if n_missing:
            note += f" · {n_missing} without a drawn sheet"
        if self._flagged:
            note += f" · ⚑ {len(self._flagged)} use flagged data"
        self.count.configure(text=note)
        if select is not None:
            self.tree.selection_set(select)
            self.tree.see(select)

    def mark_chosen(self, chosen: "Candidate | None") -> None:
        self._chosen = chosen
        self._repopulate()

    # -- moving through the list ---------------------------------------------

    def step(self, delta: int) -> bool:
        """Move the highlight `delta` rows, in the order currently displayed.

        Returns False at the ends rather than wrapping: the list is ranked, so
        wrapping from the worst candidate back to the best would quietly lose
        somebody's place in a list of a hundred and fourteen.
        """
        kids = self.tree.get_children()
        if not kids:
            return False
        sel = self.tree.selection()
        if not sel:
            target = kids[0]
        else:
            i = kids.index(sel[0]) + delta
            if i < 0 or i >= len(kids):
                return False
            target = kids[i]
        self.tree.selection_set(target)
        self.tree.focus(target)
        self.tree.see(target)
        return True

    # -- events --------------------------------------------------------------

    def _current(self) -> "Candidate | None":
        sel = self.tree.selection()
        return self._by_iid.get(sel[0]) if sel else None

    def _selected(self, _event=None) -> None:
        c = self._current()
        if c is not None:
            self.on_select(c)

    def _activated(self, _event=None) -> str:
        c = self._current()
        if c is not None:
            self.on_choose(c)
        return "break"
