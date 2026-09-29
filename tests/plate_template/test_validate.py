import pytest

from plate_template.model import EMPTY, UNASSIGNED, Cell, DilutionSpec, Template
from plate_template.presets import blank, lab_standard_8x6
from plate_template.validate import Severity, blocking, validate


def codes(t: Template) -> set[str]:
    return {i.code for i in validate(t)}


def one(t: Template, code: str):
    found = [i for i in validate(t) if i.code == code]
    assert found, f"expected {code!r}; got {sorted(codes(t))}"
    return found[0]


def clean() -> Template:
    """Two samples, one replicate, two dilutions, fully populated. No issues."""
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=2))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(0, 1, Cell.spot(2, 1, 0))
    p.set(1, 0, Cell.spot(1, 1, 1))
    p.set(1, 1, Cell.spot(2, 1, 1))
    return t


# ---------------------------------------------------------------------------
# The baselines
# ---------------------------------------------------------------------------


def test_clean_template_has_only_a_summary():
    issues = validate(clean())
    assert [i.code for i in issues] == ["summary"]
    assert issues[0].severity is Severity.INFO


def test_lab_standard_is_clean():
    """The shipped preset must stay free of errors and warnings."""
    issues = [i for i in validate(lab_standard_8x6()) if i.severity is not Severity.INFO]
    assert issues == [], [f"{i.code}: {i.message}" for i in issues]


def test_summary_reports_the_shape():
    issue = one(lab_standard_8x6(), "summary")
    assert "8 sample slots" in issue.message
    assert "4 replicates" in issue.message
    assert "3 dilution levels" in issue.message
    assert "2 plates" in issue.message
    assert "96 spots" in issue.message


def test_blank_template_is_an_error_not_a_crash():
    assert "no_placements" in codes(blank())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def test_no_plates():
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=1))
    assert one(t, "no_plates").severity is Severity.ERROR


def test_plate_ids_not_unique():
    t = clean()
    t.add_plate("1")
    assert one(t, "plate_ids_not_unique").severity is Severity.ERROR


def test_no_placements():
    t = clean()
    for r in range(2):
        for c in range(2):
            t.plate("1").set(r, c, EMPTY)
    assert one(t, "no_placements").severity is Severity.ERROR


def test_ragged_grid():
    t = clean()
    t.plate("1").cells[1] = [Cell.spot(1, 1, 1)]  # one column short
    assert one(t, "ragged_grid").severity is Severity.ERROR


def test_slot_out_of_range():
    t = clean()
    t.plate("1").set(0, 0, Cell.spot(0, 1, 0))  # bypasses token parsing
    assert one(t, "slot_out_of_range").severity is Severity.ERROR


def test_rep_out_of_range():
    t = clean()
    t.plate("1").set(0, 0, Cell.spot(1, 0, 0))
    assert one(t, "rep_out_of_range").severity is Severity.ERROR


def test_dil_out_of_range():
    t = clean()
    t.plate("1").set(0, 0, Cell.spot(1, 1, 7))  # levels is 2
    issue = one(t, "dil_out_of_range")
    assert issue.severity is Severity.ERROR
    assert "0..1" in issue.message


def test_no_control_designated():
    t = clean()
    t.plate("1").control_slot = None
    assert one(t, "no_control_designated").severity is Severity.ERROR


def test_no_control_on_plate():
    """The hard rule: normalisation is within-plate, so each plate needs one."""
    t = clean()
    t.plate("1").control_slot = 9
    issue = one(t, "no_control_on_plate")
    assert issue.severity is Severity.ERROR
    assert issue.plate_id == "1"


def test_duplicate_placement_reports_both_cells():
    t = clean()
    t.plate("1").set(1, 1, Cell.spot(1, 1, 0))  # same unit as (0, 0)
    issue = one(t, "duplicate_placement")
    assert issue.severity is Severity.ERROR
    assert set(issue.cells) == {(0, 0), (1, 1)}
    assert "r1c1" in issue.message and "r2c2" in issue.message


def test_duplicate_across_plates_is_also_caught():
    t = clean()
    p2 = t.add_plate("2", control_slot=1)
    for r in range(2):
        for c in range(2):
            p2.set(r, c, EMPTY)
    p2.set(0, 0, Cell.spot(1, 1, 0))  # already on plate 1
    assert "duplicate_placement" in codes(t)


def test_blocking_filters_to_errors():
    t = clean()
    t.plate("1").control_slot = None
    issues = validate(t)
    assert all(i.severity is Severity.ERROR for i in blocking(issues))
    assert blocking(issues)


def test_errors_sort_before_warnings():
    t = clean()
    t.plate("1").control_slot = None
    t.plate("1").set(0, 1, UNASSIGNED)
    severities = [i.severity for i in validate(t)]
    assert severities == sorted(severities, key=lambda s: [Severity.ERROR, Severity.WARNING, Severity.INFO].index(s))


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def test_unassigned_cells():
    t = clean()
    t.plate("1").set(0, 1, UNASSIGNED)
    issue = one(t, "unassigned_cells")
    assert issue.severity is Severity.WARNING
    assert issue.cells == ((0, 1),)


def test_incomplete_series():
    t = clean()
    t.plate("1").set(1, 1, EMPTY)  # slot 2 loses its dilution 1
    issue = one(t, "incomplete_series")
    assert issue.severity is Severity.WARNING
    assert "slot 2" in issue.message


def test_control_missing_dilution():
    t = clean()
    t.plate("1").set(1, 0, EMPTY)  # control slot 1 loses dilution 1
    issue = one(t, "control_missing_dilution")
    assert issue.severity is Severity.WARNING
    assert "cannot be scored" in issue.message


def test_control_differs_between_plates():
    t = clean()
    p2 = t.add_plate("2", control_slot=2)
    for r in range(2):
        for c in range(2):
            p2.set(r, c, EMPTY)
    p2.set(0, 0, Cell.spot(2, 2, 0))
    p2.set(1, 0, Cell.spot(2, 2, 1))
    assert one(t, "control_differs_between_plates").severity is Severity.WARNING


def test_replicate_split_across_plates():
    t = clean()
    t.plate("1").set(1, 0, EMPTY)  # move slot 1 rep 1 dilution 1 to plate 2
    p2 = t.add_plate("2", control_slot=1)
    for r in range(2):
        for c in range(2):
            p2.set(r, c, EMPTY)
    p2.set(0, 0, Cell.spot(1, 1, 1))
    issue = one(t, "replicate_split_across_plates")
    assert issue.severity is Severity.WARNING
    assert "per-plate" in issue.message


def test_slot_numbering_gap():
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=2))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(0, 1, Cell.spot(3, 1, 0))  # slot 2 skipped
    p.set(1, 0, Cell.spot(1, 1, 1))
    p.set(1, 1, Cell.spot(3, 1, 1))
    issue = one(t, "slot_numbering_gap")
    assert issue.severity is Severity.WARNING
    assert "2" in issue.message


def test_ragged_replicate_coverage():
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=1))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(0, 1, Cell.spot(2, 1, 0))
    p.set(1, 0, Cell.spot(1, 2, 0))  # slot 1 has two replicates, slot 2 has one
    p.set(1, 1, EMPTY)
    assert one(t, "ragged_replicate_coverage").severity is Severity.WARNING


def test_empty_column_is_flagged_once_the_plate_is_decided():
    t = Template(name="t", rows=2, cols=3, dilution=DilutionSpec(levels=2))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(0, 1, Cell.spot(2, 1, 0))
    p.set(0, 2, EMPTY)
    p.set(1, 0, Cell.spot(1, 1, 1))
    p.set(1, 1, Cell.spot(2, 1, 1))
    p.set(1, 2, EMPTY)
    issue = one(t, "empty_row_or_col")
    assert issue.severity is Severity.WARNING
    assert "column 3" in issue.message


def test_empty_column_is_silent_while_the_design_is_unfinished():
    # Half-built designs should not be nagged about grid size.
    t = Template(name="t", rows=2, cols=3, dilution=DilutionSpec(levels=2))
    p = t.add_plate("1", control_slot=1)
    p.set(0, 0, Cell.spot(1, 1, 0))
    p.set(1, 0, Cell.spot(1, 1, 1))
    assert "empty_row_or_col" not in codes(t)


# ---------------------------------------------------------------------------


def test_every_rule_in_validate_py_has_a_test():
    """Guard against adding a rule and forgetting to cover it."""
    import re
    from pathlib import Path

    from plate_template import validate as validate_module

    rules = set(
        re.findall(
            r'Severity\.(?:ERROR|WARNING|INFO),\s*"(\w+)"',
            Path(validate_module.__file__).read_text(encoding="utf-8"),
        )
    )
    assert rules, "found no Issue codes in validate.py -- has the shape changed?"

    tested = Path(__file__).read_text(encoding="utf-8")
    missing = sorted(code for code in rules if f'"{code}"' not in tested)
    assert not missing, f"no test covers: {missing}"
