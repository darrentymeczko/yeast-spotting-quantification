"""Choosing which photograph is which plate, and which dilution to score on it.

Handpicked quantification only. The two decisions belong together and belong
here, in front of the picture:

* **Which photo is plate N.** Stated outright rather than parsed out of a
  filename. A raw camera dump names every file `_9.JPG`, so there is nothing to
  parse, and getting it wrong silently mislabels biological replicates.
* **Which dilution to score.** Per plate, not per treatment. Every spot is
  normalised to the control on its OWN plate, so two plates of one treatment
  that grew to different densities can each be scored at whichever row is
  actually readable without becoming incomparable. Judging that means looking
  at the plate, which is why this moved off the conditions screen.

The slot table is the spine: one row per (condition, plate) the template
requires, filled in or not. What is missing is therefore visible at a glance
rather than discovered when a run refuses to start.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Callable

from uikit import tokens

from .imaging import ImageCache, unreadable_reason

#: Longest edge of the preview. Big enough to judge whether a row of spots is
#: readable, small enough that flipping through a folder stays instant.
PREVIEW_BOX = (620, 520)


class PlatePicker(ttk.Frame):
    def __init__(self, master, controller, on_change: Callable[[], None]):
        super().__init__(master, padding=(10, 8))
        self.ctl = controller
        self.on_change = on_change
        self.images = ImageCache()
        self.dilution = tk.StringVar(value="")
        self._showing: str = ""      # relpath currently in the preview
        self._levels: list[str] = []
        from .groups import GroupSelector
        self.group_selector = GroupSelector(self, controller, on_change)
        self.group_selector.pack(fill="x", pady=(0, 8))

        self.intro = ttk.Label(
            self,
            text=("Choose the photograph for each plate, then the dilution row "
                  "to score on it.\nEach plate has its own level: every spot is "
                  "compared to the control on its own plate."),
            foreground=tokens.TEXT_MUTED, justify="left",
        )
        self.intro.pack(anchor="w", fill="x", pady=(0, 8))
        self.bind("<Configure>", lambda e: self.intro.configure(
            wraplength=max(160, e.width - 24)))

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)
        panes.add(self._build_left(panes), weight=1)
        panes.add(self._build_right(panes), weight=1)

    # -- construction --------------------------------------------------------

    def _build_left(self, master) -> ttk.Frame:
        left = ttk.Frame(master)

        ttk.Label(left, text="Plates to fill", font=("", 9, "bold")).pack(anchor="w")
        slots = ttk.Frame(left)
        slots.pack(fill="both", expand=True, pady=(2, 8))
        columns = ("condition", "plate", "photo", "dilution")
        scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.slot_style = "PlatePicker.Treeview"
        ttk.Style().configure(self.slot_style, rowheight=int(24 * scale))
        self.slots = ttk.Treeview(
            slots, columns=columns, show="headings", height=7,
            style=self.slot_style,
        )
        for key, title, width in (("condition", "Condition", 90),
                                  ("plate", "Plate", 60),
                                  ("photo", "Photograph", 160),
                                  ("dilution", "Dilution", 80)):
            self.slots.heading(key, text=title)
            self.slots.column(key, width=int(width * scale),
                              minwidth=int(55 * scale), anchor="w")
        slot_scroll = ttk.Scrollbar(slots, orient="vertical",
                                    command=self.slots.yview)
        slot_x_scroll = ttk.Scrollbar(slots, orient="horizontal",
                                      command=self.slots.xview)
        self.slots.configure(yscrollcommand=slot_scroll.set,
                             xscrollcommand=slot_x_scroll.set)
        slots.columnconfigure(0, weight=1)
        slots.rowconfigure(0, weight=1)
        self.slots.grid(row=0, column=0, sticky="nsew")
        slot_scroll.grid(row=0, column=1, sticky="ns")
        slot_x_scroll.grid(row=1, column=0, sticky="ew")
        self.slots.tag_configure("empty", foreground=tokens.ERROR)
        self.slots.bind("<<TreeviewSelect>>", self._slot_selected)

        ttk.Label(left, text="Photographs in the folder",
                  font=("", 9, "bold")).pack(anchor="w")
        cands = ttk.Frame(left)
        cands.pack(fill="both", expand=True, pady=(2, 0))
        self.candidates = tk.Listbox(cands, activestyle="none",
                                     exportselection=False, height=8)
        cand_scroll = ttk.Scrollbar(cands, orient="vertical",
                                    command=self.candidates.yview)
        self.candidates.configure(yscrollcommand=cand_scroll.set)
        self.candidates.pack(side="left", fill="both", expand=True)
        cand_scroll.pack(side="right", fill="y")
        self.candidates.bind("<<ListboxSelect>>", self._candidate_selected)
        self.candidates.bind("<Double-1>", lambda _e: self._assign())
        self.candidates.bind("<Return>", lambda _e: self._assign())
        return left

    def _build_right(self, master) -> ttk.Frame:
        right = ttk.Frame(master, padding=(10, 0))

        self.caption = ttk.Label(right, text="", font=("", 9, "bold"),
                                 width=1, anchor="w")
        self.caption.pack(anchor="w", fill="x")

        self.preview = ttk.Label(right, anchor="center", justify="center",
                                 foreground=tokens.TEXT_MUTED)
        self.preview.pack(fill="both", expand=True, pady=(4, 6))

        nav = ttk.Frame(right)
        nav.pack(fill="x")
        ttk.Button(nav, text="< Previous", width=11,
                   command=lambda: self._step(-1)).pack(side="left")
        self.position = ttk.Label(nav, text="", foreground=tokens.TEXT_MUTED)
        self.position.pack(side="left", expand=True)
        ttk.Button(nav, text="Next >", width=11,
                   command=lambda: self._step(1)).pack(side="right")

        self.assign_button = ttk.Button(
            right, text="Use this photograph for the selected plate",
            command=self._assign)
        self.assign_button.pack(fill="x", pady=(8, 0))

        self.levels_box = ttk.LabelFrame(
            right, text="Dilution row to score on this plate", padding=(8, 6))
        self.levels_box.pack(fill="x", pady=(10, 0))
        self.levels_frame = ttk.Frame(self.levels_box)
        self.levels_frame.pack(fill="x")
        # A dropdown rather than a row of radio buttons. The number of levels is
        # whatever the template declares -- six on a plate spotted once per row
        # -- and a row of buttons runs off the edge well before that. The list
        # is numbered so it stays readable when a design has more levels than
        # there are words for.
        ttk.Label(self.levels_frame, text="Level:").pack(side="left")
        self.level_box = ttk.Combobox(self.levels_frame, state="readonly",
                                      textvariable=self.dilution, width=22)
        self.level_box.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self.level_box.bind("<<ComboboxSelected>>", lambda _e: self._set_dilution())

        self.hint = ttk.Label(right, text="", foreground=tokens.TEXT_MUTED, justify="left",
                              wraplength=PREVIEW_BOX[0])
        self.hint.pack(anchor="w", fill="x", pady=(8, 0))
        right.bind("<Configure>", lambda e: self.hint.configure(
            wraplength=max(160, e.width - 20)))
        return right

    # -- display -------------------------------------------------------------

    def refresh(self) -> None:
        self.group_selector.refresh()
        e = self.ctl.panel_experiment
        self._sync_levels()

        selected = self.selected_slot()
        self.slots.delete(*self.slots.get_children())
        for code, plate_id in self.ctl.slots():
            relpath = e.pick(code, plate_id)
            level = self.ctl.dilution_display(code, plate_id)
            iid = _iid(code, plate_id)
            self.slots.insert(
                "", "end", iid=iid,
                tags=() if (relpath and level != "-") else ("empty",),
                values=(self.ctl.condition_name(code), plate_id,
                        Path(relpath).name if relpath else "-",
                        level),
            )
        if selected and self.slots.exists(_iid(*selected)):
            self.slots.selection_set(_iid(*selected))
        elif self.slots.get_children():
            self.slots.selection_set(self.slots.get_children()[0])

        self._sync_candidates()
        self._sync_controls()

    def _sync_levels(self) -> None:
        levels = self.ctl.dilution_levels()
        if levels == self._levels:
            return
        self._levels = levels
        self.level_box.configure(values=[name for _, name in levels])

    def _sync_candidates(self) -> None:
        wanted = self.ctl.candidates()
        current = list(self.candidates.get(0, "end"))
        if current != wanted:
            self.candidates.delete(0, "end")
            for relpath in wanted:
                self.candidates.insert("end", relpath)
        slot = self.selected_slot()
        chosen = self.ctl.panel_experiment.pick(*slot) if slot else None
        target = self._showing or chosen
        if target in wanted:
            index = wanted.index(target)
            self.candidates.selection_clear(0, "end")
            self.candidates.selection_set(index)
            self.candidates.see(index)
            self._showing = target
        elif not wanted:
            self._showing = ""
        else:
            self._showing = ""

    def _sync_controls(self) -> None:
        slot = self.selected_slot()
        e = self.ctl.panel_experiment

        if slot:
            code, plate_id = slot
            index = self.ctl.dilution_index(code, plate_id)
            shown = next((name for i, name in self._levels if i == index), "")
            if self.dilution.get() != shown:
                self.dilution.set(shown)
            chosen = e.pick(code, plate_id)
            self.levels_box.configure(
                text=f"Dilution row to score on {self.ctl.condition_name(code)} {self.ctl.plate_label(plate_id)}")
            self.level_box.configure(
                state="readonly" if chosen and self._levels else "disabled")
            self.hint.configure(
                text="" if chosen else
                "Choose a photograph for this plate before setting its dilution.")
        else:
            self.dilution.set("")
            self.levels_box.configure(text="Dilution row to score on this plate")
            self.level_box.configure(state="disabled")
            self.hint.configure(
                text=("No plates to fill yet. Add at least one condition, and "
                      "choose a plate template on the Panel tab."))

        can_assign = bool(slot) and bool(self._showing)
        self.assign_button.state(["!disabled"] if can_assign else ["disabled"])
        self._show_preview()

    def _show_preview(self) -> None:
        total = self.candidates.size()
        if not self._showing:
            self.preview.configure(
                image="",
                text=("No photographs here yet -- choose the photo folder at the "
                      "top of the window." if not total else
                      "Select a photograph from the list."),
            )
            self.preview.image = None
            self.caption.configure(text="")
            self.position.configure(text="")
            return

        index = list(self.candidates.get(0, "end")).index(self._showing)
        self.position.configure(text=f"{index + 1} of {total}")

        slot = self.selected_slot()
        used_by = [f"{self.ctl.condition_name(code)} plate {plate}"
                   for code, plate in self.ctl.slots()
                   if self.ctl.panel_experiment.pick(code, plate) == self._showing]
        mark = f"   (already used for {', '.join(used_by)})" if used_by else ""
        self.caption.configure(text=f"{self._showing}{mark}")

        path = self.ctl.photo_path(self._showing)
        image = self.images.get(path, PREVIEW_BOX)
        if image is None:
            self.preview.configure(image="", text=unreadable_reason(path))
            self.preview.image = None
        else:
            self.preview.configure(image=image, text="")
            # Held so Tk does not garbage-collect it and blank the widget.
            self.preview.image = image

    # -- selection -----------------------------------------------------------

    def selected_slot(self) -> "tuple[str, str] | None":
        sel = self.slots.selection()
        return _unpack(sel[0]) if sel else None

    def _slot_selected(self, _event=None) -> None:
        slot = self.selected_slot()
        if slot:
            chosen = self.ctl.panel_experiment.pick(*slot)
            if chosen:
                self._showing = chosen
        self._sync_candidates()
        self._sync_controls()

    def _candidate_selected(self, _event=None) -> None:
        sel = self.candidates.curselection()
        if sel:
            self._showing = self.candidates.get(sel[0])
        self._sync_controls()

    def _step(self, delta: int) -> None:
        total = self.candidates.size()
        if not total:
            return
        names = list(self.candidates.get(0, "end"))
        index = names.index(self._showing) if self._showing in names else -delta
        index = max(0, min(total - 1, index + delta))
        self.candidates.selection_clear(0, "end")
        self.candidates.selection_set(index)
        self.candidates.see(index)
        self._showing = names[index]
        self._sync_controls()

    # -- edits ---------------------------------------------------------------

    def _assign(self) -> None:
        slot = self.selected_slot()
        if not slot or not self._showing:
            return
        code, plate_id = slot
        if self.ctl.set_pick(code, plate_id, self._showing):
            self._advance_to_next_empty()
            self.on_change()

    def _advance_to_next_empty(self) -> None:
        """Move to the next plate still needing a photograph.

        Filling a folder of plates is a run of the same action, so the obvious
        next one is selected rather than leaving the cursor where it was.
        """
        e = self.ctl.panel_experiment
        for code, plate_id in self.ctl.slots():
            if not e.pick(code, plate_id):
                iid = _iid(code, plate_id)
                if self.slots.exists(iid):
                    self.slots.selection_set(iid)
                    self.slots.see(iid)
                return

    def _set_dilution(self) -> None:
        slot = self.selected_slot()
        shown = self.dilution.get()
        if not slot or not shown:
            return
        index = next((i for i, name in self._levels if name == shown), None)
        if index is None:
            return
        if self.ctl.set_plate_dilution(slot[0], slot[1], index):
            self.on_change()


def _iid(code: str, plate_id: str) -> str:
    # "\x1f" (unit separator) cannot occur in a condition code or plate id, so
    # the pair always unpacks even when a code contains punctuation.
    return f"{code}\x1f{plate_id}"


def _unpack(iid: str) -> tuple[str, str]:
    code, _, plate_id = iid.partition("\x1f")
    return code, plate_id
