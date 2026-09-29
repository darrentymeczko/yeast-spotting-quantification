"""Regenerate the shipped template(s) from the preset factories.

The JSON beside this script is both the preset users start from and the golden
file `tests/plate_template/test_schema.py` locks the format against. Run this
after any intended change to the emitter or the preset:

    py plate_template/templates/regenerate.py
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from plate_template.presets import lab_standard_8x6  # noqa: E402
from plate_template.schema import dumps_template, loads_template  # noqa: E402


def main() -> int:
    template = lab_standard_8x6()
    text = dumps_template(template)

    if loads_template(text) != template:
        print("ERROR: the emitted file does not round-trip", file=sys.stderr)
        return 1

    out = HERE / "lab_standard_8x6.json"
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
