import json
from pathlib import Path

import pytest

from plate_template.model import EMPTY, UNASSIGNED, Cell, DilutionSpec, Template
from plate_template.presets import lab_standard_8x6
from plate_template.schema import (
    TemplateError,
    dumps_template,
    from_dict,
    loads_template,
    to_dict,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "plate_template" / "templates" / "lab_standard_8x6.json"

REGEN = "py plate_template/templates/regenerate.py"


def irregular() -> Template:
    """Everything the format is supposed to tolerate, in one template.

    Diagonal dilution series, ragged replicate coverage, both sentinel kinds,
    a two-digit sample slot, and a non-ASCII name.
    """
    t = Template(
        name='Δ panel "A"',
        description='Irregular: diagonal series, ragged reps, Δ and "quotes"',
        rows=3,
        cols=4,
        dilution=DilutionSpec(levels=3),
        id="irregular-0001",
        created="2026-01-02",
    )
    p1 = t.add_plate("1", "First", control_slot=10)
    p1.set(0, 0, Cell.spot(10, 1, 0))
    p1.set(1, 1, Cell.spot(10, 1, 1))   # series runs diagonally
    p1.set(2, 2, Cell.spot(10, 1, 2))
    p1.set(0, 3, Cell.spot(2, 1, 0))    # slot 2 has only one level
    p1.set(1, 3, EMPTY)                 # deliberately blank
    # everything else stays UNASSIGNED

    p2 = t.add_plate("2", "Second", control_slot=10)
    p2.set(0, 0, Cell.spot(10, 2, 0))
    return t


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [lab_standard_8x6, irregular], ids=["preset", "irregular"])
def test_round_trip(factory):
    t = factory()
    assert loads_template(dumps_template(t)) == t


@pytest.mark.parametrize("factory", [lab_standard_8x6, irregular], ids=["preset", "irregular"])
def test_dict_round_trip(factory):
    t = factory()
    assert from_dict(to_dict(t)) == t


@pytest.mark.parametrize("factory", [lab_standard_8x6, irregular], ids=["preset", "irregular"])
def test_output_is_ordinary_json(factory):
    # The hand-formatting must never stop plain json.loads working.
    json.loads(dumps_template(factory()))


def test_unicode_survives_unescaped():
    text = dumps_template(irregular())
    assert "Δ" in text
    assert "\\u0394" not in text


def test_sentinels_round_trip_distinctly():
    t = loads_template(dumps_template(irregular()))
    p = t.plate("1")
    assert p.get(1, 3) is EMPTY
    assert p.get(2, 3) is UNASSIGNED


# ---------------------------------------------------------------------------
# Shape of the emitted file -- this is the readability contract
# ---------------------------------------------------------------------------


def test_each_grid_row_is_one_physical_line():
    text = dumps_template(lab_standard_8x6())
    row_lines = [ln for ln in text.splitlines() if ln.strip().startswith('["s')]
    assert len(row_lines) == 12  # 6 rows on each of 2 plates
    for line in row_lines:
        assert line.count('"s') == 8  # all eight tokens share the line


def test_tokens_are_column_aligned_with_mixed_widths():
    text = dumps_template(irregular())
    row_lines = [ln for ln in text.splitlines() if ln.strip().startswith("[")]
    grid_lines = [ln for ln in row_lines if '"' in ln]
    # Every token in a column starts at the same offset, so the quote positions
    # repeat at a fixed stride within each line.
    for line in grid_lines:
        starts = [i for i, ch in enumerate(line) if ch == '"'][::2]
        if len(starts) > 2:
            strides = {b - a for a, b in zip(starts, starts[1:])}
            assert len(strides) == 1, f"ragged columns in: {line}"


def test_sample_slots_is_derived_not_declared():
    d = to_dict(irregular())
    assert d["sample_slots"] == 10  # highest slot actually placed


def test_no_biological_replicates_field():
    assert "biological_replicates" not in to_dict(lab_standard_8x6())


# ---------------------------------------------------------------------------
# Golden file -- locks the format
# ---------------------------------------------------------------------------


def test_golden_file_matches_the_emitter():
    assert GOLDEN.exists(), f"{GOLDEN} is missing; regenerate it with: {REGEN}"
    assert GOLDEN.read_text(encoding="utf-8") == dumps_template(lab_standard_8x6()), (
        f"the shipped template no longer matches the emitter; if the format "
        f"change is intended, regenerate with: {REGEN}"
    )


def test_golden_file_loads():
    assert GOLDEN.exists(), f"{GOLDEN} is missing; regenerate it with: {REGEN}"
    assert loads_template(GOLDEN.read_text(encoding="utf-8")) == lab_standard_8x6()


# ---------------------------------------------------------------------------
# Errors name the JSON path
# ---------------------------------------------------------------------------


def _minimal() -> dict:
    return {
        "schema_version": 1,
        "name": "t",
        "grid": {"rows": 1, "cols": 2},
        "dilution": {"levels": 1},
        "plate_slots": [{"id": "1", "control_slot": 1, "cells": [["s1r1d0", "s2r1d0"]]}],
    }


def test_minimal_document_loads():
    t = from_dict(_minimal())
    assert t.rows == 1 and t.cols == 2
    assert t.sample_slots() == 2


def test_bad_token_reports_its_position():
    d = _minimal()
    d["plate_slots"][0]["cells"][0][1] = "oops"
    with pytest.raises(TemplateError, match=r"\$\.plate_slots\[0\]\.cells\[0\]\[1\]"):
        from_dict(d)


def test_row_of_wrong_length_is_reported():
    d = _minimal()
    d["plate_slots"][0]["cells"][0].append("s3r1d0")
    with pytest.raises(TemplateError, match=r"cells\[0\].*3 tokens.*cols is 2"):
        from_dict(d)


def test_wrong_row_count_is_reported():
    d = _minimal()
    d["plate_slots"][0]["cells"].append(["s1r1d1", "s2r1d1"])
    with pytest.raises(TemplateError, match=r"cells.*2 rows.*rows is 1"):
        from_dict(d)


def test_missing_required_key_is_reported():
    d = _minimal()
    del d["grid"]
    with pytest.raises(TemplateError, match=r"\$: missing required key 'grid'"):
        from_dict(d)


def test_missing_schema_version_is_reported():
    d = _minimal()
    del d["schema_version"]
    with pytest.raises(TemplateError, match="schema_version"):
        from_dict(d)


def test_future_schema_version_is_refused():
    d = _minimal()
    d["schema_version"] = 99
    with pytest.raises(TemplateError, match="newer than this tool understands"):
        from_dict(d)


def test_wrong_type_is_reported():
    d = _minimal()
    d["grid"]["rows"] = "six"
    with pytest.raises(TemplateError, match=r"\$\.grid\.rows: expected int"):
        from_dict(d)


def test_invalid_json_is_reported():
    with pytest.raises(TemplateError, match="not valid JSON"):
        loads_template("{not json")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_load_tolerates_a_utf8_bom(tmp_path):
    # Notepad and PowerShell's Set-Content both add one; the format is meant to
    # be hand-editable, so a BOM must not make a template unreadable.
    from plate_template.schema import load

    target = tmp_path / "bom.json"
    target.write_text(dumps_template(lab_standard_8x6()), encoding="utf-8-sig")
    assert load(target) == lab_standard_8x6()


def test_save_writes_no_bom_and_bumps_revision(tmp_path):
    from plate_template.schema import load, save

    t = lab_standard_8x6()
    before = t.revision
    target = tmp_path / "out.json"
    save(t, target)

    assert not target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert t.revision == before + 1
    assert load(target).revision == before + 1


def test_save_leaves_no_temp_file_behind(tmp_path):
    from plate_template.schema import save

    save(lab_standard_8x6(), tmp_path / "out.json")
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


def test_load_reports_a_missing_file(tmp_path):
    from plate_template.schema import load

    with pytest.raises(TemplateError, match="could not read"):
        load(tmp_path / "absent.json")
