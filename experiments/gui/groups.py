"""One shared group selector for panel, condition and plate editing."""

import tkinter as tk
from tkinter import messagebox, ttk

from .conditions import _ask_text


def add_group(parent, controller, on_change):
    e = controller.experiment
    detected = tuple(dict.fromkeys(r.set_key for r in controller.resolution.rows
                                  if r.set_key)) if controller.resolution else ()
    first = None
    if not e.strain_groups:
        first = _ask_text(parent, "Name the existing strain group",
                          "Name for the strains already entered on Panel.\n"
                          "If read from names, use the exact group shown in Data:",
                          initial=e.set_key or (detected[0] if detected else "Group 1"),
                          choices=detected)
        if first is None or not first.strip():
            return False
    key = _ask_text(parent, "Add strain group",
                    "Name for the additional strain group.\n"
                    "Enter its strains and control on Panel, then assign its photos in Data:",
                    initial=next((k for k in detected if k != first and k not in e.strain_groups), "Group 2" if first else ""),
                    choices=detected)
    if key is None:
        return False
    try:
        controller.add_strain_group(key, first_key=first)
    except ValueError as exc:
        messagebox.showerror("Strain group", str(exc), parent=parent)
        return False
    on_change()
    return True


class GroupSelector(ttk.Frame):
    def __init__(self, master, controller, on_change):
        super().__init__(master)
        self.ctl, self.on_change = controller, on_change
        ttk.Label(self, text="Editing strain group:").pack(side="left")
        self.selected = tk.StringVar()
        self.box = ttk.Combobox(self, textvariable=self.selected, state="readonly", width=26)
        self.box.pack(side="left", padx=6)
        self.box.bind("<<ComboboxSelected>>", self._select)
        ttk.Button(self, text="Add strain group...",
                   command=lambda: add_group(self, controller, on_change)).pack(side="left")

    def refresh(self):
        self.ctl.panel_experiment
        names = list(self.ctl.experiment.strain_groups)
        self.box.configure(values=names or ["One strain group"],
                           state="readonly" if names else "disabled")
        self.selected.set(self.ctl.active_group or "One strain group")

    def _select(self, _event=None):
        self.ctl.select_group(self.selected.get())
        self.on_change()
