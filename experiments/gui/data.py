"""Review inferred photo metadata and describe the user's filing system."""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import messagebox, ttk

from uikit import tokens

from ..profiles import FACETS, FacetRule, NamingProfile
from .conditions import _ask_text
from .organization import example_text, reading_choices

_SOURCES = {
    "Not specified": None,
    "Folder / path level": "segment",
    "Filename": "stem",
    "Whole relative path": "path",
    "Treatment suggestions": "treatment",
}
_TRANSFORMS = {"condition": "code", "plate": "int", "timepoint": "hours", "set": "raw"}
_MULTIPLE_GROUPS = "Multiple strain groups - assign photos below"


class DataPanel(ttk.Frame):
    def __init__(self, master, controller, on_change, choose_folder):
        super().__init__(master, padding=(10, 8))
        self.ctl, self.on_change = controller, on_change
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Choose data folder...", command=choose_folder).pack(side="left")
        ttk.Button(bar, text="Detect organization", command=self._detect).pack(side="left", padx=6)
        ttk.Button(bar, text="Add detected conditions", command=self._adopt).pack(side="left")
        self.summary = ttk.Label(self, text="", justify="left", foreground=tokens.TEXT_MUTED, wraplength=850)
        self.summary.pack(fill="x", pady=6)
        self.bind("<Configure>", lambda e: self.summary.configure(wraplength=max(240, e.width - 24)))

        self.organization = ttk.LabelFrame(self, text="Where is each detail written?", padding=8)
        self.organization.pack(fill="x")
        ttk.Label(self.organization, text="Choose how to read each detail. Each choice includes where to look; check the examples below it.",
                  foreground=tokens.TEXT_MUTED).pack(anchor="w", pady=(0, 6))
        simple = ttk.Frame(self.organization)
        simple.pack(fill="x")
        simple.columnconfigure(1, weight=1)
        self.simple_rules = {}
        for row, facet in enumerate(("condition", "timepoint", "plate", "set")):
            title = {"condition": "Treatment", "timepoint": "Time since spotting",
                     "plate": "Plate number", "set": "Strain group"}[facet]
            ttk.Label(simple, text=title).grid(row=row * 2, column=0, sticky="w", padx=(0, 10))
            selected = tk.StringVar()
            choice_box = ttk.Combobox(simple, textvariable=selected, state="readonly", width=60)
            choice_box.grid(row=row * 2, column=1, sticky="ew")
            example = ttk.Label(simple, text="", foreground=tokens.TEXT_MUTED, wraplength=750)
            example.grid(row=row * 2 + 1, column=1, sticky="w", pady=(1, 7))
            self.simple_rules[facet] = {
                "selected": selected, "choices": {},
                "choice_box": choice_box, "example": example,
            }
            choice_box.bind("<<ComboboxSelected>>", lambda _e, f=facet: self._preview_choice(f))
        simple.bind("<Configure>", lambda e: [
            fields["example"].configure(wraplength=max(200, e.width - 170))
            for fields in self.simple_rules.values()])
        actions = ttk.Frame(self.organization)
        actions.pack(fill="x")
        ttk.Button(actions, text="Use these choices", command=self._apply_choices).pack(side="left")
        ttk.Label(actions, text="Updates the photo list below; individual corrections are kept.",
                  foreground=tokens.TEXT_MUTED).pack(side="left", padx=8)
        self.advanced_button = ttk.Button(self.organization, text="Show advanced pattern editor",
                                         command=self._toggle_advanced)
        self.advanced_button.pack(anchor="w", pady=(6, 0))

        settings = self.advanced = ttk.LabelFrame(self.organization, text="Advanced naming patterns", padding=6)
        # Hidden until explicitly requested. Ordinary setup needs no regexes.
        for col, title in enumerate(("Meaning", "Read from", "Level", "Pattern (capture the value in parentheses)")):
            ttk.Label(settings, text=title).grid(row=0, column=col, sticky="w", padx=4)
        settings.columnconfigure(3, weight=1)
        self.rules = {}
        for row, facet in enumerate(FACETS, 1):
            source, depth, pattern = tk.StringVar(), tk.StringVar(), tk.StringVar()
            self.rules[facet] = source, depth, pattern
            ttk.Label(settings, text={"set": "Strain panel / set", "timepoint": "Elapsed hours"}.get(facet, facet.title())).grid(row=row, column=0, sticky="w", padx=4)
            ttk.Combobox(settings, textvariable=source, values=list(_SOURCES), state="readonly", width=23).grid(row=row, column=1, sticky="ew", padx=4)
            ttk.Entry(settings, textvariable=depth, width=5).grid(row=row, column=2, padx=4)
            ttk.Entry(settings, textvariable=pattern).grid(row=row, column=3, sticky="ew", padx=4)
        ttk.Label(settings, text="Level: 0 = first folder below the root; -2 = photo's parent folder; -3 = its parent. Separate alternative patterns with ;;").grid(row=5, column=0, columnspan=4, sticky="w", pady=(5, 0))
        ttk.Button(settings, text="Apply organization", command=self._apply).grid(row=1, column=4, rowspan=2, padx=6)

        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=6)
        ttk.Label(controls, text="Correct selected photos:").pack(side="left")
        for facet, title in (("condition", "Condition"), ("plate", "Plate"), ("timepoint", "Hours"), ("set", "Strain group")):
            ttk.Button(controls, text=title, command=lambda f=facet: self._correct(f)).pack(side="left", padx=2)
        ttk.Button(controls, text="Ignore", command=lambda: self._ignore(True)).pack(side="left", padx=2)
        ttk.Button(controls, text="Include", command=lambda: self._ignore(False)).pack(side="left", padx=2)
        from .groups import add_group
        ttk.Button(controls, text="Add strain group...",
                   command=lambda: add_group(self, self.ctl, self.on_change)).pack(side="right")
        self.group_summary = ttk.Label(self, text="", foreground=tokens.TEXT_MUTED, wraplength=850)
        self.group_summary.pack(fill="x", pady=(0, 4))
        self.bind("<Configure>", lambda e: self.group_summary.configure(
            wraplength=max(240, e.width - 24)), add="+")
        area = ttk.Frame(self)
        area.pack(fill="both", expand=True)
        area.rowconfigure(0, weight=1)
        area.columnconfigure(0, weight=1)
        columns = ("path", "label", "plate", "hours", "set", "status")
        scale = max(1.0, self.winfo_fpixels("1i") / 96)
        ttk.Style().configure("Data.Treeview", rowheight=int(24 * scale))
        self.table = ttk.Treeview(area, columns=columns, show="headings",
                                 selectmode="extended", style="Data.Treeview")
        for key, width, title in zip(columns, (330, 250, 55, 65, 55, 200),
                                     ("Relative photo path", "Treatment", "Plate", "Hours", "Strain group", "Review")):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=45, stretch=key in ("path", "label", "status"))
        scroll = ttk.Scrollbar(area, orient="vertical", command=self.table.yview)
        horizontal = ttk.Scrollbar(area, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self._profile = None
        self._files_signature = None

    def _toggle_advanced(self):
        if self.advanced.winfo_manager():
            self.advanced.pack_forget()
            self.advanced_button.configure(text="Show advanced pattern editor")
        else:
            self.advanced.pack(fill="x", pady=(6, 0))
            self.advanced_button.configure(text="Hide advanced pattern editor")

    def _refresh_choices(self):
        for facet, fields in self.simple_rules.items():
            current = self.ctl.experiment.profile.rule_for(facet)
            choices = reading_choices(self.ctl.files, facet, current,
                                      self.ctl.experiment.profile.aliases)
            if facet == "set":
                if self.ctl.experiment.strain_groups:
                    choices = {label: rule for label, rule in choices.items() if rule is not None}
                choices[_MULTIPLE_GROUPS] = None
            fields["choices"] = choices
            fields["choice_box"].configure(values=list(choices))
            fields["selected"].set(next(label for label, rule in choices.items() if rule == current))
            if facet == "set" and current is None and self.ctl.experiment.strain_groups:
                fields["selected"].set(_MULTIPLE_GROUPS)
            self._preview_choice(facet)

    def _choice_rule(self, facet):
        fields = self.simple_rules[facet]
        return fields["choices"][fields["selected"].get()]

    def _preview_choice(self, facet):
        fields = self.simple_rules[facet]
        if facet == "set" and fields["selected"].get() == _MULTIPLE_GROUPS:
            fields["example"].configure(text="Define each group on Panel, then select photos below and click Strain group to assign them.")
            return
        fields["example"].configure(text=example_text(
            self.ctl.files, self._choice_rule(facet), self.ctl.experiment.profile.aliases))

    def _apply_choices(self):
        with self.ctl.transaction("Change data organization"):
            self._apply_choices_together()

    def _apply_choices_together(self):
        if (self.simple_rules["set"]["selected"].get() == _MULTIPLE_GROUPS
                and not self.ctl.experiment.strain_groups):
            from .groups import add_group
            # Creation refreshes the form; retain the other pending choices.
            pending = {f: v["selected"].get() for f, v in self.simple_rules.items()}
            if not add_group(self, self.ctl, self.on_change):
                return
            for f, label in pending.items():
                self.simple_rules[f]["selected"].set(label)
        rules = [self._choice_rule(facet) for facet in FACETS]
        profile = NamingProfile("Manual organization", tuple(r for r in rules if r),
                                dict(self.ctl.experiment.profile.aliases))
        self.ctl.set_profile(profile)
        self.on_change()

    def refresh(self):
        from ..profiles import profile_to_dict
        profile = self.ctl.experiment.profile
        signature = {**profile_to_dict(profile), "groups": list(self.ctl.experiment.strain_groups)}
        files_signature = tuple(f.relpath for f in self.ctl.files)
        if signature != self._profile or files_signature != self._files_signature:
            self._refresh_choices()
            self._files_signature = files_signature
        if signature != self._profile:
            self._profile = signature
            for facet, (source, depth, pattern) in self.rules.items():
                rule = profile.rule_for(facet)
                source.set(next((label for label, value in _SOURCES.items() if rule and value == rule.source), "Not specified"))
                depth.set(str(rule.depth if rule else -2))
                pattern.set(" ;; ".join(rule.patterns) if rule else r"^(.+)$")
        selected = self.table.selection()
        y = self.table.yview()[0]
        self.table.delete(*self.table.get_children())
        rows = self.ctl.resolution.rows if self.ctl.resolution else []
        for row in rows:
            status = ", ".join(f"needs {v}" for v in row.missing) if row.status == "unresolved" else row.status
            if row.cloud_only:
                status += "; cloud only"
            name = (self.ctl.condition_name(row.condition)
                    if row.condition and self.ctl.experiment.has_condition(row.condition)
                    else row.condition_label)
            self.table.insert("", "end", iid=row.relpath, values=(row.relpath, name, row.plate or "", row.timepoint if row.timepoint is not None else "", row.set_key or "", status))
        self.table.selection_set([key for key in selected if self.table.exists(key)])
        self.table.yview_moveto(y)
        found = len({r.condition for r in self.ctl.found_condition_rows()})
        note = self.ctl.scan_error or ("Review treatment names in Conditions. Use the photo list below for individual corrections."
                                     if rows else "Choose the folder containing this experiment's photographs to begin.")
        self.summary.configure(text=f"{len(rows)} photos found · {found} suggested treatments\n{note}")
        groups = self.ctl.experiment.strain_groups
        self.group_summary.configure(text=(
            "Defined strain groups: " + ", ".join(groups) + ". Edit their strains on Panel; all groups run separately."
            if groups else "One strain panel defined. Use Add strain group if these photos contain another panel."))

    def _detect(self):
        self.ctl.detect_organization()
        self.on_change()

    def _adopt(self):
        self.ctl.adopt_found_conditions()
        self.on_change()

    def _apply(self):
        try:
            rules = []
            for facet, (source, depth, pattern) in self.rules.items():
                value = _SOURCES[source.get()]
                if value:
                    rules.append(FacetRule(facet, value, int(depth.get()) if value == "segment" else -1,
                                           tuple(p.strip() for p in pattern.get().split(";;") if p.strip()), _TRANSFORMS[facet]))
            profile = NamingProfile("Manual organization", tuple(rules), dict(self.ctl.experiment.profile.aliases))
            self.ctl.set_profile(profile)
            self.on_change()
        except ValueError as exc:
            messagebox.showerror("Data organization", str(exc), parent=self)

    def _correct(self, facet):
        paths = self.table.selection()
        if not paths:
            return
        prompt = {"condition": "Choose a treatment, or type a new treatment name:", "plate": "Template plate number (not the biological replicate number):", "timepoint": "Elapsed hours since spotting:", "set": "Choose a defined strain group (add groups on Panel first):"}[facet]
        options = {}
        if facet == "set":
            options["choices"] = tuple(self.ctl.experiment.strain_groups)
        if facet == "condition":
            names = [c.display() for c in self.ctl.experiment.conditions]
            names += [self.ctl.condition_name(r.condition) for r in self.ctl.found_condition_rows()]
            options["choices"] = tuple(dict.fromkeys(names))
        value = _ask_text(self, f"Correct {len(paths)} photo(s)", prompt + "\nLeave blank to return to the naming rule.", **options)
        if value is None:
            return
        value = value.strip()
        try:
            if facet == "set" and value and value not in self.ctl.experiment.strain_groups:
                raise ValueError("Add this strain group on Panel first, including its strains and control.")
            if facet == "condition":
                self.ctl.assign_treatment(paths, value)
                self.on_change()
                return
            if value and facet == "plate":
                value = int(value)
                if value < 1:
                    raise ValueError("Plate numbers start at 1.")
            if value != "" and facet == "timepoint":
                value = float(value)
                if not math.isfinite(value) or value < 0:
                    raise ValueError("Hours must be a finite, nonnegative number.")
            self.ctl.override_many(paths, facet, value)
            self.on_change()
        except ValueError as exc:
            messagebox.showerror("Correct photos", str(exc), parent=self)

    def _ignore(self, ignored):
        if self.table.selection():
            self.ctl.set_ignored(self.table.selection(), ignored)
            self.on_change()

    def _set(self):
        value = _ask_text(self, "Choose strain panel", "Use photos from this set only (blank = all):", initial=self.ctl.experiment.set_key or "")
        if value is not None:
            self.ctl.set_set_key(value.strip())
            self.on_change()
