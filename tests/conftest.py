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

import os
import sys
from pathlib import Path

import pytest

# Same bootstrap idiom the rest of this repo uses: put the project root on
# sys.path rather than relying on an installed package or PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The tools hand their heavy work to worker processes (`uikit.tasks.worker`).
# Here it runs on the calling thread instead, so a test can stub what the
# work calls and no test pays for starting a process. The worker processes
# themselves are tested in tests/uikit/test_uikit_tasks.py.
os.environ.setdefault("SPOTTING_WORKERS", "thread")


@pytest.fixture(scope="session")
def tk_root():
    """THE Tk root, for the whole test session.

    Every GUI test takes a `Toplevel` off this rather than making its own root.

    It wears the shared theme from the start, as every real window does. The
    theme is process-wide, so applying it only once some test happened to ask
    would make every GUI test's result depend on the order they ran in.
    """
    import tkinter as tk

    from uikit.theme import apply_theme

    try:
        root = tk.Tk()
    except tk.TclError:                      # pragma: no cover - headless CI
        pytest.skip("no display available")
    root.withdraw()
    apply_theme(root)
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass
