"""Review timecourse results -- tkinter entry point.

    py -m results_review [<Results/Timecourse/SetNN>]

Opens a set chooser when no folder is given. Browsing needs nothing but the
results folder; correcting spots needs the photographs, and the window says so
plainly instead of failing at the moment you try.

Rebuilding, redrawing and exporting run in worker processes (`background`),
waited for on a thread. Processing full-resolution photos and drawing figures
must not block the window's event loop -- and on a thread of this process,
computing does: it holds the interpreter lock the window needs.
"""

from __future__ import annotations

import argparse
import queue
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from uikit import tasks
from uikit.host import StandaloneHost, as_host
from uikit.theme import apply_theme

from . import (PROJECT_ROOT, background, datareview, discovery, links,
               review as rv, theme)
from .discovery import SetRun
from .gui.browser import SheetView
from .gui.controller import ReviewController
from .gui.panels import CandidateList, HeaderBar, StatusBar
from .gui.table import SpotTable

APP_TITLE = "Spotting Results Review"
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


class ReviewApp(ttk.Frame):
    """The review tool, built into a host (`uikit.host`).

    A frame rather than the window itself, so the same tool can stand alone
    in a window of its own (`main`) or be one tab of the workbench. It never
    retitles, resizes or destroys the window it is in; it asks the host.
    """

    def __init__(self, master_or_host, run: SetRun) -> None:
        self.host = as_host(master_or_host, APP_TITLE)
        super().__init__(self.host.frame)
        self.pack(fill="both", expand=True)
        self.host.set_title(APP_TITLE)
        theme.set_scale(self)
        # Wide enough that the spot table and the strain summary both fit
        # beside each other at the default sash, rather than starting clipped
        # and needing a drag before the tool is readable.
        self.host.suggest_size(theme.px(1440), theme.px(900),
                               theme.px(900), theme.px(600), center=False)

        self.ctl = ReviewController(run, rv.load_for(run),
                                    on_change=self._changed,
                                    rebuilder=background.rebuild)
        # The worker processes start while the window is drawn, rather than
        # when the first rebuild is already waiting for them.
        background.warm()
        self._jobs: "queue.Queue" = queue.Queue()
        #: The `_pump` timer, cancelled on the way out. Hosted as a tab, the
        #: window outlives this tool, and a pump left running would drain a
        #: queue into widgets that no longer exist.
        self._pump_id: str | None = None
        self._closed = False
        self._busy = False
        self._preview = None          # candidate being previewed, not yet chosen
        self._graph_dir: "Path | None" = None   # scratch for redrawn graphs
        self._redrawn_sheets: dict[tuple, Path] = {}
        self._stale_media: set[str] = set()
        # Strain summaries by (frame, statistics): {key: (frame, rows)}. The
        # frame is kept and compared by identity, so a recycled id() can
        # never serve another frame's numbers.
        self._summaries: dict = {}
        self._summarizing: set = set()

        self._build()
        self._bind_keys()
        self._show_set()
        self._pump_id = self.after(60, self._pump)
        self.host.on_close(self._can_close)
        self.host.on_dispose(self._dispose)
        # Any other Tk callback that raises: visible, logged, not silent.
        self.host.set_error_handler(
            lambda error: self._job_failed(error, release=False))
        # For the surrounding program's own toolbar, when there is one.
        for name, action in (("save", self._save), ("undo", self._undo),
                             ("redo", self._redo)):
            self.host.add_command(name, action)
        # Insist on being visible. A GUI started from a console that is itself
        # minimised or background inherits that show state through STARTUPINFO
        # and opens minimised -- the window is running and reachable only from
        # the taskbar, which from the outside is a launcher that did nothing.
        self.host.present()

        # Draw the window NOW, and only then go and read photographs. The first
        # rebuild imports the measurement engine and walks the capture tree; on
        # a cold OneDrive folder that is long enough for a window that has not
        # appeared yet to look like a launcher that has failed.
        self.update_idletasks()
        self.after_idle(self._rebuild_current)

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        self.header = HeaderBar(self, on_open_set=self._open_set,
                                on_link=self._locate_photos,
                                on_step_set=self._step_set)
        # Reclaim a few pixels from the generous gaps around the workspace.
        # Together these small reductions add exactly one complete visible row
        # to all three tables at the default window size.
        self.header.grid(row=0, column=0, sticky="ew", pady=(theme.GAP, 0))

        tabs = ttk.Frame(self)
        tabs.grid(row=1, column=0, sticky="ew", padx=theme.PAD,
                  pady=(theme.GAP // 2, 0))
        tabs.columnconfigure(1, weight=1)
        # Keep the medium buttons and actions visible above the statistics.
        tabs.rowconfigure(0, minsize=theme.px(44))
        self._medium_var = tk.StringVar(value=self.ctl.medium)
        self._tabs = ttk.Frame(tabs)
        self._tabs.grid(row=0, column=0, sticky="w")

        # Statistics need their own row: sharing the medium/action row pushes
        # the recompute button off the right edge in a workbench tab.
        self.stats = ttk.Frame(tabs)
        self.stats.grid(row=1, column=0, columnspan=6, sticky="w",
                        pady=(0, theme.GAP))
        ttk.Label(self.stats, text="Statistics:", font=theme.FONT_SMALL).grid(
            row=0, column=0, padx=(theme.GAP, theme.PAD), pady=2)
        ttk.Label(self.stats, text="Test:", font=theme.FONT_SMALL).grid(
            row=0, column=1, padx=(0, 2), pady=2)
        self._test_var = tk.StringVar()
        self.test_choice = ttk.Combobox(
            self.stats, textvariable=self._test_var, state="readonly", width=22,
            values=tuple(TEST_LABELS))
        self.test_choice.grid(row=0, column=2, padx=(0, theme.PAD), pady=2)
        self.test_choice.bind("<<ComboboxSelected>>", self._statistics_changed)
        # One box, two jobs: the t-tests' multiple-comparison correction, or
        # the test that follows an ANOVA. `_choice_mode` records which list it
        # is showing, so a test switch never reads one list's label as the
        # other's ("Holm" is in both).
        self._correction_label = ttk.Label(self.stats, text="Correction:",
                                           font=theme.FONT_SMALL)
        self._correction_label.grid(row=0, column=3, padx=(0, 2), pady=2)
        self._correction_var = tk.StringVar()
        self._choice_mode = "t_test"
        self.correction_choice = ttk.Combobox(
            self.stats, textvariable=self._correction_var, state="readonly",
            width=18,
            values=tuple(CORRECTION_LABELS))
        self.correction_choice.grid(row=0, column=4, pady=2)
        self.correction_choice.bind("<<ComboboxSelected>>",
                                    self._statistics_changed)
        self.btn_comparisons = ttk.Button(self.stats, text="Comparisons…",
                                          command=self._choose_comparisons)
        self.btn_comparisons.grid(row=0, column=5, padx=(theme.PAD, 0), pady=2)
        ttk.Label(self.stats, text="p cutoff:", font=theme.FONT_SMALL).grid(
            row=0, column=6, padx=(theme.PAD, 2), pady=2)
        self._alpha_var = tk.StringVar(value="0.05")
        self.alpha_entry = ttk.Entry(self.stats, textvariable=self._alpha_var,
                                     width=6)
        self.alpha_entry.grid(row=0, column=7, pady=2)
        self.alpha_entry.bind("<Return>", self._statistics_changed)
        self.alpha_entry.bind("<FocusOut>", self._statistics_changed)

        self.btn_redraw = ttk.Button(tabs,
                                     text="↻ Recompute data — out of date",
                                     command=self._redraw_graph)
        self.btn_redraw.grid(row=0, column=3, padx=(0, theme.GAP))
        self.btn_locate_photos = ttk.Button(
            tabs, text="Locate photos…", command=self._locate_photos)
        self.btn_locate_photos.grid(row=0, column=4,
                                    padx=(0, theme.GAP))
        self.btn_open_set = ttk.Button(tabs, text="Open set…",
                                       command=self._open_set)
        self.btn_open_set.grid(row=0, column=5)

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew",
                   pady=(theme.GAP // 2, theme.GAP),
                   padx=(0, 2 * theme.PAD))

        left = ttk.Frame(panes, width=theme.SIDEBAR_W)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.candidates = CandidateList(left, on_select=self._preview_candidate,
                                        on_choose=self._choose_candidate)
        self.candidates.grid(row=0, column=0, sticky="nsew", padx=(theme.PAD, 0))
        panes.add(left, weight=0)

        right = ttk.PanedWindow(panes, orient="vertical")
        view, rotate = links.sheet_view()
        self.sheet = SheetView(right, on_toggle=self._toggle_graph, view=view,
                               rotate=rotate, on_view=links.remember_sheet_view)
        self.table = SpotTable(right, on_raw=self._set_raw,
                               on_outlier=self._set_outlier,
                               on_note=self._set_note,
                               on_revert=self._revert_spot)
        right.add(self.sheet, weight=3)
        right.add(self.table, weight=2)
        panes.add(right, weight=1)

        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        bar.columnconfigure(0, weight=1)
        self.pick_note = ttk.Label(bar, text="", font=theme.FONT_SMALL,
                                   anchor="w")
        self.pick_note.grid(row=0, column=0, sticky="ew")
        self.btn_use = ttk.Button(bar, text="Use this candidate",
                                  command=self._use_previewed)
        self.btn_use.grid(row=0, column=1, padx=theme.GAP)
        ttk.Button(bar, text="Reset to pipeline pick",
                   command=self._reset_pick).grid(row=0, column=2,
                                                  padx=theme.GAP)
        self.btn_export = ttk.Button(bar, text="Export…",
                                     command=self._export)
        self.btn_export.grid(row=0, column=3, padx=(theme.GAP, theme.PAD))

        self.status = StatusBar(self)
        self.status.grid(row=4, column=0, sticky="ew",
                         pady=(theme.GAP - 1, 0))

    def _build_tabs(self) -> None:
        for child in self._tabs.winfo_children():
            child.destroy()
        for i, medium in enumerate(self.ctl.run.media):
            ttk.Radiobutton(self._tabs, text=self.ctl.run.medium_label(medium),
                            value=medium, variable=self._medium_var,
                            style="Toolbutton",
                            command=self._medium_changed).grid(
                row=0, column=i, padx=(0, theme.GAP), ipadx=theme.PAD)

    def _bind_keys(self) -> None:
        # Through the host: live anywhere in this tool, and nowhere else --
        # not in a dialog, and not in another tool sharing the window.
        bind = self.host.bind_key
        bind("<Control-z>", lambda _e: self._undo())
        bind("<Control-y>", lambda _e: self._redo())
        bind("<Control-Z>", lambda _e: self._redo())
        bind("<Control-s>", lambda _e: self._save())
        bind("<Control-e>", lambda _e: self._export())
        bind("<Control-o>", lambda _e: self._open_set())
        bind("<Control-r>", lambda _e: self._redraw_graph())

        # Arrows: left/right change the medium, up/down walk the candidates.
        # Guarded, not global: inside the spot table the arrows have to keep
        # moving between spots, and inside a text entry they have to keep
        # moving the cursor. Stealing either would make editing unusable.
        for seq, fn in (("<Left>", lambda: self._step_medium(-1)),
                        ("<Right>", lambda: self._step_medium(1)),
                        ("<Up>", lambda: self._step_candidate(-1)),
                        ("<Down>", lambda: self._step_candidate(1))):
            bind(seq, self._arrow(fn))

        bind("<Prior>", self._arrow(lambda: self._step_candidate(-10)))
        bind("<Next>", self._arrow(lambda: self._step_candidate(10)))
        # G swaps between the pipeline's sheet and the redrawn graph.
        bind("<KeyPress-g>", self._arrow(self._toggle_graph))
        bind("<KeyPress-G>", self._arrow(self._toggle_graph))
        # V steps through the ways of showing it: fit, aligned.
        bind("<KeyPress-v>", self._arrow(self.sheet.cycle_view))
        bind("<KeyPress-V>", self._arrow(self.sheet.cycle_view))
        # R turns the graph on its side (names stay upright), and back.
        bind("<KeyPress-r>", self._arrow(self.sheet.toggle_rotate))
        bind("<KeyPress-R>", self._arrow(self.sheet.toggle_rotate))

    #: Widgets that own the arrow keys themselves while they have focus.
    def _typing(self) -> bool:
        w = self.focus_get()
        if w is None:
            return False
        if isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return True
        # The spot table: arrows move between spots, which is what a person
        # pressing them there means.
        return w is getattr(self.table, "tree", None)

    def _arrow(self, fn):
        def handler(_event=None):
            if self._typing():
                return None                  # let the focused widget have it
            fn()
            return "break"
        return handler

    # -- background work -----------------------------------------------------

    def _run_async(self, work, done, busy_text: str) -> None:
        """Run `work()` off the UI thread; hand its result to `done` on it.

        `work` should hand any computing to a worker process (`background`)
        and only wait here: a thread that computes slows this window down as
        much as computing on the window's own thread would.

        Only one at a time. The slow things here -- walking a capture tree,
        drawing figures, two Python background subtractions per montage -- all touch the
        same measurement cache and the same output folder, so overlapping them
        buys nothing and invites two writers in one directory.
        """
        if self._busy:
            self.status.say("still working on the last thing…", "warn")
            return
        self._busy = True
        self._set_enabled(False)
        self.status.say(busy_text)
        self.status.busy(True)
        self.host.set_busy(True)

        def run():
            try:
                self._jobs.put(("done", done, work(), None))
            except Exception as e:                # pragma: no cover - defensive
                self._jobs.put(("done", done, None, e))

        threading.Thread(target=run, daemon=True).start()

    def _summarize_later(self, frame, stats: dict, key) -> None:
        """Compute a strain summary's p-values off the UI thread.

        Not `_run_async`: that is the one-at-a-time lane for jobs that disable
        the buttons, and a p column filling in must neither wait behind an
        export nor lock the window. A key already being computed is not
        started twice -- one click can refresh the table more than once.
        """
        if key in self._summarizing:
            return
        self._summarizing.add(key)

        def run():
            try:
                result = background.summarize(frame, **stats)
                self._jobs.put(("summary", (key, frame, result), None, None))
            except Exception as e:                # pragma: no cover - defensive
                self._jobs.put(("summary", (key, frame, None), None, e))

        threading.Thread(target=run, daemon=True).start()

    def _summary_landed(self, key, frame, result, error) -> None:
        self._summarizing.discard(key)
        if error is not None:
            self.status.say(f"statistics failed: {type(error).__name__}: "
                            f"{error}", "error")
            return
        if len(self._summaries) > 64:
            self._summaries.clear()
        self._summaries[key] = (frame, result)
        # Only redraw if it is still what the table should show; a summary
        # for settings or a medium since left stays cached for coming back.
        current = self.ctl.cached_frame(cand=self._preview)
        if current is frame and key == (
                id(frame), tuple(sorted(
                    self.ctl.review.statistics_kwargs().items()))):
            self._refresh_table()

    def _progress(self, text: str) -> None:
        """Called from the worker thread; only queues, never touches a widget."""
        self._jobs.put(("progress", text, None, None))

    def _pump(self) -> None:
        """Drain the worker queue on the UI thread.

        Progress notes and completions travel the same queue but are kept
        distinct: a progress note must not clear the busy state, or the buttons
        come back to life in the middle of an export.

        Each job is handled on its own and the pump ALWAYS reschedules. It
        used not to: one callback raising ended the loop, every later job then
        finished into a queue nobody read, and the window was left looking
        busy for good -- Redraw greyed out and doing nothing -- with the
        traceback lost, since the launcher runs pythonw with no console.
        """
        try:
            while True:
                try:
                    job = self._jobs.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_job(*job)
                except Exception as e:
                    # Only a completion ends the busy state; a failed progress
                    # note or p-value refresh must not release a running job.
                    self._job_failed(e, release=job[0] not in
                                     ("progress", "summary"))
        finally:
            self._pump_id = None if self._closed else self.after(60, self._pump)

    def _handle_job(self, kind, a, b, error) -> None:
        if kind == "progress":
            self.status.say(a)
            return
        if kind == "summary":
            self._summary_landed(*a, error)
            return
        self._busy = False
        self._set_enabled(True)
        self.status.busy(False)
        self.host.set_busy(False)
        if error is not None:
            self.status.say(f"{type(error).__name__}: {error}", "error")
        else:
            a(b)

    def _job_failed(self, error: BaseException, release: bool = False) -> None:
        """A callback raised: say so, log it, and stay usable."""
        import traceback

        try:
            if release:
                self._busy = False
                self._set_enabled(True)
                self.status.busy(False)
                self.host.set_busy(False)
            self.status.say(f"{type(error).__name__}: {error} — details in "
                            f"{ERROR_LOG.name}", "error")
        except Exception:
            pass
        try:
            ERROR_LOG.write_text(
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\n" + "".join(
                    traceback.format_exception(type(error), error,
                                               error.__traceback__)),
                encoding="utf-8")
        except OSError:
            pass

    def _set_enabled(self, on: bool) -> None:
        state = ("!disabled" if on else "disabled")
        for w in (self.btn_use, self.btn_export, self.btn_redraw,
                  self.btn_comparisons):
            w.state([state])
        self.test_choice.configure(state="readonly" if on else "disabled")
        self.alpha_entry.configure(state="normal" if on else "disabled")
        self._sync_statistics_controls(enabled=on)

    # -- showing a set -------------------------------------------------------

    def _show_set(self) -> None:
        run = self.ctl.run
        self.host.set_title(f"{APP_TITLE} — {run.label}", tab=run.label)
        self.host.set_path(run.results_dir)
        links.remember_set(run.results_dir)
        self._build_tabs()
        self._medium_var.set(self.ctl.medium)
        self._sync_statistics_controls()

        ok = self.ctl.can_edit
        sets = [p.resolve() for p in discovery.list_sets()]
        try:
            index = sets.index(run.results_dir.resolve())
        except ValueError:
            index = -1
        self.header.show(run.label, links.describe(self.ctl.capture_root)
                         if ok else (self.ctl.config_error or "not linked"),
                         ok, index, len(sets))
        self._redrawn_sheets.clear()
        self._stale_media.clear()

        stale = rv.stale_picks(run, self.ctl.review)
        if stale:
            self.status.say(
                f"the saved pick for "
                f"{', '.join(run.medium_label(m) for m in stale)} is not in this run's "
                f"results any more — choose again", "warn")
        self._refresh()

    def _medium_changed(self) -> None:
        self.ctl.set_medium(self._medium_var.get())
        self._preview = None
        self._refresh()
        self._rebuild_current()

    # -- moving around --------------------------------------------------------

    def _step_medium(self, delta: int) -> None:
        """Left/right through the media of this set."""
        media = self.ctl.run.media
        if len(media) < 2:
            return
        i = media.index(self.ctl.medium) if self.ctl.medium in media else 0
        i = max(0, min(len(media) - 1, i + delta))
        if media[i] != self.ctl.medium:
            self._medium_var.set(media[i])
            self._medium_changed()

    def _step_candidate(self, delta: int) -> None:
        """Up/down through the candidates, in the order on screen.

        This only moves the HIGHLIGHT -- it previews, it does not choose. A key
        you hold down to flick through a hundred sheets must not quietly change
        which one the export will use; Enter, or "Use this candidate", does that.
        """
        if not self.candidates.step(delta):
            self.bell()

    def _step_set(self, delta: int) -> None:
        """The header arrows: previous / next results folder."""
        sets = discovery.list_sets()
        here = self.ctl.run.results_dir.resolve()
        try:
            i = [p.resolve() for p in sets].index(here)
        except ValueError:
            return
        j = i + delta
        if not (0 <= j < len(sets)):
            return self.bell()
        self._load_set(sets[j])

    def _toggle_graph(self) -> None:
        if not self.sheet.toggle():
            self.status.say("no redrawn graph yet — press Ctrl+R after an edit")

    def _changed(self) -> None:
        """Controller callback: state moved, redraw everything that shows it."""
        self._refresh()

    def _sync_statistics_controls(self, enabled: bool = True) -> None:
        """Reflect the saved test settings.

        With t-tests the second box is the multiple-comparison correction;
        with an ANOVA it is the post-hoc test. Dunnett is left off the list
        while every pair is being compared, since it only compares with a
        reference.
        """
        review = self.ctl.review
        self._test_var.set(next(
            label for label, value in TEST_LABELS.items()
            if value == review.statistical_test))
        if review.statistical_test == "anova":
            labels = {k: v for k, v in POSTHOC_LABELS.items()
                      if not (review.all_pairs and v == "dunnett")}
            current = review.posthoc
            self._correction_label.configure(text="Post-hoc:")
        else:
            labels, current = CORRECTION_LABELS, review.p_adjust
            self._correction_label.configure(text="Correction:")
        self._choice_mode = review.statistical_test
        self.correction_choice.configure(values=tuple(labels))
        self._correction_var.set(next(
            (label for label, value in labels.items() if value == current),
            next(iter(labels))))
        self._alpha_var.set(f"{review.alpha:g}")
        self.correction_choice.configure(
            state="readonly" if enabled else "disabled")
        n_extra = len(review.extra_references)
        short = ("all pairs" if review.all_pairs
                 else review.comparisons_text() if n_extra <= 1
                 else f"vs control + {n_extra}")
        self.btn_comparisons.configure(text=f"Comparisons: {short}…")

    def _statistics_changed(self, _event=None) -> None:
        review = self.ctl.review
        statistical_test = TEST_LABELS.get(self._test_var.get(), "t_test")
        choice = self._correction_var.get()
        # Only read the box as the list it is currently showing; a test that
        # has just been switched keeps its own saved choice.
        p_adjust, posthoc = review.p_adjust, review.posthoc
        if self._choice_mode == "t_test" and statistical_test == "t_test":
            p_adjust = CORRECTION_LABELS.get(choice, p_adjust)
        elif self._choice_mode == "anova" and statistical_test == "anova":
            posthoc = POSTHOC_LABELS.get(choice, posthoc)
        try:
            alpha = float(self._alpha_var.get())
            if not 0 < alpha < 1:
                raise ValueError
        except ValueError:
            self.status.say("p cutoff must be a number between 0 and 1", "warn")
            self._alpha_var.set(f"{review.alpha:g}")
            return
        try:
            changed = self.ctl.set_statistics(statistical_test, p_adjust,
                                              alpha, posthoc)
        except ValueError as e:
            self.status.say(str(e), "warn")
            changed = False
        if changed:
            self._statistics_applied()
        self._sync_statistics_controls(enabled=not self._busy)

    def _statistics_applied(self) -> None:
        """Every graph now predates the statistics it should show."""
        self._stale_media.update(self.ctl.run.media)
        self._redrawn_sheets.clear()
        self.sheet.show_updated(None)
        self._refresh()

    def _choose_comparisons(self) -> None:
        names = self.ctl.strain_names()
        if not names:
            self.status.say("the strain panel is not available until the "
                            "photographs are linked", "warn")
            return
        got = ask_comparisons(self.winfo_toplevel(), names,
                              self.ctl.control_name(),
                              self.ctl.review.extra_references,
                              self.ctl.review.all_pairs)
        if got is None:
            return
        if self.ctl.set_comparisons(*got):
            self._statistics_applied()
            self.status.say(self.ctl.last_change)
        self._sync_statistics_controls(enabled=not self._busy)

    def _refresh(self) -> None:
        ctl = self.ctl
        if ctl.reload_data_flags():
            # Saved in the data review meanwhile: the table's marks are stale.
            self.after_idle(self._rebuild_current)
        medium = ctl.medium
        chosen = ctl.chosen(medium)
        default = ctl.run.pipeline_best(medium)
        cands = ctl.candidates(medium)
        drawn = {c.key for c in cands if ctl.run.sheet(c).exists()}
        flagged = {}
        for c in cands:
            counts = ctl.candidate_flags(c)
            if any(counts):
                flagged[c.key] = counts
        self.candidates.show(cands, default, chosen, drawn, flagged)

        shown = self._preview or chosen
        self._show_sheet(shown)
        self._refresh_pick_note(shown, chosen, default)
        self._refresh_table()

        self.status.show_state(
            f"{ctl.review.n_edits} edit(s)"
            + (" · unsaved" if ctl.dirty else ""), ctl.dirty)
        self.host.set_dirty(ctl.dirty)
        if ctl.last_change:
            self.status.say(ctl.last_change)

    def _refresh_pick_note(self, shown, chosen, default) -> None:
        if shown is None:
            self.pick_note.configure(text="", foreground=theme.MUTED)
            self.btn_use.state(["disabled"])
            return
        is_chosen = chosen is not None and shown.key == chosen.key
        is_default = default is not None and shown.key == default.key
        if is_chosen:
            text = ("chosen · this is also the pipeline's pick" if is_default
                    else "chosen · differs from the pipeline's pick "
                         f"({default.label})" if default else "chosen")
            colour = theme.MUTED if is_default else theme.CHOSEN
            self.btn_use.state(["disabled"])
        else:
            text = f"previewing {shown.label} — not chosen yet"
            colour = theme.MANUAL
            self.btn_use.state(["!disabled"] if not self._busy else ["disabled"])
        # Say it where the eye already is: on the button, over the graph. A
        # graph that silently predates the corrections under it is the one thing
        # this tool must not show without comment.
        stale = shown.medium in self._stale_media
        self.btn_redraw.configure(
            text="↻ Recompute data — out of date" if stale else "↻ Recompute data")
        flagged = datareview.describe(*self.ctl.candidate_flags(shown))
        if flagged:
            text += f" · ⚑ uses {flagged} (data review)"
            colour = theme.REVIEWED_BAD
        if stale:
            text = "graph is out of date — Recompute data (Ctrl+R) · " + text
            colour = theme.MANUAL
        self.pick_note.configure(text=text, foreground=colour)

    def _refresh_table(self) -> None:
        ctl = self.ctl
        if not ctl.can_edit:
            self.table.show_message(
                "The photographs for this set are not linked, so the numbers "
                "behind a candidate cannot be rebuilt.\n\n"
                "Only the automatic winner's per-spot data is kept on disk; "
                "everything else is regenerated from the photos and the "
                "measurement cache.\n\n"
                "Browsing and choosing still work — use “Locate photos…” above "
                "to enable editing.", "warn")
            return
        # Cache-only: a redraw must never block on reading a photograph.
        # `_rebuild_current` does the work and calls back in here when it lands.
        # The table follows the graph on screen: a previewed candidate's own
        # numbers, not the chosen one's.
        preview = self._preview
        frame = ctl.cached_frame(cand=preview)
        if frame is None:
            if ctl.is_pending(cand=preview):
                name = ctl.run.medium_label(ctl.medium)
                what = f"{name} {preview.label}" if preview else name
                self.table.show_message(
                    f"Rebuilding {what} from the measurement cache…")
            else:
                self.table.show_message(
                    ctl.error(cand=preview) or "nothing to show", "error")
            return
        from .rebuild import summarize
        stats = ctl.review.statistics_kwargs()
        key = (id(frame), tuple(sorted(stats.items())))
        hit = self._summaries.get(key)
        if hit is not None and hit[0] is frame:
            summary = hit[1]
        else:
            # Means now, p-values when the worker lands: a post-hoc test over
            # a large panel takes most of a second, which on this thread froze
            # the window on every click of the statistics menus.
            summary = summarize(frame, with_p=False, **stats)
            for row in summary:
                row["p_heading"] += " …"
            self._summarize_later(frame, stats, key)
        self.table.show(frame.tidy.to_dict("records"), summary)
        if frame.messages:
            self.status.say(frame.messages[0], "warn")

    # -- choosing ------------------------------------------------------------

    def _show_sheet(self, cand) -> None:
        """Show a candidate and restore its redrawn sheet when one exists."""
        self.sheet.show(cand, self.ctl.run.sheet(cand) if cand else None)
        if cand is None or cand.medium in self._stale_media:
            return
        updated = self._redrawn_sheets.get(cand.key)
        if updated is not None and updated.exists():
            self.sheet.show_updated(updated)

    def _preview_candidate(self, cand) -> None:
        chosen = self.ctl.chosen()
        self._preview = None if (chosen and cand.key == chosen.key) else cand
        shown = self._preview or chosen
        self._show_sheet(shown)
        self._refresh_pick_note(shown, chosen,
                                self.ctl.run.pipeline_best(self.ctl.medium))
        # The numbers under the graph must be the numbers behind it.
        self._refresh_table()
        self._rebuild_current()

    def _choose_candidate(self, cand) -> None:
        self._preview = None
        if self.ctl.choose(cand):
            self._rebuild_current()

    def _use_previewed(self) -> None:
        if self._preview is not None:
            self._choose_candidate(self._preview)

    def _reset_pick(self) -> None:
        self._preview = None
        if self.ctl.reset_pick():
            self._rebuild_current()

    def _rebuild_current(self) -> None:
        """Rebuild this medium off the UI thread, then redraw the table."""
        if not self.ctl.can_edit:
            return
        medium = self.ctl.medium
        preview = self._preview

        def landed(_frame) -> None:
            # Clear the "rebuilding..." line. Leaving it up once the table has
            # filled in reads as a job that never finished.
            self.status.say("")
            self._refresh_table()
            # ...and re-state whether the graph now predates the numbers.
            self._refresh_pick_note(
                self._preview or self.ctl.chosen(), self.ctl.chosen(),
                self.ctl.run.pipeline_best(self.ctl.medium))

        self._run_async(lambda: self.ctl.frame(medium, cand=preview), landed,
                        f"rebuilding {self.ctl.run.medium_label(medium)} from "
                        f"the measurement cache…")

    # -- redrawing the graph --------------------------------------------------

    def _redraw_graph(self) -> None:
        """Run the corrected numbers back through the PyPrism renderer.

        Not automatic on every keystroke: somebody excluding four spots in a
        row would otherwise sit through four intermediate redraws. The table
        and strain means update instantly; the graph is the deliberate "show me
        what that did".
        """
        frame = self.ctl.cached_frame(cand=self._preview)
        if frame is None:
            self.status.say("nothing to redraw yet", "warn")
            return
        if self._graph_dir is None:
            self._graph_dir = Path(tempfile.mkdtemp(prefix="rr_graph_"))

        work = self._graph_dir
        statistics = self.ctl.review.statistics_kwargs()
        original_sheet = self.ctl.run.sheet(frame.candidate)
        key = frame.candidate.key
        self._run_async(
            lambda: (key, background.draw_preview(
                frame, work, original_sheet=original_sheet, **statistics)),
            self._redraw_finished,
            f"redrawing {self.ctl.run.medium_label(frame.medium)} with "
            f"PyPrism Plot…")

    def _redraw_finished(self, result) -> None:
        key, result = result if result else (None, None)
        graph, publication = result if result else (None, True)
        if graph is None:
            self.sheet.show_updated(None)
            self.status.say(
                "no graph could be drawn from the retained data — "
                "check the exclusions and control spots in the table", "error")
            return
        graph = Path(graph)
        self._redrawn_sheets[key] = graph
        medium = key[0]
        self._stale_media.discard(medium)
        shown = self._preview or self.ctl.chosen()
        if shown is not None and shown.key == key:
            self.sheet.show_updated(graph, publication=publication)
        if publication:
            self.status.say("comparison sheet redrawn from your corrections "
                            "with PyPrism Plot — press G for the original", "ok")
        else:
            self.status.say("PyPrism Plot could not redraw the graph; see "
                            "plot_error.log in the preview folder.", "warn")
        self._refresh_pick_note(self._preview or self.ctl.chosen(),
                                self.ctl.chosen(),
                                self.ctl.run.pipeline_best(self.ctl.medium))

    # -- edits ---------------------------------------------------------------

    def _after_edit(self, changed: bool) -> None:
        if changed:
            medium = self.ctl.medium
            self._stale_media.add(medium)
            self._redrawn_sheets = {
                key: path for key, path in self._redrawn_sheets.items()
                if key[0] != medium
            }
            self._refresh()
            self._rebuild_current()

    def _set_raw(self, row, value) -> None:
        self._after_edit(self.ctl.set_raw(row["replicate"], row["strain_col"],
                                          row["strain"], value))

    def _set_outlier(self, row, value) -> None:
        self._after_edit(self.ctl.set_outlier(row["replicate"],
                                              row["strain_col"],
                                              row["strain"], value))

    def _set_note(self, row, note) -> None:
        self._after_edit(self.ctl.set_note(row["replicate"], row["strain_col"],
                                           row["strain"], note))

    def _revert_spot(self, row) -> None:
        self._after_edit(self.ctl.revert_spot(row["replicate"],
                                              row["strain_col"], row["strain"]))

    def _undo(self) -> None:
        label = self.ctl.undo()
        self.status.say(f"undid: {label}" if label else "nothing to undo")
        if label:
            medium = self.ctl.medium
            self._stale_media.add(medium)
            self._redrawn_sheets = {
                key: path for key, path in self._redrawn_sheets.items()
                if key[0] != medium
            }
            self._refresh()
            self._rebuild_current()

    def _redo(self) -> None:
        label = self.ctl.redo()
        self.status.say(f"redid: {label}" if label else "nothing to redo")
        if label:
            medium = self.ctl.medium
            self._stale_media.add(medium)
            self._redrawn_sheets = {
                key: path for key, path in self._redrawn_sheets.items()
                if key[0] != medium
            }
            self._refresh()
            self._rebuild_current()

    # -- files ---------------------------------------------------------------

    def _save(self) -> None:
        try:
            path = self.ctl.save()
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"Could not save:\n{e}", parent=self)
            return
        self.status.say(f"saved {path.name}", "ok")

    def _locate_photos(self) -> None:
        got = filedialog.askdirectory(
            parent=self, title=f"Photographs for {self.ctl.run.label}",
            initialdir=str(self.ctl.capture_root or links.default_start_dir()))
        if not got:
            return
        ok, msg = self.ctl.link_photos(Path(got))
        self.status.say(msg, "ok" if ok else "error")
        if ok:
            self._show_set()
            self._rebuild_current()

    def _load_set(self, results_dir: Path) -> bool:
        """Swap the whole window over to another results folder."""
        if not self._confirm_discard():
            return False
        try:
            run = discovery.load_set(results_dir)
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e), parent=self)
            return False
        self.ctl = ReviewController(run, rv.load_for(run),
                                    on_change=self._changed,
                                    rebuilder=background.rebuild)
        self._preview = None
        self.sheet.cache.clear()
        self._show_set()
        self._rebuild_current()
        return True

    def _open_set(self) -> None:
        got = choose_set(self.winfo_toplevel())
        # In a tab of its own where the host has tabs; otherwise in place.
        if got is not None and not self.host.open_document("review", got):
            self._load_set(got)

    # -- export --------------------------------------------------------------

    def _export(self) -> None:
        if self._busy:
            return
        ctl = self.ctl
        if not ctl.can_edit:
            messagebox.showinfo(
                APP_TITLE,
                "Link the photographs with Locate photos before exporting.",
                parent=self)
            return
        from .gui.export_dialog import ask_export
        from .model import Review
        from copy import deepcopy

        # The slides are laid out as the sheet is shown right now.
        request = ask_export(self.winfo_toplevel(), ctl.run, ctl.review,
                             self.sheet.view, self.sheet.rotated)
        if request is None:
            return
        try:
            ctl.save()
        except Exception as e:
            self.status.say(f"could not save the review: {e}", "error")
            return

        # Export a fixed snapshot: navigating or editing during the worker must
        # never change which photographs or statistics enter this deck.
        review = Review.from_dict(deepcopy(ctl.review.to_dict()))
        run, root, cfg = ctl.run, ctl.capture_root, deepcopy(ctl.cfg)
        flags = deepcopy(ctl.data_flags())
        self._run_async(
            lambda: background.export_review(
                run, review, request.media, root, cfg, options=request.options,
                outdir=request.outdir, data_flags=flags,
                progress=self._progress),
            self._exported, "exporting chosen photo sets…")

    def _exported(self, result) -> None:
        if result is None:
            return
        if not result.ok:
            message = "; ".join(result.warnings) or "export failed"
            self.status.say(message, "error")
            messagebox.showerror(APP_TITLE, message, parent=self)
            return
        parts = [f"{len(result.files)} file(s)"]
        if result.powerpoint:
            parts.append(result.powerpoint.name)
        self.status.say(f"wrote {result.output_dir} — {', '.join(parts)}",
                        "warn" if result.warnings else "ok")
        if result.warnings:
            messagebox.showwarning(APP_TITLE,
                                   "Exported, with warnings:\n\n"
                                   + "\n".join(result.warnings), parent=self)

    # -- closing -------------------------------------------------------------

    def _confirm_discard(self) -> bool:
        if not self.ctl.dirty:
            return True
        answer = messagebox.askyesnocancel(
            APP_TITLE, "Save the review first?", parent=self)
        if answer is None:
            return False
        if answer:
            self._save()
        return True

    def _can_close(self) -> bool:
        """Asked by the host before closing: False keeps the tool open."""
        if self._busy and not messagebox.askokcancel(
                APP_TITLE,
                "Something is still being worked on -- an export, a rebuild "
                "or a redraw. Closing abandons it unfinished.\n\nClose anyway?",
                parent=self):
            return False
        return self._confirm_discard()

    def _dispose(self) -> None:
        """Run once on the way out, whatever closed the tool."""
        self._closed = True
        if self._pump_id is not None:
            try:
                self.after_cancel(self._pump_id)
            except tk.TclError:
                pass
            self._pump_id = None
        if self._graph_dir is not None:
            # Preview graphs are scratch: they were never a result, and leaving
            # them behind invites somebody finding one later and believing it.
            shutil.rmtree(self._graph_dir, ignore_errors=True)


# ---------------------------------------------------------------------------


def _is_usable_parent(widget) -> bool:
    """True only for a window that is actually on screen.

    `transient()` against a WITHDRAWN master makes Tk withdraw the child too:
    it never maps, stays 1x1, and `wait_window` then blocks on a window nobody
    can see -- a launcher that opens a console and appears to do nothing. So a
    parent has to be checked, not assumed, and the chooser owns its own root
    when there is no real one.
    """
    try:
        return bool(widget is not None and widget.winfo_exists()
                    and widget.winfo_viewable())
    except tk.TclError:
        return False


def choose_set(parent=None) -> "Path | None":
    """Pick one of the timecourse results folders.

    A list rather than a folder dialog: the folders all look alike and only some
    of them are results, so showing exactly the valid ones is both faster and
    impossible to get wrong. The folder dialog stays available for a results
    tree kept somewhere else.
    """
    sets = discovery.list_sets()
    if not sets:
        return _ask_folder(parent if _is_usable_parent(parent) else None)

    nested = _is_usable_parent(parent)
    win = tk.Toplevel(parent) if nested else tk.Tk()
    apply_theme(win)
    win.title("Open a timecourse result")
    theme.set_scale(win)
    win.geometry(f"{theme.px(460)}x{theme.px(400)}")
    win.minsize(theme.px(360), theme.px(260))
    win.rowconfigure(1, weight=1)
    win.columnconfigure(0, weight=1)
    ttk.Label(win, text="Which set?", font=theme.FONT_BOLD).grid(
        row=0, column=0, sticky="w", padx=theme.PAD, pady=theme.PAD)

    listbox = tk.Listbox(win, font=theme.FONT, activestyle="none")
    last = links.last_set()
    start = 0
    for i, p in enumerate(sets):
        n = len(discovery.load_candidates(p / discovery.CANDIDATES_CSV))
        reviewed = " · reviewed" if (p / discovery.REVIEW_JSON).exists() else ""
        info = discovery.load_run_info(p)
        label = info.name if info and info.name else p.name
        listbox.insert("end", f"{label}   ({n} candidates{reviewed})")
        if last is not None and p.resolve() == last.resolve():
            start = i
    listbox.selection_set(start)
    listbox.activate(start)
    listbox.see(start)
    listbox.grid(row=1, column=0, sticky="nsew", padx=theme.PAD)

    chosen: list[Path] = []

    def take(_event=None):
        sel = listbox.curselection()
        if sel:
            chosen.append(sets[sel[0]])
            win.destroy()

    def browse():
        got = _ask_folder(win)
        if got is not None:
            chosen.append(got)
            win.destroy()

    listbox.bind("<Double-1>", take)
    listbox.bind("<Return>", take)
    win.bind("<Escape>", lambda _e: win.destroy())

    row = ttk.Frame(win)
    row.grid(row=2, column=0, sticky="ew", padx=theme.PAD, pady=theme.PAD)
    row.columnconfigure(0, weight=1)
    ttk.Button(row, text="Browse…", command=browse).grid(row=0, column=1,
                                                         padx=theme.GAP)
    ttk.Button(row, text="Open", command=take).grid(row=0, column=2)

    if nested:
        win.transient(parent)
        win.grab_set()
    # Put it in front of the console the launcher opened, rather than behind it.
    win.update_idletasks()
    win.lift()
    win.attributes("-topmost", True)
    win.after_idle(lambda: win.attributes("-topmost", False))
    listbox.focus_force()
    win.wait_window()
    return chosen[0] if chosen else None


def ask_comparisons(parent, strains: list[str], control: str,
                    extra: list[str], all_pairs: bool
                    ) -> "tuple[list[str], bool] | None":
    """Which strains to compare: (extra references, every pair), or None.

    Every strain is always compared with the control. The list adds further
    reference strains, each compared with every other strain; the checkbox
    replaces all of that with every possible pair.
    """
    win = tk.Toplevel(parent)
    win.title("Comparisons")
    win.geometry(f"{theme.px(400)}x{theme.px(480)}")
    win.minsize(theme.px(320), theme.px(320))
    win.columnconfigure(0, weight=1)
    win.rowconfigure(3, weight=1)

    every = tk.BooleanVar(value=all_pairs)
    ttk.Checkbutton(win, text="Compare every pair of strains",
                    variable=every).grid(row=0, column=0, sticky="w",
                                         padx=theme.PAD, pady=(theme.PAD, 2))
    who = f" ({control})" if control else ""
    ttk.Label(win, font=theme.FONT_SMALL, foreground=theme.MUTED,
              wraplength=360, justify="left",
              text=f"Every strain is always compared with the control{who}. "
                   "Select further strains to compare every other strain "
                   "against as well:").grid(
        row=1, column=0, sticky="w", padx=theme.PAD, pady=(theme.GAP, 2))

    frame = ttk.Frame(win)
    frame.grid(row=3, column=0, sticky="nsew", padx=theme.PAD)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    choices = [s for s in strains if s != control]
    listbox = tk.Listbox(frame, selectmode="multiple", font=theme.FONT,
                         activestyle="none", exportselection=False)
    for i, name in enumerate(choices):
        listbox.insert("end", name)
        if name in extra:
            listbox.selection_set(i)
    listbox.grid(row=0, column=0, sticky="nsew")
    bar = ttk.Scrollbar(frame, orient="vertical", command=listbox.yview)
    bar.grid(row=0, column=1, sticky="ns")
    listbox.configure(yscrollcommand=bar.set)

    def follow(*_):
        listbox.configure(state="disabled" if every.get() else "normal")
    every.trace_add("write", follow)
    follow()

    result: list = []

    def ok(_event=None):
        # Keep extras that are not in this list -- another medium's control,
        # say -- rather than silently dropping them.
        hidden = [r for r in extra if r not in choices]
        listbox.configure(state="normal")
        picked = [choices[i] for i in listbox.curselection()]
        result.append((hidden + picked, every.get()))
        win.destroy()

    row = ttk.Frame(win)
    row.grid(row=4, column=0, sticky="ew", padx=theme.PAD, pady=theme.PAD)
    row.columnconfigure(0, weight=1)
    ttk.Button(row, text="Clear", command=lambda: listbox.selection_clear(
        0, "end")).grid(row=0, column=0, sticky="w")
    ttk.Button(row, text="Cancel", command=win.destroy).grid(
        row=0, column=1, padx=theme.GAP)
    ttk.Button(row, text="OK", command=ok).grid(row=0, column=2)
    win.bind("<Escape>", lambda _e: win.destroy())
    win.bind("<Return>", ok)

    win.transient(parent)
    win.grab_set()
    listbox.focus_set()
    win.wait_window()
    return result[0] if result else None


def _ask_folder(parent) -> "Path | None":
    got = filedialog.askdirectory(
        parent=parent, title="A Results/Timecourse set folder",
        initialdir=str(discovery.TIMECOURSE_RESULTS
                       if discovery.TIMECOURSE_RESULTS.is_dir() else Path.home()))
    return Path(got) if got else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="results_review",
        description="Review timecourse results: choose which candidate to "
                    "carry forward, and correct individual spots.")
    ap.add_argument("results", nargs="?", type=Path,
                    help="a Results/Timecourse/<set> folder; without one, "
                         "reopens the set you had open last")
    ap.add_argument("--pick", action="store_true",
                    help="always show the set chooser")
    args = ap.parse_args(argv)
    # Before any window: the set chooser below is a Tk root too.
    theme.enable_dpi_awareness()
    tasks.favour_the_window()

    # Straight back to where you left off. Reviewing a set spans more than one
    # sitting, and picking the same folder out of fifteen every time is the kind
    # of friction that stops a tool being opened at all. `--pick`, and the
    # header's "Open set..." button, are how you get somewhere else.
    target = args.results
    if target is None and not args.pick:
        target = links.last_set()
    if target is None:
        # No hidden root to parent this to: a dialog made transient to a
        # withdrawn window inherits its withdrawn state and never appears.
        target = choose_set()
        if target is None:
            return 0
    try:
        run = discovery.load_set(target)
    except Exception as e:
        _fatal(e)
        return 1

    try:
        root = tk.Tk()
        ReviewApp(StandaloneHost(root, APP_TITLE), run)
        root.mainloop()
    except Exception as e:                        # pragma: no cover - defensive
        _fatal(e)
        return 1
    finally:
        # The window has gone; so does anything still running for it, rather
        # than the program waiting, invisibly, for an abandoned export.
        tasks.shutdown()
    return 0


#: Where a crash goes when there is no console to print it to.
ERROR_LOG = PROJECT_ROOT / "review_error.log"


def _fatal(error: BaseException) -> None:
    """Report a startup failure even when launched without a console.

    `run_review.bat` hands the window to pythonw so nothing is left sitting
    behind it, which means a traceback on stderr goes nowhere at all. Silence is
    the one outcome a launcher must never produce, so it is written down and
    shown.
    """
    import traceback

    text = "".join(traceback.format_exception(type(error), error,
                                              error.__traceback__))
    print(f"  {error}", file=sys.stderr)
    try:
        ERROR_LOG.write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{text}", encoding="utf-8")
    except OSError:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            f"{APP_TITLE} could not start.\n\n{error}\n\n"
            f"Details were written to:\n{ERROR_LOG}")
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
