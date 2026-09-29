"""Creating many experiments at once, one per photo folder.

Ten sets of the same assay differ only in who is on the plate. Building them one
window at a time means re-choosing the same template and re-typing the same
conditions ten times, and the tenth is where the mistake goes in.

So the shape is: one template and one set of conditions for all of them, and a
grid of strain names with a column per folder. Each column may leave slots
blank, which is a positive statement that nothing was spotted there -- so the
panels can differ in how many strains they carry while still being the same
assay on the same layout.

Nothing is written until Create is pressed, and an existing file is never
overwritten silently.
"""

from __future__ import annotations

import tkinter as tk
import uuid
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import BUNDLE, REPO
from ..model import TIMECOURSE, Condition, Experiment
from ..profiles import capture_tree
from ..schema import save
from .controller import _relative_to_repo

#: Wide enough to read a strain name, narrow enough that several folders fit.
_NAME_WIDTH = 16


class BulkCreateDialog(tk.Toplevel):
    """Pick folders, one template and one condition set, then type each panel."""

    def __init__(self, parent, out_dir: Path, template_path: str = "",
                 conditions: list[Condition] | None = None):
        super().__init__(parent)
        self.title("Create experiments from several folders")
        self.transient(parent.winfo_toplevel())

        self.out_dir = Path(out_dir)
        self.template = None
        self.template_path = template_path
        self.folders: list[Path] = []
        self.conditions = list(conditions or [])
        self.created: list[Path] = []
        self._cells: dict[tuple[int, int], tk.StringVar] = {}
        self._controls: list[tk.IntVar] = []

        body = ttk.Frame(self, padding=(12, 10))
        body.pack(fill="both", expand=True)
        self._build_sources(body)
        self._build_panel(body)
        self._build_buttons(body)

        if self.template_path:
            self._load_template(self.template_path)
        self._refresh()

        self.geometry("980x640")
        self.grab_set()
        parent.winfo_toplevel().wait_window(self)

    # -- construction --------------------------------------------------------

    def _build_sources(self, master) -> None:
        ttk.Label(master,
                  text="One experiment is created per photo folder. They share "
                       "a plate template and a set of conditions.",
                  foreground="#555").pack(anchor="w")

        row = ttk.Frame(master)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="Add folder...", command=self._add_folder).pack(
            side="left")
        ttk.Button(row, text="Add every folder inside...",
                   command=self._add_children).pack(side="left", padx=4)
        ttk.Button(row, text="Remove", command=self._remove_folder).pack(
            side="left", padx=(12, 0))

        lists = ttk.Frame(master)
        lists.pack(fill="x", pady=(6, 0))
        self.folder_list = tk.Listbox(lists, height=4, activestyle="none",
                                      exportselection=False)
        scroll = ttk.Scrollbar(lists, orient="vertical",
                               command=self.folder_list.yview)
        self.folder_list.configure(yscrollcommand=scroll.set)
        self.folder_list.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        row2 = ttk.Frame(master)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="Plate template...",
                   command=self._choose_template).pack(side="left")
        self.template_label = ttk.Label(row2, text="none chosen",
                                        foreground="#555")
        self.template_label.pack(side="left", padx=(8, 0))

        ttk.Button(row2, text="Conditions...",
                   command=self._edit_conditions).pack(side="left", padx=(20, 0))
        self.conditions_label = ttk.Label(row2, text="none", foreground="#555")
        self.conditions_label.pack(side="left", padx=(8, 0))

    def _build_panel(self, master) -> None:
        box = ttk.LabelFrame(master, text="Strains", padding=(8, 6))
        box.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            box,
            text="One column per folder. Leave a slot blank if nothing was "
                 "spotted there, so panels may differ in size.\n"
                 "The radio button under each column marks that panel's "
                 "positive control.",
            foreground="#555", justify="left",
        ).pack(anchor="w", pady=(0, 6))

        # A canvas so a wide grid of folders scrolls rather than forcing the
        # window past the screen.
        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0)
        x_scroll = ttk.Scrollbar(wrap, orient="horizontal",
                                 command=self.canvas.xview)
        y_scroll = ttk.Scrollbar(wrap, orient="vertical",
                                 command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=x_scroll.set,
                              yscrollcommand=y_scroll.set)
        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.pack(side="top", fill="both", expand=True)
        x_scroll.pack(side="bottom", fill="x")
        y_scroll.pack(side="right", fill="y")

    def _build_buttons(self, master) -> None:
        row = ttk.Frame(master)
        row.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(row, text="", foreground="#555")
        self.status.pack(side="left")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right")
        self.create_button = ttk.Button(row, text="Create", command=self._create)
        self.create_button.pack(side="right", padx=4)

    # -- sources -------------------------------------------------------------

    def _add_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Photo folder for one experiment",
                                         parent=self)
        if chosen:
            self._add(Path(chosen))

    def _add_children(self) -> None:
        """Every immediate subfolder, which is how a season of sets is filed."""
        chosen = filedialog.askdirectory(
            title="Folder containing one folder per experiment", parent=self)
        if not chosen:
            return
        try:
            subs = sorted(p for p in Path(chosen).iterdir() if p.is_dir())
        except OSError as exc:
            messagebox.showerror("Add folders", str(exc), parent=self)
            return
        added = sum(1 for sub in subs if not sub.name.startswith(".")
                    and self._add(sub))
        if not added:
            messagebox.showinfo("Add folders",
                                "No new subfolders found there.", parent=self)

    def _add(self, path: Path) -> bool:
        if path in self.folders:
            return False
        self.folders.append(path)
        self._refresh()
        return True

    def _remove_folder(self) -> None:
        sel = self.folder_list.curselection()
        if sel:
            del self.folders[sel[0]]
            self._refresh()

    def _choose_template(self) -> None:
        start = REPO / "Plate Templates"
        if not start.is_dir():
            start = BUNDLE / "plate_template" / "templates"
        chosen = filedialog.askopenfilename(
            title="Choose a plate template", parent=self,
            initialdir=str(start if start.is_dir() else REPO),
            filetypes=[("Plate template", "*.json"), ("All files", "*.*")],
        )
        if chosen:
            self._load_template(chosen)
            self._refresh()

    def _load_template(self, raw: str) -> None:
        from plate_template.schema import TemplateError, load

        path = Path(raw) if Path(raw).is_absolute() else REPO / raw
        if not path.exists() and not Path(raw).is_absolute():
            path = BUNDLE / raw          # a template that ships inside the .exe
        try:
            self.template = load(path)
        except (TemplateError, OSError) as exc:
            self.template = None
            messagebox.showerror("Plate template", str(exc), parent=self)
            return
        self.template_path = _relative_to_repo(path)

    def _edit_conditions(self) -> None:
        current = ", ".join(c.code for c in self.conditions)
        raw = _ask_line(
            self, "Conditions",
            "Every medium or treatment these panels were spotted on,\n"
            "separated by commas (e.g. GLU, GLY, K-OAc):",
            current,
        )
        if raw is None:
            return
        codes = [t.strip() for t in raw.split(",") if t.strip()]
        seen, unique = set(), []
        for code in codes:
            if code not in seen:
                seen.add(code)
                unique.append(code)
        self.conditions = [Condition(code, code) for code in unique]
        self._refresh()

    # -- the grid ------------------------------------------------------------

    def _refresh(self) -> None:
        self.folder_list.delete(0, "end")
        for folder in self.folders:
            self.folder_list.insert("end", str(folder))

        self.template_label.configure(
            text=(f"{self.template.name} ({self.template.sample_slots()} slots)"
                  if self.template is not None else "none chosen"))
        self.conditions_label.configure(
            text=", ".join(c.code for c in self.conditions) or "none")

        self._rebuild_grid()
        self._sync_create()

    def _rebuild_grid(self) -> None:
        previous = {key: var.get() for key, var in self._cells.items()}
        controls = [v.get() for v in self._controls]
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self._cells.clear()
        self._controls.clear()

        if self.template is None or not self.folders:
            ttk.Label(self.grid_frame,
                      text="Choose a plate template and at least one folder.",
                      foreground="#777").grid(row=0, column=0, padx=6, pady=6)
            return

        slots = self.template.sample_slots()
        ttk.Label(self.grid_frame, text="Slot", font=("", 9, "bold")).grid(
            row=0, column=0, padx=(0, 8), sticky="w")
        for col, folder in enumerate(self.folders, start=1):
            ttk.Label(self.grid_frame, text=folder.name, font=("", 9, "bold")).grid(
                row=0, column=col, padx=4, sticky="w")

        for slot in range(1, slots + 1):
            ttk.Label(self.grid_frame, text=str(slot)).grid(
                row=slot, column=0, padx=(0, 8), sticky="w")
            for col in range(1, len(self.folders) + 1):
                var = tk.StringVar(value=previous.get((slot, col), ""))
                ttk.Entry(self.grid_frame, textvariable=var,
                          width=_NAME_WIDTH).grid(row=slot, column=col, padx=4,
                                                  pady=1)
                var.trace_add("write", lambda *_: self._sync_create())
                self._cells[(slot, col)] = var

        ttk.Label(self.grid_frame, text="Control", font=("", 9, "bold")).grid(
            row=slots + 1, column=0, padx=(0, 8), sticky="w", pady=(6, 0))
        for col in range(1, len(self.folders) + 1):
            var = tk.IntVar(value=controls[col - 1] if col <= len(controls) else 1)
            holder = ttk.Frame(self.grid_frame)
            holder.grid(row=slots + 1, column=col, pady=(6, 0))
            for slot in range(1, slots + 1):
                ttk.Radiobutton(holder, text=str(slot), value=slot,
                                variable=var,
                                command=self._sync_create).pack(side="left")
            self._controls.append(var)

    def _sync_create(self) -> None:
        problems = self._problems()
        self.status.configure(text=problems[0] if problems else
                              f"{len(self.folders)} experiment(s) ready")
        self.create_button.state(["disabled"] if problems else ["!disabled"])

    def _problems(self) -> list[str]:
        if not self.folders:
            return ["Add at least one photo folder."]
        if self.template is None:
            return ["Choose a plate template."]
        if not self.conditions:
            return ["Name at least one condition."]
        for col, folder in enumerate(self.folders, start=1):
            names = self._names(col)
            if not any(names):
                return [f"{folder.name}: name at least one strain."]
            control = self._controls[col - 1].get()
            if not (1 <= control <= len(names)) or not names[control - 1]:
                return [f"{folder.name}: its control slot has no strain."]
        return []

    def _names(self, col: int) -> list[str | None]:
        slots = self.template.sample_slots() if self.template else 0
        return [self._cells[(slot, col)].get().strip() or None
                for slot in range(1, slots + 1)]

    # -- creating ------------------------------------------------------------

    def _create(self) -> None:
        if self._problems():
            return
        planned: list[tuple[Path, Experiment]] = []
        for col, folder in enumerate(self.folders, start=1):
            experiment = Experiment(
                name=folder.name,
                template_id=self.template.id,
                template_path=self.template_path,
                strains=self._names(col),
                control_slot=self._controls[col - 1].get(),
                conditions=[Condition(c.code, c.label or c.code)
                            for c in self.conditions],
                mode=TIMECOURSE,
                photo_root=str(folder),
                profile=capture_tree(),
                id=str(uuid.uuid4()),
                created=date.today().isoformat(),
                description="Created with several folders at once",
            )
            planned.append((self.out_dir / f"{folder.name}.spotexp.json",
                            experiment))

        clashes = [path.name for path, _ in planned if path.exists()]
        if clashes:
            shown = ", ".join(clashes[:5])
            more = f" and {len(clashes) - 5} more" if len(clashes) > 5 else ""
            if not messagebox.askokcancel(
                "Already there",
                f"{len(clashes)} file(s) already exist and would be replaced:\n"
                f"{shown}{more}\n\nOverwrite them?",
                parent=self,
            ):
                return

        written, failed = [], []
        for path, experiment in planned:
            try:
                save(experiment, path, bump_revision=False)
                written.append(path)
            except OSError as exc:
                failed.append(f"{path.name}: {exc}")

        self.created = written
        if failed:
            messagebox.showerror(
                "Create experiments",
                f"Wrote {len(written)}; {len(failed)} failed:\n"
                + "\n".join(failed[:5]),
                parent=self,
            )
        self.destroy()


def _ask_line(parent, title: str, prompt: str, initial: str = "") -> str | None:
    """A one-line text prompt parented to the dialog, not to a tab frame."""
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    ttk.Label(win, text=prompt, padding=(12, 10), justify="left").pack(anchor="w")

    var = tk.StringVar(value=initial)
    entry = ttk.Entry(win, textvariable=var, width=48)
    entry.pack(fill="x", padx=12)
    entry.select_range(0, "end")

    result: dict[str, str | None] = {"value": None}

    def accept(_event=None) -> None:
        result["value"] = var.get()
        win.destroy()

    buttons = ttk.Frame(win, padding=(12, 10))
    buttons.pack(fill="x")
    ttk.Button(buttons, text="OK", command=accept).pack(side="right")
    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right",
                                                                 padx=4)
    win.bind("<Return>", accept)
    win.bind("<Escape>", lambda _e: win.destroy())
    entry.focus_set()
    win.grab_set()
    parent.winfo_toplevel().wait_window(win)
    return result["value"]
