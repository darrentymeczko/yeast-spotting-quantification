"""The sheet pane: the comparison figure for whichever candidate is highlighted.

Each sheet is the marked spot montage beside the graph that candidate would
actually produce -- drawn by the pipeline through the same PyPrism renderer the
final figure uses, so what is on screen is the result, not an impression of it.

The pane redraws on resize, and only then: scaling a 1500 px sheet on every
mouse move makes the window feel broken. A short debounce is the difference.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .. import theme
from ..model import Candidate
from .imaging import HAVE_PIL, ImageCache

#: Milliseconds of quiet after a resize before the sheet is rescaled.
REDRAW_DELAY = 120
ADJUST_DELAY = 60
MIN_ZOOM = 1.0
MAX_ZOOM = 5.0
ZOOM_STEP = 1.25


class SheetView(ttk.Frame):
    def __init__(self, master, on_redraw=None, on_toggle=None, **kw) -> None:
        super().__init__(master, **kw)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", padx=theme.PAD)
        top.columnconfigure(0, weight=1)

        # width=1 so the caption's own text length cannot drive the row's
        # requested width. A long caption in a weight=1 column pushes the
        # buttons past the right edge of the window, where they cannot be
        # clicked -- the caption is the part that can afford to be clipped.
        self.caption = ttk.Label(top, text="", font=theme.FONT_SMALL,
                                 foreground=theme.MUTED, anchor="w", width=1)
        self.caption.grid(row=0, column=0, sticky="ew", pady=4)

        self.btn_toggle = ttk.Button(top, text="Original sheet (G)", width=17,
                                     command=on_toggle or (lambda: None))

        # Display-only controls: these never change measurements, review data,
        # statistical results, source files, or exported figures.
        self.display_controls = ttk.Frame(
            self, padding=(theme.GAP, theme.PAD))
        self.display_controls.columnconfigure(1, weight=1)
        self.display_controls.columnconfigure(4, weight=1)

        self._adjust_after: "str | None" = None
        self._brightness_var = tk.DoubleVar(value=1.0)
        self._contrast_var = tk.DoubleVar(value=1.0)
        self._brightness_text = tk.StringVar(value="100%")
        self._contrast_text = tk.StringVar(value="100%")
        self._zoom_text = tk.StringVar(value="100%")

        ttk.Label(self.display_controls, text="Brightness",
                  font=theme.FONT_SMALL).grid(row=0, column=0,
                                              padx=(0, theme.GAP))
        self.brightness_scale = ttk.Scale(
            self.display_controls, from_=0.25, to=2.0,
            variable=self._brightness_var, command=self._adjusted)
        self.brightness_scale.grid(row=0, column=1, sticky="ew")
        ttk.Label(self.display_controls, textvariable=self._brightness_text,
                  width=5, anchor="e", font=theme.FONT_SMALL).grid(
            row=0, column=2, padx=(2, theme.PAD))

        ttk.Label(self.display_controls, text="Contrast",
                  font=theme.FONT_SMALL).grid(row=0, column=3,
                                              padx=(0, theme.GAP))
        self.contrast_scale = ttk.Scale(
            self.display_controls, from_=0.25, to=3.0,
            variable=self._contrast_var, command=self._adjusted)
        self.contrast_scale.grid(row=0, column=4, sticky="ew")
        ttk.Label(self.display_controls, textvariable=self._contrast_text,
                  width=5, anchor="e", font=theme.FONT_SMALL).grid(
            row=0, column=5, padx=(2, 0))

        ttk.Label(self.display_controls,
                  text="Mouse wheel zooms · drag to pan",
                  foreground=theme.MUTED, font=theme.FONT_SMALL).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.btn_toggle.grid(row=1, column=2, sticky="e",
                             padx=(theme.GAP, theme.PAD), pady=(2, 0))
        zoom = ttk.Frame(self.display_controls)
        zoom.grid(row=1, column=3, columnspan=3, sticky="e", pady=(2, 0))
        ttk.Label(zoom, text="Zoom", font=theme.FONT_SMALL).grid(
            row=0, column=0, padx=(0, theme.GAP))
        self.btn_zoom_out = ttk.Button(
            zoom, text="−", width=3, command=lambda: self._zoom_by(1 / ZOOM_STEP))
        self.btn_zoom_out.grid(row=0, column=1)
        ttk.Label(zoom, textvariable=self._zoom_text, width=5, anchor="center",
                  font=theme.FONT_SMALL).grid(row=0, column=2, padx=2)
        self.btn_zoom_in = ttk.Button(
            zoom, text="+", width=3, command=lambda: self._zoom_by(ZOOM_STEP))
        self.btn_zoom_in.grid(row=0, column=3)
        self.btn_reset_view = ttk.Button(zoom, text="Reset view",
                                         command=self.reset_view)
        self.btn_reset_view.grid(row=0, column=4, padx=(theme.GAP, 0))

        self.canvas = tk.Canvas(self, background=theme.SUNKEN,
                                highlightthickness=0, height=theme.SHEET_MIN_H)
        # Its right edge lines up with the right edge of the summary table
        # below.  The asymmetric inset belongs to both pieces of content; the
        # outer application pane contributes the remaining window margin.
        self.canvas.grid(row=1, column=0, sticky="nsew",
                         padx=(theme.PAD, 2 * theme.PAD), pady=(2, 0))

        self.cache = ImageCache()
        self._path: "Path | None" = None
        self._cand: "Candidate | None" = None
        self._after: "str | None" = None
        self._image = None                      # keeps the Tk image referenced
        self._updated: "Path | None" = None     # graph redrawn from corrections
        self._updated_is_publication = True
        self._showing_updated = False
        self._zoom = MIN_ZOOM
        self._pan_x = 0.0
        self._pan_y = 0.0
        self._drag_at: "tuple[int, int] | None" = None
        self._hide_after: "str | None" = None
        self.canvas.bind("<Configure>", self._resized)
        self.canvas.bind("<Enter>", self._photo_motion)
        self.canvas.bind("<Motion>", self._photo_motion)
        self.canvas.bind("<Leave>", self._schedule_hide_controls)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._wheel(e, 1))
        self.canvas.bind("<Button-5>", lambda e: self._wheel(e, -1))
        self.canvas.bind("<ButtonPress-1>", self._pan_start)
        self.canvas.bind("<B1-Motion>", self._pan_drag)
        self.canvas.bind("<ButtonRelease-1>", self._pan_end)

        if not HAVE_PIL:
            self.brightness_scale.state(["disabled"])
            self.contrast_scale.state(["disabled"])

        self._bind_control_hover(self.display_controls)

    # -- controls over the graph --------------------------------------------

    def show_toggle(self, on: bool) -> None:
        """The sheet/graph swap only exists once there is a graph to swap to."""
        if on:
            self.btn_toggle.grid()
        else:
            self.btn_toggle.grid_remove()

    # -- contents ------------------------------------------------------------

    def show(self, cand: "Candidate | None", path: "Path | None") -> None:
        """The pipeline's comparison sheet for a candidate."""
        self._cand = cand
        self._path = path
        self._updated = None
        self._showing_updated = False
        self._reset_position()
        self._caption()
        self._draw()

    def show_updated(self, graph: "Path | None",
                     publication: bool = True) -> None:
        """A graph redrawn from the current corrections, in place of the sheet."""
        if graph is not None:
            # Each redraw is written to the same filename, so the scaled copy
            # already in the cache is of the previous edit's graph.
            self.cache.invalidate(graph)
        self._updated = graph
        self._updated_is_publication = publication
        self._showing_updated = graph is not None
        self._reset_position()
        self._caption()
        self._draw()

    def toggle(self) -> bool:
        """Swap between the pipeline's sheet and the redrawn graph."""
        if self._updated is None:
            return False
        self._showing_updated = not self._showing_updated
        self._reset_position()
        self._caption()
        self._draw()
        return True

    @property
    def has_updated(self) -> bool:
        return self._updated is not None

    def _caption(self) -> None:
        self.show_toggle(self._updated is not None)
        if self._cand is None:
            self.caption.configure(text="", foreground=theme.MUTED)
            return
        c = self._cand
        base = (f"{c.medium_label or c.medium} · {c.timepoint} · "
                f"{c.dilution} dilution · {c.detail}")
        if self._showing_updated:
            how = ("redrawn from your corrections"
                   if self._updated_is_publication
                   else "redrawn from your corrections (built-in preview; "
                        "no significance testing)")
            self.caption.configure(text=f"{base}   —   {how}",
                                   foreground=theme.MANUAL)
            self.btn_toggle.configure(text="Original sheet (G)")
        else:
            self.caption.configure(text=base, foreground=theme.MUTED)
            self.btn_toggle.configure(text="Your graph (G)")

    def clear(self) -> None:
        self.show(None, None)

    # -- drawing -------------------------------------------------------------

    def _bind_control_hover(self, widget) -> None:
        """Keep an overlay open while its sliders and buttons are in use."""
        widget.bind("<Enter>", self._show_controls, add="+")
        widget.bind("<Leave>", self._schedule_hide_controls, add="+")
        for child in widget.winfo_children():
            self._bind_control_hover(child)

    def _photo_motion(self, event) -> None:
        """Reveal only in the top strip the hidden toolbar actually occupies."""
        if event.y < self.display_controls.winfo_reqheight():
            self._show_controls()
        elif self.display_controls.winfo_manager():
            self._hide_controls()

    def _show_controls(self, _event=None) -> None:
        if self._hide_after is not None:
            self.after_cancel(self._hide_after)
            self._hide_after = None
        if self._image is None:
            return
        # Cover the full canvas width. The frame supplies its own inner
        # padding; an outer inset here exposed two distracting strips of the
        # image at the left and right edges of the grey overlay.
        width = max(1, self.canvas.winfo_width())
        self.display_controls.place(in_=self.canvas, x=0, y=0,
                                    width=width)
        self.display_controls.lift()

    def _schedule_hide_controls(self, _event=None) -> None:
        if self._hide_after is not None:
            self.after_cancel(self._hide_after)
        self._hide_after = self.after(120, self._hide_controls_if_outside)

    def _hide_controls_if_outside(self) -> None:
        self._hide_after = None
        x, y = self.winfo_pointerxy()
        left, top = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        inside = (left <= x < left + self.canvas.winfo_width() and
                  top <= y < top + self.canvas.winfo_height())
        if not inside:
            self._hide_controls()

    def _hide_controls(self) -> None:
        if self._hide_after is not None:
            self.after_cancel(self._hide_after)
            self._hide_after = None
        self.display_controls.place_forget()

    def _adjusted(self, _value=None) -> None:
        """Debounce continuous sliders and show their current percentages."""
        self._brightness_text.set(f"{self._brightness_var.get():.0%}")
        self._contrast_text.set(f"{self._contrast_var.get():.0%}")
        if self._adjust_after is not None:
            self.after_cancel(self._adjust_after)
        self._adjust_after = self.after(ADJUST_DELAY, self._draw)

    def _reset_position(self) -> None:
        self._zoom = MIN_ZOOM
        self._pan_x = self._pan_y = 0.0
        self._zoom_text.set("100%")

    def reset_view(self) -> None:
        """Restore fit-to-window, neutral tones, and a centred image."""
        self._brightness_var.set(1.0)
        self._contrast_var.set(1.0)
        self._brightness_text.set("100%")
        self._contrast_text.set("100%")
        self._reset_position()
        self._draw()

    def _zoom_by(self, factor: float, event=None):
        old = self._zoom
        self._zoom = min(MAX_ZOOM, max(MIN_ZOOM, old * factor))
        if self._zoom == old:
            return "break"

        # Keep the point beneath the mouse approximately fixed while zooming.
        # _draw performs the final clamp at the image edges.
        if event is not None:
            cx = self.canvas.winfo_width() / 2
            cy = self.canvas.winfo_height() / 2
            ratio = self._zoom / old
            self._pan_x = event.x - cx - (event.x - cx - self._pan_x) * ratio
            self._pan_y = event.y - cy - (event.y - cy - self._pan_y) * ratio
        self._zoom_text.set(f"{self._zoom:.0%}")
        self._draw()
        return "break"

    def _wheel(self, event, direction: "int | None" = None):
        direction = (direction if direction is not None
                     else (1 if event.delta > 0 else -1))
        return self._zoom_by(ZOOM_STEP if direction > 0 else 1 / ZOOM_STEP,
                             event)

    def _pan_start(self, event) -> None:
        if self._image is not None and self._zoom > MIN_ZOOM:
            self._drag_at = (event.x, event.y)
            self.canvas.configure(cursor="fleur")

    def _pan_drag(self, event) -> None:
        if self._drag_at is None:
            return
        x, y = self._drag_at
        self._pan_x += event.x - x
        self._pan_y += event.y - y
        self._drag_at = (event.x, event.y)
        self._draw()

    def _pan_end(self, _event=None) -> None:
        self._drag_at = None
        self.canvas.configure(cursor="")

    def _resized(self, _event=None) -> None:
        if self._after is not None:
            self.after_cancel(self._after)
        self._after = self.after(REDRAW_DELAY, self._draw)

    def _message(self, text: str, colour: str = theme.MUTED) -> None:
        self._hide_controls()
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 1)
        h = max(self.canvas.winfo_height(), 1)
        self.canvas.create_text(w // 2, h // 2, text=text, fill=colour,
                                font=theme.FONT, width=max(200, w - 4 * theme.PAD),
                                justify="center")

    def _draw(self) -> None:
        self._after = None
        self._adjust_after = None
        self.canvas.delete("all")
        self._image = None
        if self._cand is None:
            return self._message("Choose a candidate to see its sheet.")

        path = self._updated if self._showing_updated else self._path
        if path is None or not Path(path).exists():
            if self._showing_updated:
                return self._message("Could not read the redrawn graph.",
                                     theme.ERROR)
            return self._message(
                "No sheet was drawn for this candidate.\n\n"
                "The run was given --figures none, or a limit per medium. "
                "The candidate can still be chosen and exported; only the "
                "preview is missing.", theme.WARNING)

        canvas_w = max(self.canvas.winfo_width() - 2, 64)
        canvas_h = max(self.canvas.winfo_height() - 2, 64)
        box = (int(canvas_w * self._zoom), int(canvas_h * self._zoom))
        img = self.cache.get(path, box, self._brightness_var.get(),
                             self._contrast_var.get())
        if img is None:
            return self._message(f"Could not read {Path(path).name}.",
                                 theme.ERROR)
        self._image = img
        max_x = max((img.width() - canvas_w) / 2, 0)
        max_y = max((img.height() - canvas_h) / 2, 0)
        self._pan_x = min(max_x, max(-max_x, self._pan_x))
        self._pan_y = min(max_y, max(-max_y, self._pan_y))
        self.canvas.create_image(canvas_w // 2 + 1 + self._pan_x,
                                 canvas_h // 2 + 1 + self._pan_y, image=img)
        if self._showing_updated:
            # No overlay caption here: the graph carries its own footnote and
            # the line above the canvas already says whose numbers these are.
            return
        if not HAVE_PIL:
            # Say so rather than let a coarse preview be read as a bad plate.
            self.canvas.create_text(
                4, canvas_h - 4, anchor="sw", fill=theme.MUTED,
                font=theme.FONT_SMALL,
                text="preview scaled coarsely — install Pillow for a smooth one")
