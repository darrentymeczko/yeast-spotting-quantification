import sys
from pathlib import Path

import pytest

# Same bootstrap idiom the rest of this repo uses: put the project root on
# sys.path rather than relying on an installed package or PYTHONPATH.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Real output from a real run, used by the tests that check this tool agrees
#: with the pipeline about filenames and about which candidate won. Skipped
#: rather than faked when it is absent: a synthetic fixture would assert that
#: the code agrees with itself, which is the one thing these tests must not do.
LIVE_RESULTS = ROOT / "Results" / "Timecourse"


def _have(module: str) -> bool:
    from importlib.util import find_spec
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


needs_pandas = pytest.mark.skipif(
    not _have("pandas"), reason="pandas is not installed in this environment")

needs_engine = pytest.mark.skipif(
    not (_have("pandas") and _have("numpy") and _have("skimage")),
    reason="the measurement stack (numpy/pandas/scikit-image) is not installed")


# `tk_root` now lives in tests/conftest.py. A Tk process tolerates exactly one
# root, and this suite is no longer the only one that needs it: a second root
# defined here would overlap the experiment designer's and break whichever ran
# second. The fixture name and behaviour are unchanged.


@pytest.fixture(scope="session")
def live_sets():
    """Every real results folder on this machine, or skip."""
    from results_review import discovery

    sets = discovery.list_sets(LIVE_RESULTS)
    if not sets:
        pytest.skip(f"no timecourse results under {LIVE_RESULTS}")
    return sets


@pytest.fixture(scope="session")
def live_run(live_sets):
    """One loaded results folder -- the first with sheets actually drawn."""
    from results_review import discovery

    for path in live_sets:
        run = discovery.load_set(path)
        if run.candidates and not discovery.missing_sheets(run):
            return run
    pytest.skip("no results folder has a complete set of drawn sheets")
