"""Which photos there are, what is in each cell, and every decision about them."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from data_review import catalog, flags as ff
from data_review_fixtures import PHOTOS, relpath
from data_review.controller import FILTERS, DataReviewController
from experiments import geometry, intake
from experiments.model import StrainGroup
from plate_template.presets import lab_standard_8x6


def _resolved(e):
    files, _ = intake.scan_images(Path(e.photo_root))
    return intake.resolve(e, files)


# --- catalog -------------------------------------------------------------------


def test_photos_are_the_ones_a_run_would_measure_in_reading_order(project):
    _, e = project
    photos = catalog.photos(e, _resolved(e))
    assert [p.relpath for p in photos] == [
        relpath("24 Hours", "GLU", 1, "a.jpg"), relpath("24 Hours", "GLU", 1, "b.jpg"),
        relpath("24 Hours", "GLU", 2, "a.jpg"), relpath("48 Hours", "GLU", 1, "a.jpg"),
        relpath("48 Hours", "GLU", 2, "a.jpg"), relpath("24 Hours", "GLY", 1, "a.jpg"),
        relpath("24 Hours", "GLY", 2, "a.jpg")]
    second = photos[1]
    assert second.shot == 2 and second.title == "Glucose · 24 Hours · plate 1 · shot 2"


def test_ignored_photos_and_undeclared_conditions_are_left_out(project):
    _, e = project
    e.ignored = [relpath("24 Hours", "GLU", 1, "b.jpg")]
    e.conditions = [c for c in e.conditions if c.code == "GLU"]
    photos = catalog.photos(e, _resolved(e))
    assert len(photos) == 4 and all(p.condition == "GLU" for p in photos)


def test_cells_carry_strain_replicate_and_level_from_the_template(project):
    _, e = project
    template = lab_standard_8x6()
    photo = catalog.photos(e, _resolved(e))[2]           # GLU plate 2
    cells = catalog.cells(e, template, photo)
    assert len(cells) == 48
    first = cells[(0, 0)]
    assert (first.strain, first.slot, first.is_control) == ("WT", 1, True)
    assert first.replicate == 3 and first.level == 0     # plate 2 holds reps 3-4
    assert cells[(3, 2)].replicate == 4 and cells[(3, 2)].strain == "B"
    assert cells[(5, 7)].strain is None                  # slot 8 is empty
    sibs = catalog.siblings(first, cells)
    assert [(s.row, s.col) for s in sibs] == [(3, 0)]


def test_cells_follow_the_camera_orientation(project):
    _, e = project
    template = lab_standard_8x6()
    e = replace(e, photo_top="right")
    photo = catalog.photos(e, _resolved(e))[0]
    cells = catalog.cells(e, template, photo)
    expected = {(r, c): p.sample_slot for level in range(3)
                for r, c, p in geometry.oriented_cells_for(template, "1", level, "right")}
    assert {k: v.slot for k, v in cells.items()} == expected
    assert catalog.grid_shape(e, template) == (8, 6)


def test_cells_are_named_by_the_photos_own_strain_group(project):
    _, e = project
    template = lab_standard_8x6()
    photo = replace(catalog.photos(e, _resolved(e))[0], group="G1")
    e.strain_groups = {"G1": StrainGroup(strains=["X1", "X2"], control_slot=2)}
    cells = catalog.cells(e, template, photo)
    assert cells[(0, 0)].strain == "X1" and not cells[(0, 0)].is_control
    assert cells[(0, 1)].is_control


# --- the controller ---------------------------------------------------------------


@pytest.fixture
def ctl(project):
    path, _ = project
    c = DataReviewController(ff.sidecar_for(path))
    c.load()
    assert not c.error, c.error
    return c


def test_load_finds_the_experiment_and_its_photos(ctl):
    assert ctl.experiment.name == "Synthetic"
    assert len(ctl.photos) == len(PHOTOS)
    assert ctl.current is ctl.photos[0]
    assert not ctl.dirty


def test_a_missing_experiment_is_reported_not_raised(tmp_path):
    c = DataReviewController(tmp_path / "Gone.datareview.json")
    c.load()
    assert "not found" in c.error and not c.photos


def test_flag_a_spot_click_again_to_clear(ctl):
    assert ctl.toggle_spot(0, 1, "contamination")
    rel = ctl.current.relpath
    assert ctl.flags.spot_reason(rel, 1, 2) == "contamination"
    assert "Flag A rep 1" in ctl.last_change
    assert ctl.toggle_spot(0, 1, "anything")
    assert ctl.flags.spot_reason(rel, 1, 2) == ""


def test_undo_redo_and_dirty_follow_every_decision(ctl):
    ctl.set_plate("smeared")
    ctl.set_spot(2, 3, "bubble")
    assert ctl.dirty and ctl.counts()["plates"] == 1 and ctl.counts()["spots"] == 1
    assert ctl.undo().startswith("Flag")
    assert ctl.counts()["spots"] == 0
    ctl.undo()
    assert not ctl.dirty
    ctl.redo()
    assert ctl.flags.plate_reason(ctl.current.relpath) == "smeared"


def test_save_writes_the_review_beside_the_experiment(ctl, project):
    path, _ = project
    ctl.set_plate("out of focus")
    saved = ctl.save()
    assert saved == ff.sidecar_for(path) and not ctl.dirty
    back = ff.load(saved)
    assert back.experiment == path.name and back.experiment_id == "synthetic-id"
    assert back.plate_reason(ctl.current.relpath) == "out of focus"


def test_reload_keeps_unsaved_work(ctl):
    ctl.set_plate("smeared")
    ctl.reload_flags()
    assert ctl.flags.n_plates == 1


def test_looking_through_with_a_filter(ctl):
    first, second = ctl.photos[0], ctl.photos[1]
    ctl.set_reviewed(True)
    ctl.set_filter("Not looked at")
    assert first not in ctl.visible()
    # The current photo was filtered out; stepping on goes to the one after it.
    assert ctl.step(1) and ctl.current is second
    ctl.set_filter("Flagged")
    assert ctl.visible() == []
    ctl.set_spot(0, 0, "bubble")
    assert ctl.visible() == [second]
    ctl.set_filter(FILTERS[0])
    assert ctl.position() == (2, len(PHOTOS))
    while ctl.step(1):
        pass
    assert ctl.current is ctl.photos[-1] and not ctl.step(1)


def test_clearing_a_photo_forgets_plate_and_spots(ctl):
    ctl.set_plate("smeared")
    ctl.set_spot(1, 1, "bubble")
    assert ctl.clear_photo()
    assert not ctl.flags.is_flagged(ctl.current.relpath)


def test_flags_for_photos_no_longer_in_the_experiment_are_kept_and_counted(ctl):
    ctl.flags.set_plate("gone/photo.jpg", "old")
    assert ctl.counts()["orphaned"] == 1
    assert ctl.counts()["plates"] == 0
