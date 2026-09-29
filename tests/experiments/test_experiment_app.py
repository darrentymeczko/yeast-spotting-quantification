"""The window builds, renders, and survives the states it will actually meet.

Not a test of what it looks like -- a test that it comes up at all for a blank
experiment, a complete one, and the awkward middle states: no template, no
photo folder, a folder that has moved. Any of those raising on startup would
make the tool unopenable, which is the failure worth guarding.

Skipped where there is no display.
"""

import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk  # noqa: E402

from experiments.app import ExperimentApp  # noqa: E402
from experiments.gui import conditions as conditions_gui  # noqa: E402
from experiments.model import QUANTIFY, TIMECOURSE, Condition, Experiment  # noqa: E402
from experiments.profiles import capture_tree  # noqa: E402
from plate_template import theme as template_theme  # noqa: E402


@pytest.fixture
def root(app_tk_root):
    """A throwaway window for one test. Toplevel supports everything the app
    asks of its root: title, geometry, menu, protocol and key bindings."""
    win = tk.Toplevel(app_tk_root)
    win.withdraw()
    yield win
    try:
        win.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def tree(tmp_path):
    for hours in (16, 40):
        for plate in (1, 2):
            d = tmp_path / f"{hours} Hours" / "Glucose" / f"Plate {plate} (Rep 1+2)"
            d.mkdir(parents=True)
            (d / "_9.JPG").write_bytes(b"x")
    return tmp_path


def complete(tree) -> Experiment:
    return Experiment(
        name="Set01",
        template_path="plate_template/templates/lab_standard_8x6.json",
        strains=[f"s{i}" for i in range(1, 9)],
        control_slot=1,
        conditions=[Condition("GLU", "Glucose")],
        mode=TIMECOURSE,
        photo_root=str(tree),
        profile=capture_tree(),
    )


def build(root, experiment) -> ExperimentApp:
    app = ExperimentApp(root, experiment)
    root.update_idletasks()
    app.refresh()
    return app


# --- it comes up ------------------------------------------------------------


def test_a_blank_experiment_opens(root):
    app = build(root, Experiment())
    assert app.controller.experiment.name == "Untitled"
    assert "Error:" in app.run_findings.get("1.0", "end")


def test_a_complete_experiment_opens_and_reads_its_photos(root, tree):
    app = build(root, complete(tree))
    assert len(app.controller.files) == 4
    assert len(app.controller.resolution.usable()) == 4


def test_the_template_is_loaded_and_shown(root, tree):
    app = build(root, complete(tree))
    assert app.controller.template is not None
    assert "8 slots" in app.panel.template_label.cget("text")
    assert "2 plates" in app.panel.template_details.cget("text")
    assert app.panel.template_preview.find_all()
    assert app.panel.choose_template_button.master is app.panel.template_label.master
    assert app.panel.intro.master is app.panel.strain_column
    assert app.panel.intro.master is not app.panel.preview_column


def test_clicking_an_empty_template_preview_opens_the_picker(root, monkeypatch):
    app = build(root, Experiment())
    called = []
    monkeypatch.setattr(app.panel, "_choose_template", lambda: called.append(True))

    assert app.panel._preview_clicked() == "break"
    assert called == [True]


def test_clicking_a_populated_template_preview_does_not_reopen_picker(
        root, tree, monkeypatch):
    app = build(root, complete(tree))
    called = []
    monkeypatch.setattr(app.panel, "_choose_template", lambda: called.append(True))

    assert app.panel._preview_clicked() is None
    assert called == []


def test_two_template_plates_are_previewed_side_by_side(root, tree):
    app = build(root, complete(tree))
    root.deiconify()
    root.geometry("1200x800")
    root.update()
    app.panel._draw_preview()
    first = app.panel.template_preview.bbox("plate-0")
    second = app.panel.template_preview.bbox("plate-1")
    assert first is not None and second is not None
    assert first[0] < second[0]
    assert abs(first[1] - second[1]) < 5
    assert len(app.panel.template_preview.find_withtag("plate-divider")) == 1
    left = app.panel.strain_column.winfo_width()
    right = app.panel.preview_column.winfo_width()
    assert right > left
    assert abs(left / (left + right) - 0.40) < 0.04
    assert int(app.panel.intro.cget("wraplength")) <= left


def test_template_preview_uses_the_real_dilution_fade(root, tree):
    app = build(root, complete(tree))
    root.deiconify()
    root.geometry("1200x800")
    root.update()
    app.panel._draw_preview()

    least = next(item for item in app.panel.template_preview.find_withtag("plate-0")
                 if "slot-1" in app.panel.template_preview.gettags(item)
                 and "dilution-0" in app.panel.template_preview.gettags(item))
    most = next(item for item in app.panel.template_preview.find_withtag("plate-0")
                if "slot-1" in app.panel.template_preview.gettags(item)
                and "dilution-2" in app.panel.template_preview.gettags(item))
    assert app.panel.template_preview.itemcget(least, "fill") == (
        template_theme.spot_fill(1, 3, 0))
    assert app.panel.template_preview.itemcget(most, "fill") == (
        template_theme.spot_fill(1, 3, 2))
    assert app.panel.template_preview.itemcget(least, "fill") != (
        app.panel.template_preview.itemcget(most, "fill"))
    assert app.panel.template_preview.find_withtag("replicate-label")
    root.withdraw()


def test_more_than_two_template_plates_enable_horizontal_scrolling(root, tree):
    app = build(root, complete(tree))
    root.deiconify()
    root.geometry("1200x800")
    root.update()
    third = deepcopy(app.controller.template.plates[0])
    third.id = "3"
    third.label = "Plate 3"
    app.controller.template.plates.append(third)
    app.panel.refresh()
    root.update()

    assert app.panel.template_scroll.winfo_manager() == "grid"
    assert len(app.panel.template_preview.find_withtag("plate-divider")) == 2
    scrollregion = [float(v) for v in
                    app.panel.template_preview.cget("scrollregion").split()]
    assert scrollregion[2] > app.panel.template_preview.winfo_width()


def visible_tabs(app) -> list[str]:
    """Tab labels actually on screen. `hide()` leaves a tab in `tabs()`."""
    return [app.notebook.tab(t, "text") for t in app.notebook.tabs()
            if app.notebook.tab(t, "state") != "hidden"]


def test_a_timecourse_has_no_plate_picker(root, tree):
    """It chooses its own photographs; picking them by hand would contradict it."""
    app = build(root, complete(tree))
    assert visible_tabs(app) == ["1. Panel", "2. Conditions", "3. Run"]


def test_handpicked_mode_adds_the_plate_picker(root, tree):
    e = complete(tree)
    e.mode = QUANTIFY
    app = build(root, e)
    assert visible_tabs(app) == ["1. Panel", "2. Conditions", "3. Plates",
                                 "4. Run"]


def test_the_picker_comes_and_goes_with_the_mode(root, tree):
    """A hidden tab stays in `tabs()`, so this is easy to get wrong once."""
    app = build(root, complete(tree))
    for _ in range(2):
        app.controller.set_mode(QUANTIFY)
        app.refresh()
        assert "3. Plates" in visible_tabs(app)
        app.controller.set_mode(TIMECOURSE)
        app.refresh()
        assert "3. Plates" not in visible_tabs(app)


def test_the_photo_folder_lives_in_the_header(root, tree):
    """Both modes need it, so it is not a step on a tab of its own."""
    app = build(root, complete(tree))
    assert str(tree) in app.header.folder.cget("text")
    assert "4 of 4 read" in app.header.folder_note.cget("text")


# --- awkward states ---------------------------------------------------------


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda e: setattr(e, "photo_root", ""), "no photo folder"),
        (lambda e: setattr(e, "photo_root", "/nowhere/at/all"), "folder has moved"),
        (lambda e: setattr(e, "template_path", "gone.json"), "template missing"),
        (lambda e: setattr(e, "strains", []), "no panel"),
        (lambda e: setattr(e, "conditions", []), "no conditions"),
        (lambda e: setattr(e, "control_slot", None), "no control"),
        (lambda e: setattr(e, "mode", QUANTIFY), "handpicked mode"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_it_still_opens_when_something_is_missing(root, tree, mutate, why):
    e = complete(tree)
    mutate(e)
    app = build(root, e)
    assert app.run_findings.get("1.0", "end").strip(), f"{why}: nothing reported"


def test_a_missing_photo_folder_is_reported_in_the_header(root, tree):
    e = complete(tree)
    e.photo_root = str(tree / "gone")
    app = build(root, e)
    assert "not found" in app.header.folder_note.cget("text")


# --- the plate picker -------------------------------------------------------


def handpicked(tree) -> Experiment:
    e = complete(tree)
    e.mode = QUANTIFY
    return e


def test_the_picker_lists_one_row_per_condition_and_plate(root, tree):
    app = build(root, handpicked(tree))
    rows = app.plates.slots.get_children()
    # One condition in the fixture, two plates in the lab standard template.
    assert len(rows) == 2
    assert [app.plates.slots.set(r, "plate") for r in rows] == ["1", "2"]
    assert {app.plates.slots.set(r, "condition") for r in rows} == {"GLU"}


def test_the_picker_lists_every_photograph_in_the_folder(root, tree):
    """Unfiltered by any naming profile: the names may encode nothing."""
    app = build(root, handpicked(tree))
    assert app.plates.candidates.size() == 4


def test_choosing_a_photograph_fills_its_row(root, tree):
    app = build(root, handpicked(tree))
    target = app.controller.candidates()[0]
    app.plates.slots.selection_set(app.plates.slots.get_children()[0])
    app.plates._showing = target
    app.plates._assign()
    app.refresh()
    assert app.controller.experiment.pick("GLU", "1") == target
    assert app.plates.slots.set("GLU\x1f1", "photo") == Path(target).name


def test_choosing_moves_on_to_the_next_empty_plate(root, tree):
    app = build(root, handpicked(tree))
    app.plates.slots.selection_set("GLU\x1f1")
    app.plates._showing = app.controller.candidates()[0]
    app.plates._assign()
    assert app.plates.selected_slot() == ("GLU", "2")


def test_the_two_plates_of_one_condition_can_take_different_dilutions(root, tree):
    app = build(root, handpicked(tree))
    for plate, index in (("1", 0), ("2", 2)):
        app.controller.set_pick("GLU", plate, app.controller.candidates()[0])
        app.controller.set_plate_dilution("GLU", plate, index)
    app.refresh()
    assert app.plates.slots.set("GLU\x1f1", "dilution") == "1 - least"
    assert app.plates.slots.set("GLU\x1f2", "dilution") == "3 - most"


def test_the_dilution_cannot_be_set_before_a_photograph_is_chosen(root, tree):
    app = build(root, handpicked(tree))
    app.plates.slots.selection_set("GLU\x1f1")
    app.plates._slot_selected()
    assert str(app.plates.level_box.cget("state")) == "disabled"
    assert "before setting its dilution" in app.plates.hint.cget("text")


def test_the_level_list_comes_from_the_template(root, tree):
    """Three for the lab standard -- but however many the template declares."""
    app = build(root, handpicked(tree))
    assert list(app.plates.level_box.cget("values")) == [
        "1 - least", "2 - middle", "3 - most"]


def test_choosing_a_level_from_the_list_stores_its_index(root, tree):
    """The index, not the name: a renamed level must not move the rows."""
    app = build(root, handpicked(tree))
    app.controller.set_pick("GLU", "1", app.controller.candidates()[0])
    app.plates.slots.selection_set("GLU\x1f1")
    app.plates.dilution.set("3 - most")
    app.plates._set_dilution()
    assert app.controller.experiment.dilution_for("GLU", "1") == 2


def test_an_unfilled_plate_is_marked(root, tree):
    app = build(root, handpicked(tree))
    assert "empty" in app.plates.slots.item("GLU\x1f1", "tags")
    app.controller.set_pick("GLU", "1", app.controller.candidates()[0])
    app.controller.set_plate_dilution("GLU", "1", 1)
    app.refresh()
    assert "empty" not in app.plates.slots.item("GLU\x1f1", "tags")


def test_the_least_dilute_level_counts_as_chosen(root, tree):
    """Level 0 is a real choice; treating it as falsy would keep the row red."""
    app = build(root, handpicked(tree))
    app.controller.set_pick("GLU", "1", app.controller.candidates()[0])
    app.controller.set_plate_dilution("GLU", "1", 0)
    app.refresh()
    assert app.controller.experiment.dilution_for("GLU", "1") == 0
    assert "empty" not in app.plates.slots.item("GLU\x1f1", "tags")


def test_stepping_moves_through_the_photographs(root, tree):
    app = build(root, handpicked(tree))
    names = app.controller.candidates()
    app.plates._showing = names[0]
    app.plates._step(1)
    assert app.plates._showing == names[1]
    app.plates._step(-1)
    assert app.plates._showing == names[0]
    app.plates._step(-1)                       # already at the start
    assert app.plates._showing == names[0]


def test_the_caption_says_when_a_photograph_is_already_in_use(root, tree):
    app = build(root, handpicked(tree))
    target = app.controller.candidates()[0]
    app.controller.set_pick("GLU", "1", target)
    app.plates._showing = target
    app.refresh()
    assert "already used for GLU plate 1" in app.plates.caption.cget("text")


def test_the_conditions_tab_counts_photos_per_condition(root, tree):
    app = build(root, complete(tree))
    assert app.conditions.tree.set("GLU", "photos") == "4"


def test_the_conditions_table_scales_rows_and_fits_columns(root, tree):
    app = build(root, complete(tree))
    style = ttk.Style(root)
    assert int(style.lookup(app.conditions.tree_style, "rowheight")) >= int(
        24 * app.conditions.scale
    )
    assert all(app.conditions.tree.column(key, "width") > 0
               for key, *_rest in app.conditions._column_specs)


def test_selecting_a_condition_populates_the_inspector(root, tree):
    app = build(root, complete(tree))
    app.conditions.tree.selection_set("GLU")
    app.conditions._selection_changed()

    assert "Glucose" in app.conditions.condition_name.cget("text")
    assert "4 photographs" in app.conditions.condition_photos.cget("text")
    # str(): ttk returns a Tcl_Obj from cget("state"), which never compares
    # equal to a plain Python string.
    assert str(app.conditions.control_picker.cget("state")) == "readonly"
    assert len(app.conditions._exclude_vars) == 8


def test_the_condition_inspector_edits_control_and_exclusions(root, tree):
    app = build(root, complete(tree))
    app.conditions.tree.selection_set("GLU")
    app.conditions._selection_changed()

    app.conditions.condition_control.set("2: s2")
    app.conditions._control_selected()
    assert app.controller.experiment.control_for("GLU") == 2

    app.conditions._exclude_vars[3].set(True)
    app.conditions._excluded_toggled()
    assert app.controller.experiment.exclude_for("GLU") == (3,)


def test_adding_a_condition_prefills_the_focused_name_prompt(
        root, tree, monkeypatch):
    app = build(root, complete(tree))
    replies = iter(["K-OAc", "Potassium Acetate"])
    prompts = []

    def answer(_parent, _title, prompt, initial=""):
        prompts.append((prompt, initial))
        return next(replies)

    monkeypatch.setattr(conditions_gui, "_ask_text", answer)
    app.conditions._add()

    assert prompts[1][1] == "K-OAc"
    assert app.controller.experiment.condition("K-OAc").label == "Potassium Acetate"


def test_findings_appear_only_in_the_experiment_summary(root):
    app = build(root, Experiment())
    assert not hasattr(app, "findings")
    checks = app.run_findings.get("1.0", "end")
    assert "Error:" in checks
    assert "sample slot" in checks


def test_the_panel_shows_the_strain_names(root, tree):
    app = build(root, complete(tree))
    assert [var.get() for _, var in app.panel._rows][:2] == ["s1", "s2"]
    assert app.panel.control.get() == 1


def test_enter_saves_a_strain_and_moves_to_the_next_slot(root, tree, monkeypatch):
    app = build(root, complete(tree))
    slot, var = app.panel._rows[0]
    var.set("new strain")
    focused = []
    monkeypatch.setattr(app.panel._entries[2], "focus_set",
                        lambda: focused.append(2))

    assert app.panel._commit_and_move(slot, var, 1) == "break"
    assert app.controller.experiment.strain(1) == "new strain"
    assert focused == [2]


def test_arrow_navigation_moves_up_and_down_between_strain_slots(
        root, tree, monkeypatch):
    app = build(root, complete(tree))
    focused = []
    monkeypatch.setattr(app.panel._entries[1], "focus_set",
                        lambda: focused.append(1))
    monkeypatch.setattr(app.panel._entries[3], "focus_set",
                        lambda: focused.append(3))
    _, var = app.panel._rows[1]

    app.panel._commit_and_move(2, var, -1)
    app.panel._commit_and_move(2, var, 1)
    assert focused == [1, 3]


@pytest.mark.parametrize("column, editor", [("#3", "control"), ("#4", "exclude")])
def test_double_click_edits_the_clicked_condition_cell(
        root, tree, monkeypatch, column, editor):
    app = build(root, complete(tree))
    called = []
    monkeypatch.setattr(app.conditions.tree, "identify_region",
                        lambda _x, _y: "cell")
    monkeypatch.setattr(app.conditions.tree, "identify_row", lambda _y: "GLU")
    monkeypatch.setattr(app.conditions.tree, "identify_column", lambda _x: column)
    monkeypatch.setattr(app.conditions, "_set_control",
                        lambda: called.append("control"))
    monkeypatch.setattr(app.conditions, "_set_exclude",
                        lambda: called.append("exclude"))

    app.conditions._edit(SimpleNamespace(x=10, y=10))
    assert called == [editor]
    assert app.conditions.selected_code() == "GLU"


def test_excluded_slots_are_chosen_from_the_named_slot_picker(
        root, tree, monkeypatch):
    app = build(root, complete(tree))
    app.conditions.tree.selection_set("GLU")
    shown = {}

    def choose(_parent, _title, _prompt, options, *, selected):
        shown["options"] = options
        shown["selected"] = selected
        return [options[1], options[3]]

    monkeypatch.setattr(conditions_gui, "_ask_choices", choose)
    app.conditions._set_exclude()

    assert shown["options"][:4] == ["1: s1", "2: s2", "3: s3", "4: s4"]
    assert shown["selected"] == set()
    assert app.controller.experiment.exclude_for("GLU") == (2, 4)


# --- the title says whether there is unsaved work ---------------------------


def test_the_title_marks_unsaved_changes(root, tree):
    app = build(root, complete(tree))
    assert not app.root.title().startswith("*")
    app.controller.set_strain(2, "changed")
    app.refresh()
    assert app.root.title().startswith("*")


# --- the entry point --------------------------------------------------------


def _run_module(*args):
    """`py -m experiments.app ...` in a fresh process.

    A subprocess rather than calling `main` directly: `main` creates its own Tk
    root, and a second root in a process that already has one is unreliable.
    This also tests the entry point the launcher actually uses.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    repo = _Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [_sys.executable, "-m", "experiments.app", *args],
        capture_output=True, text=True, env=env, cwd=str(repo), timeout=120,
    )


def test_selftest_exits_cleanly():
    done = _run_module("--selftest")
    if "no display" in done.stderr or "TclError" in done.stderr:
        pytest.skip("no display available")
    assert done.returncode == 0, done.stderr
    assert "selftest OK" in done.stdout


def test_a_real_experiment_opens_from_the_command_line(tmp_path, tree):
    from experiments import schema

    path = tmp_path / "e.spotexp.json"
    schema.save(complete(tree), path, bump_revision=False)

    done = _run_module(str(path), "--selftest")
    if "no display" in done.stderr or "TclError" in done.stderr:
        pytest.skip("no display available")
    assert done.returncode == 0, done.stderr
    assert "4 photo(s)" in done.stdout


def test_an_unreadable_file_is_refused_with_exit_2(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    done = _run_module(str(bad), "--selftest")
    assert done.returncode == 2
    assert "bad.json" in done.stderr
