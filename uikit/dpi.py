"""DPI awareness, and sizes given at 96 dpi converted to this display.

Every tool used to carry its own copy of the DPI call. It lives here now.
"""

from __future__ import annotations


def enable_dpi_awareness() -> None:
    """Draw at the display's real resolution on Windows. Call before any Tk root.

    Without it Windows renders the window at 96 dpi and enlarges the result, so
    on a 200% display the text is blurry and every photo is shown at half its
    resolution and doubled. Repeating the call is harmless.
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass


def scale_of(widget) -> float:
    """Screen pixels per 96-dpi pixel: 1.0 unless DPI-aware on a scaled display.

    Tk reports the true dpi only to a DPI-aware process; an unaware one is told
    96 and stretched by Windows instead, and then this stays at 1.0.
    """
    try:
        return max(1.0, float(widget.winfo_fpixels("1i")) / 96.0)
    except Exception:                                # pragma: no cover - defensive
        return 1.0


def px(widget, n: float) -> int:
    """A 96-dpi pixel size in this display's pixels."""
    return int(round(n * scale_of(widget)))
