import sys
from pathlib import Path

# Same bootstrap idiom the rest of this repo uses: put the project root on
# sys.path rather than relying on an installed package or PYTHONPATH.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
