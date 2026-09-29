"""Experiment Designer -- tkinter entry point.

    py -m experiments.app [experiment.spotexp.json]

A plate template says what the assay looks like. An experiment says who was on
it, what they were grown on, which one is the reference, and where the
photographs are -- everything a photograph cannot tell you.

Four tabs, in the order the decisions actually depend on each other:

    1. Panel       bind a plate template, name each slot, pick the control
    2. Conditions  the media, and any per-condition control override
    3. Photos      point at the folder; confirm or correct what was read
    4. Run         handpicked quantification, or the time course

Validation findings are consolidated in the Run tab's experiment summary.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import FROZEN, REPO
from .gui.conditions import ConditionsPanel
from .gui.controller import ExperimentController
from .gui.panels import HeaderBar, StatusBar
from .gui.plates import PlatePicker
from .gui.strains import StrainPanel
from .model import PHOTO_TOPS, QUANTIFY, TIMECOURSE, Experiment
from .schema import ExperimentError, load, save
from .validate import Severity

APP_TITLE = "Experiment Designer"
FILE_SUFFIX = ".spotexp.json"

_PHOTO_TOP_LABELS = {
    "top": "At the top of the photo",
    "right": "At the right side of the photo",
    "bottom": "At the bottom of the photo",
    "left": "At the left side of the photo",
}

QUICK_START = """\
A plate template describes the SHAPE of the assay. An experiment says what was
actually on it, and where the photographs of it are.

The photo folder is chosen at the top of the window, since both ways of
quantifying need it.

  1. Panel -- choose the plate template you spotted on, then name each sample
     slot. Leave a slot blank if nothing was spotted there. Mark the positive
     control: every strain is reported relative to it, on its own plate.

  2. Conditions -- each medium or treatment. A condition can name its OWN
     control, which is what you want when a strain does not grow on one medium
     and so cannot be the reference there.

  3. Plates -- handpicked quantification only. One row per plate the template
     needs. Select a row, flip through the photographs on the left, and press
     "Use this photograph". Then set the dilution row to score, while looking
     at the plate.

     The dilution is per PLATE, not per treatment. Every spot is compared to
     the control on its own plate, so two plates of one medium that grew
     differently can each be scored at whichever row is actually readable.

     Which photograph is which plate is never guessed here -- you say so. A raw
     camera dump names every file the same thing, and guessing would mislabel
     biological replicates without saying so.

  4. Run -- first say which edge of each photograph is the experiment's top.
     This lets a plate photographed sideways or upside down use the same plate
     template. A time course then scores every timepoint, every re-shot pairing
     and every dilution in the template, then ranks them; it reads the folder
     layout itself and needs no plate picking.

To set up a whole season at once, File > New from several folders: pick the
folders, one template and one set of conditions, then type each panel's
strains. Slots left blank are empty, so the panels may differ in size.

The experiment summary on the Run tab lists everything wrong or worth a look.
Warnings never stop you saving; errors stop a run.
"""

MODE_TEXT = {
    QUANTIFY: (
        "Handpicked quantification\n\n"
        "For photographs you choose by eye. Pick one per plate on the Plates "
        "tab, and set that plate's dilution row there while you can see it."
    ),
    TIMECOURSE: (
        "Time course\n\n"
        "For a whole capture tree. Every timepoint, every pairing of re-shot "
        "photos and all three dilution levels are scored and ranked, and the "
        "least variable candidate is proposed.\n\n"
        "Ranking is on spread across replicates, not on significance: picking "
        "the pairing with the smallest p-values would be selecting on the "
        "outcome."
    ),
}


def _enable_dpi_awareness() -> None:
    """Without this tkinter is blurry on high-DPI Windows. Must run before Tk."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def default_experiment_dir() -> Path:
    # Not "Experiments": Windows paths are case-insensitive, so that folder
    # would collide with the `experiments/` package itself.
    return REPO / "Experiment Designs"


class ExperimentApp:
    def __init__(self, root: tk.Tk, experiment: Experiment,
                 path: Path | None = None) -> None:
        self.root = root
        self.path = path
        self._run_process = None
        self.controller = ExperimentController(experiment, on_change=self.refresh)
        self.controller.load_template(path.parent if path else None)

        root.title(APP_TITLE)
        self._size_window()
        self._build_menu()
        self._build_body()
        self.controller.rescan()
        self.refresh()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- construction --------------------------------------------------------

    def _size_window(self) -> None:
        """Size from the screen, not a fixed pixel count -- the window is
        DPI-aware, so a size that looks right at 100% is cramped at 200%."""
        root = self.root
        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(900, min(1500, int(screen_w * 0.66)))
        height = max(620, min(1050, int(screen_h * 0.80)))
        root.geometry(
            f"{width}x{height}+{max(0, (screen_w - width) // 2)}"
            f"+{max(0, (screen_h - height) // 3)}"
        )
        line = root.winfo_fpixels("1i") / 96 * 22
        root.minsize(int(line * 28), int(line * 18))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="New experiment", accelerator="Ctrl+N",
                              command=self.new_experiment)
        file_menu.add_command(label="New from several folders...",
                              command=self.bulk_create)
        file_menu.add_command(label="Open...", accelerator="Ctrl+O",
                              command=self.open_file)
        file_menu.add_separator()
        file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.save_file)
        file_menu.add_command(label="Save As...", command=self.save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="Import existing configuration...",
                              command=self.import_config)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=False)
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Rename experiment...", command=self.rename)
        menubar.add_cascade(label="Edit", menu=edit_menu)
        self.edit_menu = edit_menu

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="Quick start", command=self.quick_start)
        help_menu.add_command(label="About", command=self._about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.root.config(menu=menubar)
        for sequence, action in (
            ("<Control-n>", self.new_experiment),
            ("<Control-o>", self.open_file),
            ("<Control-s>", self.save_file),
            ("<Control-z>", self.undo),
            ("<Control-y>", self.redo),
        ):
            self.root.bind(sequence, lambda _e, fn=action: self._shortcut(fn))

    def _shortcut(self, action) -> str:
        """Ignore an accelerator while a text field has focus, so Ctrl+Z in an
        entry box undoes typing rather than the whole experiment."""
        widget = self.root.focus_get()
        if isinstance(widget, (ttk.Entry, tk.Entry, tk.Text)):
            return ""
        action()
        return "break"

    def _build_body(self) -> None:
        self.header = HeaderBar(self.root, on_rename=self.rename,
                                on_choose_photos=self.choose_photos,
                                on_rescan=self.reread_photos)
        self.header.pack(fill="x")
        ttk.Separator(self.root).pack(fill="x")

        # Reserve the status bar before giving the notebook all remaining
        # space. Validation details live once, in the Run summary.
        self.status = StatusBar(self.root)
        self.status.pack(side="bottom", fill="x")

        scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        base = tkfont.nametofont("TkDefaultFont")
        ttk.Style().configure(
            "Experiment.TNotebook.Tab",
            padding=(int(20 * scale), int(9 * scale)),
            font=(base.cget("family"), base.cget("size") + 1, "bold"),
        )
        self.notebook = ttk.Notebook(self.root, style="Experiment.TNotebook")
        self.notebook.pack(side="top", fill="both", expand=True,
                           padx=8, pady=(8, 4))

        self.panel = StrainPanel(self.notebook, self.controller, self.refresh)
        self.conditions = ConditionsPanel(self.notebook, self.controller, self.refresh)
        self.plates = PlatePicker(self.notebook, self.controller, self.refresh)
        self.run_tab = self._build_run_tab(self.notebook)

        self.notebook.add(self.panel, text="1. Panel")
        self.notebook.add(self.conditions, text="2. Conditions")
        self.notebook.add(self.plates, text="3. Plates")
        self.notebook.add(self.run_tab, text="4. Run")

    def _sync_tabs(self) -> None:
        """Show the plate picker only where it means something.

        A time course chooses its own photographs -- that is the whole job of
        the pipeline it feeds -- so picking one per plate by hand would be
        contradicting it.
        """
        wanted = self.controller.experiment.mode == QUANTIFY
        # A hidden tab stays in `tabs()`, so visibility has to be read from its
        # state; testing membership would report it as shown forever and it
        # would never come back.
        try:
            hidden = self.notebook.tab(self.plates, "state") == "hidden"
        except tk.TclError:                     # pragma: no cover - not managed
            hidden = True
        if wanted and hidden:
            # `add` on a tab that is merely hidden re-shows it where it was.
            self.notebook.add(self.plates)
            self.notebook.tab(self.plates, text="3. Plates")
        elif not wanted and not hidden:
            self.notebook.hide(self.plates)
        self.notebook.tab(self.run_tab,
                          text="4. Run" if wanted else "3. Run")

    def _build_run_tab(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=(10, 8))
        self.mode = tk.StringVar(value=self.controller.experiment.mode)

        panes = ttk.PanedWindow(frame, orient="horizontal")
        panes.pack(fill="both", expand=True)
        choices = ttk.LabelFrame(panes, text="Quantification mode", padding=(12, 10))
        readiness = ttk.LabelFrame(
            panes, text="Experiment summary", padding=(12, 10))
        panes.add(choices, weight=1)
        panes.add(readiness, weight=1)

        ttk.Label(choices, text="How should these photos be quantified?",
                  font=("", 10, "bold")).pack(anchor="w")
        for value, text in ((TIMECOURSE, "Time course"),
                            (QUANTIFY, "Handpicked quantification")):
            ttk.Radiobutton(choices, text=text, value=value, variable=self.mode,
                            command=self._mode_changed).pack(anchor="w", pady=(6, 0))

        self.mode_text = ttk.Label(choices, text="", foreground="#555",
                                   justify="left", wraplength=640)
        self.mode_text.pack(anchor="w", fill="x", pady=(10, 0))

        orientation = ttk.LabelFrame(
            choices, text="Photo orientation", padding=(10, 8),
        )
        orientation.pack(fill="x", pady=(18, 0))
        ttk.Label(
            orientation,
            text="Where is the experiment's top edge in each photograph?",
            justify="left",
        ).pack(anchor="w", fill="x")
        self.photo_top = tk.StringVar()
        self.photo_top_picker = ttk.Combobox(
            orientation,
            textvariable=self.photo_top,
            values=[_PHOTO_TOP_LABELS[edge] for edge in PHOTO_TOPS],
            state="readonly",
        )
        self.photo_top_picker.pack(fill="x", pady=(5, 0))
        self.photo_top_picker.bind(
            "<<ComboboxSelected>>", self._photo_top_changed,
        )

        self.run_summary = ttk.Label(
            readiness, text="", justify="left", font=("", 10, "bold"),
        )
        self.run_summary.pack(anchor="w", fill="x")

        bar = ttk.Frame(readiness)
        bar.pack(anchor="w", pady=(16, 0))
        self.estimate_button = ttk.Button(bar, text="How long would this take?",
                                          command=self._estimate)
        self.estimate_button.pack(side="left")
        self.run_button = ttk.Button(
            bar, text="Run quantification", command=self._run_quantification,
        )
        self.run_button.pack(side="left", padx=(8, 0))

        ttk.Separator(readiness).pack(fill="x", pady=16)
        self.run_details = ttk.Label(
            readiness, text="", foreground="#555", justify="left",
        )
        self.run_details.pack(anchor="w", fill="x", pady=(5, 0))

        checks = ttk.LabelFrame(readiness, text="Checks", padding=(6, 5))
        checks.pack(fill="both", expand=True, pady=(14, 0))
        self.run_findings = tk.Text(
            checks, height=7, wrap="word", borderwidth=0,
            highlightthickness=0, padx=4, pady=3, cursor="arrow",
        )
        checks_scroll = ttk.Scrollbar(
            checks, orient="vertical", command=self.run_findings.yview)
        self.run_findings.configure(yscrollcommand=checks_scroll.set)
        self.run_findings.pack(side="left", fill="both", expand=True)
        checks_scroll.pack(side="right", fill="y")
        self.run_findings.tag_configure("error", foreground="#b3261e")
        self.run_findings.tag_configure("warning", foreground="#8a6100")
        self.run_findings.tag_configure("info", foreground="#4a4a4a")
        self.run_findings.configure(state="disabled")

        self.run_help = ttk.Label(
            readiness,
            text=("Run quantification saves this experiment and opens a separate "
                  "progress window. It can be left to work while this designer "
                  "stays open; results land in Results/ and open in the review "
                  "tool."),
            foreground="#555", justify="left",
        )
        self.run_help.pack(anchor="w", fill="x", pady=(18, 0))
        choices.bind("<Configure>", self._resize_run_text)
        readiness.bind("<Configure>", self._resize_run_text)
        return frame

    def _resize_run_text(self, event) -> None:
        width = max(160, event.width - 24)
        if event.widget is self.mode_text.master:
            self.mode_text.configure(wraplength=width)
        else:
            self.run_help.configure(wraplength=width)
            self.run_details.configure(wraplength=width)

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> None:
        e = self.controller.experiment
        issues = self.controller.issues()

        marker = "*" if self.controller.dirty else ""
        self.root.title(f"{marker}{e.name} -- {APP_TITLE}")
        filled = len(e.filled_slots())
        self.header.show(
            e.name,
            f"{filled} strain(s)   {len(e.conditions)} condition(s)   "
            f"control slot {e.control_slot or '-'}",
            folder=e.photo_root,
            folder_note=self._folder_note(),
        )

        self._sync_tabs()
        self.panel.refresh()
        self.conditions.refresh()
        self.plates.refresh()
        self._refresh_run_tab(issues)

        self.edit_menu.entryconfigure(
            "Undo", state="normal" if self.controller.undo_stack.can_undo else "disabled")
        self.edit_menu.entryconfigure(
            "Redo", state="normal" if self.controller.undo_stack.can_redo else "disabled")
        self.status.set_detail(str(self.path) if self.path else "not saved yet")

    def _refresh_run_tab(self, issues) -> None:
        e = self.controller.experiment
        if self.mode.get() != e.mode:
            self.mode.set(e.mode)
        orientation = _PHOTO_TOP_LABELS[e.photo_top]
        if self.photo_top.get() != orientation:
            self.photo_top.set(orientation)
        self.mode_text.configure(text=MODE_TEXT.get(e.mode, ""))

        res = self.controller.resolution
        errors = [i for i in issues if i.severity is Severity.ERROR]
        ready = bool(self.controller.files) and not errors

        if not self.controller.files:
            text = ("No photographs read yet -- choose the photo folder at the "
                    "top of the window.")
        elif e.mode == QUANTIFY:
            slots = self.controller.slots()
            chosen = sum(1 for code, plate in slots if e.pick(code, plate))
            lines = [f"{chosen} of {len(slots)} plate(s) chosen."]
            lines.append(
                "Ready to run." if ready
                else f"{len(errors)} thing(s) below must be settled first."
            )
            text = "\n".join(lines)
        else:
            groups = res.by_group() if res else {}
            complete = sum(1 for plates in groups.values() if len(plates) >= 2)
            usable = len(res.usable()) if res else 0
            lines = [
                f"{usable} photo(s) ready, in {complete} complete sitting(s).",
                f"About {complete * 3} candidate(s) would be scored.",
                "Ready to run." if ready
                else f"{len(errors)} thing(s) below must be settled first.",
            ]
            text = "\n".join(lines)

        self.run_summary.configure(text=text)
        template = self.controller.template
        conditions = ", ".join(c.display() for c in e.conditions) or "none"
        control = (f"slot {e.control_slot}: {e.strain(e.control_slot) or 'empty'}"
                   if e.control_slot else "not selected")
        self.run_details.configure(text=(
            f"Panel: {len(e.filled_slots())} of {e.slot_count()} slots filled\n"
            f"Control: {control}\n"
            f"Conditions: {conditions}\n"
            f"Experiment top: {_PHOTO_TOP_LABELS[e.photo_top]}\n"
            f"Template: {template.name if template else 'none selected'}"
        ))
        self._show_run_findings(issues)
        running = (self._run_process is not None
                   and self._run_process.poll() is None)
        button_state = ["!disabled"] if ready and not running else ["disabled"]
        self.estimate_button.state(button_state)
        self.run_button.state(button_state)

    def _show_run_findings(self, issues) -> None:
        self.run_findings.configure(state="normal")
        self.run_findings.delete("1.0", "end")
        if not issues:
            self.run_findings.insert("end", "All checks passed.", "info")
        for issue in issues:
            where = (f"[{issue.condition}] "
                     if getattr(issue, "condition", "") else "")
            prefix = {
                Severity.ERROR: "Error",
                Severity.WARNING: "Check",
                Severity.INFO: "Note",
            }[issue.severity]
            self.run_findings.insert(
                "end", f"{prefix}: {where}{issue.message}\n",
                issue.severity.value,
            )
        self.run_findings.configure(state="disabled")

    # -- actions -------------------------------------------------------------

    def _folder_note(self) -> str:
        """What the photo folder turned out to hold, in a few words."""
        ctl = self.controller
        if ctl.scan_error:
            return ctl.scan_error
        if not ctl.files:
            return ""
        n = len(ctl.files)
        if ctl.experiment.mode == QUANTIFY:
            return f"{n} photograph(s)"
        res = ctl.resolution
        if res is None:
            return f"{n} photograph(s)"
        complete = sum(1 for plates in res.by_group().values() if len(plates) >= 2)
        note = f"{len(res.usable())} of {n} read, {complete} complete sitting(s)"
        missed = len(res.unresolved())
        return note + (f", {missed} unreadable" if missed else "")

    def choose_photos(self) -> None:
        start = self.controller.experiment.photo_root or str(Path.home())
        chosen = filedialog.askdirectory(
            title="Where are the photographs?",
            initialdir=start if Path(start).is_dir() else str(Path.home()),
            parent=self.root,
        )
        if not chosen:
            return
        experiment = self.controller.experiment
        if experiment.picks and Path(chosen) != Path(experiment.photo_root or ""):
            if not messagebox.askokcancel(
                "Change photo folder",
                "The photographs already chosen for each plate refer to the old "
                "folder and will be cleared.\n\nChange the folder anyway?",
                parent=self.root,
            ):
                return
            experiment.picks.clear()
        self.controller.set_photo_root(Path(chosen))
        self.status.say(f"Reading {Path(chosen).name} ...")
        self.refresh()
        self.status.say(self._folder_note() or "No photographs found there")

    def reread_photos(self) -> None:
        if not self.controller.experiment.photo_root:
            self.choose_photos()
            return
        self.controller.rescan(infer=self.controller.experiment.is_timecourse)
        self.refresh()
        self.status.say(self._folder_note() or "No photographs found there")

    def _mode_changed(self) -> None:
        if self.controller.set_mode(self.mode.get()):
            self.status.say(f"Mode set to {self.mode.get()}")
            self.refresh()

    def _photo_top_changed(self, _event=None) -> None:
        selected = self.photo_top.get()
        edge = next(
            (key for key, label in _PHOTO_TOP_LABELS.items()
             if label == selected),
            None,
        )
        if edge and self.controller.set_photo_top(edge):
            self.status.say(f"Experiment top is at the photo's {edge} side")
            self.refresh()

    def _estimate(self) -> None:
        """Ask the pipeline how much work this would be, without doing it."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        self.status.say("Working out how much there is to measure...")
        self.root.update_idletasks()
        output = io.StringIO()
        try:
            from . import run as runner

            # The pipeline's estimate is deliberately reported through stdout
            # for command-line use.  Capture it here because the packaged GUI
            # has no console in which that otherwise-correct answer can appear.
            with redirect_stdout(output), redirect_stderr(output):
                code = runner.run(
                    self.controller.experiment,
                    estimate=True,
                    res=self.controller.resolution,
                    template=self.controller.template,
                )
        except Exception as exc:  # the pipeline is heavy; never take the window down
            detail = output.getvalue().strip()
            message = str(exc) + (f"\n\n{detail}" if detail else "")
            messagebox.showerror("Could not estimate", message, parent=self.root)
            self.status.say("Could not estimate")
            return

        detail = output.getvalue().strip()
        if code == 0:
            messagebox.showinfo(
                "Estimated run time",
                detail or "Everything needed for this run is already cached.",
                parent=self.root,
            )
            self.status.say("Run-time estimate ready")
        else:
            messagebox.showerror(
                "Could not estimate",
                detail or f"The estimator stopped with exit code {code}.",
                parent=self.root,
            )
            self.status.say("Could not estimate")

    def _run_quantification(self) -> None:
        """Save and run this experiment in a separate console process."""
        import os
        import subprocess

        errors = [issue for issue in self.controller.issues()
                  if issue.severity is Severity.ERROR]
        if errors:
            messagebox.showerror(
                "Cannot run quantification",
                "Resolve the errors in Checks before starting quantification.",
                parent=self.root,
            )
            return

        # The command-line runner deliberately consumes the saved artifact,
        # not mutable GUI state.  Saving here guarantees it receives exactly
        # what the summary currently describes.
        if not self.save_file():
            return
        assert self.path is not None

        mode = ("time course" if self.controller.experiment.mode == TIMECOURSE
                else "handpicked quantification")
        if not messagebox.askokcancel(
            "Run quantification",
            f"Start {mode} for {self.controller.experiment.name}?\n\n"
            "A separate progress window will open. You can leave it running "
            "and continue using this window.",
            parent=self.root,
        ):
            return

        path = self.path.resolve()
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        popen_kwargs = {
            "cwd": str(REPO),
            "env": env,
            "close_fds": True,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        if FROZEN:
            # Start the all-in-one program's non-interactive experiment runner.
            # Resetting the PyInstaller environment lets the child outlive this
            # window and unpack its own bundled files safely.
            command = [sys.executable, "experiment-cli", "run", str(path)]
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            env["SPOTTING_NEW_CONSOLE"] = "1"
        else:
            # Go through spotting_app rather than invoking the CLI module
            # directly: it owns console setup and crash reporting on Windows.
            python = self._pipeline_python(env)
            if python is None:
                messagebox.showerror(
                    "Cannot start quantification",
                    "No Python environment containing the scientific packages "
                    "was found. Start the designer with "
                    "run_experiment_designer.bat, or install requirements.txt "
                    "in the Python environment running this window.",
                    parent=self.root,
                )
                self.status.say("Quantification environment not found")
                return
            launcher = REPO / "spotting_app.py"
            command = [str(python), str(launcher),
                       "experiment-cli", "run", str(path)]
            env["SPOTTING_NEW_CONSOLE"] = "1"
            env["EXPERIMENT_GUI_RUN_PAUSE"] = "1"
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE

        try:
            self._run_process = subprocess.Popen(command, **popen_kwargs)
        except OSError as exc:
            self._run_process = None
            messagebox.showerror(
                "Could not start quantification", str(exc), parent=self.root,
            )
            self.status.say("Could not start quantification")
            self.refresh()
            return

        self.status.say("Quantification started in a separate progress window")
        self.refresh()
        self.root.after(1000, self._check_run_process)

    @staticmethod
    def _pipeline_python(env) -> Path | None:
        """Find a source-tree Python that can import the analysis stack.

        Opening the designer needs only Tk, so it is easy to start it with a
        base Python that looks fine until Run imports pandas.  Use the same
        project-environment preference as the launch scripts, but verify each
        candidate before opening the progress console.
        """
        import os
        import subprocess

        roots = [
            Path(env.get("USERPROFILE", "")) / "miniconda3/envs/spotting/python.exe",
            Path(env.get("LOCALAPPDATA", "")) / "miniconda3/envs/spotting/python.exe",
            Path(env.get("USERPROFILE", "")) / "anaconda3/envs/spotting/python.exe",
            Path(sys.executable),
        ]
        candidates = []
        for candidate in roots:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved.is_file() and resolved not in candidates:
                candidates.append(resolved)

        probe = "import numpy, pandas, scipy, numba, skimage, PIL, tifffile"
        for candidate in candidates:
            kwargs = {
                "cwd": str(REPO),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 30,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            try:
                done = subprocess.run([str(candidate), "-c", probe], **kwargs)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.returncode == 0:
                return candidate
        return None

    def _check_run_process(self) -> None:
        """Re-enable Run after the independent quantification process exits."""
        process = self._run_process
        if process is None:
            return
        code = process.poll()
        if code is None:
            self.root.after(1000, self._check_run_process)
            return

        self._run_process = None
        self.refresh()
        if code == 0:
            self.status.say("Quantification finished; results are in Results")
            messagebox.showinfo(
                "Quantification finished",
                "Quantification completed successfully. The results were "
                "written to the Results folder.",
                parent=self.root,
            )
        else:
            self.status.say(f"Quantification stopped with exit code {code}")
            messagebox.showerror(
                "Quantification did not finish",
                f"The quantification process stopped with exit code {code}.\n\n"
                "Review the progress window for details.",
                parent=self.root,
            )

    def rename(self) -> None:
        name = simpledialog.askstring(
            "Rename experiment", "Name (it also names the results folder):",
            parent=self.root, initialvalue=self.controller.experiment.name,
        )
        if name and self.controller.set_name(name):
            self.refresh()

    def new_experiment(self) -> None:
        if not self._confirm_discard():
            return
        self._load(Experiment(), None)

    def bulk_create(self) -> None:
        """Build one experiment per photo folder, sharing template and conditions."""
        from .gui.bulk import BulkCreateDialog

        out_dir = default_experiment_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        dialog = BulkCreateDialog(
            self.root, out_dir,
            template_path=self.controller.experiment.template_path,
            conditions=self.controller.experiment.conditions,
        )
        if not dialog.created:
            return
        self.status.say(f"Created {len(dialog.created)} experiment(s) in "
                        f"{out_dir.name}")
        if self._confirm_discard():
            # Open the first, so the run is not left wondering whether it worked.
            try:
                self._load(load(dialog.created[0]), dialog.created[0])
            except ExperimentError as exc:
                messagebox.showerror("Open experiment", str(exc), parent=self.root)

    def open_file(self) -> None:
        if not self._confirm_discard():
            return
        start = default_experiment_dir()
        chosen = filedialog.askopenfilename(
            title="Open experiment",
            initialdir=str(start if start.is_dir() else Path.cwd()),
            filetypes=[("Experiment", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return
        try:
            experiment = load(Path(chosen))
        except ExperimentError as exc:
            messagebox.showerror("Open experiment", str(exc), parent=self.root)
            return
        self._load(experiment, Path(chosen))

    def _load(self, experiment: Experiment, path: Path | None) -> None:
        self.controller.replace(experiment)
        self.path = path
        self.controller.load_template(path.parent if path else None)
        self.controller.rescan()
        self.refresh()
        self.status.say(f"Opened {path.name}" if path else "New experiment")

    def save_file(self) -> bool:
        if self.path is None:
            return self.save_file_as()
        try:
            save(self.controller.experiment, self.path)
        except OSError as exc:
            messagebox.showerror("Save", str(exc), parent=self.root)
            return False
        self.controller.mark_saved()
        self.status.say(f"Saved {self.path.name}")
        self.refresh()
        return True

    def save_file_as(self) -> bool:
        start = default_experiment_dir()
        start.mkdir(parents=True, exist_ok=True)
        chosen = filedialog.asksaveasfilename(
            title="Save experiment",
            initialdir=str(start),
            initialfile=f"{self.controller.experiment.name}{FILE_SUFFIX}",
            defaultextension=".json",
            filetypes=[("Experiment", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return False
        self.path = Path(chosen)
        return self.save_file()

    def import_config(self) -> None:
        """Build experiments from the config files the pipelines already wrote."""
        if not self._confirm_discard():
            return
        chosen = filedialog.askopenfilename(
            title="Import spotting_config.json or timecourse_config.json",
            filetypes=[("Pipeline configuration", "*.json"), ("All files", "*.*")],
        )
        if not chosen:
            return

        from . import migrate

        path = Path(chosen)
        try:
            if path.name == "timecourse_config.json":
                experiment, notes = migrate.from_capture_tree(path.parent)
                experiments = [experiment]
            else:
                experiments, notes = migrate.from_spotting_config(path)
        except migrate.MigrationError as exc:
            messagebox.showerror("Import", str(exc), parent=self.root)
            return
        if not experiments:
            messagebox.showinfo("Import", "Nothing to import from that file.",
                                parent=self.root)
            return

        self._load(experiments[0], None)
        extra = (f"\n\n{len(experiments) - 1} other experiment(s) are in that file; "
                 f"use the command line to convert them all:\n"
                 f"    py -m experiments.cli migrate \"{path}\""
                 if len(experiments) > 1 else "")
        messagebox.showinfo(
            "Import",
            f"Imported {experiments[0].name}."
            + ("\n\n" + "\n".join(notes) if notes else "")
            + extra,
            parent=self.root,
        )

    def undo(self) -> None:
        label = self.controller.undo()
        self.status.say(f"Undid: {label}" if label else "Nothing to undo")
        self.refresh()

    def redo(self) -> None:
        label = self.controller.redo()
        self.status.say(f"Redid: {label}" if label else "Nothing to redo")
        self.refresh()

    def quick_start(self) -> None:
        _show_text(self.root, "Quick start", QUICK_START)

    def _about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "Binds a plate template to the strains, conditions and photographs "
            "of one experiment, and drives the quantification pipelines with it.",
            parent=self.root,
        )

    # -- closing -------------------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.controller.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes", "Save the current experiment first?",
            parent=self.root)
        if answer is None:
            return False
        if answer:
            return self.save_file()
        return True

    def _on_close(self) -> None:
        if self._confirm_discard():
            self.root.destroy()


def _show_text(parent, title: str, body: str) -> None:
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    text = tk.Text(win, wrap="word", width=84, height=30, padx=12, pady=10,
                   borderwidth=0)
    text.insert("1.0", body)
    text.configure(state="disabled")
    text.pack(fill="both", expand=True)
    ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 10))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="experiments.app", description=APP_TITLE)
    parser.add_argument("experiment", nargs="?",
                        help="an experiment .spotexp.json to open")
    parser.add_argument("--selftest", action="store_true",
                        help="build the window, render once, and exit")
    args = parser.parse_args(argv)

    path: Path | None = None
    experiment = Experiment()
    if args.experiment:
        path = Path(args.experiment)
        try:
            experiment = load(path)
        except ExperimentError as exc:
            print(exc, file=sys.stderr)
            return 2

    _enable_dpi_awareness()
    root = tk.Tk()
    app = ExperimentApp(root, experiment, path)

    if args.selftest:
        root.update_idletasks()
        root.update()
        app.refresh()
        root.destroy()
        print(f"selftest OK: {len(app.controller.issues())} finding(s), "
              f"{len(app.controller.files)} photo(s)")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
