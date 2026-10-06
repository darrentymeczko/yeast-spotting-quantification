"""The look and the window contract every tool in this project shares.

The three tools -- the plate designer, the experiment designer and the review
window -- each live in their own package and each still run on their own. What
they share lives here, and only that:

    tokens   colours, fonts and spacing, as plain data
    dpi      DPI awareness and the 96-dpi pixel helpers
    theme    the flat ttk theme built from those tokens
    icons    icon glyphs from the Windows symbol fonts

This file imports nothing on purpose. `tokens` is read by the tools' headless
core modules (their own `theme.py`), which must stay importable with no display
and no tkinter, and importing the package must not drag tkinter in behind them.
"""
