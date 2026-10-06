"""Shared by the data review tests. A module of its own, not `conftest`, so
importing it cannot pick up another suite's conftest of the same name."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "plate_template" / "templates" / "lab_standard_8x6.json"

#: (timepoint folder, condition folder, plate, file) -- two shots of GLU plate 1,
#: so re-shots are numbered, and a second condition.
PHOTOS = [
    ("24 Hours", "GLU", 1, "a.jpg"),
    ("24 Hours", "GLU", 1, "b.jpg"),
    ("24 Hours", "GLU", 2, "a.jpg"),
    ("24 Hours", "GLY", 1, "a.jpg"),
    ("24 Hours", "GLY", 2, "a.jpg"),
    ("48 Hours", "GLU", 1, "a.jpg"),
    ("48 Hours", "GLU", 2, "a.jpg"),
]


def relpath(hours, condition, plate, name) -> str:
    return f"{hours}/{condition}/Plate {plate}/{name}"


def make_project(tmp_path: Path):
    """A photo folder of small real JPEGs, and an experiment saved for it.

    Returns (experiment path, experiment). The experiment reads its photos
    the way a migrated capture tree does, so nothing here is hand-assigned.
    """
    from PIL import Image

    from experiments import intake, schema
    from experiments.model import Condition, Experiment, TIMECOURSE

    photos = tmp_path / "photos"
    for i, (hours, condition, plate, name) in enumerate(PHOTOS):
        path = photos / relpath(hours, condition, plate, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Each a different size: the measurement cache is keyed on file name,
        # size and time, so identical files called a.jpg would share an entry.
        Image.new("RGB", (240 + 16 * i, 160), (40, 60, 80)).save(path, "JPEG")
    files, _ = intake.scan_images(photos)
    profile, _ = intake.infer_profile(files, mode=TIMECOURSE)
    e = Experiment(
        name="Synthetic", template_path=str(TEMPLATE), mode=TIMECOURSE,
        strains=["WT", "A", "B", "C", "D", "E", "F", None], control_slot=1,
        conditions=[Condition("GLU", "Glucose"), Condition("GLY", "Glycerol")],
        photo_root=str(photos), profile=profile, id="synthetic-id")
    path = tmp_path / "Experiment Designs" / "Synthetic.spotexp.json"
    schema.save(e, path, bump_revision=False)
    return path, e
