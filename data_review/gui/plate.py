"""The photograph with its detected spots drawn on it, and a magnifier.

A plate photo is ~6000 px across and is shown at ~1000, where a spot is a
couple of dozen pixels -- enough to see that something is wrong, not enough to
see what. So the photo is drawn from a quick reduced decode, and the magnifier
beside it shows whichever spot the pointer is on, with its neighbours, from the
full-resolution pixels.

Pixels are shown as the file holds them: no EXIF rotation, no contrast change.
The engine reads the file the same way (`spotting_quant.load_gray8` opens it
with Pillow and applies nothing), so the grid it detected lands exactly on the
picture here.
"""

from __future__ import annotations

import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:                                            # pragma: no cover - env dependent
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:                               # pragma: no cover - env dependent
    Image = ImageTk = None
    HAVE_PIL = False

from uikit import tokens

#: Behind the photo: near-black, like the table the plates are shot on, so the
#: letterbox around the picture does not read as part of it.
PHOTO_GROUND = "#14171C"

#: Drawn on a photograph, so chosen to stand out against agar and yeast rather
#: than to match the window. Flags are red here: the one thing this tool makes.
SPOT = "#2FD3FF"
CONTROL = "#B7F36B"
EMPTY = "#6B7785"
RIM = "#FF9F1C"
FLAG = "#FF3B30"
HOVER = "#FFFFFF"

#: Long edge of the quick decode the photo is drawn from.
PREVIEW_EDGE = 2000


@dataclass
class Pixels:
    """One photograph, decoded: a quick reduced copy, and the full one later."""

    path: Path
    size: tuple[int, int]            # full-resolution (width, height)
    preview: "object"                # PIL image, reduced
    full: "object | None" = None     # PIL image, full resolution

    def best(self):
        """(image, scale from full-resolution pixels to it)."""
        image = self.full if self.full is not None else self.preview
        return image, image.width / self.size[0]


def open_preview(path: Path) -> Pixels:
    """Decode a photo quickly at about PREVIEW_EDGE pixels. Raises if unreadable."""
    if not HAVE_PIL:
        raise RuntimeError("Pillow is not installed on this Python, so photos "
                           "cannot be shown")
    with Image.open(path) as im:
        size = im.size
        try:
            # JPEG decodes at 1/2, 1/4 or 1/8 for a fraction of the cost.
            im.draft("RGB", (PREVIEW_EDGE, PREVIEW_EDGE))
        except Exception:
            pass
        im.load()
        preview = im.convert("RGB")
    if max(preview.size) > PREVIEW_EDGE * 1.5:
        preview.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.BILINEAR)
    return Pixels(Path(path), size, preview)


def open_full(path: Path):
    with Image.open(path) as im:
        im.load()
        return im.convert("RGB")


class PreviewCache:
    """A few recent previews, so flipping back and forth is instant."""

    def __init__(self, limit: int = 6) -> None:
        self.limit = limit
        self._items: "OrderedDict[str, Pixels]" = OrderedDict()

    def get(self, path: Path) -> "Pixels | None":
        key = str(path)
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        return None

    def put(self, pixels: Pixels) -> Pixels:
        key = str(pixels.path)
        self._items[key] = pixels
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            _, old = self._items.popitem(last=False)
            old.full = None                     # the big one goes first
        return pixels

    def drop_full(self, keep: "Path | None") -> None:
        """Full-resolution copies are ~70 MB each: keep only the one shown."""
        for key, pixels in self._items.items():
            if keep is None or key != str(keep):
                pixels.full = None


def _shadowed_text(canvas: tk.Canvas, x, y, text, font, fill, tags) -> None:
    canvas.create_text(x + 1, y + 1, text=text, font=font, fill="#000000",
                       tags=tags)
    canvas.create_text(x, y, text=text, font=font, fill=fill, tags=tags)


class PlateView(tk.Canvas):
    """The photo, fitted to the widget, with every detected spot outlined."""

    def __init__(self, master, *, on_hover: Callable, on_click: Callable,
                 on_menu: Callable, **kw) -> None:
        super().__init__(master, background=PHOTO_GROUND, highlightthickness=0,
                         cursor="crosshair", **kw)
        self.on_hover = on_hover
        self.on_click = on_click
        self.on_menu = on_menu
        self.pixels: "Pixels | None" = None
        self.message = ""
        self.detected = None
        self.cells: dict = {}
        self.flags: dict = {}
        self.plate_reason = ""
        self.show_values = False
        self.hover: "tuple[int, int] | None" = None
        self._tk_image = None
        self._fit = (1.0, 0, 0, 0, 0)            # scale, x0, y0, width, height
        self._fitted_for = None
        self._render_id: "str | None" = None
        self.bind("<Configure>", lambda _e: self._schedule())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self._set_hover(None, notify=True))
        self.bind("<Button-1>", self._click)
        self.bind("<Button-3>", self._menu)

    # -- what is shown ---------------------------------------------------------

    def show_photo(self, pixels: "Pixels | None", message: str = "") -> None:
        self.pixels = pixels
        self.message = message
        self.hover = None
        self._fitted_for = None
        self._render()

    def set_overlay(self, detected, cells: dict, flags: dict, plate_reason: str,
                    show_values: bool) -> None:
        self.detected = detected
        self.cells = cells
        self.flags = flags
        self.plate_reason = plate_reason
        self.show_values = show_values
        self._draw_overlay()

    def _schedule(self) -> None:
        if self._render_id is not None:
            self.after_cancel(self._render_id)
        self._render_id = self.after(60, self._render)

    def _render(self) -> None:
        self._render_id = None
        self.delete("all")
        w, h = max(1, self.winfo_width()), max(1, self.winfo_height())
        if self.pixels is None:
            self._tk_image = None
            self.create_text(w // 2, h // 2, text=self.message, fill="#C9D1D9",
                             font=(tokens.FONT_FAMILY, 10), justify="center",
                             width=max(200, w - 60))
            return
        image = self.pixels.preview
        fw, fh = self.pixels.size
        scale = min(w / fw, h / fh)
        dw, dh = max(1, int(fw * scale)), max(1, int(fh * scale))
        x0, y0 = (w - dw) // 2, (h - dh) // 2
        if self._fitted_for != (id(image), dw, dh):
            shown = image.resize((dw, dh), Image.LANCZOS, reducing_gap=2.0)
            self._tk_image = ImageTk.PhotoImage(shown)
            self._fitted_for = (id(image), dw, dh)
        self._fit = (scale, x0, y0, dw, dh)
        self.create_image(x0, y0, anchor="nw", image=self._tk_image, tags="photo")
        if self.message:
            _shadowed_text(self, w // 2, y0 + 18, self.message,
                           (tokens.FONT_FAMILY, 10, "bold"), "#FFFFFF", "photo")
        self._draw_overlay()

    def _draw_overlay(self) -> None:
        self.delete("overlay")
        self.delete("hover")
        if self.pixels is None:
            return
        scale, x0, y0, dw, dh = self._fit
        if self.plate_reason:
            self.create_rectangle(x0 + 2, y0 + 2, x0 + dw - 3, y0 + dh - 3,
                                  outline=FLAG, width=5, tags="overlay")
        found = self.detected
        if found is None:
            return
        small = (tokens.FONT_FAMILY, 7)
        rows, cols = found.shape
        for r in range(rows):
            for c in range(cols):
                y, x = found.center(r, c)
                cx, cy = x0 + x * scale, y0 + y * scale
                rad = max(3.0, found.radius.get((r, c), 0.0) * scale)
                cell = self.cells.get((r, c))
                flagged = (r, c) in self.flags
                if flagged:
                    opts = {"outline": FLAG, "width": 3}
                elif cell is None:
                    opts = {"outline": EMPTY, "width": 1, "dash": (2, 3)}
                elif (r, c) in found.rim:
                    opts = {"outline": RIM, "width": 2, "dash": (4, 2)}
                elif cell.is_control:
                    opts = {"outline": CONTROL, "width": 1}
                else:
                    opts = {"outline": SPOT, "width": 1}
                self.create_oval(cx - rad, cy - rad, cx + rad, cy + rad,
                                 tags="overlay", **opts)
                if flagged:
                    d = rad * 0.55
                    for a, b in ((-d, -d), (-d, d)):
                        self.create_line(cx + a, cy + b, cx - a, cy - b,
                                         fill=FLAG, width=2, tags="overlay")
                if self.show_values and cell is not None:
                    _shadowed_text(self, cx, cy + rad + 7,
                                   f"{found.net.get((r, c), 0.0):.1f}", small,
                                   "#FFFFFF", "overlay")
        self._draw_hover()

    def _draw_hover(self) -> None:
        self.delete("hover")
        if self.hover is None or self.detected is None or self.pixels is None:
            return
        scale, x0, y0, _, _ = self._fit
        r, c = self.hover
        y, x = self.detected.center(r, c)
        cx, cy = x0 + x * scale, y0 + y * scale
        rad = max(4.0, self.detected.radius.get((r, c), 0.0) * scale) + 4
        self.create_oval(cx - rad, cy - rad, cx + rad, cy + rad,
                         outline=HOVER, width=2, tags="hover")

    # -- pointer ---------------------------------------------------------------

    def cell_at(self, x: int, y: int) -> "tuple[int, int] | None":
        if self.detected is None or self.pixels is None:
            return None
        scale, x0, y0, dw, dh = self._fit
        if not (x0 <= x < x0 + dw and y0 <= y < y0 + dh):
            return None
        return self.detected.nearest((y - y0) / scale, (x - x0) / scale)

    def highlight(self, cell: "tuple[int, int] | None") -> None:
        """Ring one spot, as if the pointer were on it."""
        self._set_hover(cell, notify=False)

    def _set_hover(self, cell, notify: bool) -> None:
        if cell != self.hover:
            self.hover = cell
            self._draw_hover()
            if notify and cell is not None:
                self.on_hover(cell)

    def _motion(self, event) -> None:
        self._set_hover(self.cell_at(event.x, event.y), notify=True)

    def _click(self, event) -> None:
        self.focus_set()
        cell = self.cell_at(event.x, event.y)
        if cell is not None:
            self.on_click(cell)

    def _menu(self, event) -> None:
        self.on_menu(self.cell_at(event.x, event.y), event.x_root, event.y_root)


class Magnifier(tk.Canvas):
    """One spot and its neighbours, from the full-resolution photo."""

    #: How far around the spot to show, in grid pitches either side.
    REACH = 1.5

    def __init__(self, master, size: int, **kw) -> None:
        super().__init__(master, width=size, height=size, background=PHOTO_GROUND,
                         highlightthickness=0, **kw)
        self.size = size
        self._tk_image = None

    def clear(self, text: str = "") -> None:
        self.delete("all")
        self._tk_image = None
        if text:
            self.create_text(self.size // 2, self.size // 2, text=text,
                             fill="#C9D1D9", font=(tokens.FONT_FAMILY, 9),
                             width=self.size - 24, justify="center")

    def show(self, pixels: "Pixels | None", found, cell: "tuple[int, int] | None",
             flags: dict, cells: dict) -> None:
        if pixels is None or found is None or cell is None:
            self.clear("Point at a spot to see it up close."
                       if found is not None else "")
            return
        image, k = pixels.best()
        cy, cx = found.center(*cell)
        half = self.REACH * found.pitch
        box = [cx - half, cy - half, cx + half, cy + half]
        crop = image.crop(tuple(int(round(v * k)) for v in box))
        shown = crop.resize((self.size, self.size), Image.LANCZOS)
        self.delete("all")
        self._tk_image = ImageTk.PhotoImage(shown)
        self.create_image(0, 0, anchor="nw", image=self._tk_image)
        zoom = self.size / (2 * half)
        rows, cols = found.shape
        for r in range(max(0, cell[0] - 2), min(rows, cell[0] + 3)):
            for c in range(max(0, cell[1] - 2), min(cols, cell[1] + 3)):
                y, x = found.center(r, c)
                px, py = (x - box[0]) * zoom, (y - box[1]) * zoom
                rad = found.radius.get((r, c), 0.0) * zoom
                this = (r, c) == cell
                if (r, c) in flags:
                    colour, width = FLAG, 3 if this else 2
                elif this:
                    colour, width = HOVER, 2
                elif (r, c) in cells:
                    colour, width = SPOT, 1
                else:
                    colour, width = EMPTY, 1
                self.create_oval(px - rad, py - rad, px + rad, py + rad,
                                 outline=colour, width=width,
                                 dash=() if this or (r, c) in flags else (3, 3))
        if pixels.full is None:
            _shadowed_text(self, self.size // 2, self.size - 10,
                           "loading full resolution…", (tokens.FONT_FAMILY, 8),
                           "#FFFFFF", "note")
