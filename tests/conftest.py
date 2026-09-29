"""Fixtures shared by every test suite in this repository.

Right now that is one thing: the Tk root.

Three GUI packages are tested here -- the plate designer, the experiment
designer and the review window -- and a Tk process tolerates exactly one root.
Several roots in one process is legal but flaky on Windows: a later `tk.Tk()`
intermittently fails, the test SKIPS with "no display", and the guard it
contained quietly stops running. Two roots ALIVE AT ONCE is worse, and breaks
whichever suite runs second.

Both failure modes were observed here: first when a per-test root was used, then
again the moment a second GUI test module appeared in one package. Neither is a
real failure and both are silent, which is exactly why the root lives at the top
of the tree instead of being solved once per package.
"""

import sys
from pathlib import Path

import pytest

# Same bootstrap idiom the rest of this repo uses: put the project root on
# sys.path rather than relying on an installed package or PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def tk_root():
    """THE Tk root, for the whole test session.

    Every GUI test takes a `Toplevel` off this rather than making its own root.
    """
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:                      # pragma: no cover - headless CI
        pytest.skip("no display available")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
