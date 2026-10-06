"""The conditions: what the panel was spotted on, and the control for each.

The per-condition control override is the reason this screen is not just a list
of names. The WT used in this lab has a growth defect on respiring media, so
normalising K-OAc against it divides by a strain that barely grew; several sets
therefore name a different reference there. That is a real biological decision
and it needs somewhere obvious to live.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Callable

from uikit import tokens

_NONE = "(use the experiment's)"


class ConditionsPanel(ttk.Frame):
    def __init__(self, master, controller, on_change: Callable[[], None]):
        super().__init__(master, padding=(10, 8))
        self.ctl = controller
        self.on_change = on_change
        self.condition_control = tk.StringVar(value="")
        self._exclude_vars: dict[int, tk.BooleanVar] = {}
        from .groups import GroupSelector
        self.group_selector = GroupSelector(self, controller, on_change)
        self.group_selector.pack(fill="x", pady=(0, 8))

        self.intro = ttk.Label(
            self,
            text=("Each medium or treatment the panel was spotted on. A "
                  "condition may name its own control:\nuse that when a strain "
                  "does not grow on one medium and cannot be the reference there."),
            foreground=tokens.TEXT_MUTED, justify="left",
        )
        self.intro.pack(anchor="w", fill="x", pady=(0, 8))
        self.bind("<Configure>", lambda e: self.intro.configure(
            wraplength=max(160, e.width - 24)))

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="Add...", command=self._add).pack(side="left")
        ttk.Button(bar, text="Rename...", command=self._rename).pack(side="left", padx=4)
        ttk.Button(bar, text="Remove", command=self._remove).pack(side="left", padx=4)
        self.adopt = ttk.Button(bar, text="Add the ones found in the photos",
                                command=self._adopt)
        self.adopt.pack(side="left", padx=(12, 0))

        panes = ttk.PanedWindow(self, orient="horizontal")
        self.panes = panes
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes, padding=(0, 0, 10, 0))
        self.inspector = ttk.LabelFrame(
            panes, text="Selected condition", padding=(12, 10))
        panes.add(left, weight=3)
        panes.add(self.inspector, weight=2)
        panes.bind("<Configure>", self._resize_panes)

        # No dilution column: which row to score is judged against the picture
        # and is set per plate, on the Plates tab.
        columns = ("label", "control", "exclude", "photos")
        table = ttk.Frame(left)
        self.table_frame = table
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)

        # Windows scales ttk's font but not Treeview rows or pixel-based
        # columns.  Without compensating, the lower half of every condition is
        # clipped on a high-DPI display and longer values are cut off inside
        # their cells even when the table itself has room.
        self.scale = max(1.0, self.winfo_fpixels("1i") / 96)
        self.tree_style = "Conditions.Treeview"
        ttk.Style().configure(self.tree_style, rowheight=int(24 * self.scale))
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", height=8,
            style=self.tree_style,
        )
        self._column_specs = (
            ("label", "Treatment", 110, 3),
            ("control", "Control", 70, 2),
            ("exclude", "Excluded slots", 78, 2),
            ("photos", "Photos", 45, 1),
        )
        for key, title, width, _weight in self._column_specs:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=int(width * self.scale),
                             minwidth=int(38 * self.scale), stretch=False,
                             anchor="w")
        self.condition_y_scroll = ttk.Scrollbar(
            table, orient="vertical", command=self.tree.yview)
        self.condition_x_scroll = ttk.Scrollbar(
            table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=self.condition_y_scroll.set,
                            xscrollcommand=self.condition_x_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.condition_y_scroll.grid(row=0, column=1, sticky="ns")
        self.condition_x_scroll.grid(row=1, column=0, sticky="ew")
        table.bind("<Configure>", self._resize_columns)
        self.tree.bind("<Double-1>", self._edit)
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

        self.condition_name = ttk.Label(
            self.inspector, text="No condition selected", font=("", 11, "bold"),
            anchor="w", justify="left",
        )
        self.condition_name.pack(fill="x")
        self.condition_photos = ttk.Label(
            self.inspector, text="", foreground=tokens.TEXT_MUTED, anchor="w")
        self.condition_photos.pack(fill="x", pady=(2, 12))
        ttk.Label(self.inspector, text="Control for this condition",
                  font=("", 9, "bold")).pack(anchor="w")
        self.control_picker = ttk.Combobox(
            self.inspector, textvariable=self.condition_control,
            state="readonly",
        )
        self.control_picker.pack(fill="x", pady=(4, 12))
        self.control_picker.bind("<<ComboboxSelected>>", self._control_selected)
        ttk.Label(self.inspector, text="Excluded slots",
                  font=("", 9, "bold")).pack(anchor="w")
        self.exclude_hint = ttk.Label(
            self.inspector,
            text="Uncheck every slot to include the full panel.",
            foreground=tokens.TEXT_MUTED, justify="left",
        )
        self.exclude_hint.pack(anchor="w", fill="x", pady=(2, 4))
        exclude_area = ttk.Frame(self.inspector)
        exclude_area.pack(fill="both", expand=True)
        self.exclude_canvas = tk.Canvas(exclude_area, highlightthickness=0,
                                        borderwidth=0, height=100)
        exclude_scroll = ttk.Scrollbar(
            exclude_area, orient="vertical", command=self.exclude_canvas.yview)
        self.exclude_canvas.configure(yscrollcommand=exclude_scroll.set)
        self.exclude_canvas.pack(side="left", fill="both", expand=True)
        exclude_scroll.pack(side="right", fill="y")
        self.exclude_frame = ttk.Frame(self.exclude_canvas)
        self._exclude_window = self.exclude_canvas.create_window(
            (0, 0), window=self.exclude_frame, anchor="nw")
        self.exclude_frame.bind("<Configure>", lambda _e:
                                self.exclude_canvas.configure(
                                    scrollregion=self.exclude_canvas.bbox("all")))
        self.exclude_canvas.bind("<Configure>", lambda e:
                                 self.exclude_canvas.itemconfigure(
                                     self._exclude_window, width=e.width))
        self.inspector.bind("<Configure>", self._resize_inspector)

    # -- display -------------------------------------------------------------

    def _resize_panes(self, event) -> None:
        """Keep enough room for all columns while retaining a useful inspector."""
        if event.width > 1:
            try:
                self.panes.sashpos(0, int(event.width * 0.64))
            except tk.TclError:
                pass

    def _resize_columns(self, event) -> None:
        """Fit every column into the table; scroll only at extreme widths."""
        available = max(
            1, event.width - self.condition_y_scroll.winfo_reqwidth() - 4)
        font = tkfont.nametofont("TkDefaultFont")
        minimums = [
            max(int(base * self.scale), font.measure(title) + int(10 * self.scale))
            for _key, title, base, _weight in self._column_specs
        ]
        minimum_total = sum(minimums)
        if available < minimum_total:
            widths = minimums
            self.condition_x_scroll.grid()
        else:
            weights = [spec[3] for spec in self._column_specs]
            extra = available - minimum_total
            widths = [minimum + extra * weight // sum(weights)
                      for minimum, weight in zip(minimums, weights)]
            widths[-1] += available - sum(widths)
            self.condition_x_scroll.grid_remove()
        for (key, _title, _base, _weight), width in zip(
                self._column_specs, widths):
            self.tree.column(key, width=width)

    def refresh(self) -> None:
        self.group_selector.refresh()
        e = self.ctl.panel_experiment
        selected = self.selected_code()
        counts = {}
        if self.ctl.resolution is not None:
            for row in self.ctl.found_condition_rows():
                if self.ctl.experiment.strain_groups and row.set_key != e.set_key:
                    continue
                counts[row.condition] = counts.get(row.condition, 0) + 1

        self.tree.delete(*self.tree.get_children())
        for c in e.conditions:
            own = c.control_slot
            control = (f"slot {own} ({e.strain(own) or 'empty'})" if own
                       else f"slot {e.control_slot} (shared)"
                       if e.control_slot else "-")
            self.tree.insert(
                "", "end", iid=c.code,
                values=(c.display(), control,
                        ", ".join(str(s) for s in c.exclude) or "-",
                        counts.get(c.code, 0)),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        elif self.tree.get_children():
            self.tree.selection_set(self.tree.get_children()[0])

        found = {r.condition for r in self.ctl.found_condition_rows()}
        missing = found - set(e.condition_codes())
        self.adopt.state(["!disabled"] if missing else ["disabled"])
        self.adopt.configure(
            text=(f"Add the {len(missing)} found in the photos" if missing
                  else "Add the ones found in the photos")
        )
        self._refresh_inspector(counts)

    def selected_code(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    # -- edits ---------------------------------------------------------------

    def _add(self) -> None:
        name = _ask_text(
            self,
            "Add condition",
            "Treatment name (e.g. Glucose or Glucose + H2O2):",
        )
        if not name:
            return
        try:
            code = self.ctl.add_named_condition(name)
            self.on_change()
            self._show(code)
        except ValueError as exc:
            messagebox.showerror("Add condition", str(exc), parent=self)

    def _remove(self) -> None:
        code = self.selected_code()
        if code and self.ctl.remove_condition(code):
            self.on_change()

    def _rename(self) -> None:
        old = self.selected_code()
        if not old:
            return
        condition = self.ctl.experiment.condition(old)
        label = _ask_text(self, "Rename treatment", "Treatment name:", initial=condition.display())
        if label is None:
            return
        try:
            if self.ctl.set_condition_name(old, label):
                self.on_change()
                self._show(old)
        except ValueError as exc:
            messagebox.showerror("Edit condition", str(exc), parent=self)

    def _adopt(self) -> None:
        before = set(self.ctl.experiment.condition_codes())
        if self.ctl.adopt_found_conditions():
            self.on_change()
            added = [c for c in self.ctl.experiment.condition_codes()
                     if c not in before]
            if added:
                self._show(added[-1])

    def _show(self, code: str) -> None:
        """Select a newly-added condition and scroll all of its row into view."""
        if self.tree.exists(code):
            self.tree.selection_set(code)
            self.tree.focus(code)
            self.tree.see(code)

    def _edit(self, event) -> None:
        """Open the editor belonging to the double-clicked table cell."""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        code = self.tree.identify_row(event.y)
        if not code:
            return
        self.tree.selection_set(code)
        self.tree.focus(code)
        column = self.tree.identify_column(event.x)
        if column == "#1":
            self._rename()
        elif column == "#2":
            self._set_control()
        elif column == "#3":
            self._set_exclude()

    def _selection_changed(self, _event=None) -> None:
        self._refresh_inspector()

    def _resize_inspector(self, event) -> None:
        width = max(120, event.width - 28)
        self.condition_name.configure(wraplength=width)
        self.condition_photos.configure(wraplength=width)
        self.exclude_hint.configure(wraplength=width)

    def _refresh_inspector(self, counts: dict[str, int] | None = None) -> None:
        code = self.selected_code()
        if not code:
            self.condition_name.configure(text="No condition selected")
            self.condition_photos.configure(text="Add or select a condition.")
            self.control_picker.configure(values=(), state="disabled")
            self.condition_control.set("")
            for child in self.exclude_frame.winfo_children():
                child.destroy()
            self._exclude_vars.clear()
            return

        e = self.ctl.panel_experiment
        condition = e.condition(code)
        if counts is None:
            counts = {}
            if self.ctl.resolution is not None:
                for row in self.ctl.found_condition_rows():
                    if self.ctl.experiment.strain_groups and row.set_key != e.set_key:
                        continue
                    counts[row.condition] = counts.get(row.condition, 0) + 1
        self.condition_name.configure(text=condition.display())
        photos = counts.get(code, 0)
        self.condition_photos.configure(
            text=f"{photos} photograph{'s' if photos != 1 else ''} found")

        options = [_NONE] + [f"{s}: {e.strain(s)}" for s in e.filled_slots()]
        self.control_picker.configure(values=options, state="readonly")
        if condition.control_slot is None:
            self.condition_control.set(_NONE)
        else:
            self.condition_control.set(
                f"{condition.control_slot}: "
                f"{e.strain(condition.control_slot) or 'empty'}")

        for child in self.exclude_frame.winfo_children():
            child.destroy()
        self._exclude_vars.clear()
        excluded = set(condition.exclude)
        for row, slot in enumerate(e.filled_slots()):
            var = tk.BooleanVar(value=slot in excluded)
            self._exclude_vars[slot] = var
            ttk.Checkbutton(
                self.exclude_frame, text=f"{slot}: {e.strain(slot)}",
                variable=var, command=self._excluded_toggled,
            ).grid(row=row // 2, column=row % 2, sticky="w", padx=(0, 12), pady=2)
        self.exclude_frame.columnconfigure(0, weight=1)
        self.exclude_frame.columnconfigure(1, weight=1)
        self.exclude_canvas.yview_moveto(0)

    def _control_selected(self, _event=None) -> None:
        code = self.selected_code()
        chosen = self.condition_control.get()
        if not code or not chosen:
            return
        slot = None if chosen == _NONE else int(chosen.split(":", 1)[0])
        if self.ctl.set_condition_control(code, slot):
            self.on_change()

    def _excluded_toggled(self) -> None:
        code = self.selected_code()
        if not code:
            return
        slots = [slot for slot, var in self._exclude_vars.items() if var.get()]
        if self.ctl.set_condition_exclude(code, slots):
            self.on_change()

    def _set_control(self) -> None:
        code = self.selected_code()
        if not code:
            return
        e = self.ctl.panel_experiment
        options = [_NONE] + [
            f"{s}: {e.strain(s)}" for s in e.filled_slots()
        ]
        chosen = _ask_choice(self, f"Control for {e.condition(code).display()}",
                             "Which slot is the positive control here?", options)
        if chosen is None:
            return
        slot = None if chosen == _NONE else int(chosen.split(":", 1)[0])
        if self.ctl.set_condition_control(code, slot):
            self.on_change()

    def _set_exclude(self) -> None:
        code = self.selected_code()
        if not code:
            return
        e = self.ctl.panel_experiment
        slot_options = {
            s: f"{s}: {e.strain(s)}" for s in e.filled_slots()
        }
        chosen = _ask_choices(
            self,
            f"Excluded slots for {e.condition(code).display()}",
            "Which slots should be excluded here? Click a slot to toggle it.",
            list(slot_options.values()),
            selected={slot_options[s] for s in e.exclude_for(code)
                      if s in slot_options},
        )
        if chosen is None:
            return
        slots = [int(option.split(":", 1)[0]) for option in chosen]
        if self.ctl.set_condition_exclude(code, slots):
            self.on_change()


def _ask_text(parent, title: str, prompt: str, initial: str = "", *, choices=None) -> str | None:
    """A text prompt that reliably gives its entry keyboard focus on Windows."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)

    ttk.Label(win, text=prompt, padding=(10, 8)).pack(anchor="w")
    value = tk.StringVar(value=initial)
    entry = (ttk.Combobox(win, textvariable=value, values=choices, width=42)
             if choices is not None else ttk.Entry(win, textvariable=value, width=42))
    entry.pack(fill="x", padx=10)

    result: dict[str, str | None] = {"value": None}

    def accept(_event=None) -> None:
        result["value"] = value.get()
        win.destroy()

    def cancel(_event=None) -> None:
        win.destroy()

    buttons = ttk.Frame(win, padding=(10, 8))
    buttons.pack(fill="x")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right")
    ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=4)
    win.bind("<Return>", accept)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)

    def focus_entry() -> None:
        if win.winfo_exists():
            win.lift()
            entry.focus_force()
            entry.selection_range(0, "end")
            entry.icursor("end")

    entry.focus_set()
    win.after_idle(focus_entry)
    win.grab_set()
    parent.winfo_toplevel().wait_window(win)
    return result["value"]


def _ask_choice(parent, title: str, prompt: str, options: list[str]) -> str | None:
    """A small modal list picker; tkinter has no built-in one."""
    chosen = _ask_list(parent, title, prompt, options, multiple=False)
    return chosen[0] if chosen else None


def _ask_choices(parent, title: str, prompt: str, options: list[str], *,
                 selected: set[str] | None = None) -> list[str] | None:
    """The same picker, allowing several slots to be toggled at once."""
    return _ask_list(parent, title, prompt, options, multiple=True,
                     selected=selected)


def _ask_list(parent, title: str, prompt: str, options: list[str], *,
              multiple: bool, selected: set[str] | None = None) -> list[str] | None:
    if not options:
        return None
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    ttk.Label(win, text=prompt, padding=(10, 8)).pack(anchor="w")

    box = tk.Listbox(
        win, height=min(10, len(options)), activestyle="none",
        exportselection=False, selectmode=tk.MULTIPLE if multiple else tk.BROWSE,
    )
    for option in options:
        box.insert("end", option)
    selected = selected or ({options[0]} if not multiple else set())
    for index, option in enumerate(options):
        if option in selected:
            box.selection_set(index)
    box.pack(fill="both", expand=True, padx=10)

    result: dict[str, list[str] | None] = {"value": None}

    def accept(_event=None) -> None:
        result["value"] = [options[index] for index in box.curselection()]
        win.destroy()

    buttons = ttk.Frame(win, padding=(10, 8))
    buttons.pack(fill="x")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right")
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=4)
    if not multiple:
        box.bind("<Double-1>", accept)
    win.bind("<Return>", accept)
    win.bind("<Escape>", lambda _e: win.destroy())

    box.focus_set()
    win.grab_set()
    parent.winfo_toplevel().wait_window(win)
    return result["value"]
