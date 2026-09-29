"""Creating many experiments at once, one per photo folder.

The case that matters is the one the feature exists for: ten sets of the same
assay, same layout, same media, different strains -- and panels that differ in
SIZE because some sets leave columns empty.

The dialog is driven directly rather than clicked, so these run headless.
"""

import pytest

tk = pytest.importorskip("tkinter")

from experiments import schema  # noqa: E402
from experiments.gui.bulk import BulkCreateDialog  # noqa: E402
from experiments.model import Condition  # noqa: E402

TEMPLATE = "plate_template/templates/lab_standard_8x6.json"


@pytest.fixture
def bulk_tk_root(app_tk_root):
    """The package-wide root from conftest; see its docstring for why one."""
    return app_tk_root


@pytest.fixture
def folders(tmp_path):
    made = []
    for name in ("Set01", "Set02", "Set03"):
        d = tmp_path / "Data" / name
        d.mkdir(parents=True)
        (d / "_9.JPG").write_bytes(b"x")
        made.append(d)
    return made


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "Experiment Designs"
    d.mkdir()
    return d


def open_dialog(root, out_dir, folders, conditions=("GLU", "GLY")):
    """Build the dialog without entering its modal loop."""
    dialog = BulkCreateDialog.__new__(BulkCreateDialog)
    tk.Toplevel.__init__(dialog, root)
    dialog.withdraw()
    dialog.out_dir = out_dir
    dialog.template = None
    dialog.template_path = TEMPLATE
    dialog.folders = list(folders)
    dialog.conditions = [Condition(c, c) for c in conditions]
    dialog.created = []
    dialog._cells = {}
    dialog._controls = []

    from tkinter import ttk
    body = ttk.Frame(dialog)
    body.pack()
    dialog._build_sources(body)
    dialog._build_panel(body)
    dialog._build_buttons(body)
    dialog._load_template(TEMPLATE)
    dialog._refresh()
    return dialog


def fill(dialog, column: int, names, control: int = 1):
    for slot, name in enumerate(names, start=1):
        dialog._cells[(slot, column)].set(name or "")
    dialog._controls[column - 1].set(control)


# --- the grid ---------------------------------------------------------------


def test_the_grid_has_a_column_per_folder_and_a_row_per_slot(bulk_tk_root, out_dir,
                                                             folders):
    d = open_dialog(bulk_tk_root, out_dir, folders)
    assert d.template.sample_slots() == 8
    assert len(d._cells) == 8 * 3
    assert len(d._controls) == 3
    d.destroy()


def test_typed_names_survive_adding_another_folder(bulk_tk_root, out_dir, folders,
                                                   tmp_path):
    d = open_dialog(bulk_tk_root, out_dir, folders[:1])
    fill(d, 1, ["WT BY", "ΔATX1"])
    extra = tmp_path / "Data" / "Set09"
    extra.mkdir(parents=True)
    d._add(extra)
    assert d._cells[(1, 1)].get() == "WT BY"
    assert d._cells[(2, 1)].get() == "ΔATX1"
    d.destroy()


# --- what it refuses --------------------------------------------------------


def test_it_will_not_create_without_a_panel(bulk_tk_root, out_dir, folders):
    d = open_dialog(bulk_tk_root, out_dir, folders)
    assert d._problems()
    assert "name at least one strain" in d._problems()[0]
    d.destroy()


def test_it_will_not_create_when_the_control_slot_is_empty(bulk_tk_root, out_dir,
                                                           folders):
    d = open_dialog(bulk_tk_root, out_dir, folders[:1])
    fill(d, 1, [None, "ΔATX1"], control=1)      # control points at a blank slot
    assert "control slot has no strain" in d._problems()[0]
    d.destroy()


def test_it_will_not_create_without_conditions(bulk_tk_root, out_dir, folders):
    d = open_dialog(bulk_tk_root, out_dir, folders[:1], conditions=())
    fill(d, 1, ["WT BY"])
    assert "condition" in d._problems()[0]
    d.destroy()


# --- creating ---------------------------------------------------------------


def test_one_file_per_folder_is_written(bulk_tk_root, out_dir, folders):
    d = open_dialog(bulk_tk_root, out_dir, folders)
    for col in (1, 2, 3):
        fill(d, col, [f"s{col}-{i}" for i in range(1, 9)])
    d._create()

    written = sorted(p.name for p in out_dir.glob("*.spotexp.json"))
    assert written == ["Set01.spotexp.json", "Set02.spotexp.json",
                       "Set03.spotexp.json"]
    assert len(d.created) == 3


def test_each_experiment_gets_its_own_panel_and_folder(bulk_tk_root, out_dir,
                                                       folders):
    d = open_dialog(bulk_tk_root, out_dir, folders)
    fill(d, 1, ["WT BY", "ΔATX1"])
    fill(d, 2, ["WT BY", "ΔGRX5"])
    fill(d, 3, ["WT BY", "ΔSOD1"])
    d._create()

    one = schema.load(out_dir / "Set01.spotexp.json")
    two = schema.load(out_dir / "Set02.spotexp.json")
    assert one.strain(2) == "ΔATX1"
    assert two.strain(2) == "ΔGRX5"
    assert one.photo_root == str(folders[0])
    assert two.photo_root == str(folders[1])


def test_the_template_and_conditions_are_shared(bulk_tk_root, out_dir, folders):
    d = open_dialog(bulk_tk_root, out_dir, folders)
    for col in (1, 2, 3):
        fill(d, col, ["WT BY"])
    d._create()

    loaded = [schema.load(p) for p in sorted(out_dir.glob("*.spotexp.json"))]
    assert {e.template_path for e in loaded} == {TEMPLATE}
    assert {tuple(e.condition_codes()) for e in loaded} == {("GLU", "GLY")}
    assert len({e.id for e in loaded}) == 3       # but each is its own experiment


def test_panels_may_differ_in_size_through_blank_slots(bulk_tk_root, out_dir,
                                                       folders):
    """The stated requirement: same architecture, different numbers of strains."""
    d = open_dialog(bulk_tk_root, out_dir, folders)
    fill(d, 1, ["WT BY", "a", "b", "c", "d", "e", "f", "g"])
    fill(d, 2, ["WT BY", "a", None, None, "d", None, None, None])
    fill(d, 3, [None, "WT BY", "x", None, None, None, None, None], control=2)
    d._create()

    one, two, three = (schema.load(out_dir / f"Set0{n}.spotexp.json")
                       for n in (1, 2, 3))
    assert len(one.filled_slots()) == 8
    assert two.filled_slots() == [1, 2, 5]
    assert three.filled_slots() == [2, 3]
    # Same shape underneath: every panel still has eight slots and one control.
    assert {e.slot_count() for e in (one, two, three)} == {8}
    assert three.control_slot == 2
    assert three.strain(three.control_slot) == "WT BY"


def test_a_created_experiment_validates(bulk_tk_root, out_dir, folders):
    from plate_template import presets

    from experiments.validate import blocking, validate

    d = open_dialog(bulk_tk_root, out_dir, folders[:1])
    fill(d, 1, ["WT BY", "ΔATX1", None, None, None, None, None, None])
    d._create()

    e = schema.load(out_dir / "Set01.spotexp.json")
    errors = [i.code for i in blocking(validate(e, presets.lab_standard_8x6()))]
    assert errors == []


def test_existing_files_are_not_overwritten_without_asking(bulk_tk_root, out_dir,
                                                           folders, monkeypatch):
    (out_dir / "Set01.spotexp.json").write_text("{}", encoding="utf-8")
    d = open_dialog(bulk_tk_root, out_dir, folders[:1])
    fill(d, 1, ["WT BY"])

    asked = {}

    def refuse(*args, **kwargs):
        asked["called"] = True
        return False

    monkeypatch.setattr("experiments.gui.bulk.messagebox.askokcancel", refuse)
    d._create()
    assert asked.get("called")
    assert d.created == []
    assert (out_dir / "Set01.spotexp.json").read_text(encoding="utf-8") == "{}"
    d.destroy()
