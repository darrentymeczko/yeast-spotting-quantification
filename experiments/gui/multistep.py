"""Configure the additional analysis without changing the endpoint workflow."""

from copy import deepcopy
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from .. import geometry
from ..multistep import METHODS, SCOPES, from_dict, to_dict


class MultiStepDialog(tk.Toplevel):
    def __init__(self, parent, controller, data_flags=None):
        super().__init__(parent)
        self.ctl = controller
        #: The data review's flags (`data_review.flags.DataFlags`), shown so
        #: it is clear which photos and spots the analysis will leave out.
        self.data_flags = data_flags
        self.settings = deepcopy(controller.experiment.multi_step)
        self.title("Additional multi-step analysis")
        self.geometry("1080x720")
        self.minsize(800, 560)
        self.transient(parent)
        self.enabled = tk.BooleanVar(value=self.settings.enabled)
        self.scope = tk.StringVar(value=next(k for k, v in SCOPES.items() if v == self.settings.scope))
        self.method = tk.StringVar(value=next(k for k, v in METHODS.items() if v == self.settings.method))
        self.hours = tk.StringVar(value=", ".join(f"{h:g}" for h in self.settings.hours))
        self.levels = {label: index for index, label in geometry.levels(controller.template)}
        self.dilution = tk.StringVar(value=next((k for k, v in self.levels.items() if v == self.settings.dilution), ""))

        # Reserve the footer FIRST. Pack otherwise gives all available height
        # to the form/table before allocating the buttons on smaller displays.
        buttons = ttk.Frame(self, padding=(12, 8, 12, 12))
        buttons.pack(side="bottom", fill="x")
        self.undo_button = ttk.Button(buttons, text="Undo", command=self.undo)
        self.undo_button.pack(side="left")
        self.redo_button = ttk.Button(buttons, text="Redo", command=self.redo)
        self.redo_button.pack(side="left", padx=6)
        self.save_button = ttk.Button(buttons, text="Save settings", command=self.save)
        self.save_button.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=8)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Checkbutton(body, text="Run additional multi-step analysis and create comparison graphs",
                        variable=self.enabled).pack(anchor="w")
        form = ttk.Frame(body)
        form.pack(fill="x", pady=8)
        form.columnconfigure(1, weight=1)
        for row, (label, variable, values) in enumerate([
            ("Measurements", self.scope, list(SCOPES)),
            ("Analysis", self.method, list(METHODS)),
            ("Dilution", self.dilution, list(self.levels)),
        ]):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=3)
            ttk.Combobox(form, textvariable=variable, values=values, state="readonly").grid(
                row=row, column=1, sticky="ew", pady=3)
        self.rows = list(controller.resolution.rows) if controller.resolution else []
        available = sorted({r.timepoint for r in self.rows if r.timepoint is not None})
        # Every available hour starts ticked unless a selection was saved earlier.
        saved = set(self.settings.hours)
        chosen = [h for h in available if h in saved] if saved else list(available)
        self.hours.set(", ".join(f"{h:g}" for h in chosen))
        ttk.Label(form, text="Hours to include").grid(row=3, column=0, sticky="nw", padx=(0, 12), pady=3)
        hour_box = ttk.Frame(form)
        hour_box.grid(row=3, column=1, sticky="w", pady=3)
        self.hour_checks = {}
        for h in available:
            var = tk.BooleanVar(value=h in chosen)
            var.trace_add("write", lambda *_: self._checks_changed())
            self.hour_checks[h] = var
            ttk.Checkbutton(hour_box, text=f"{h:g}", variable=var).pack(side="left", padx=(0, 10))
        if not available:
            ttk.Label(hour_box, text="Resolve photos on Data first").pack(side="left")
        note = ttk.Label(body, justify="left", wraplength=1000, text=(
            "Technical replicates alone analyse each selected hour separately; repeated measures need at least two hours and analyse them together. "
            "The same dilution is used at every selected hour. Each strain is compared with the control "
            "in its biological block (the template replicate number). Two-step averages technical log ratios "
            "before inference; full hierarchical retains every plate observation. Mixed-model tests are approximate, "
            "especially with few biological replicates. Holm correction covers all tests within each condition.\n\n"
            "Assign A, B, etc. to the physical technical plates. Keep the same label for the same plate at every hour. "
            "Labels are separate within each strain group, condition and template plate position. "
            "A duplicate photo of a plate at the same hour must be excluded. Exclusions here affect only this additional analysis. "
            "Plates and spots flagged in the data review are always left out of it, and need no label."))
        note.pack(fill="x", pady=8)
        body.bind("<Configure>", lambda e: note.configure(wraplength=max(500, e.width - 24)))

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="Assign technical plate...", command=self.assign).pack(side="left")
        ttk.Button(bar, text="Exclude with reason...", command=self.exclude).pack(side="left", padx=6)
        ttk.Button(bar, text="Include", command=self.include).pack(side="left")
        area = ttk.Frame(body)
        area.pack(fill="both", expand=True)
        columns = ("path", "group", "condition", "template", "hour", "physical", "qc")
        self.table = ttk.Treeview(area, columns=columns, show="headings", selectmode="extended")
        for key, label, width in zip(columns,
            ("Photo", "Strain group", "Condition", "Template plate", "Hours", "Technical plate", "Exclusion / status"),
            (310, 95, 90, 90, 65, 100, 210)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=50)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(area, orient="vertical", command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        self.table.bind("<Double-1>", lambda _e: self.assign())
        self.refresh_rows()
        self._undo, self._redo = [], []
        self._restoring = False
        self._last_state = self._state()
        for variable in self._variables():
            variable.trace_add("write", lambda *_: self._record())
        self.bind("<Control-z>", lambda _e: self.undo())
        self.bind("<Control-y>", lambda _e: self.redo())
        self._history_buttons()

    def _checks_changed(self):
        if self._restoring:
            return
        self.hours.set(", ".join(f"{h:g}" for h, v in self.hour_checks.items() if v.get()))

    def _sync_checks(self):
        ticked = {float(h) for h in self.hours.get().split(",") if h.strip()}
        for h, var in self.hour_checks.items():
            var.set(h in ticked)

    def _variables(self):
        return (self.enabled, self.scope, self.method, self.hours, self.dilution)

    def _state(self):
        return deepcopy(self.settings), tuple(v.get() for v in self._variables())

    def _record(self):
        if self._restoring:
            return
        current = self._state()
        if current != self._last_state:
            self._undo.append(self._last_state)
            self._undo = self._undo[-200:]
            self._redo.clear()
            self._last_state = current
        self._history_buttons()

    def _history_buttons(self):
        self.undo_button.configure(state="normal" if self._undo else "disabled")
        self.redo_button.configure(state="normal" if self._redo else "disabled")

    def _restore_history(self, source, destination):
        if source:
            destination.append(self._state())
            settings, values = source.pop()
            self._restoring = True
            try:
                self.settings = deepcopy(settings)
                for var, value in zip(self._variables(), values):
                    var.set(value)
                self._sync_checks()
            finally:
                self._restoring = False
            self._last_state = self._state()
            self.refresh_rows()
            self._history_buttons()
        return "break"

    def undo(self):
        return self._restore_history(self._undo, self._redo)

    def redo(self):
        return self._restore_history(self._redo, self._undo)

    def _review_status(self, relpath: str) -> str:
        flags = self.data_flags
        if flags is None:
            return ""
        plate = flags.plate_reason(relpath)
        if plate:
            return f"data review: plate flagged ({plate})"
        n = len(flags.spots_on(relpath))
        return f"data review: {n} spot(s) flagged" if n else ""

    def refresh_rows(self):
        selected = self.table.selection()
        self.table.delete(*self.table.get_children())
        for r in self.rows:
            status = "; ".join(s for s in (
                self.settings.excluded_photos.get(r.relpath, "")
                or (r.status if not r.is_ok else ""),
                self._review_status(r.relpath)) if s)
            self.table.insert("", "end", iid=r.relpath, values=(
                r.relpath, r.set_key or "", r.condition or "", r.plate or "",
                r.timepoint if r.timepoint is not None else "",
                self.settings.plate_ids.get(r.relpath, ""), status))
        self.table.selection_set(selected)

    def assign(self):
        paths = self.table.selection()
        if not paths:
            return
        value = simpledialog.askstring("Technical plate", "Physical technical plate label for selected photos (e.g. A):\nUse the same label across timepoints.", parent=self,
                                       initialvalue=self.settings.plate_ids.get(paths[0], ""))
        if value is None:
            return
        for path in paths:
            if value.strip():
                self.settings.plate_ids[path] = value.strip()
            else:
                self.settings.plate_ids.pop(path, None)
        self.refresh_rows()
        self._record()

    def exclude(self):
        paths = self.table.selection()
        if not paths:
            return
        reason = simpledialog.askstring("Exclude technical plate observation", "Document the technical failure or reason for exclusion:", parent=self)
        if reason and reason.strip():
            for path in paths:
                self.settings.excluded_photos[path] = reason.strip()
            self.refresh_rows()
            self._record()

    def include(self):
        for path in self.table.selection():
            self.settings.excluded_photos.pop(path, None)
        self.refresh_rows()
        self._record()

    def save(self):
        try:
            raw = to_dict(self.settings)
            raw.update(enabled=self.enabled.get(), scope=SCOPES[self.scope.get()],
                       method=METHODS[self.method.get()], dilution=self.levels.get(self.dilution.get()),
                       hours=[float(h.strip()) for h in self.hours.get().split(",") if h.strip()])
            self.ctl.set_multi_step(from_dict(raw))
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Multi-step analysis", str(exc), parent=self)
            return
        self.destroy()
