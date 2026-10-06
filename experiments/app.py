"""Experiment Designer -- tkinter entry point.

    py -m experiments.app [experiment.spotexp.json]

A plate template says what the assay looks like. An experiment says who was on
it, what they were grown on, which one is the reference, and where the
photographs are -- everything a photograph cannot tell you.

Five tabs, starting with the plate template before importing photographs:

    1. Panel       bind a plate template, name each slot, pick the control
    2. Data        link a folder, review parsing and correct its organization
    3. Conditions  the media, and any per-condition control override
    4. Plates      choose photographs for handpicked quantification
    5. Run         handpicked quantification or the time course, and the
                   statistical tests the figures report

Validation findings are listed when Run quantification is pressed, with the
choice to go ahead anyway.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, simpledialog, ttk

from uikit import dpi, tokens
from uikit.host import JobSpec, StandaloneHost, as_host

from . import FROZEN, REPO
from .gui.conditions import ConditionsPanel
from .gui.data import DataPanel
from .gui.controller import ExperimentController
from .gui.panels import HeaderBar, StatusBar
from .gui.plates import PlatePicker
from .gui.strains import StrainPanel
from .model import (
    DATA_OUTPUT,
    FULL_OUTPUT,
    PHOTO_TOPS,
    QUANTIFY,
    TIMECOURSE,
    Experiment,
)
from .schema import ExperimentError, load, save
from .validate import Severity

APP_TITLE = "Experiment Designer"
FILE_SUFFIX = ".spotexp.json"

#: Interpreters found able to run the pipeline, by the candidates tried.
_PIPELINE_PYTHON: dict[tuple, Path] = {}

_PHOTO_TOP_LABELS = {
    "top": "At the top of the photo",
    "right": "At the right side of the photo",
    "bottom": "At the bottom of the photo",
    "left": "At the left side of the photo",
}

# The review tool's labels (results_review/app.py), so a choice reads the same
# in both places.
TEST_LABELS = {
    "Ratio paired t-tests": "t_test",
    "One-way ANOVA": "anova",
}
CORRECTION_LABELS = {
    "None": "none",
    "Holm": "holm",
    "Bonferroni": "bonferroni",
    "Šidák": "sidak",
}
# After an ANOVA the same box chooses the post-hoc test instead.
POSTHOC_LABELS = {
    "Dunnett": "dunnett",
    "Tukey HSD": "tukey",
    "Holm": "holm",
    "Bonferroni": "bonferroni",
    "Šidák": "sidak",
    "None (omnibus only)": "none",
}
OUTPUT_LABELS = {
    FULL_OUTPUT: "Full: graphs, charts and statistics",
    DATA_OUTPUT: "Data only: raw + normalized grey .csv",
}
COMPARE_LABELS = {
    "control": "Each strain vs the control",
    "references": "Vs the control and chosen strains",
    "all": "Every pair of strains",
}

#: How many findings the pre-run warning lists before summarising the rest.
_FINDINGS_SHOWN = 12

QUICK_START = """\
A plate template describes the SHAPE of the assay. An experiment says what was
actually on it, and where the photographs of it are.

Start on Panel to choose and preview the plate template. Then import the
photographs on Data, selecting the folder for one experiment.

  1. Panel -- choose the plate template, name each sample slot, and mark the
     positive control. Leave a slot blank if nothing was spotted there.
     For multiple strain groups, Add strain group, then select each group here
     and fill its own strains and control. All groups use this plate template.

  2. Data -- linking a folder suggests conditions from folders and filenames.
     Each photo shows the evidence and any missing plate or elapsed hours.
     After reviewing Conditions, return here to specify the organization:
     choose where each detail is written and Use these choices, or correct photos
     together. R1, 1a, and camera counters are not automatically plate numbers.
     Re-read keeps corrections; Detect organization explicitly re-detects rules.
     For multiple groups, assign selected photos with Strain group, or read
     group names using a naming rule. Every photo needs a defined group.

  3. Conditions -- review the suggested treatment names; edit, add or remove
     any errors. Treatments are suggested even when plates/hours are unknown.
     A condition can name its OWN
     control, which is what you want when a strain does not grow on one medium
     and so cannot be the reference there.
     With multiple groups, the group selector chooses whose controls and
     exclusions you are editing; treatment names are shared.

  4. Plates -- handpicked quantification only. One row per plate the template
     needs. Select a row, flip through the photographs on the left, and press
     "Use this photograph". Then set the dilution row to score, while looking
     at the plate.

     The dilution is per PLATE, not per treatment. Every spot is compared to
     the control on its own plate, so two plates of one medium that grew
     differently can each be scored at whichever row is actually readable.

     Which photograph is which plate is never guessed here -- you say so. A raw
     camera dump names every file the same thing, and guessing would mislabel
     biological replicates without saying so.

  5. Run (4 in time-course mode) -- say which edge is the experiment's top.
     This lets a plate photographed sideways or upside down use the same plate
     template. A time course then scores every timepoint, every re-shot pairing
     and every dilution in the template, then ranks them; it reads the folder
     layout itself and needs no plate picking.

     The Statistics box chooses the tests every figure reports: ratio paired
     t-tests (with an optional correction) or a one-way ANOVA (with a post-hoc
     test), the p cutoff, and which strains are compared. The review tool
     opens on the same choices and can still change them.

     Output chooses between full results and data only. Data only skips
     every graph, chart and statistic and writes just the per-spot CSV
     (spotting_results_normalized.csv), with each spot's raw grey value
     (raw_growth) and its value normalized to the control (relative_growth).
     A time course still scores and ranks every candidate; the CSV is for
     each medium's best one, under best/.

To set up a whole season at once, File > New from several folders: pick the
folders, one template and one set of conditions, then type each panel's
strains. Slots left blank are empty, so the panels may differ in size.

Pressing Run quantification lists everything wrong or worth a look and asks
whether to go ahead. Nothing here ever stops you saving.
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
        "photos and every dilution level is scored and ranked, and one "
        "candidate per condition is proposed.\n\n"
        "Ranking weighs both the spread across replicates and how many "
        "strains differ from the control, counted with the test chosen under "
        "Statistics. That is selecting partly on the outcome, so the "
        "proposal is a place to start: check the winner by eye before "
        "reporting it."
    ),
}


def _enable_dpi_awareness() -> None:
    """Without this tkinter is blurry on high-DPI Windows. Must run before Tk."""
    dpi.enable_dpi_awareness()


def default_experiment_dir() -> Path:
    # Not "Experiments": Windows paths are case-insensitive, so that folder
    # would collide with the `experiments/` package itself.
    return REPO / "Experiment Designs"


class ExperimentApp:
    def __init__(self, root, experiment: Experiment,
                 path: Path | None = None) -> None:
        """`root` is a host (`uikit.host`), or a Tk window to stand alone in."""
        self.host = as_host(root, APP_TITLE)
        #: The toplevel, for dialog parents and focus. Hosted in the workbench
        #: it is the workbench's own window, so it is never retitled, resized
        #: or destroyed from here -- all of that goes through `self.host`.
        self.root = self.host.window
        self.path = path
        #: The running quantification (a `uikit.host.JobHandle`), if any.
        self._run_process = None
        self._run_poll: str | None = None
        self.controller =ExperimentController(experiment, on_change=self.refresh)
        self.controller.load_template(path.parent if path else None)

        self.host.set_title(APP_TITLE)
        self._size_window()
        self._build_menu()
        self._build_body()
        self.controller.rescan()
        self.refresh()
        self.host.on_close(self._confirm_discard)
        self.host.on_dispose(self._dispose)
        # For the surrounding program's own toolbar, when there is one.
        for name, action in (("save", self.save_file), ("undo", self.undo),
                             ("redo", self.redo),
                             ("run", self._run_quantification)):
            self.host.add_command(name, action)

    # -- construction --------------------------------------------------------

    def _size_window(self) -> None:
        """Size from the screen, not a fixed pixel count -- the window is
        DPI-aware, so a size that looks right at 100% is cramped at 200%."""
        root = self.root
        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(900, min(1500, int(screen_w * 0.66)))
        height = max(620, min(1050, int(screen_h * 0.80)))
        line = root.winfo_fpixels("1i") / 96 * 22
        self.host.suggest_size(width, height, int(line * 28), int(line * 18))

    def _build_menu(self) -> None:
        file_menu = self.host.add_menu("File")
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
        file_menu.add_command(label=self.host.close_label, command=self.host.close)
        self.file_menu = file_menu

        edit_menu = self.host.add_menu("Edit")
        edit_menu.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        edit_menu.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        edit_menu.add_separator()
        edit_menu.add_command(label="Rename experiment...", command=self.rename)
        self.edit_menu = edit_menu

        help_menu = self.host.add_menu("Help")
        help_menu.add_command(label="Quick start", command=self.quick_start)
        help_menu.add_command(label="About", command=self._about)

        for sequence, action in (
            ("<Control-n>", self.new_experiment),
            ("<Control-o>", self.open_file),
            ("<Control-s>", self.save_file),
            ("<Control-z>", self.undo),
            ("<Control-y>", self.redo),
        ):
            self.host.bind_key(sequence, lambda _e, fn=action: self._shortcut(fn))

    def _shortcut(self, action) -> str:
        """Entries/readonly comboboxes have no Tk undo stack; use ours.

        Only a Text widget with native undo enabled owns its undo shortcut.
        """
        widget = self.root.focus_get()
        if (action in (self.undo, self.redo) and isinstance(widget, tk.Text)
                and widget.cget("undo")):
            return ""
        action()
        return "break"

    def _build_body(self) -> None:
        body = self.host.frame
        self.header = HeaderBar(body, on_rename=self.rename,
                                on_choose_photos=self.choose_photos,
                                on_rescan=self.reread_photos)
        self.header.pack(fill="x")
        ttk.Separator(body).pack(fill="x")

        # Reserve the status bar before giving the notebook all remaining
        # space. Validation details live once, in the Run summary.
        self.status = StatusBar(body)
        self.status.pack(side="bottom", fill="x")

        scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        base = tkfont.nametofont("TkDefaultFont")
        ttk.Style().configure(
            "Experiment.TNotebook.Tab",
            padding=(int(20 * scale), int(9 * scale)),
            font=(base.cget("family"), base.cget("size") + 1, "bold"),
        )
        self.notebook = ttk.Notebook(body, style="Experiment.TNotebook")
        self.notebook.pack(side="top", fill="both", expand=True,
                           padx=8, pady=(8, 4))

        self.panel = StrainPanel(self.notebook, self.controller, self.refresh)
        self.data = DataPanel(self.notebook, self.controller, self.refresh, self.choose_photos)
        self.conditions = ConditionsPanel(self.notebook, self.controller, self.refresh)
        self.plates = PlatePicker(self.notebook, self.controller, self.refresh)
        self.run_tab = self._build_run_tab(self.notebook)

        self.notebook.add(self.panel, text="1. Panel")
        self.notebook.add(self.data, text="2. Data")
        self.notebook.add(self.conditions, text="3. Conditions")
        self.notebook.add(self.plates, text="4. Plates")
        self.notebook.add(self.run_tab, text="5. Run")

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
            self.notebook.tab(self.plates, text="4. Plates")
        elif not wanted and not hidden:
            self.notebook.hide(self.plates)
        self.notebook.tab(self.run_tab,
                          text="5. Run" if wanted else "4. Run")

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

        self.mode_text = ttk.Label(choices, text="", foreground=tokens.TEXT_MUTED,
                                   justify="left", wraplength=640)
        self.mode_text.pack(anchor="w", fill="x", pady=(10, 0))

        orientation = ttk.LabelFrame(
            choices, text="Photo orientation", padding=(10, 8),
        )
        # Controls before the mode's description: on a short window it is the
        # explanation that runs out of room, not a choice that has to be made.
        orientation.pack(fill="x", pady=(12, 0), before=self.mode_text)
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

        output = ttk.LabelFrame(choices, text="Output", padding=(10, 8))
        output.pack(fill="x", pady=(12, 0), before=self.mode_text)
        self.output = tk.StringVar(value=self.controller.experiment.output)
        for value, text in OUTPUT_LABELS.items():
            ttk.Radiobutton(output, text=text, value=value,
                            variable=self.output,
                            command=self._output_changed).pack(anchor="w",
                                                               pady=(2, 0))

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
            readiness, text="", foreground=tokens.TEXT_MUTED, justify="left",
        )
        self.run_details.pack(anchor="w", fill="x", pady=(5, 0))

        review = ttk.LabelFrame(readiness, text="Data review", padding=(10, 8))
        review.pack(fill="x", pady=(14, 0))
        self.data_review_note = ttk.Label(review, text="", justify="left",
                                          wraplength=320)
        self.data_review_note.pack(anchor="w", fill="x")
        ttk.Button(review, text="Review data...",
                   command=self._review_data).pack(anchor="w", pady=(8, 0))

        self._build_statistics(readiness).pack(fill="x", pady=(14, 0))

        where = ("opens a separate progress window" if self.host.standalone
                 else "follows its progress in the Jobs panel")
        self.run_help = ttk.Label(
            readiness,
            text=("Run quantification saves this experiment, lists anything "
                  f"worth a look, then {where}. Results "
                  "land in Results/ and open in the review tool."),
            foreground=tokens.TEXT_MUTED, justify="left", wraplength=320,
        )
        self.run_help.pack(anchor="w", fill="x", pady=(18, 0))
        choices.bind("<Configure>", self._resize_run_text)
        readiness.bind("<Configure>", self._resize_run_text)
        return frame

    def _build_statistics(self, master) -> ttk.LabelFrame:
        """The tests the run's figures report -- the review tool's choices,
        made before the run instead of after it."""
        frame = ttk.LabelFrame(master, text="Statistics", padding=(10, 8))
        frame.columnconfigure(1, weight=1)
        self.stats_frame = frame

        ttk.Label(frame, text="Test:").grid(row=0, column=0, sticky="w", pady=2)
        self.stats_test = tk.StringVar()
        self.test_picker = ttk.Combobox(
            frame, textvariable=self.stats_test, state="readonly", width=16,
            values=tuple(TEST_LABELS))
        self.test_picker.grid(row=0, column=1, columnspan=2, sticky="ew",
                              padx=(8, 0), pady=2)
        self.test_picker.bind("<<ComboboxSelected>>", self._test_changed)

        # One box, two jobs, as in the review tool: the t-tests' correction or
        # the test that follows an ANOVA. `_correction_labels` is the list it
        # is showing, so a label is always read against the right one.
        self.correction_label = ttk.Label(frame, text="Correction:")
        self.correction_label.grid(row=1, column=0, sticky="w", pady=2)
        self.stats_correction = tk.StringVar()
        self._correction_labels: dict[str, str] = dict(CORRECTION_LABELS)
        self.correction_picker = ttk.Combobox(
            frame, textvariable=self.stats_correction, state="readonly",
            width=16)
        self.correction_picker.grid(row=1, column=1, columnspan=2, sticky="ew",
                                    padx=(8, 0), pady=2)
        self.correction_picker.bind("<<ComboboxSelected>>",
                                    self._correction_changed)

        ttk.Label(frame, text="p cutoff:").grid(row=2, column=0, sticky="w",
                                               pady=2)
        self.stats_alpha = tk.StringVar()
        self.alpha_entry = ttk.Entry(frame, textvariable=self.stats_alpha,
                                     width=8)
        self.alpha_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=2)
        self.alpha_entry.bind("<Return>", self._alpha_changed)
        self.alpha_entry.bind("<FocusOut>", self._alpha_changed)

        ttk.Label(frame, text="Compare:").grid(row=3, column=0, sticky="w",
                                              pady=2)
        self.stats_compare = tk.StringVar()
        self.compare_picker = ttk.Combobox(
            frame, textvariable=self.stats_compare, state="readonly", width=16,
            values=tuple(COMPARE_LABELS.values()))
        self.compare_picker.grid(row=3, column=1, sticky="ew", padx=(8, 0),
                                 pady=2)
        self.compare_picker.bind("<<ComboboxSelected>>", self._compare_changed)
        self.references_button = ttk.Button(
            frame, text="Strains...", command=self._choose_references)
        self.references_button.grid(row=3, column=2, sticky="e", padx=(6, 0),
                                    pady=2)

        # A starting wrap, so the unwrapped text does not claim the whole
        # window before `_resize_run_text` has measured the pane.
        self.stats_note = ttk.Label(
            frame, foreground=tokens.TEXT_MUTED, justify="left", wraplength=320,
            text=("Used for every figure this run draws. The review tool "
                  "starts from these and can still change them."),
        )
        self.stats_note.grid(row=4, column=0, columnspan=3, sticky="ew",
                             pady=(6, 0))
        self.multi_step_button = ttk.Button(
            frame, text="Additional multi-step analysis...", command=self._configure_multi_step)
        self.multi_step_button.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 2))
        self.multi_step_note = ttk.Label(frame, text="Off", wraplength=320, justify="left")
        self.multi_step_note.grid(row=6, column=0, columnspan=3, sticky="ew")
        return frame

    def _configure_multi_step(self):
        from .gui.multistep import MultiStepDialog
        MultiStepDialog(self.root, self.controller, self._data_flags())

    def _data_flags(self):
        """This experiment's data review flags (empty when there are none)."""
        from data_review.flags import load_for_experiment
        return load_for_experiment(self.path)

    def _review_data(self) -> None:
        """Open the data review for this experiment: in a tab, or a window.

        It reads the SAVED experiment, so unsaved changes are saved first --
        otherwise it would show photos as they were before this session's
        corrections.
        """
        if self.path is None or self.controller.dirty:
            if not messagebox.askokcancel(
                    "Review data",
                    "The data review reads the saved experiment, so it has to "
                    "be saved first.\n\nSave it now?", parent=self.root):
                return
            if not self.save_file():
                return
        from data_review.flags import sidecar_for

        target = sidecar_for(self.path)
        if self.host.open_document("data-review", target):
            return
        from data_review.app import DataReviewApp

        DataReviewApp(tk.Toplevel(self.root), target)

    def _resize_run_text(self, event) -> None:
        width = max(160, event.width - 24)
        if event.widget is self.mode_text.master:
            self.mode_text.configure(wraplength=width)
        else:
            self.run_help.configure(wraplength=width)
            self.run_details.configure(wraplength=width)
            self.data_review_note.configure(wraplength=max(140, width - 24))
            self.stats_note.configure(wraplength=max(140, width - 24))
            self.multi_step_note.configure(wraplength=max(140, width - 24))

    # -- refresh -------------------------------------------------------------

    def refresh(self) -> None:
        e = self.controller.experiment
        self._review_flags = self._data_flags()
        self.controller.review_plates = {
            path: flag.reason for path, flag in self._review_flags.plates.items()}
        issues = self.controller.issues()

        marker = "*" if self.controller.dirty else ""
        shown = self.path.stem if self.path else e.name
        self.host.set_title(f"{marker}{shown} -- {APP_TITLE}", tab=shown)
        self.host.set_dirty(self.controller.dirty)
        self.host.set_path(self.path)
        filled = len(e.filled_slots())
        self.header.show(
            e.name,
            (f"{len(e.strain_groups)} strain groups   {len(e.conditions)} condition(s)"
             if e.strain_groups else
             f"{filled} strain(s)   {len(e.conditions)} condition(s)   "
             f"control slot {e.control_slot or '-'}"),
            folder=e.photo_root,
            folder_note=self._folder_note(),
        )

        self._sync_tabs()
        self.data.refresh()
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
        if self.output.get() != e.output:
            self.output.set(e.output)
        self.mode_text.configure(text=MODE_TEXT.get(e.mode, ""))

        res = self.controller.resolution
        errors = [i for i in issues if i.severity is Severity.ERROR]
        ready = bool(self.controller.files) and not errors

        not_ready = (f"{len(errors)} error(s) found; Run lists them and asks "
                     f"before starting.")
        if not self.controller.files:
            text = ("No photographs read yet -- choose the photo folder at the "
                    "top of the window.")
        elif e.mode == QUANTIFY:
            slots = self.controller.slots()
            panels = [e.for_group(k) for k in e.strain_groups] if e.strain_groups else [e]
            chosen = sum(1 for panel in panels for code, plate in slots if panel.pick(code, plate))
            lines = [f"{chosen} of {len(slots) * len(panels)} plate(s) chosen."]
            lines.append("Ready to run." if ready else not_ready)
            text = "\n".join(lines)
        else:
            groups = res.by_group() if res else {}
            complete = sum(1 for plates in groups.values() if len(plates) >= 2)
            usable = len(res.usable()) if res else 0
            lines = [
                f"{usable} photo(s) ready, in {complete} complete sitting(s).",
                f"About {complete * 3} candidate(s) would be scored.",
                "Ready to run." if ready else not_ready,
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
        if e.strain_groups:
            panels = [e.for_group(k) for k in e.strain_groups]
            self.run_details.configure(text=(
                "All strain groups will run separately:\n" + "\n".join(
                    f"{p.set_key}: {len(p.filled_slots())} strains; control "
                    f"{p.strain(p.control_slot) if p.control_slot else 'not selected'}"
                    for p in panels) +
                f"\nConditions: {conditions}\nTemplate: {template.name if template else 'none selected'}"))
        self._sync_statistics()
        self._sync_data_review()
        running = (self._run_process is not None
                   and self._run_process.poll() is None)
        self.estimate_button.state(
            ["!disabled"] if ready and not running else ["disabled"])
        # Run stays available with errors: it lists them and asks first.
        self.run_button.state(["disabled"] if running else ["!disabled"])

    def _sync_data_review(self) -> None:
        flags = getattr(self, "_review_flags", None) or self._data_flags()
        if flags.is_empty:
            text = ("Not started. Before running, flip through the photos and "
                    "flag bad plates or single spots.")
        else:
            text = (f"{len(flags.reviewed | set(flags.plates) | set(flags.spots))} "
                    f"photo(s) looked at; {flags.n_plates} plate(s) and "
                    f"{flags.n_spots} spot(s) flagged.")
        self.data_review_note.configure(text=(
            f"{text}\nFlags are left out of the multi-step analysis and marked "
            f"in Review results; the endpoint analysis does not drop them."))

    def _sync_statistics(self) -> None:
        """Show the experiment's statistics in the Statistics box."""
        s = self.controller.experiment.statistics
        multi = self.controller.experiment.multi_step
        self.multi_step_note.configure(text=(
            f"On: {'two-step' if multi.method == 'two_step' else 'full hierarchical'}, "
            f"{'technical plates + time' if multi.scope == 'repeated' else 'technical plates'}; "
            "separate comparison graphs" if multi.enabled else "Off: current analysis only"))
        self.stats_test.set(next(
            label for label, value in TEST_LABELS.items() if value == s.test))
        if s.test == "anova":
            # Dunnett only compares with a reference, so it is not offered
            # while every pair is being compared.
            labels = {k: v for k, v in POSTHOC_LABELS.items()
                      if not (s.all_pairs and v == "dunnett")}
            current = s.posthoc
            self.correction_label.configure(text="Post-hoc:")
        else:
            labels, current = dict(CORRECTION_LABELS), s.p_adjust
            self.correction_label.configure(text="Correction:")
        self._correction_labels = labels
        self.correction_picker.configure(values=tuple(labels))
        self.stats_correction.set(next(
            (label for label, value in labels.items() if value == current),
            next(iter(labels))))
        try:
            typing = self.root.focus_get() is self.alpha_entry
        except (KeyError, tk.TclError):         # a combobox popdown has focus
            typing = False
        if not typing:
            self.stats_alpha.set(f"{s.alpha:g}")
        mode = ("all" if s.all_pairs
                else "references" if s.extra_references else "control")
        self.stats_compare.set(COMPARE_LABELS[mode])
        n = len(s.extra_references)
        self.references_button.configure(
            text=f"Strains ({n})..." if n and not s.all_pairs else "Strains...")
        self.references_button.state(
            ["disabled"] if s.all_pairs else ["!disabled"])

        # A data-only run draws nothing and tests nothing, so the choices are
        # kept but cannot be edited until full results are asked for again.
        data_only = self.controller.experiment.output == DATA_OUTPUT
        for picker in (self.test_picker, self.correction_picker,
                       self.compare_picker):
            picker.state(["disabled"] if data_only else ["!disabled", "readonly"])
        self.alpha_entry.state(["disabled"] if data_only else ["!disabled"])
        if data_only:
            self.references_button.state(["disabled"])
        self.stats_note.configure(text=(
            "Not used: a data-only run makes no graphs or statistics. The "
            "review tool starts from these if you run full results later."
            if data_only else
            "Used for every figure this run draws. The review tool starts "
            "from these and can still change them."))

    def _output_changed(self) -> None:
        if self.controller.set_output(self.output.get()):
            self.status.say(
                "Output: data CSV only" if self.output.get() == DATA_OUTPUT
                else "Output: full results")

    def _set_statistics(self, **changes) -> None:
        try:
            changed = self.controller.set_statistics(**changes)
        except ValueError as exc:
            self.status.say(str(exc))
            changed = False
        if changed:
            self.status.say(
                f"Statistics: {self._statistics_text()}")
        else:
            self._sync_statistics()

    def _statistics_text(self) -> str:
        s = self.controller.experiment.statistics
        test = next(k for k, v in TEST_LABELS.items() if v == s.test)
        if s.test == "anova":
            follow = next(k for k, v in POSTHOC_LABELS.items() if v == s.posthoc)
        else:
            follow = next(k for k, v in CORRECTION_LABELS.items()
                          if v == s.p_adjust)
        compare = ("every pair" if s.all_pairs
                   else "vs control" + (" + " + ", ".join(s.extra_references)
                                        if s.extra_references else ""))
        return f"{test}, {follow}, p < {s.alpha:g}, {compare}"

    def _test_changed(self, _event=None) -> None:
        self._set_statistics(test=TEST_LABELS[self.stats_test.get()])

    def _correction_changed(self, _event=None) -> None:
        value = self._correction_labels.get(self.stats_correction.get())
        if value is None:
            return
        if self.controller.experiment.statistics.test == "anova":
            self._set_statistics(posthoc=value)
        else:
            self._set_statistics(p_adjust=value)

    def _alpha_changed(self, _event=None) -> None:
        current = self.controller.experiment.statistics.alpha
        try:
            alpha = float(self.stats_alpha.get())
            if not 0 < alpha < 1:
                raise ValueError
        except ValueError:
            self.status.say("p cutoff must be a number between 0 and 1")
            self.stats_alpha.set(f"{current:g}")
            return
        if alpha != current:
            self._set_statistics(alpha=alpha)

    def _compare_changed(self, _event=None) -> None:
        mode = next((k for k, v in COMPARE_LABELS.items()
                     if v == self.stats_compare.get()), "control")
        if mode == "all":
            self._set_statistics(all_pairs=True)
        elif mode == "control":
            self._set_statistics(all_pairs=False, extra_references=())
        else:
            self._choose_references()

    def _choose_references(self) -> None:
        """Pick the strains every other strain is also compared with."""
        from .gui.conditions import _ask_choices

        e = self.controller.experiment
        panels = [e.for_group(k) for k in e.strain_groups] if e.strain_groups else [e]
        names = list(dict.fromkeys(
            p.strain(s) for p in panels for s in p.filled_slots()
            if s != p.control_slot))
        if not names:
            self.status.say("Name the strains on the Panel tab first")
            self._sync_statistics()
            return
        chosen = _ask_choices(
            self.root, "Comparison strains",
            "Every strain is compared with the control. Also compare every "
            "strain with:", names,
            selected=set(e.statistics.extra_references) & set(names),
        )
        if chosen is None:
            self._sync_statistics()
            return
        self._set_statistics(all_pairs=False, extra_references=tuple(chosen))

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
        self.controller.set_photo_root(Path(chosen), adopt_conditions=True)
        self.status.say(f"Reading {Path(chosen).name} ...")
        self.refresh()
        self.notebook.select(self.data)
        self.status.say(self._folder_note() or "No photographs found there")

    def reread_photos(self) -> None:
        if not self.controller.experiment.photo_root:
            self.choose_photos()
            return
        self.controller.rescan()
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
        """Ask the pipeline how much work this would be, without doing it.

        In a worker process, waited for on a thread: the first estimate
        imports the whole measurement stack, which on the window's own thread
        froze the window -- and in the workbench, every other tab with it --
        and on a thread of this process still made all of it lag.
        """
        from copy import deepcopy

        from uikit.tasks import run_in_thread, worker

        self.status.say("Working out how much there is to measure...")
        self.estimate_button.state(["disabled"])
        experiment = deepcopy(self.controller.experiment)
        res, template = self.controller.resolution, self.controller.template

        def work():
            # Kept a while for the next estimate, then let go.
            return worker("experiments", idle_s=ESTIMATE_IDLE_S).call(
                estimate_report, experiment, res, template)

        def done(result, error) -> None:
            self.estimate_button.state(["!disabled"])
            code, detail, exc = result if result else (None, "", error)
            self._estimate_finished(code, detail, exc or error)

        run_in_thread(self.notebook, work, done)

    def _estimate_finished(self, code, detail: str, exc) -> None:
        if exc is not None:  # the pipeline is heavy; never take the window down
            message = str(exc) + (f"\n\n{detail}" if detail else "")
            messagebox.showerror("Could not estimate", message, parent=self.root)
            self.status.say("Could not estimate")
            return

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
        """Save and run this experiment as a separate process.

        Alone, it gets a console window of its own; in the workbench, its
        output streams into the Jobs panel. Either way the host starts it --
        this only decides WHAT to run.
        """
        import os

        findings = [issue for issue in self.controller.issues()
                    if issue.severity is not Severity.INFO]
        errors = [issue for issue in findings if issue.is_error]
        mode = ("time course" if self.controller.experiment.mode == TIMECOURSE
                else "handpicked quantification")
        if self.controller.experiment.output == DATA_OUTPUT:
            mode += " (data CSV only)"
        if findings and not self._confirm_findings(findings, errors, mode):
            return

        # The command-line runner deliberately consumes the saved artifact,
        # not mutable GUI state.  Saving here guarantees it receives exactly
        # what the summary currently describes.
        if not self.save_file():
            return
        assert self.path is not None

        where = ("A separate progress window will open." if self.host.standalone
                 else "Its progress is shown in the Jobs panel.")
        if not findings and not messagebox.askokcancel(
            "Run quantification",
            f"Start {mode} for {self.controller.experiment.name}?\n\n"
            f"{where} You can leave it running and continue using this "
            "window.",
            parent=self.root,
        ):
            return

        path = self.path.resolve()
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        #: Only for the console presentation: a console of its own, and a pause
        #: at the end so the last lines can be read before it closes.
        console_env = {"SPOTTING_NEW_CONSOLE": "1"}
        new_console = False

        if FROZEN:
            # Start the all-in-one program's non-interactive experiment runner.
            # Resetting the PyInstaller environment lets the child outlive this
            # window and unpack its own bundled files safely.
            command = [sys.executable, "experiment-cli", "run", str(path)]
            if errors:
                command.append("--force")
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
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
            if errors:
                # Already confirmed above; without this the runner would
                # refuse the same errors again in its own window.
                command.append("--force")
            console_env["EXPERIMENT_GUI_RUN_PAUSE"] = "1"
            new_console = True

        spec = JobSpec(
            title=f"{self.controller.experiment.name} — {mode}",
            argv=command, cwd=str(REPO), env=env, kind="experiment-run",
            console_env=console_env, new_console=new_console,
            # The progress window stays open until Enter is pressed, so its
            # exit is not when the run finished. The runner writes its exit
            # code to the file this names first, and that reports the result.
            status_env="EXPERIMENT_GUI_RUN_STATUS",
            meta={"experiment": str(path)},
        )
        try:
            self._run_process = self.host.run_job(spec)
        except OSError as exc:
            self._run_process = None
            messagebox.showerror(
                "Could not start quantification", str(exc), parent=self.root,
            )
            self.status.say("Could not start quantification")
            self.refresh()
            return

        self.status.say("Quantification started in a separate progress window"
                        if self.host.standalone
                        else "Quantification started; see the Jobs panel")
        self.refresh()
        self._schedule_run_check()

    @staticmethod
    def findings_lines(issues, condition_names=None) -> list[str]:
        """One readable line per finding, worst first."""
        prefix = {
            Severity.ERROR: "Error",
            Severity.WARNING: "Check",
            Severity.INFO: "Note",
        }
        lines = []
        condition_names = condition_names or {}
        for issue in issues:
            where = (f"[{condition_names.get(issue.condition, issue.condition)}] "
                     if getattr(issue, "condition", "") else "")
            lines.append(f"{prefix[issue.severity]}: {where}{issue.message}")
        return lines

    def _confirm_findings(self, findings, errors, mode: str) -> bool:
        """List what is wrong or worth a look, and ask whether to run anyway."""
        lines = self.findings_lines(findings, {
            c.code: c.display() for c in self.controller.experiment.conditions})
        shown = "\n".join(f"• {line}" for line in lines[:_FINDINGS_SHOWN])
        if len(lines) > _FINDINGS_SHOWN:
            shown += f"\n• ... and {len(lines) - _FINDINGS_SHOWN} more"
        if errors:
            head = (f"{len(errors)} error(s) and "
                    f"{len(findings) - len(errors)} other check(s) were found:")
            tail = ("Errors usually mean the run will fail or give wrong "
                    "results.\n\nAre you sure you want to start "
                    f"{mode} anyway?")
        else:
            head = f"{len(findings)} check(s) are worth a look:"
            tail = f"Are you sure you want to start {mode}?"
        return messagebox.askyesno(
            "Run quantification?",
            f"{head}\n\n{shown}\n\n{tail}",
            icon="error" if errors else "warning",
            default="no",
            parent=self.root,
        )

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

        # Probing imports the whole science stack in a fresh interpreter --
        # seconds each time, on the window's own thread. An interpreter that
        # passed once passes again, so remember it for the life of the process.
        key = tuple(candidates)
        if key in _PIPELINE_PYTHON:
            return _PIPELINE_PYTHON[key]

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
                _PIPELINE_PYTHON[key] = candidate
                return candidate
        # A failure is not remembered: installing the packages and pressing
        # Run again has to work without restarting the window.
        return None

    def _schedule_run_check(self) -> None:
        # On a widget this tool owns, not the window: hosted in the workbench
        # the window outlives this tab, and a timer on it would go on calling
        # into widgets that no longer exist.
        self._run_poll = self.notebook.after(1000, self._check_run_process)

    def _check_run_process(self) -> None:
        """Report the result and re-enable Run once quantification finishes.

        Finishing is when the runner reports its exit code, not when its
        progress window is closed; the job handle reads that report first.
        """
        self._run_poll = None
        process = self._run_process
        if process is None:
            return
        code = process.poll()
        if code is None:
            self._schedule_run_check()
            return

        self._run_process = None
        self.refresh()
        where = ("the progress window" if self.host.standalone
                 else "the job's log in the Jobs panel")
        if code == 0:
            self.status.say("Quantification finished; results are in Results")
            self.host.notify(
                "Quantification finished",
                "Quantification completed successfully. The results were "
                "written to the Results folder.",
            )
        else:
            self.status.say(f"Quantification stopped with exit code {code}")
            self.host.notify(
                "Quantification did not finish",
                f"The quantification process stopped with exit code {code}.\n\n"
                f"Review {where} for details.",
                kind="error",
            )

    def _dispose(self) -> None:
        """This tool is going away: stop watching the run. The run itself
        carries on -- it is a separate process, and its results still land."""
        if self._run_poll is not None:
            try:
                self.notebook.after_cancel(self._run_poll)
            except tk.TclError:
                pass
            self._run_poll = None

    def rename(self) -> None:
        name = simpledialog.askstring(
            "Rename experiment", "Name (it also names the results folder):",
            parent=self.root, initialvalue=self.controller.experiment.name,
        )
        if name and self.controller.set_name(name):
            self.refresh()

    def new_experiment(self) -> None:
        # In a tab of its own where the host has tabs; otherwise in place of
        # this one, which first means asking about unsaved changes.
        if self.host.open_document("experiment"):
            return
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
        if self.host.open_document("experiment", dialog.created[0]):
            return
        if self._confirm_discard():
            # Open the first, so the run is not left wondering whether it worked.
            try:
                self._load(load(dialog.created[0]), dialog.created[0])
            except ExperimentError as exc:
                messagebox.showerror("Open experiment", str(exc), parent=self.root)

    def open_file(self) -> None:
        # Alone, opening replaces what is here, so ask about it first. In the
        # workbench it opens beside it and there is nothing to discard.
        if self.host.standalone and not self._confirm_discard():
            return
        start = default_experiment_dir()
        chosen = filedialog.askopenfilename(
            title="Open experiment",
            initialdir=str(start if start.is_dir() else Path.cwd()),
            filetypes=[("Experiment", "*.json"), ("All files", "*.*")],
            parent=self.root,
        )
        if not chosen:
            return
        if self.host.open_document("experiment", Path(chosen)):
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
        experiment = self.controller.experiment
        name = experiment.name
        # The file dialog names the file, while runs use the name stored inside
        # it. Give unnamed experiments that same name, including older files
        # saved with the placeholder; preserve names chosen through Rename.
        if not name.strip() or name.strip().casefold() == "untitled":
            name = (self.path.name[:-len(FILE_SUFFIX)]
                    if self.path.name.lower().endswith(FILE_SUFFIX)
                    else self.path.stem).strip() or "Untitled"
        saved = replace(experiment, name=name)
        try:
            save(saved, self.path)
        except OSError as exc:
            messagebox.showerror("Save", str(exc), parent=self.root)
            return False
        self.controller.set_name(saved.name)
        experiment.revision = saved.revision
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
            parent=self.root,
        )
        if not chosen:
            return False
        previous_path = self.path
        self.path = Path(chosen)
        if self.save_file():
            return True
        self.path = previous_path
        return False

    def import_config(self) -> None:
        """Build experiments from the config files the pipelines already wrote."""
        if self.host.standalone and not self._confirm_discard():
            return
        chosen = filedialog.askopenfilename(
            title="Import spotting_config.json or timecourse_config.json",
            filetypes=[("Pipeline configuration", "*.json"), ("All files", "*.*")],
            parent=self.root,
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

        if not self.host.open_document("experiment", None, experiments[0]):
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


#: Seconds the estimate's worker process is kept with nothing to do.
ESTIMATE_IDLE_S = 600


def estimate_report(experiment, res, template) -> tuple:
    """The pipeline's run-time estimate: (exit code, what it printed, error).

    At module level so that it can run in a worker process.
    """
    from uikit.tasks import capture_output

    # The pipeline's estimate is deliberately reported through stdout for
    # command-line use. Capture it from this thread only, because the
    # packaged GUI has no console for it to appear in.
    with capture_output() as output:
        try:
            from . import run as runner

            code = runner.run(experiment, estimate=True, res=res,
                              template=template)
        except Exception as exc:
            return None, output.getvalue().strip(), exc
    return code, output.getvalue().strip(), None


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
    from uikit import tasks

    tasks.favour_the_window()
    root = tk.Tk()
    app = ExperimentApp(StandaloneHost(root, APP_TITLE), experiment, path)

    if args.selftest:
        root.update_idletasks()
        root.update()
        app.refresh()
        root.destroy()
        print(f"selftest OK: {len(app.controller.issues())} finding(s), "
              f"{len(app.controller.files)} photo(s)")
        return 0

    try:
        root.mainloop()
    finally:
        tasks.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
