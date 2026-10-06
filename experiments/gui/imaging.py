"""Showing a plate photograph without stalling the window.

A capture folder holds hundreds of 3-6 MB JPEGs. Nothing is decoded until it is
about to be drawn, and what is decoded is kept in a small LRU, because flipping
through candidates re-shows the same few pictures constantly.

Pillow is used when it is there (it is in `requirements.txt`) because it resizes
smoothly and can read JPEG at all; Tk's own `PhotoImage` handles only GIF and
PNG and can merely `subsample` by whole numbers. Both paths are kept: choosing
which photograph to quantify is exactly the situation where somebody may be
sitting at a machine without the science stack.

Tk images must stay referenced from Python or they are garbage collected and the
widget silently goes blank, which is why the cache holds them rather than each
widget doing it ad hoc.

Deliberately a sibling of `results_review/gui/imaging.py` rather than an import
of it: the two windows are separate applications, and the shared part is a
hundred lines of generic Tk plumbing with nothing experiment- or review-specific
in it.
"""

from __future__ import annotations

import tkinter as tk
from collections import OrderedDict
from pathlib import Path

try:                                            # pragma: no cover - env dependent
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:                               # pragma: no cover - env dependent
    Image = ImageTk = None
    HAVE_PIL = False


class ImageCache:
    """Decoded, scaled images, keyed by (path, target box)."""

    def __init__(self, limit: int = 12) -> None:
        self.limit = limit
        self._items: "OrderedDict[tuple, tk.PhotoImage]" = OrderedDict()

    def clear(self) -> None:
        self._items.clear()

    def _put(self, key, img):
        self._items[key] = img
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return img

    def get(self, path: Path, box: tuple[int, int]) -> "tk.PhotoImage | None":
        """An image for `path` scaled to fit `box`, or None if it cannot be read.

        Aspect ratio is preserved and the image is never scaled up: a photograph
        blown past its own resolution looks like a fault, and the detail being
        judged -- whether a row of spots is readable -- is not there to recover.

        Returns None rather than raising on an unreadable file. A OneDrive
        placeholder, a truncated download or a stray non-image all end up here,
        and none of them should take the window down.
        """
        path = Path(path)
        w, h = max(1, int(box[0])), max(1, int(box[1]))
        key = (str(path), w, h)
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        if not path.exists():
            return None
        try:
            img = (self._with_pil(path, w, h) if HAVE_PIL
                   else self._with_tk(path, w, h))
        except Exception:
            return None
        return self._put(key, img) if img is not None else None

    # -- backends ------------------------------------------------------------

    @staticmethod
    def _with_pil(path: Path, w: int, h: int):
        with Image.open(path) as im:
            # draft() lets the JPEG decoder skip most of the work when the
            # target is far smaller than the file, which it always is here: a
            # 6000 px plate shown at 600 px decodes in a fraction of the time.
            try:
                im.draft("RGB", (w, h))
            except Exception:
                pass
            im.load()
            scale = min(w / im.width, h / im.height, 1.0)
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)),
                                max(1, int(im.height * scale))),
                               Image.LANCZOS)
            return ImageTk.PhotoImage(im.convert("RGB"))

    @staticmethod
    def _with_tk(path: Path, w: int, h: int):
        img = tk.PhotoImage(file=str(path))
        # subsample only divides by whole numbers, so pick the smallest factor
        # that fits rather than the one that fits best.
        factor = 1
        while (img.width() // factor > w or img.height() // factor > h) \
                and factor < 16:
            factor += 1
        return img.subsample(factor, factor) if factor > 1 else img


def unreadable_reason(path: Path) -> str:
    """Why a photograph could not be shown, in words, for the preview area.

    When Pillow is missing the message names the interpreter that is running.
    Pillow is in `requirements.txt` and is normally installed in the project
    environment, so by far the likeliest cause is not that it is absent but that
    this window was started on a different Python -- a bare Microsoft Store
    build on PATH, say. "Pillow is not installed" alone sends somebody off to
    install what they already have.
    """
    path = Path(path)
    if not path.exists():
        return "this file is no longer in the photo folder"
    if not HAVE_PIL and path.suffix.lower() not in (".png", ".gif"):
        import sys

        return (
            "Pillow is not installed on the Python running this window, so\n"
            "JPEG previews are unavailable.\n\n"
            f"Running on:\n{sys.executable}\n\n"
            "If that is not the project environment, start the window with\n"
            "run_experiment_designer.bat, which prefers it. Otherwise:\n"
            "    pip install -r requirements.txt"
        )
    try:
        if path.stat().st_size == 0:
            return "this file is empty"
    except OSError:
        pass
    return "this image could not be read"
