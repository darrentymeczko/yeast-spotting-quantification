"""The controller has no tkinter imports, so it is testable headless."""

from plate_template.autofill import Plan, RunFillSpec, plan_run_fill
from plate_template.gui.controller import EditorController, UndoStack
from plate_template.model import EMPTY, UNASSIGNED, Cell, DilutionSpec, Template
from plate_template.presets import blank
from plate_template.schema import to_dict


def controller() -> EditorController:
    t = Template(name="t", rows=3, cols=3, dilution=DilutionSpec(levels=2))
    t.add_plate("1", control_slot=None)
    return EditorController(t)


def place_row(c: EditorController, count: int = 3) -> bool:
    plan = plan_run_fill(
        c.template, "1", RunFillSpec(start=(0, 0), direction="right", count=count)
    )
    return c.apply("1", plan, "Place samples")


# ---------------------------------------------------------------------------
# UndoStack
# ---------------------------------------------------------------------------


def test_undo_stack_round_trips():
    stack = UndoStack()
    assert not stack.can_undo and not stack.can_redo

    stack.push({"v": 1}, "first")
    assert stack.can_undo
    assert stack.undo_label == "first"

    assert stack.undo({"v": 2}) == ({"v": 1}, "first")
    assert stack.can_redo
    assert stack.redo({"v": 1}) == ({"v": 2}, "first")


def test_undo_stack_drops_redo_on_a_new_edit():
    stack = UndoStack()
    stack.push({"v": 1}, "a")
    stack.undo({"v": 2})
    assert stack.can_redo
    stack.push({"v": 3}, "b")
    assert not stack.can_redo


def test_undo_stack_honours_its_limit():
    stack = UndoStack(limit=3)
    for i in range(10):
        stack.push({"v": i}, str(i))
    # Oldest entries fall off the bottom; the newest survive.
    assert stack.undo_label == "9"
    for _ in range(3):
        stack.undo({})
    assert not stack.can_undo


# ---------------------------------------------------------------------------
# EditorController
# ---------------------------------------------------------------------------


def test_edit_then_undo_restores_the_previous_state():
    c = controller()
    before = to_dict(c.template)
    assert place_row(c)
    assert to_dict(c.template) != before

    assert c.undo() == "Place samples"
    assert to_dict(c.template) == before

    assert c.redo() == "Place samples"
    assert c.template.spot_count() == 3


def test_a_no_op_edit_is_not_recorded():
    c = controller()
    # Painting UNASSIGNED over already-unassigned cells changes nothing.
    assert not c.apply("1", Plan([(0, 0, UNASSIGNED)], []), "Erase")
    assert not c.undo_stack.can_undo


def test_undo_on_an_empty_history_returns_none():
    assert controller().undo() is None
    assert controller().redo() is None


def test_dirty_tracking():
    c = controller()
    assert not c.dirty
    place_row(c)
    assert c.dirty
    c.mark_saved()
    assert not c.dirty
    c.undo()
    assert c.dirty


def test_undo_survives_a_round_trip_through_the_serialiser():
    """Snapshots are to_dict output, so undo exercises schema on every edit."""
    c = controller()
    place_row(c)
    c.apply("1", Plan([(1, 1, EMPTY)], []), "Mark empty")
    c.undo()
    assert c.template.plate("1").get(1, 1) is UNASSIGNED
    assert c.template.spot_count() == 3


def test_control_resize_and_plate_edits_are_undoable():
    c = controller()
    place_row(c)

    c.set_control("1", 2)
    assert c.template.plate("1").control_slot == 2
    c.undo()
    assert c.template.plate("1").control_slot is None

    c.resize(5, 5)
    assert (c.template.rows, c.template.cols) == (5, 5)
    c.undo()
    assert (c.template.rows, c.template.cols) == (3, 3)

    c.add_plate("2")
    assert len(c.template.plates) == 2
    c.undo()
    assert len(c.template.plates) == 1


def test_remove_and_rename_plate():
    c = controller()
    c.add_plate("2")
    c.rename_plate("2", "Second")
    assert c.template.plate("2").label == "Second"
    c.remove_plate("2")
    assert not c.template.has_plate("2")
    c.undo()
    assert c.template.plate("2").label == "Second"


def test_replace_clears_history_and_resets_dirty():
    c = controller()
    place_row(c)
    assert c.undo_stack.can_undo

    c.replace(blank())
    assert not c.undo_stack.can_undo
    assert not c.dirty


def test_on_change_fires_for_every_committed_edit():
    calls = []
    t = Template(name="t", rows=2, cols=2, dilution=DilutionSpec(levels=1))
    t.add_plate("1")
    c = EditorController(t, on_change=lambda: calls.append(1))

    c.apply("1", Plan([(0, 0, Cell.spot(1, 1, 0))], []), "Place")
    c.undo()
    c.redo()
    assert len(calls) == 3
