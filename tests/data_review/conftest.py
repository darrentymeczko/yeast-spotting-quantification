import sys
from pathlib import Path

import pytest

# Same bootstrap idiom the rest of this repo uses: put the project root on
# sys.path rather than relying on an installed package or PYTHONPATH.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


@pytest.fixture
def project(tmp_path):
    """(experiment path, experiment) for a small synthetic photo folder."""
    from data_review_fixtures import make_project

    return make_project(tmp_path)
