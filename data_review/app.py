"""Data review -- tkinter entry point.

    py -m data_review <experiment.spotexp.json | its .datareview.json>

Flip through every photograph an experiment imported, before any statistics,
and flag what is not good data: a whole plate, or single spots by clicking on
them. Spot positions come from the program's own detection, which runs as a
background job (`cli detect`) and fills the measurement cache the run reads.

Flags are saved beside the experiment (`flags.py`). The additional multi-step
analysis excludes them; the results review marks them for the person to judge.
"""

from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from uikit import tokens
from uikit.dpi import enable_dpi_awareness, px
from uikit.host import JobSpec, StandaloneHost, as_host
from uikit.tasks import favour_the_window, run_in_thread
from uikit.theme import FONT_SMALL, FONT_STRONG

from . import FROZEN, PROJECT_ROOT, flags as flagfile
from .catalog import siblings
from .controller import (FILTERS, PLATE_REASONS, SPOT_REASONS,
                         DataReviewController)
from .gui.plate import (FLAG, Magnifier, PlateView, PreviewCache, open_full,
                        open_preview)

APP_TITLE = "Data Review"

#: How often a running detection job is checked on, and how often the photos
#: it has finished are picked up while it runs, in milliseconds.
JOB_POLL_MS = 1500
JOB_RECHECK_MS = 15000

HINT = ("Click a spot to flag it; click again to clear it. Right-click for more.\n"
        "Space: looks good, next  ·  P: flag the plate  ·  PgUp / PgDn: "
        "previous / next  ·  V: grey values")


def program_command(*args: str) -> list[str]:
    """`spotting_app.py <args>`, however this program is running."""
    if FROZEN:
        return [sys.executable, *args]
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    return [str(exe), str(PROJECT_ROOT / "spotting_app.py"), *args]


class DataReviewApp(ttk.Frame):
    """The data review, built into a host (`uikit.host`): a window or a tab."""

    def __init__(self, master_or_host, flags_path: Path) -> None:
        self.host = as_host(master_or_host, APP_TITLE)
        super().__init__(self.host.frame)
        self.pack(fill="both", expand=True)
        self.flags_path = Path(flags_path)
        self.ctl = DataReviewController(self.flags_path, on_change=self._changed)
        self.host.set_title(APP_TITLE, tab=flagfile.stem_of(self.flags_path))
        self.host.set_path(self.flags_path)
        self.host.suggest_size(px(self, 1440), px(self, 900),
                               px(self, 960), px(self, 620), center=False)

        self.previews = PreviewCache()
        self._spot: "tuple[int, int] | None" = None
        self._showing = None                 # the Photo whose pixels are drawn
        self._refresh_id: "str | None" = None
        self._list_ids: dict[str, str] = {}  # relpath -> tree iid
        self._list_paths: dict[str, str] = {}
        self._job = None
        self._job_poll: "str | None" = None
        self._job_ticks = 0
        self._closed = False
        self.show_values = tk.BooleanVar(value=False)
        self.spot_reason = tk.StringVar(value=SPOT_REASONS[0])
        self.plate_reason = tk.StringVar(value=PLATE_REASONS[0])
        self.filter = tk.StringVar(value=FILTERS[0])

        self._build()
        self._bind_keys()
        self.host.on_close(self._can_close)
        self.host.on_dispose(self._dispose)
        for name, action in (("save", self._save), ("undo", self._undo),
                             ("redo", self._redo)):
            self.host.add_command(name, action)
        self.host.present()
        self.update_idletasks()
        self.after_idle(self._load)

    # -- layout --------------------------------------------------------------

    def _build(self) -> None:
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        pad = px(self, 8)

        head = ttk.Frame(self, padding=(pad, pad, pad, px(self, 4)))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self.title_label = ttk.Label(head, text=flagfile.stem_of(self.flags_path),
                                     font=FONT_STRONG)
        self.title_label.grid(row=0, column=0, sticky="w")
        self.counts_label = ttk.Label(head, text="Reading the experiment…",
                                      style="Muted.TLabel")
        self.counts_label.grid(row=1, column=0, sticky="w")
        buttons = ttk.Frame(head)
        buttons.grid(row=0, column=1, rowspan=2, sticky="e")
        self.detect_button = ttk.Button(buttons, text="Locate spots…",
                                        command=self._detect, takefocus=False)
        self.detect_button.pack(side="left", padx=(0, px(self, 6)))
        self.open_button = ttk.Button(buttons, text="Open experiment",
                                      command=self._open_experiment,
                                      takefocus=False)
        if not self.host.standalone:
            self.open_button.pack(side="left", padx=(0, px(self, 6)))
        ttk.Button(buttons, text="Reload", command=self._load,
                   takefocus=False).pack(side="left", padx=(0, px(self, 6)))
        ttk.Button(buttons, text="Save", command=self._save,
                   takefocus=False).pack(side="left")

        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=pad, pady=(0, px(self, 4)))
        panes.add(self._build_list(panes), weight=0)
        panes.add(self._build_photo(panes), weight=1)
        panes.add(self._build_side(panes), weight=0)

        self.status = ttk.Label(self, text="", style="Muted.TLabel", anchor="w",
                                padding=(pad, 2, pad, px(self, 4)))
        self.status.grid(row=2, column=0, sticky="ew")

    def _build_list(self, master) -> ttk.Frame:
        left = ttk.Frame(master, width=px(self, 250))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        top = ttk.Frame(left)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(self, 4)))
        ttk.Label(top, text="Show:").pack(side="left")
        chooser = ttk.Combobox(top, textvariable=self.filter, values=FILTERS,
                               state="readonly", width=18)
        chooser.pack(side="left", padx=(px(self, 6), 0), fill="x", expand=True)
        chooser.bind("<<ComboboxSelected>>", self._filter_changed)

        self.tree = ttk.Treeview(left, columns=("status",), show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="Photo", anchor="w")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=px(self, 150), stretch=True)
        self.tree.column("status", width=px(self, 90), stretch=False)
        self.tree.tag_configure("group", font=FONT_SMALL, foreground=tokens.TEXT_MUTED)
        self.tree.tag_configure("flagged", foreground=FLAG)
        self.tree.tag_configure("done", foreground=tokens.OK)
        self.tree.tag_configure("undetected", foreground=tokens.TEXT_DISABLED)
        bar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        bar.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        # Left and right walk the photos here too, instead of folding the
        # groups away (which would only take the selection off the photo).
        self.tree.bind("<Left>", lambda _e: (self._step(-1), "break")[1])
        self.tree.bind("<Right>", lambda _e: (self._step(1), "break")[1])
        return left

    def _build_photo(self, master) -> ttk.Frame:
        mid = ttk.Frame(master, padding=(px(self, 8), 0))
        mid.rowconfigure(3, weight=1)
        mid.columnconfigure(0, weight=1)
        self.caption = ttk.Label(mid, text="", font=FONT_STRONG, anchor="w")
        self.caption.grid(row=0, column=0, sticky="ew")
        self.where = ttk.Label(mid, text="", style="Small.TLabel", anchor="w")
        self.where.grid(row=1, column=0, sticky="ew")
        self.banner = tk.Label(mid, text="", anchor="w", justify="left",
                               background=tokens.BG, foreground=FLAG,
                               font=(tokens.FONT_FAMILY, tokens.SIZE_BODY, "bold"))
        self.banner.grid(row=2, column=0, sticky="ew")
        self.view = PlateView(mid, on_hover=self._hovered, on_click=self._clicked,
                              on_menu=self._context_menu)
        self.view.grid(row=3, column=0, sticky="nsew", pady=(px(self, 4), px(self, 6)))

        # Two rows, so neither is cut off on a narrow window: moving through
        # the photos, then the two verdicts on the one shown.
        nav = ttk.Frame(mid)
        nav.grid(row=4, column=0, sticky="ew")
        ttk.Button(nav, text="◀ Previous", takefocus=False,
                   command=lambda: self._step(-1)).pack(side="left")
        self.position = ttk.Label(nav, text="", style="Muted.TLabel")
        self.position.pack(side="left", padx=px(self, 10))
        ttk.Button(nav, text="Next ▶", takefocus=False,
                   command=lambda: self._step(1)).pack(side="left")
        ttk.Checkbutton(nav, text="Grey values (V)", variable=self.show_values,
                        takefocus=False,
                        command=self._redraw_overlay).pack(side="right")
        verdict = ttk.Frame(mid)
        verdict.grid(row=5, column=0, sticky="ew", pady=(px(self, 6), 0))
        self.good_button = ttk.Button(verdict, text="✓ Looks good, next (Space)",
                                      style="Accent.TButton", takefocus=False,
                                      command=self._looks_good)
        self.good_button.pack(side="left")
        self.plate_button = ttk.Button(verdict, text="Flag whole plate (P)",
                                       takefocus=False, command=self._toggle_plate)
        self.plate_button.pack(side="left", padx=(px(self, 6), 0))
        return mid

    def _build_side(self, master) -> ttk.Frame:
        wide = px(self, 250)
        side = ttk.Frame(master, width=wide)
        side.columnconfigure(0, weight=1)
        self.magnifier = Magnifier(side, size=wide)
        self.magnifier.grid(row=0, column=0, sticky="n", pady=(0, px(self, 8)))

        self.spot_title = ttk.Label(side, text="", font=FONT_STRONG,
                                    wraplength=wide, justify="left")
        self.spot_title.grid(row=1, column=0, sticky="ew")
        self.spot_text = ttk.Label(side, text="", justify="left", wraplength=wide)
        self.spot_text.grid(row=2, column=0, sticky="ew", pady=(px(self, 2), 0))
        self.spot_flag = ttk.Label(side, text="", foreground=FLAG, justify="left",
                                   wraplength=wide)
        self.spot_flag.grid(row=3, column=0, sticky="ew")

        reasons = ttk.LabelFrame(side, text="Reasons for new flags",
                                 padding=(px(self, 8), px(self, 6)))
        reasons.grid(row=4, column=0, sticky="ew", pady=(px(self, 10), 0))
        reasons.columnconfigure(1, weight=1)
        ttk.Label(reasons, text="Spot:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(reasons, textvariable=self.spot_reason,
                     values=SPOT_REASONS).grid(row=0, column=1, sticky="ew",
                                               padx=(px(self, 6), 0), pady=2)
        ttk.Label(reasons, text="Plate:").grid(row=1, column=0, sticky="w")
        ttk.Combobox(reasons, textvariable=self.plate_reason,
                     values=PLATE_REASONS).grid(row=1, column=1, sticky="ew",
                                                padx=(px(self, 6), 0), pady=2)

        ttk.Label(side, text="Flags on this photo", font=FONT_STRONG).grid(
            row=5, column=0, sticky="w", pady=(px(self, 10), px(self, 2)))
        self.flag_list = tk.Listbox(side, height=6, activestyle="none",
                                    exportselection=False, borderwidth=0,
                                    highlightthickness=1,
                                    highlightbackground=tokens.LINE)
        self.flag_list.grid(row=6, column=0, sticky="ew")
        self.flag_list.bind("<<ListboxSelect>>", self._flag_selected)
        self.flag_list.bind("<Delete>", self._delete_selected_flag)
        self.flag_list.bind("<BackSpace>", self._delete_selected_flag)
        self._flag_rows: list = []

        ttk.Label(side, text=HINT, style="Small.TLabel", justify="left",
                  wraplength=wide).grid(row=7, column=0, sticky="ew",
                                        pady=(px(self, 10), 0))
        return side

    # -- keys ----------------------------------------------------------------

    def _bind_keys(self) -> None:
        bind = self.host.bind_key
        bind("<Control-s>", lambda _e: self._save())
        bind("<Control-z>", lambda _e: self._undo())
        bind("<Control-y>", lambda _e: self._redo())
        bind("<Control-Z>", lambda _e: self._redo())
        for seq, fn in (("<Prior>", lambda: self._step(-1)),
                        ("<Next>", lambda: self._step(1)),
                        ("<Left>", lambda: self._step(-1)),
                        ("<Right>", lambda: self._step(1)),
                        ("<space>", self._looks_good),
                        ("<KeyPress-p>", self._toggle_plate),
                        ("<KeyPress-P>", self._toggle_plate),
                        ("<KeyPress-v>", self._toggle_values),
                        ("<KeyPress-V>", self._toggle_values),
                        ("<KeyPress-f>", self._flag_selected_spot),
                        ("<KeyPress-F>", self._flag_selected_spot)):
            bind(seq, self._guarded(fn))

    def _typing(self) -> bool:
        try:
            w = self.focus_get()
        except (KeyError, tk.TclError):        # a combobox popdown has focus
            return True
        return isinstance(w, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox,
                              tk.Listbox))

    def _guarded(self, fn):
        def handler(_event=None):
            if self._typing():
                return None
            fn()
            return "break"
        return handler

    # -- loading -------------------------------------------------------------

    def _load(self) -> None:
        if self.ctl.loaded:
            # A reload re-reads the flags too -- another window may have saved
            # them -- unless that would throw away changes made here.
            try:
                self.ctl.reload_flags()
            except (OSError, ValueError) as exc:
                self._say(f"could not re-read the flags: {exc}", "error")
        self._say("Reading the experiment and its photo folder…")
        self.host.set_busy(True)
        run_in_thread(self, self.ctl.load, self._loaded)

    def _loaded(self, _result, error) -> None:
        self.host.set_busy(False)
        if self._closed:
            return
        if error is not None:
            self.ctl.error = f"{type(error).__name__}: {error}"
        e = self.ctl.experiment
        if e is not None:
            self.title_label.configure(text=f"{e.name}  —  data review")
            self.host.set_title(f"{e.name} — {APP_TITLE}",
                                tab=f"{e.name} · data")
        self._fill_list()
        self._refresh()
        if self.ctl.error:
            self._say(self.ctl.error.splitlines()[0], "error")
        if not self.ctl.photos:
            return
        self._say("Checking which photos have their spots located…")
        run_in_thread(self, self.ctl.check_detection, self._detection_checked)

    def _detection_checked(self, _result, error) -> None:
        if self._closed:
            return
        if error is not None:
            self._say(f"Spot locations cannot be read: {type(error).__name__}: "
                      f"{error}", "error")
        else:
            c = self.ctl.counts()
            missing = c["photos"] - c["detected"]
            self._say(f"{missing} photo(s) still need their spots located — press "
                      f"Locate spots…" if missing else
                      "Ready. Click a spot to flag it · Space: looks good, next "
                      "· P: flag the whole plate")
        self._showing = None                 # re-read this photo's grid
        self._refresh()

    # -- the photo list ------------------------------------------------------

    def _fill_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._list_ids.clear()
        self._list_paths.clear()
        groups: dict[tuple, str] = {}
        for i, photo in enumerate(self.ctl.visible()):
            key = (photo.group, photo.condition, photo.timepoint)
            parent = groups.get(key)
            if parent is None:
                bits = [b for b in (photo.group, photo.condition_label, photo.when) if b]
                parent = self.tree.insert("", "end", text=" · ".join(bits),
                                          open=True, tags=("group",))
                groups[key] = parent
            text = f"plate {photo.plate}" + (f", shot {photo.shot}"
                                             if photo.shot > 1 else "")
            iid = self.tree.insert(parent, "end", text=f"{text}   {Path(photo.relpath).name}")
            self._list_ids[photo.relpath] = iid
            self._list_paths[iid] = photo.relpath
        self._sync_list()

    def _status_of(self, photo) -> tuple[str, tuple]:
        f = self.ctl.flags
        bits = []
        if f.plate_reason(photo.relpath):
            bits.append("plate")
        n = len(f.spots_on(photo.relpath))
        if n:
            bits.append(f"{n} spot{'s' if n != 1 else ''}")
        if bits:
            return "⚑ " + ", ".join(bits), ("flagged",)
        if not self.ctl.detected.get(photo.relpath, False):
            return ("not located" if self.ctl.layout is not None else ""), ("undetected",)
        if f.is_reviewed(photo.relpath):
            return "✓ looked at", ("done",)
        return "", ()

    def _sync_list(self) -> None:
        for relpath, iid in self._list_ids.items():
            photo = self.ctl.find(relpath)
            if photo is None:
                continue
            text, tags = self._status_of(photo)
            self.tree.item(iid, values=(text,), tags=tags)
        current = self.ctl.current
        iid = self._list_ids.get(current.relpath) if current else None
        if iid and self.tree.selection() != (iid,):
            self.tree.selection_set(iid)
            self.tree.see(iid)

    def _tree_selected(self, _event=None) -> None:
        sel = self.tree.selection()
        relpath = self._list_paths.get(sel[0]) if sel else None
        if relpath:
            self.ctl.show(self.ctl.find(relpath))

    def _filter_changed(self, _event=None) -> None:
        self.ctl.set_filter(self.filter.get())
        self._fill_list()
        self.tree.focus_set()

    # -- redrawing -----------------------------------------------------------

    def _changed(self) -> None:
        """Controller callback. Coalesced: several changes, one redraw."""
        if self._refresh_id is None and not self._closed:
            self._refresh_id = self.after_idle(self._refresh)

    def _refresh(self) -> None:
        self._refresh_id = None
        if self._closed:
            return
        ctl = self.ctl
        c = ctl.counts()
        if ctl.loaded:
            text = (f"{c['reviewed']} of {c['photos']} photo(s) looked at  ·  "
                    f"{c['plates']} plate(s) and {c['spots']} spot(s) flagged")
            if ctl.layout is not None:
                text += f"  ·  spots located on {c['detected']} of {c['photos']}"
            if c["orphaned"]:
                text += (f"  ·  {c['orphaned']} flagged photo(s) are no longer "
                         f"in the experiment (kept)")
            self.counts_label.configure(text=text)
        elif ctl.error:
            self.counts_label.configure(text=ctl.error)
        dirty = ctl.dirty
        self.host.set_dirty(dirty)
        name = ctl.experiment.name if ctl.experiment else flagfile.stem_of(self.flags_path)
        self.host.set_title(f"{'*' if dirty else ''}{name} — {APP_TITLE}",
                            tab=f"{name} · data")
        running = self._job is not None and self._job.poll() is None
        self.detect_button.configure(text="Locating spots…" if running
                                     else "Locate spots…")
        self.detect_button.state(["disabled"] if running or not ctl.photos
                                 else ["!disabled"])
        if {p.relpath for p in ctl.visible()} != set(self._list_ids):
            self._fill_list()                # a flag moved a photo in or out
        else:
            self._sync_list()
        self._show_current()

    def _show_current(self) -> None:
        photo = self.ctl.current
        at, total = self.ctl.position()
        self.position.configure(text=f"{at} of {total}" if at else f"— of {total}")
        if photo is None:
            self.caption.configure(text="")
            self.where.configure(text="")
            self.banner.configure(text="")
            self.view.show_photo(None, self.ctl.error or (
                "No photographs to review." if self.ctl.loaded else "Loading…"))
            self.magnifier.clear()
            self._show_spot()
            return
        self.caption.configure(text=photo.title)
        self.where.configure(text=photo.relpath)
        plate = self.ctl.flags.plate_reason(photo.relpath)
        located = self.ctl.detected.get(photo.relpath, False)
        if plate:
            banner = f"⚑ Whole plate flagged: {plate}"
        elif self.ctl.layout is not None and not located:
            banner = ("Spots not located on this photo yet — press Locate "
                      "spots… to run detection. The plate can still be flagged.")
        else:
            banner = ""
        self.banner.configure(text=banner,
                              foreground=FLAG if plate else tokens.WARNING)
        self.plate_button.configure(text="Clear plate flag (P)" if plate
                                    else "Flag whole plate (P)")
        if self._showing is not photo:
            self._open_photo(photo)
        else:
            self._redraw_overlay()

    def _open_photo(self, photo) -> None:
        """Show a photo: its pixels and its grid, read off the UI thread."""
        self._showing = photo
        self._spot = None
        self.previews.drop_full(photo.path)
        pixels = self.previews.get(photo.path)
        if pixels is not None and self.ctl.has_geometry(photo):
            self.view.show_photo(pixels)
            self._redraw_overlay()
            self._load_full(photo, pixels)
            self._prefetch()
            return
        self.view.show_photo(None, "Opening the photograph…"
                             + ("\n(cloud-only: downloading it first)"
                                if photo.cloud_only else ""))
        # The worker holds no reference to the window: if this tab closes
        # meanwhile, the window must not be freed from the worker thread.
        previews, ctl = self.previews, self.ctl

        def work():
            got = previews.get(photo.path) or open_preview(photo.path)
            if ctl.layout is not None:
                ctl.geometry(photo)
            return got

        def done(pixels, error):
            if self._closed or self.ctl.current is not photo:
                return
            if error is not None:
                self.view.show_photo(None, f"This photograph could not be shown:\n"
                                           f"{type(error).__name__}: {error}")
                return
            self.previews.put(pixels)
            self.view.show_photo(pixels)
            self._redraw_overlay()
            self._sync_list()
            self._load_full(photo, pixels)
            self._prefetch()

        run_in_thread(self, work, done)

    def _load_full(self, photo, pixels) -> None:
        if pixels.full is not None:
            return

        def done(full, error):
            if error is None and not self._closed:
                pixels.full = full
                if self.ctl.current is photo:
                    self._show_spot()

        run_in_thread(self, lambda: open_full(photo.path), done)

    def _prefetch(self) -> None:
        """Decode the next photo while this one is being looked at."""
        pool = self.ctl.visible()
        if self.ctl.current not in pool:
            return
        i = pool.index(self.ctl.current) + 1
        if i >= len(pool):
            return
        nxt = pool[i]
        if self.previews.get(nxt.path) is not None or nxt.cloud_only:
            return
        ctl = self.ctl

        def work():
            pixels = open_preview(nxt.path)
            if ctl.layout is not None:
                ctl.geometry(nxt)
            return pixels

        run_in_thread(self, work, lambda pixels, error: (
            self.previews.put(pixels) if error is None else None))

    def _redraw_overlay(self) -> None:
        photo = self.ctl.current
        if photo is None:
            return
        found = self.ctl.cached_geometry(photo)
        cells = self.ctl.cells(photo)
        flags = {(r - 1, c - 1): f for (r, c), f in
                 self.ctl.flags.spots_on(photo.relpath).items()}
        self.view.set_overlay(found, cells, flags,
                              self.ctl.flags.plate_reason(photo.relpath),
                              self.show_values.get())
        self._fill_flag_list(photo, flags, cells)
        self._show_spot()

    def _fill_flag_list(self, photo, flags, cells) -> None:
        self.flag_list.delete(0, "end")
        self._flag_rows = []
        plate = self.ctl.flags.plate_reason(photo.relpath)
        if plate:
            self.flag_list.insert("end", f"Whole plate — {plate}")
            self._flag_rows.append(None)
        for (r, c), flag in sorted(flags.items()):
            cell = cells.get((r, c))
            who = (f"{cell.strain or 'empty slot'} rep {cell.replicate}"
                   if cell else f"row {r + 1}, col {c + 1}")
            self.flag_list.insert("end", f"{who} — {flag.reason}")
            self._flag_rows.append((r, c))

    def _show_spot(self) -> None:
        photo = self.ctl.current
        found = self.ctl.cached_geometry(photo)
        cells = self.ctl.cells(photo) if photo else {}
        flags = ({(r - 1, c - 1): f for (r, c), f in
                  self.ctl.flags.spots_on(photo.relpath).items()} if photo else {})
        pixels = self.previews.get(photo.path) if photo else None
        if found is None or self._spot is None:
            self.magnifier.show(pixels, found, None, flags, cells)
            self.spot_title.configure(text="")
            self.spot_text.configure(text="")
            self.spot_flag.configure(text="")
            return
        self.magnifier.show(pixels, found, self._spot, flags, cells)
        r, c = self._spot
        cell = cells.get((r, c))
        grey = found.net.get((r, c))
        lines = []
        if cell is None:
            self.spot_title.configure(text=f"Row {r + 1}, column {c + 1}")
            lines.append("Nothing is spotted here in the plate template.")
        else:
            from .spots import control_mean

            self.spot_title.configure(text=cell.label)
            ctrl = control_mean(found, cells, cell.level)
            value = f"Grey {grey:.2f}" if grey is not None else "Grey —"
            if grey is not None and grey <= found.floor:
                # A ratio of noise to the control is a number that means
                # nothing, and a negative one reads like an error.
                value += "   ·   no growth above the plate's noise"
            elif ctrl and not cell.is_control and grey is not None:
                value += f"   ·   {grey / ctrl:.2f} × the plate's control"
            lines.append(value)
            others = []
            for sib in siblings(cell, cells):
                g = found.net.get((sib.row, sib.col))
                mark = " ⚑" if (sib.row, sib.col) in flags else ""
                others.append(f"rep {sib.replicate} {g:.2f}{mark}"
                              if g is not None else f"rep {sib.replicate} —")
            if others:
                lines.append("Same strain and dilution here: " + ", ".join(others))
            lines.append(f"Row {r + 1}, column {c + 1} of the photograph")
        if (r, c) in found.rim:
            lines.append("The engine flags this ROI as touching the rim or a label.")
        if found.approximate:
            lines.append("(Values from another dilution's ROI: this photo was "
                         "measured for a different design.)")
        self.spot_text.configure(text="\n".join(lines))
        flag = flags.get((r, c))
        self.spot_flag.configure(text=f"⚑ Flagged: {flag.reason}" if flag else "")

    # -- acting --------------------------------------------------------------

    def _say(self, text: str, kind: str = "") -> None:
        colour = {"error": tokens.ERROR, "ok": tokens.OK,
                  "warn": tokens.WARNING}.get(kind, tokens.TEXT_MUTED)
        self.status.configure(text=text, foreground=colour)

    def _hovered(self, cell) -> None:
        self._spot = cell
        self._show_spot()

    def _clicked(self, cell) -> None:
        self._spot = cell
        if self.ctl.toggle_spot(*cell, self.spot_reason.get().strip()):
            self._say(self.ctl.last_change)

    def _flag_selected_spot(self) -> None:
        if self._spot is not None:
            self._clicked(self._spot)

    def _context_menu(self, cell, x: int, y: int) -> None:
        photo = self.ctl.current
        if photo is None:
            return
        menu = tk.Menu(self, tearoff=False)
        if cell is not None:
            self._spot = cell
            self._show_spot()
            flagged = self.ctl.flags.spot_reason(photo.relpath, cell[0] + 1, cell[1] + 1)
            if flagged:
                menu.add_command(label="Clear this spot's flag",
                                 command=lambda: self.ctl.set_spot(*cell, None))
            menu.add_command(label="Flag this spot with a reason…",
                             command=lambda: self._ask_spot(cell))
            menu.add_separator()
        plate = self.ctl.flags.plate_reason(photo.relpath)
        if plate:
            menu.add_command(label="Clear the plate flag",
                             command=lambda: self.ctl.set_plate(None))
        menu.add_command(label="Flag the whole plate with a reason…",
                         command=self._ask_plate)
        menu.add_separator()
        menu.add_command(label="Clear every flag on this photo",
                         command=self.ctl.clear_photo)
        reviewed = photo.relpath in self.ctl.flags.reviewed
        menu.add_command(label="Mark as not looked at" if reviewed
                         else "Mark as looked at",
                         command=lambda: self.ctl.set_reviewed(not reviewed))
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _ask_spot(self, cell) -> None:
        reason = simpledialog.askstring(
            "Flag spot", "Why is this spot not good data?",
            initialvalue=self.spot_reason.get(), parent=self)
        if reason and reason.strip():
            self.ctl.set_spot(*cell, reason.strip())

    def _ask_plate(self) -> None:
        reason = simpledialog.askstring(
            "Flag plate", "Why is this whole plate not good data?",
            initialvalue=self.plate_reason.get(), parent=self)
        if reason and reason.strip():
            self.ctl.set_plate(reason.strip())

    def _toggle_plate(self) -> None:
        photo = self.ctl.current
        if photo is None:
            return
        if self.ctl.flags.plate_reason(photo.relpath):
            self.ctl.set_plate(None)
        else:
            reason = self.plate_reason.get().strip() or "flagged in data review"
            self.ctl.set_plate(reason)
        self._say(self.ctl.last_change)

    def _toggle_values(self) -> None:
        self.show_values.set(not self.show_values.get())
        self._redraw_overlay()

    def _looks_good(self) -> None:
        if self.ctl.current is None:
            return
        self.ctl.set_reviewed(True)
        if not self.ctl.step(1):
            c = self.ctl.counts()
            left = c["photos"] - c["reviewed"]
            self._say("That was the last photo." + (
                f" {left} still not looked at — choose Show: Not looked at."
                if left else " Every photo has been looked at."), "ok")

    def _step(self, delta: int) -> None:
        if not self.ctl.step(delta):
            self._say("No more photos that way.")

    def _flag_selected(self, _event=None) -> None:
        sel = self.flag_list.curselection()
        if sel and sel[0] < len(self._flag_rows) and self._flag_rows[sel[0]]:
            self._spot = self._flag_rows[sel[0]]
            self.view.highlight(self._spot)
            self._show_spot()

    def _delete_selected_flag(self, _event=None) -> str:
        sel = self.flag_list.curselection()
        if sel and sel[0] < len(self._flag_rows):
            row = self._flag_rows[sel[0]]
            if row is None:
                self.ctl.set_plate(None)
            else:
                self.ctl.set_spot(*row, None)
        return "break"

    # -- detection -----------------------------------------------------------

    def _detect(self) -> None:
        ctl = self.ctl
        if ctl.experiment is None or not ctl.photos:
            return
        if ctl.layout is None:
            messagebox.showinfo(APP_TITLE, "Still checking which photos are done; "
                                "try again in a moment.", parent=self)
            return
        todo = [p for p in ctl.photos if not ctl.detected.get(p.relpath)]
        if not todo:
            messagebox.showinfo(APP_TITLE, "Every photo already has its spots "
                                "located.", parent=self)
            return
        workers = max(1, min(8, (os.cpu_count() or 2) // 2))
        minutes = len(todo) * 70 / 60 / workers
        where = ("in a separate window" if self.host.standalone
                 else "in the Jobs panel")
        if not messagebox.askokcancel(
                APP_TITLE,
                f"Locate the spots on {len(todo)} photo(s)?\n\nThis is the "
                f"measurement step of a run, about {minutes:.0f} min on "
                f"{workers} worker(s), and runs {where}. A later run reuses "
                f"it, so nothing is done twice.\n\nPhotos appear here as they "
                f"are finished; you can start reviewing straight away.",
                parent=self):
            return
        spec = JobSpec(
            title=f"{ctl.experiment.name} — locate spots",
            argv=program_command("data-review-cli", "detect",
                                 str(ctl.experiment_path.resolve())),
            # Detection IS the measuring step of a run, on the same processors
            # and the same cache, so it queues with runs rather than racing one.
            cwd=str(PROJECT_ROOT), kind="experiment-run",
            console_env={"SPOTTING_NEW_CONSOLE": "1", "SPOTTING_PAUSE": "1"},
            new_console=True, status_env="DATA_REVIEW_JOB_STATUS",
            meta={"experiment": str(ctl.experiment_path)})
        try:
            self._job = self.host.run_job(spec)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Could not start detection:\n{exc}",
                                 parent=self)
            return
        self._job_ticks = 0
        self._say("Locating spots" + (" — follow it in the Jobs panel."
                                      if not self.host.standalone else "…"))
        self._refresh()
        self._poll_job()

    def _poll_job(self) -> None:
        self._job_poll = None
        if self._closed or self._job is None:
            return
        code = self._job.poll()
        self._job_ticks += 1
        if code is None:
            if self._job_ticks % max(1, JOB_RECHECK_MS // JOB_POLL_MS) == 0:
                run_in_thread(self, self.ctl.check_detection,
                              lambda *_: self._after_recheck(final=False))
            self._job_poll = self.after(JOB_POLL_MS, self._poll_job)
            return
        self._job = None
        self._say("Detection finished." if code == 0 else
                  f"Detection ended with exit code {code}; see its log.",
                  "ok" if code == 0 else "warn")
        run_in_thread(self, self.ctl.check_detection,
                      lambda *_: self._after_recheck(final=True, code=code))

    def _after_recheck(self, final: bool, code: int = 0) -> None:
        if self._closed:
            return
        photo = self.ctl.current
        if photo is not None and self.ctl.cached_geometry(photo) is None:
            self._showing = None             # its grid may exist now
        self._refresh()
        if final:
            c = self.ctl.counts()
            name = self.ctl.experiment.name if self.ctl.experiment else ""
            self.host.notify(
                f"{name}: spots located" if code == 0 else
                f"{name}: spot detection did not finish",
                f"Spots are located on {c['detected']} of {c['photos']} photo(s).",
                kind="info" if code == 0 else "warning")

    # -- elsewhere -----------------------------------------------------------

    def _open_experiment(self) -> None:
        if not self.host.open_document("experiment", self.ctl.experiment_path):
            messagebox.showinfo(APP_TITLE, str(self.ctl.experiment_path),
                                parent=self)

    # -- saving and history --------------------------------------------------

    def _save(self) -> bool:
        try:
            path = self.ctl.save()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not save:\n{exc}", parent=self)
            return False
        self.host.set_path(path)
        self._say(f"Saved {path.name}", "ok")
        return True

    def _undo(self) -> None:
        label = self.ctl.undo()
        self._say(f"Undid: {label}" if label else "Nothing to undo.")

    def _redo(self) -> None:
        label = self.ctl.redo()
        self._say(f"Redid: {label}" if label else "Nothing to redo.")

    def _can_close(self) -> bool:
        if not self.ctl.dirty:
            return True
        answer = messagebox.askyesnocancel(
            APP_TITLE, "Save the data review first?", parent=self)
        if answer is None:
            return False
        return self._save() if answer else True

    def _dispose(self) -> None:
        self._closed = True
        for timer in (self._job_poll, self._refresh_id):
            if timer is not None:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        self.previews.drop_full(None)
        # A load still running on a worker holds the controller; without this
        # it would also hold the window, and free it from that thread.
        self.ctl.on_change = None
        # Tk variables must be freed on this thread. Left to the cycle
        # collector, they go whenever it next runs -- on whichever thread
        # that happens to be, where Tk refuses ("main thread is not in main
        # loop").
        for name in ("show_values", "spot_reason", "plate_reason", "filter"):
            setattr(self, name, None)


# ---------------------------------------------------------------------------


def flags_path_for(path: Path) -> Path:
    """Accept either an experiment or its data review; return the review's path."""
    path = Path(path)
    return path if flagfile.is_flags_file(path) else flagfile.sidecar_for(path)


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="data_review", description=APP_TITLE)
    parser.add_argument("file", nargs="?",
                        help="an experiment .spotexp.json, or its .datareview.json")
    parser.add_argument("--selftest", action="store_true",
                        help="build the window, render once, and exit")
    args = parser.parse_args(argv)

    enable_dpi_awareness()
    favour_the_window()
    root = tk.Tk()
    path = Path(args.file) if args.file else None
    if path is None:
        root.withdraw()
        from tkinter import filedialog

        from experiments.app import default_experiment_dir

        chosen = filedialog.askopenfilename(
            parent=root, title="Review the data of which experiment?",
            initialdir=str(default_experiment_dir()),
            filetypes=[("Experiment", "*.spotexp.json"),
                       ("Data review", "*.datareview.json"), ("All files", "*.*")])
        if not chosen:
            root.destroy()
            return 0
        path = Path(chosen)
        root.deiconify()
    try:
        app = DataReviewApp(StandaloneHost(root, APP_TITLE), flags_path_for(path))
    except ValueError as exc:
        messagebox.showerror(APP_TITLE, str(exc), parent=root)
        root.destroy()
        return 2
    if args.selftest:
        root.update_idletasks()
        root.update()
        app.ctl.load()
        app._refresh()
        root.update()
        root.destroy()
        print(f"selftest OK: {len(app.ctl.photos)} photo(s)")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
