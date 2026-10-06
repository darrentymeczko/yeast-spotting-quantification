"""A leaping frog, drawn from scalable geometry on an emerald tile.

No external assets or imaging-library startup cost. Supersampling keeps the
splayed toes and bent hind legs readable at taskbar and title-bar sizes.
"""

from __future__ import annotations

import tkinter as tk

_SAMPLES = 3

_images: list = []


def _rgb(colour: str) -> tuple[float, float, float]:
    c = colour.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(v))) for v in rgb)


def _mix(a, b, amount: float):
    return tuple(x + (y - x) * amount for x, y in zip(a, b))


def _inside_tile(x: float, y: float, n: int, radius: float) -> bool:
    cx = min(max(x, radius), n - radius)
    cy = min(max(y, radius), n - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


# Four limbs, reaching forward and kicking back, with three splayed toes.
_LIMBS = (
    (.43, .45, .23, .37, .065), (.23, .37, .31, .59, .038),
    (.31, .59, .13, .70, .027),
    (.46, .55, .56, .76, .064), (.56, .76, .32, .74, .037),
    (.32, .74, .20, .88, .027),
    (.57, .35, .49, .22, .031), (.49, .22, .65, .14, .024),
    (.66, .42, .81, .50, .031), (.81, .50, .87, .33, .024),
    (.65, .14, .65, .08, .017), (.65, .14, .72, .11, .017),
    (.65, .14, .71, .17, .017),
    (.87, .33, .85, .26, .017), (.87, .33, .92, .27, .017),
    (.87, .33, .94, .34, .017),
    (.13, .70, .08, .69, .017), (.13, .70, .09, .76, .017),
    (.20, .88, .14, .89, .017), (.20, .88, .22, .94, .017),
)


def _frog(x: float, y: float) -> bool:
    # Body and head point toward the upper right.
    along = ((x - .48) - (y - .48)) * .7071
    across = ((x - .48) + (y - .48)) * .7071
    if (along / .22) ** 2 + (across / .13) ** 2 <= 1:
        return True
    for cx, cy, radius in ((.65, .32, .118), (.62, .22, .054), (.75, .33, .054)):
        if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
            return True
    for ax, ay, bx, by, radius in _LIMBS:
        dx, dy = bx - ax, by - ay
        u = min(1., max(0., ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        if (x - ax - u * dx) ** 2 + (y - ay - u * dy) ** 2 <= radius ** 2:
            return True
    return False


def mark(master, size: int) -> tk.PhotoImage:
    """The mark at `size` x `size` pixels."""
    accent, white = _rgb("#16734A"), (245.0, 255.0, 224.0)
    radius = size * 0.22
    step = 1.0 / _SAMPLES
    rows, clear = [], []
    for py in range(size):
        row = []
        for px_ in range(size):
            tile = spot = 0.0
            for sy in range(_SAMPLES):
                for sx in range(_SAMPLES):
                    x = px_ + (sx + 0.5) * step
                    y = py + (sy + 0.5) * step
                    if not _inside_tile(x, y, size, radius):
                        continue
                    tile += 1
                    nx, ny = x / size, y / size
                    eye = any((nx - ex) ** 2 + (ny - ey) ** 2 < .020 ** 2
                              for ex, ey in ((.628, .216), (.757, .322)))
                    if not eye and _frog(nx, ny):
                        spot += 1
            total = _SAMPLES * _SAMPLES
            if tile < total / 2:
                clear.append((px_, py))
                row.append(_hex(accent))
                continue
            amount = spot / tile if tile else 0.0
            row.append(_hex(_mix(accent, white, amount)))
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(master=master, width=size, height=size)
    img.put(" ".join(rows))
    for x, y in clear:
        img.transparency_set(x, y, True)
    _images.append(img)
    return img


def set_window_icon(window) -> None:
    """The mark as the window's and the taskbar's icon, at the sizes Windows
    asks for."""
    try:
        window.iconphoto(True, *(mark(window, n) for n in (64, 48, 32, 16)))
    except tk.TclError:                              # pragma: no cover - defensive
        pass
