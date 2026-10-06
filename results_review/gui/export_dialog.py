"""Choose reviewed outputs and treatments before starting export work."""

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .. import theme
from ..export import (ExportOptions, in_use, in_use_message, replaced_outputs,
                      validate_destination)
from ..review import chosen_candidate


@dataclass(frozen=True)
class ExportRequest:
    options: ExportOptions
    media: list[str]
    outdir: Path


class ExportDialog(tk.Toplevel):
    def __init__(self, parent, run, review, view="fit", rotate=False):
        super().__init__(parent)
        self.withdraw()
        self.title("Export reviewed results")
        self.transient(parent.winfo_toplevel())
        self.result = None
        self.run = run
        self.media = list(run.media)
        self.review = review
        #: The review's view of the sheet, which the slides are laid out in.
        self.view, self.rotate = view, bool(rotate)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        body = ttk.Frame(self, padding=16)
        body.grid(sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        ttk.Label(body, text="Export your chosen photo sets",
                  font=("Segoe UI", 12, "bold")).grid(sticky="w")
        ttk.Label(body, text="Uses ‘Use this candidate’ for each treatment, including your edits.\n"
                  "A preview is not exported until you choose it.").grid(
                      row=1, column=0, sticky="w", pady=(4, 12))

        outputs = ttk.LabelFrame(body, text="Outputs", padding=8)
        outputs.grid(row=2, column=0, sticky="ew")
        labels = (
            ("powerpoint", "PowerPoint (.pptx) — chosen photos + updated graph"),
            ("data", "Per-spot data (.csv)"),
            ("summary", "Chosen photo sets and settings (.csv)"),
            ("statistics", "Statistical results (.csv)"),
            ("figures", "Graphs (.png and .pdf)"),
            ("montages", "Photo montages (.png)"),
        )
        self.outputs = {}
        for row, (key, label) in enumerate(labels):
            var = tk.BooleanVar(self, value=True)
            self.outputs[key] = var
            ttk.Checkbutton(outputs, text=label, variable=var).grid(
                row=row, column=0, sticky="w", pady=2)
        shown = {"fit": "Fit", "aligned": "Aligned"}.get(view, view)
        ttk.Label(outputs, text=f"Slides are laid out as the sheet is shown: "
                  f"{shown}{', graph rotated' if self.rotate else ''}.",
                  foreground=theme.MUTED).grid(row=len(labels), column=0,
                                             sticky="w", pady=(4, 0))
        actions = ttk.Frame(outputs)
        actions.grid(row=len(labels) + 1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(actions, text="PowerPoint only", command=self._deck_only).pack(side="left")
        ttk.Button(actions, text="Select all outputs", command=self._all_outputs).pack(
            side="left", padx=6)

        ttk.Label(body, text="Treatments — all selected initially").grid(
            row=3, column=0, sticky="w", pady=(12, 4))
        selection = ttk.Frame(body)
        selection.grid(row=4, column=0, sticky="nsew")
        selection.columnconfigure(0, weight=1)
        selection.rowconfigure(0, weight=1)
        self.treatments = tk.Listbox(selection, selectmode="multiple",
                                     exportselection=False, height=min(6, max(2, len(self.media))),
                                     width=65)
        self.treatments.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(selection, orient="vertical", command=self.treatments.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.treatments.configure(yscrollcommand=scroll.set)
        for medium in self.media:
            cand = chosen_candidate(run, review, medium)
            detail = (f"{cand.timepoint} / {cand.dilution} / {', '.join(cand.photos)}"
                      if cand else "choose a current candidate first")
            self.treatments.insert("end", f"{run.medium_label(medium)}: {detail}")
        self.treatments.selection_set(0, "end")
        ttk.Label(body, text="Click a treatment to include or exclude it.").grid(
            row=5, column=0, sticky="w", pady=(3, 8))

        destination = ttk.LabelFrame(body, text="Save to folder", padding=8)
        destination.grid(row=6, column=0, sticky="ew")
        destination.columnconfigure(0, weight=1)
        self.destination = tk.StringVar(self, value=str(run.chosen_dir))
        ttk.Entry(destination, textvariable=self.destination).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(destination, text="Browse…", command=self._browse).grid(
            row=0, column=1, padx=(8, 0))
        ttk.Label(body, text="Existing files with matching names will be replaced.\n"
                  "Other files in the folder are kept.").grid(
                      row=7, column=0, sticky="w", pady=8)
        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="left", padx=8)
        ttk.Button(buttons, text="Export", command=self._accept).pack(side="left")
        self.bind("<Escape>", lambda event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _deck_only(self):
        for name, var in self.outputs.items():
            var.set(name == "powerpoint")

    def _all_outputs(self):
        for var in self.outputs.values():
            var.set(True)

    def _browse(self):
        path = filedialog.askdirectory(parent=self, title="Export folder",
                                       initialdir=self.destination.get())
        if path:
            self.destination.set(path)

    def _accept(self):
        options = ExportOptions(**{key: var.get() for key, var in self.outputs.items()},
                                view=self.view, rotate=self.rotate)
        media = [self.media[i] for i in self.treatments.curselection()]
        try:
            if not options.any_selected or not media:
                raise ValueError("Select at least one output and one treatment.")
            if not self.destination.get().strip():
                raise ValueError("Choose an export folder.")
            outdir = validate_destination(self.run, Path(self.destination.get()))
            if outdir.exists() and not outdir.is_dir():
                raise ValueError("The destination must be a folder.")
            # Said now, while the dialog is open, not after the export has run.
            busy = in_use(replaced_outputs(self.run, self.review, options, outdir))
            if busy:
                raise ValueError(in_use_message(busy))
            for medium in media:
                if chosen_candidate(self.run, self.review, medium) is None:
                    raise ValueError("Choose a current candidate for "
                                     f"{self.run.medium_label(medium)} first.")
        except ValueError as exc:
            messagebox.showerror("Export reviewed results", str(exc), parent=self)
            return
        self.result = ExportRequest(options, media, outdir)
        self.destroy()


def ask_export(parent, run, review, view="fit", rotate=False):
    dialog = ExportDialog(parent, run, review, view, rotate)
    dialog.deiconify()
    dialog.grab_set()
    dialog.wait_window()
    return dialog.result
