"""The plate grid, drawn on a tk.Canvas.

Two deliberate choices:

* Every change redraws the whole canvas. A plate is a few hundred items and
  redraws in single-digit milliseconds, which buys immunity from a whole class
  of stale-item bugs.
* Hit testing is arithmetic, not `find_closest`. The lattice is uniform, so the
  cell under a pointer is a division -- exact, fast, and indifferent to which
  item happens to be on top.

The canvas owns no editing rules. A drag is turned into a Plan by a provider
the application supplies, drawn as ghosts, and only committed on release, so
what the user sees before letting go is exactly what gets written.

All cell geometry goes through `cell_rect()` so a later photo overlay only has
to change one function.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import Callable

from .. import theme
from ..autofill import Plan
from ..model import CellKind


class GridCanvas(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        controller,
        plate_id: str,
        on_hover: Callable[[tuple[int, int] | None], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(master, background=theme.BG, highlightthickness=0, **kwargs)
        self.controller = controller
        self.plate_id = plate_id
        self.show_tokens = False
        self.on_hover = on_hover

        #: Supplied by the app: turns a drag into a Plan (anchor, current).
        self.plan_provider: Callable[
            [tuple[int, int], tuple[int, int]], Plan | None
        ] | None = None
        #: Supplied by the app: commits a Plan produced by plan_provider.
        self.commit_handler: Callable[[Plan], None] | None = None
        #: Supplied by the app: single-click tools (e.g. set control).
        self.click_handler: Callable[[tuple[int, int]], None] | None = None
        #: Supplied by the app: double-click opens the cell editor.
        self.edit_handler: Callable[[tuple[int, int]], None] | None = None
        #: Supplied by the app: the empty-grid prompt.
        self.empty_hint: str = ""

        self._origin = (0.0, 0.0)
        self._pitch = 0.0
        self._hover: tuple[int, int] | None = None
        self._anchor: tuple[int, int] | None = None
        #: (slot, replicate) -> least dilute level present; rebuilt per redraw.
        self._lowest_dilution: dict[tuple[int, int], int] = {}
        self._preview: Plan | None = None
        self._flash: set[tuple[int, int]] = set()
        self._flash_job: str | None = None

        #: Reserved for the deferred photo overlay; drawn first, tagged "bg".
        self.background_image: tk.PhotoImage | None = None

        self._base_font = tkfont.nametofont("TkDefaultFont")

        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double)

    # -- model access --------------------------------------------------------

    @property
    def template(self):
        return self.controller.template

    @property
    def plate(self):
        return self.template.plate(self.plate_id)

    # -- geometry ------------------------------------------------------------

    def _compute_geometry(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        rows, cols = self.template.rows, self.template.cols
        avail_w = w - theme.HEADER - 2 * theme.PAD
        avail_h = h - theme.HEADER - 2 * theme.PAD
        pitch = min(avail_w / max(cols, 1), avail_h / max(rows, 1))
        self._pitch = max(pitch, 0.0)
        self._origin = (
            theme.PAD + theme.HEADER + (avail_w - self._pitch * cols) / 2,
            theme.PAD + theme.HEADER + (avail_h - self._pitch * rows) / 2,
        )

    def cell_rect(self, r: int, c: int) -> tuple[float, float, float, float]:
        ox, oy = self._origin
        p = self._pitch
        return ox + c * p, oy + r * p, ox + (c + 1) * p, oy + (r + 1) * p

    def cell_at(self, x: float, y: float) -> tuple[int, int] | None:
        if self._pitch <= 0:
            return None
        ox, oy = self._origin
        c = int((x - ox) // self._pitch)
        r = int((y - oy) // self._pitch)
        if 0 <= r < self.template.rows and 0 <= c < self.template.cols:
            return r, c
        return None

    # -- pointer -------------------------------------------------------------

    def _on_motion(self, event: tk.Event) -> None:
        cell = self.cell_at(event.x, event.y)
        if cell != self._hover:
            self._hover = cell
            self.redraw()
            if self.on_hover:
                self.on_hover(cell)

    def _on_leave(self, _event: tk.Event) -> None:
        if self._hover is not None:
            self._hover = None
            self.redraw()
        if self.on_hover:
            self.on_hover(None)

    def _on_press(self, event: tk.Event) -> None:
        cell = self.cell_at(event.x, event.y)
        if cell is None:
            return
        if self.click_handler is not None:
            self.click_handler(cell)
            return
        self._anchor = cell
        self._update_preview(cell)

    def _on_drag(self, event: tk.Event) -> None:
        if self._anchor is None:
            return
        cell = self.cell_at(event.x, event.y)
        if cell is not None:
            self._update_preview(cell)

    def _on_release(self, _event: tk.Event) -> None:
        plan, self._preview, self._anchor = self._preview, None, None
        if plan is not None and self.commit_handler is not None:
            self.commit_handler(plan)
        self.redraw()

    def _on_double(self, event: tk.Event) -> None:
        cell = self.cell_at(event.x, event.y)
        if cell is not None and self.edit_handler is not None:
            self._anchor = None
            self._preview = None
            self.edit_handler(cell)

    def _update_preview(self, current: tuple[int, int]) -> None:
        if self._anchor is None or self.plan_provider is None:
            return
        self._preview = self.plan_provider(self._anchor, current)
        self.redraw()

    def flash(self, cells) -> None:
        """Briefly highlight cells, e.g. when a validation row is selected."""
        self._flash = {tuple(c) for c in cells}
        self.redraw()
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(1400, self._clear_flash)

    def _clear_flash(self) -> None:
        self._flash_job = None
        self._flash = set()
        self.redraw()

    # -- drawing -------------------------------------------------------------

    def redraw(self) -> None:
        # A canvas inside a Notebook tab that has never been shown reports 1x1;
        # drawing then produces garbage that survives into the real layout.
        if self.winfo_width() < 50 or self.winfo_height() < 50:
            return
        self._compute_geometry()
        if self._pitch < theme.MIN_PITCH:
            self.delete("all")
            self.create_text(
                self.winfo_width() / 2, self.winfo_height() / 2,
                text="window too small to show the grid",
                fill=theme.MUTED, font=self._scaled_font(1.0),
            )
            return

        self.delete("all")
        self._recompute_labels()
        if self.background_image is not None:
            self.create_image(
                *self._origin, image=self.background_image, anchor="nw", tags="bg"
            )
        self._draw_headers()
        for r in range(self.template.rows):
            for c in range(self.template.cols):
                self._draw_cell(r, c)
        self._draw_preview()
        self._draw_empty_hint()

    def _scaled_font(self, fraction: float, *, bold: bool = False) -> tkfont.Font:
        # Negative sizes are PIXELS in Tk; positive are points. The pitch this
        # is derived from is in pixels, so a positive size would be inflated by
        # the display scaling -- 2.7x on a 200% monitor, enough to make tokens
        # overflow their cells.
        size = max(8, int(self._pitch * fraction))
        return tkfont.Font(
            family=self._base_font.cget("family"),
            size=-size,
            weight="bold" if bold else "normal",
        )

    def _draw_headers(self) -> None:
        font = self._scaled_font(0.20)
        ox, oy = self._origin
        for c in range(self.template.cols):
            x0, _, x1, _ = self.cell_rect(0, c)
            self.create_text(
                (x0 + x1) / 2, oy - theme.HEADER / 2,
                text=str(c + 1), fill=theme.MUTED, font=font,
            )
        for r in range(self.template.rows):
            _, y0, _, y1 = self.cell_rect(r, 0)
            self.create_text(
                ox - theme.HEADER / 2, (y0 + y1) / 2,
                text=str(r + 1), fill=theme.MUTED, font=font,
            )

    def _draw_cell(self, r: int, c: int) -> None:
        x0, y0, x1, y1 = self.cell_rect(r, c)
        cell = self.plate.get(r, c)

        if cell.kind is CellKind.EMPTY:
            self.create_rectangle(
                x0, y0, x1, y1, fill=theme.EMPTY_BG, outline=theme.GRID_LINE,
                stipple="gray25",
            )
        elif cell.kind is CellKind.UNASSIGNED:
            self.create_rectangle(
                x0, y0, x1, y1, fill=theme.UNASSIGNED_BG, outline=theme.GRID_LINE
            )
        else:
            self.create_rectangle(
                x0, y0, x1, y1, fill=theme.CELL_BG, outline=theme.GRID_LINE
            )
            self._draw_spot(r, c, x0, y0, x1, y1)

        if (r, c) in self._flash:
            self.create_rectangle(
                x0 + 1, y0 + 1, x1 - 1, y1 - 1, outline=theme.FLASH, width=3
            )
        if self._hover == (r, c):
            self.create_rectangle(
                x0 + 1, y0 + 1, x1 - 1, y1 - 1, outline=theme.HOVER, width=2
            )

    def _recompute_labels(self) -> None:
        """Work out where the sparse labels go, once per redraw.

        The sample number is printed on the least dilute spot of each series
        rather than on all of them, and the replicate number only where the
        replicate actually changes -- 48 of each was louder than the plate.
        """
        first: dict[tuple[int, int], int] = {}
        for _r, _c, p in self.plate.placements():
            unit = (p.sample_slot, p.replicate)
            if unit not in first or p.dilution < first[unit]:
                first[unit] = p.dilution
        self._lowest_dilution = first

    def _badge_here(self, r: int, c: int, replicate: int) -> bool:
        """True on the first cell of a run, scanning down each column.

        Works for the usual row-block layout (badges the top row of each
        replicate band) and for column-wise replicates (badges the top row
        only), without needing to know which layout it is looking at.
        """
        if r == 0:
            return True
        above = self.plate.get(r - 1, c).placement
        return above is None or above.replicate != replicate

    def _draw_growth(
        self, cx: float, cy: float, slot: int, replicate: int, dilution: int
    ) -> None:
        """One disc, dimmed toward the background as the dilution deepens."""
        radius = theme.spot_radius(self._pitch)
        self.create_oval(
            cx - radius, cy - radius, cx + radius, cy + radius,
            fill=theme.spot_fill(slot, replicate, dilution),
            outline=theme.spot_edge(slot, replicate, dilution),
        )

    def _draw_spot(self, r: int, c: int, x0, y0, x1, y1) -> None:
        placement = self.plate.get(r, c).placement
        if placement is None:
            return
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        slot, replicate = placement.sample_slot, placement.replicate

        if self.show_tokens:
            # The unambiguous read: exactly what will be written to the file.
            self.create_text(
                cx, cy, text=placement.token(), fill=theme.TEXT,
                font=self._scaled_font(0.16),
            )
        else:
            self._draw_growth(cx, cy, slot, replicate, placement.dilution)
            if self._lowest_dilution.get((slot, replicate)) == placement.dilution:
                self.create_text(
                    cx, cy, text=str(slot),
                    fill=theme.spot_ink(slot, replicate, placement.dilution),
                    font=self._scaled_font(0.26, bold=True),
                )

        if self._badge_here(r, c, replicate):
            # Just the digit, in one fixed ink. It sits in the cell corner,
            # clear of the spot, so it needs no disc behind it -- and a disc
            # that changed colour per replicate only competed with the sample.
            inset = self._pitch * theme.LABEL_INSET
            self.create_text(
                x0 + inset, y0 + inset, text=str(replicate), anchor="nw",
                fill=theme.REPLICATE_LABEL,
                font=self._scaled_font(theme.LABEL_SIZE, bold=True),
            )

        if slot == self.plate.control_slot:
            inset = self._pitch * 0.06
            self.create_oval(
                x0 + inset, y0 + inset, x1 - inset, y1 - inset,
                outline=theme.CONTROL_RING, width=2,
            )

    def _draw_preview(self) -> None:
        """Ghost the pending edit, so nothing is committed unseen."""
        if self._preview is None:
            return
        font = self._scaled_font(0.22, bold=True)
        for r, c, cell in self._preview.writes:
            x0, y0, x1, y1 = self.cell_rect(r, c)
            self.create_rectangle(
                x0 + 2, y0 + 2, x1 - 2, y1 - 2,
                outline=theme.ACCENT, width=2, dash=(4, 3),
            )
            if cell.placement is not None:
                self.create_text(
                    (x0 + x1) / 2, (y0 + y1) / 2,
                    text=str(cell.placement.sample_slot),
                    fill=theme.ACCENT, font=font,
                )

    def _draw_empty_hint(self) -> None:
        if not self.empty_hint or self.plate.slots_present():
            return
        ox, oy = self._origin
        self.create_text(
            ox + self._pitch * self.template.cols / 2,
            oy + self._pitch * self.template.rows / 2,
            text=self.empty_hint,
            fill=theme.MUTED,
            font=self._base_font,
            width=self._pitch * max(self.template.cols - 1, 1),
            justify="center",
        )
