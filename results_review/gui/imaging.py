"""Loading sheet images without stalling the window.

A set has up to ~200 comparison sheets at roughly 450 KB and 1500x1200 px each.
Decoding them all would cost hundreds of megabytes for pictures nobody is
looking at, so nothing is decoded until it is about to be drawn, and what is
decoded is kept in a small LRU.

Pillow is used when it is there (it is in `requirements.txt`) because it resizes
smoothly; Tk's own PhotoImage can only `subsample` by whole numbers, which makes
spot edges crawl. Both paths are kept, since browsing results is exactly the
situation where somebody may not have the science stack installed.

Tk images must be referenced from Python or they are garbage collected and the
widget silently goes blank, which is why the cache holds them rather than the
widgets doing it ad hoc.
"""

from __future__ import annotations

import tkinter as tk
from collections import OrderedDict
from pathlib import Path

try:                                            # pragma: no cover - env dependent
    from PIL import Image, ImageEnhance, ImageTk
    HAVE_PIL = True
except Exception:                               # pragma: no cover - env dependent
    Image = ImageEnhance = ImageTk = None
    HAVE_PIL = False


class ImageCache:
    """Decoded, scaled images, keyed by (path, target box)."""

    def __init__(self, limit: int = 48) -> None:
        self.limit = limit
        self._items: "OrderedDict[tuple, tk.PhotoImage]" = OrderedDict()

    def clear(self) -> None:
        self._items.clear()

    def invalidate(self, path: Path) -> None:
        """Forget every scaled copy of one file.

        A redrawn graph is written back to the same filename, so without this
        the cache would keep serving the picture from before the edit -- the
        one case where a stale image is not merely untidy but wrong.
        """
        key = str(Path(path))
        for k in [k for k in self._items if k[0] == key]:
            del self._items[k]

    def _put(self, key, img):
        self._items[key] = img
        while len(self._items) > self.limit:
            self._items.popitem(last=False)
        return img

    def get(self, path: Path, box: tuple[int, int], brightness: float = 1.0,
            contrast: float = 1.0) -> "tk.PhotoImage | None":
        """An adjusted image for `path` scaled to fit `box`.

        Aspect ratio is preserved and the image is never scaled UP: a sheet
        blown past its own resolution looks like a mistake, and the detail the
        person is checking -- whether a spot is actually quantifiable -- is not
        there to recover. Brightness and contrast affect this preview only;
        neither the source image nor an exported figure is changed.
        """
        path = Path(path)
        w, h = max(1, int(box[0])), max(1, int(box[1]))
        brightness = round(max(0.01, float(brightness)), 2)
        contrast = round(max(0.01, float(contrast)), 2)
        key = (str(path), w, h, brightness, contrast)
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        if not path.exists():
            return None
        try:
            img = (self._with_pil(path, w, h, brightness, contrast) if HAVE_PIL
                   else self._with_tk(path, w, h))
        except Exception:
            return None
        return self._put(key, img) if img is not None else None

    # -- backends ------------------------------------------------------------

    @staticmethod
    def _with_pil(path: Path, w: int, h: int, brightness: float,
                  contrast: float):
        with Image.open(path) as im:
            im.load()
            scale = min(w / im.width, h / im.height, 1.0)
            if scale < 1.0:
                im = im.resize((max(1, int(im.width * scale)),
                                max(1, int(im.height * scale))),
                               Image.LANCZOS)
            im = im.convert("RGB")
            if brightness != 1.0:
                im = ImageEnhance.Brightness(im).enhance(brightness)
            if contrast != 1.0:
                im = ImageEnhance.Contrast(im).enhance(contrast)
            return ImageTk.PhotoImage(im)

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


def size_of(path: Path) -> "tuple[int, int] | None":
    """Pixel size without decoding the whole image, when Pillow is available."""
    if not HAVE_PIL:
        return None
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None
