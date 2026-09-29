"""Review timecourse results -- tkinter entry point.

    py -m results_review [<Results/Timecourse/SetNN>]

Opens a set chooser when no folder is given. Browsing needs nothing but the
results folder; correcting spots needs the photographs, and the window says so
plainly instead of failing at the moment you try.

Rebuilding and exporting run on a worker thread. Processing full-resolution
photos and drawing figures must not block the window's event loop.
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

from . import PROJECT_ROOT, discovery, links, review as rv, theme
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


class ReviewApp(tk.Tk):
    def __init__(self, run: SetRun) -> None:
        super().__init__()
        self.title(APP_TITLE)
        # Wide enough that the spot table and the strain summary both fit
        # beside each other at the default sash, rather than starting clipped
        # and needing a drag before the tool is readable.
        self.geometry("1440x900")
        self.minsize(900, 600)
        self.configure(background=theme.BG)

        self.ctl = ReviewController(run, rv.load_for(run),
                                    on_change=self._changed)
        self._jobs: "queue.Queue" = queue.Queue()
        self._busy = False
        self._preview = None          # candidate being previewed, not yet chosen
        self._graph_dir: "Path | None" = None   # scratch for redrawn graphs
        self._redrawn_sheets: dict[tuple, Path] = {}
        self._stale_media: set[str] = set()

        self._build()
        self._bind_keys()
        self._show_set()
        self.after(60, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)
        # Insist on being visible. A GUI started from a console that is itself
        # minimised or background inherits that show state through STARTUPINFO
        # and opens minimised -- the window is running and reachable only from
        # the taskbar, which from the outside is a launcher that did nothing.
        self.deiconify()
        self.lift()

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
        # Keep this row the same height as before, while centring both the
        # medium buttons and the inline statistics controls within it.
        tabs.rowconfigure(0, minsize=44)
        self._medium_var = tk.StringVar(value=self.ctl.medium)
        self._tabs = ttk.Frame(tabs)
        self._tabs.grid(row=0, column=0, sticky="w")

        # Analysis choices stay beside the medium tabs, where they remain
        # visible even when Windows display scaling leaves little vertical
        # room.  Putting these on a second row beneath the bottom action bar
        # could place them below the usable screen edge.
        self.stats = ttk.Frame(tabs)
        self.stats.grid(row=0, column=2, sticky="e",
                        padx=(theme.GAP, theme.PAD))
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
        ttk.Label(self.stats, text="Correction:", font=theme.FONT_SMALL).grid(
            row=0, column=3, padx=(0, 2), pady=2)
        self._correction_var = tk.StringVar()
        self.correction_choice = ttk.Combobox(
            self.stats, textvariable=self._correction_var, state="readonly",
            width=12,
            values=tuple(CORRECTION_LABELS))
        self.correction_choice.grid(row=0, column=4, pady=2)
        self.correction_choice.bind("<<ComboboxSelected>>",
                                    self._statistics_changed)
        ttk.Label(self.stats, text="p cutoff:", font=theme.FONT_SMALL).grid(
            row=0, column=5, padx=(theme.PAD, 2), pady=2)
        self._alpha_var = tk.StringVar(value="0.05")
        self.alpha_entry = ttk.Entry(self.stats, textvariable=self._alpha_var,
                                     width=6)
        self.alpha_entry.grid(row=0, column=6, pady=2)
        self.alpha_entry.bind("<Return>", self._statistics_changed)
        self.alpha_entry.bind("<FocusOut>", self._statistics_changed)

        self.btn_redraw = ttk.Button(tabs,
                                     text="↻ Redraw graph — out of date",
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
        self.sheet = SheetView(right, on_toggle=self._toggle_graph)
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
        self.btn_export = ttk.Button(bar, text="Export chosen/",
                                     command=self._export)
        self.btn_export.grid(row=0, column=3, padx=(theme.GAP, theme.PAD))

        self.status = StatusBar(self)
        self.status.grid(row=4, column=0, sticky="ew",
                         pady=(theme.GAP - 1, 0))

    def _build_tabs(self) -> None:
        for child in self._tabs.winfo_children():
            child.destroy()
        for i, medium in enumerate(self.ctl.run.media):
            ttk.Radiobutton(self._tabs, text=medium, value=medium,
                            variable=self._medium_var, style="Toolbutton",
                            command=self._medium_changed).grid(
                row=0, column=i, padx=(0, theme.GAP), ipadx=theme.PAD)

    def _bind_keys(self) -> None:
        self.bind_all("<Control-z>", lambda _e: self._undo())
        self.bind_all("<Control-y>", lambda _e: self._redo())
        self.bind_all("<Control-Z>", lambda _e: self._redo())
        self.bind_all("<Control-s>", lambda _e: self._save())
        self.bind_all("<Control-e>", lambda _e: self._export())
        self.bind_all("<Control-o>", lambda _e: self._open_set())
        self.bind_all("<Control-r>", lambda _e: self._redraw_graph())

        # Arrows: left/right change the medium, up/down walk the candidates.
        # Guarded, not global: inside the spot table the arrows have to keep
        # moving between spots, and inside a text entry they have to keep
        # moving the cursor. Stealing either would make editing unusable.
        for seq, fn in (("<Left>", lambda: self._step_medium(-1)),
                        ("<Right>", lambda: self._step_medium(1)),
                        ("<Up>", lambda: self._step_candidate(-1)),
                        ("<Down>", lambda: self._step_candidate(1))):
            self.bind_all(seq, self._arrow(fn))

        self.bind_all("<Prior>", self._arrow(lambda: self._step_candidate(-10)))
        self.bind_all("<Next>", self._arrow(lambda: self._step_candidate(10)))
        # G swaps between the pipeline's sheet and the redrawn graph.
        self.bind_all("<KeyPress-g>", self._arrow(self._toggle_graph))
        self.bind_all("<KeyPress-G>", self._arrow(self._toggle_graph))

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
        self.configure(cursor="watch")

        def run():
            try:
                self._jobs.put(("done", done, work(), None))
            except Exception as e:                # pragma: no cover - defensive
                self._jobs.put(("done", done, None, e))

        threading.Thread(target=run, daemon=True).start()

    def _progress(self, text: str) -> None:
        """Called from the worker thread; only queues, never touches a widget."""
        self._jobs.put(("progress", text, None, None))

    def _pump(self) -> None:
        """Drain the worker queue on the UI thread.

        Progress notes and completions travel the same queue but are kept
        distinct: a progress note must not clear the busy state, or the buttons
        come back to life in the middle of an export.
        """
        try:
            while True:
                kind, a, b, error = self._jobs.get_nowait()
                if kind == "progress":
                    self.status.say(a)
                    continue
                self._busy = False
                self._set_enabled(True)
                self.status.busy(False)
                self.configure(cursor="")
                if error is not None:
                    self.status.say(f"{type(error).__name__}: {error}", "error")
                else:
                    a(b)
        except queue.Empty:
            pass
        self.after(60, self._pump)

    def _set_enabled(self, on: bool) -> None:
        state = ("!disabled" if on else "disabled")
        for w in (self.btn_use, self.btn_export, self.btn_redraw):
            w.state([state])
        self.test_choice.configure(state="readonly" if on else "disabled")
        self.alpha_entry.configure(state="normal" if on else "disabled")
        self._sync_statistics_controls(enabled=on)

    # -- showing a set -------------------------------------------------------

    def _show_set(self) -> None:
        run = self.ctl.run
        self.title(f"{APP_TITLE} — {run.label}")
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
                f"the saved pick for {', '.join(stale)} is not in this run's "
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
        """Reflect the saved test settings and ANOVA's omnibus semantics."""
        review = self.ctl.review
        self._test_var.set(next(
            label for label, value in TEST_LABELS.items()
            if value == review.statistical_test))
        self._correction_var.set(next(
            label for label, value in CORRECTION_LABELS.items()
            if value == review.p_adjust))
        self._alpha_var.set(f"{review.alpha:g}")
        correction_state = ("readonly" if enabled and
                            review.statistical_test == "t_test" else "disabled")
        self.correction_choice.configure(state=correction_state)

    def _statistics_changed(self, _event=None) -> None:
        statistical_test = TEST_LABELS.get(self._test_var.get(), "t_test")
        p_adjust = CORRECTION_LABELS.get(self._correction_var.get(), "none")
        try:
            alpha = float(self._alpha_var.get())
            if not 0 < alpha < 1:
                raise ValueError
        except ValueError:
            self.status.say("p cutoff must be a number between 0 and 1", "warn")
            self._alpha_var.set(f"{self.ctl.review.alpha:g}")
            return
        if self.ctl.set_statistics(statistical_test, p_adjust, alpha):
            self._stale_media.update(self.ctl.run.media)
            self._redrawn_sheets.clear()
            self.sheet.show_updated(None)
            self._refresh()
        self._sync_statistics_controls(enabled=not self._busy)

    def _refresh(self) -> None:
        ctl = self.ctl
        medium = ctl.medium
        chosen = ctl.chosen(medium)
        default = ctl.run.pipeline_best(medium)
        cands = ctl.candidates(medium)
        drawn = {c.key for c in cands if ctl.run.sheet(c).exists()}
        self.candidates.show(cands, default, chosen, drawn)

        shown = self._preview or chosen
        self._show_sheet(shown)
        self._refresh_pick_note(shown, chosen, default)
        self._refresh_table()

        self.status.show_state(
            f"{ctl.review.n_edits} edit(s)"
            + (" · unsaved" if ctl.dirty else ""), ctl.dirty)
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
            text="↻ Redraw graph — out of date" if stale else "↻ Redraw graph")
        if stale:
            text = "graph is out of date — Redraw graph (Ctrl+R) · " + text
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
        frame = ctl.cached_frame()
        if frame is None:
            if ctl.is_pending():
                self.table.show_message(
                    f"Rebuilding {ctl.medium} from the measurement cache…")
            else:
                self.table.show_message(ctl.error() or "nothing to show",
                                        "error")
            return
        from .rebuild import summarize
        review = ctl.review
        summary = summarize(frame, statistical_test=review.statistical_test,
                            p_adjust=review.p_adjust, alpha=review.alpha)
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

        def landed(_frame) -> None:
            # Clear the "rebuilding..." line. Leaving it up once the table has
            # filled in reads as a job that never finished.
            self.status.say("")
            self._refresh_table()
            # ...and re-state whether the graph now predates the numbers.
            self._refresh_pick_note(
                self._preview or self.ctl.chosen(), self.ctl.chosen(),
                self.ctl.run.pipeline_best(self.ctl.medium))

        self._run_async(lambda: self.ctl.frame(medium), landed,
                        f"rebuilding {medium} from the measurement cache…")

    # -- redrawing the graph --------------------------------------------------

    def _redraw_graph(self) -> None:
        """Run the corrected numbers back through the PyPrism renderer.

        Not automatic on every keystroke: somebody excluding four spots in a
        row would otherwise sit through four intermediate redraws. The table
        and strain means update instantly; the graph is the deliberate "show me
        what that did".
        """
        frame = self.ctl.cached_frame()
        if frame is None:
            self.status.say("nothing to redraw yet", "warn")
            return
        if self._graph_dir is None:
            self._graph_dir = Path(tempfile.mkdtemp(prefix="rr_graph_"))

        from .export import draw_preview
        work = self._graph_dir
        statistical_test = self.ctl.review.statistical_test
        p_adjust = self.ctl.review.p_adjust
        alpha = self.ctl.review.alpha
        original_sheet = self.ctl.run.sheet(frame.candidate)
        key = frame.candidate.key
        self._run_async(
            lambda: (key, draw_preview(frame, work,
                                       statistical_test=statistical_test,
                                       p_adjust=p_adjust,
                                       alpha=alpha,
                                       original_sheet=original_sheet)),
            self._redraw_finished,
            f"redrawing {frame.medium} with PyPrism Plot…")

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
                                    on_change=self._changed)
        self._preview = None
        self.sheet.cache.clear()
        self._show_set()
        self._rebuild_current()
        return True

    def _open_set(self) -> None:
        got = choose_set(self)
        if got is not None:
            self._load_set(got)

    # -- export --------------------------------------------------------------

    def _export(self) -> None:
        ctl = self.ctl
        if not ctl.can_edit:
            messagebox.showinfo(
                APP_TITLE,
                "Exporting rebuilds every medium from the photographs, so the "
                "capture folder has to be linked first.", parent=self)
            return
        frames, bad = ctl.frames()
        if not frames:
            messagebox.showerror(APP_TITLE,
                                 "Nothing could be rebuilt:\n\n" + "\n".join(bad),
                                 parent=self)
            return
        if bad and not messagebox.askokcancel(
                APP_TITLE,
                "These media could not be rebuilt and will be left out:\n\n"
                + "\n".join(bad) + "\n\nExport the rest?", parent=self):
            return

        target = ctl.run.chosen_dir
        if target.exists() and not messagebox.askokcancel(
                APP_TITLE,
                f"{target} already exists and will be overwritten.\n\n"
                f"(best/ is never touched.)\n\nContinue?", parent=self):
            return

        try:
            ctl.save()
        except Exception as e:
            self.status.say(f"could not save the review: {e}", "error")
            return

        from .export import export
        root, cfg, review, run = (ctl.capture_root, ctl.cfg, ctl.review, ctl.run)
        self._run_async(
            lambda: export(run, review, frames, root, cfg,
                           progress=self._progress),
            self._exported, "exporting…")

    def _exported(self, result) -> None:
        if result is None:
            return
        if not result.ok:
            self.status.say("; ".join(result.warnings) or "export failed",
                            "error")
            return
        parts = [f"{len(result.figures)} figure(s)",
                 f"{len(result.montages)} montage(s)"]
        self.status.say(f"wrote {result.csv_path.parent} — {', '.join(parts)}",
                        "ok")
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

    def _close(self) -> None:
        if not self._confirm_discard():
            return
        if self._graph_dir is not None:
            # Preview graphs are scratch: they were never a result, and leaving
            # them behind invites somebody finding one later and believing it.
            shutil.rmtree(self._graph_dir, ignore_errors=True)
        self.destroy()


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
    win.title("Open a timecourse result")
    win.geometry("460x400")
    win.minsize(360, 260)
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
        listbox.insert("end", f"{p.name}   ({n} candidates{reviewed})")
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
        ReviewApp(run).mainloop()
    except Exception as e:                        # pragma: no cover - defensive
        _fatal(e)
        return 1
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
