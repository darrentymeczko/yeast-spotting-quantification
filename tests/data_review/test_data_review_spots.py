"""Reading where detection put the spots, from the measurement cache itself."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from data_review import catalog, cli, spots
from experiments import intake
from plate_template.presets import lab_standard_8x6

PITCH = 50.0


def _photos(e):
    files, _ = intake.scan_images(Path(e.photo_root))
    return catalog.photos(e, intake.resolve(e, files))


def _write_entry(path, layout, plate, *, skip_level=None):
    """A cache entry laid out exactly as `spotting_batch.measure` saves one."""
    sb, _ = spots._engine()
    cf = spots.cache_file(path, layout)
    cf.parent.mkdir(parents=True, exist_ok=True)
    rows, cols = layout.n_rows, layout.n_cols
    centers = np.zeros((rows, cols, 2))
    for r in range(rows):
        for c in range(cols):
            centers[r, c] = (100 + PITCH * r, 200 + PITCH * c)
    store = {}
    for i, level in enumerate(layout.levels):
        if i == skip_level:
            continue
        key = sb._rowset_key(level.quant_rows(plate))
        rim = np.zeros((rows, cols), dtype=bool)
        rim[level.rows(plate)[0], cols - 1] = True
        store.update({f"{key}_net": np.full((rows, cols), 10.0 + i),
                      f"{key}_rim": rim, f"{key}_radius": 12.0 + i,
                      f"{key}_bg_mean": 0.0, f"{key}_spread": 0.0,
                      f"{key}_bg_samples": np.array([0.0, 0.1, -0.1, 0.05, 0.0])})
    np.savez_compressed(cf, centers=centers, plate_center=np.array([300.0, 400.0]),
                        plate_radius=500.0, **store)
    return cf


@pytest.fixture
def setup(project, tmp_path, monkeypatch):
    _, e = project
    monkeypatch.setattr(spots, "cache_dir", lambda: tmp_path / "cache")
    template = lab_standard_8x6()
    return e, template, spots.layout_for(e, template), _photos(e)


def test_a_photo_without_a_cache_entry_is_not_detected_yet(setup):
    e, template, layout, photos = setup
    assert not spots.is_detected(photos[0].path, layout)
    assert spots.load(photos[0].path, layout, photos[0].plate) is None


def test_each_cell_is_read_at_its_own_dilutions_roi(setup):
    e, template, layout, photos = setup
    photo = photos[2]                                   # GLU, plate 2
    _write_entry(photo.path, layout, photo.plate)
    assert spots.is_detected(photo.path, layout)
    found = spots.load(photo.path, layout, photo.plate)
    assert found.shape == (6, 8) and not found.approximate
    for i, level in enumerate(layout.levels):
        for cell in level.cells(photo.plate):
            assert found.net[(cell.row, cell.col)] == 10.0 + i
            assert found.radius[(cell.row, cell.col)] == 12.0 + i
    assert found.pitch == pytest.approx(PITCH)
    assert (0, 7) in found.rim and (0, 6) not in found.rim


def test_pointing_finds_the_nearest_spot_and_nothing_between_them(setup):
    e, template, layout, photos = setup
    _write_entry(photos[0].path, layout, 1)
    found = spots.load(photos[0].path, layout, 1)
    assert found.nearest(100 + 2 * PITCH + 3, 200 + 4 * PITCH - 2) == (2, 4)
    assert found.nearest(100 + PITCH / 2, 200 + PITCH / 2) is None


def test_a_missing_level_borrows_another_and_says_so(setup):
    e, template, layout, photos = setup
    _write_entry(photos[0].path, layout, 1, skip_level=2)
    found = spots.load(photos[0].path, layout, 1)
    assert found.approximate
    assert len(found.net) == 48


def test_the_control_mean_skips_artifact_spots(setup):
    e, template, layout, photos = setup
    photo = photos[0]
    _write_entry(photo.path, layout, photo.plate)
    found = spots.load(photo.path, layout, photo.plate)
    cells = catalog.cells(e, template, photo)
    assert spots.control_mean(found, cells, 0) == pytest.approx(10.0)
    for cell in cells.values():
        if cell.is_control:
            found.rim.add((cell.row, cell.col))
    assert spots.control_mean(found, cells, 0) is None


def test_a_photo_changed_since_detection_reads_as_not_detected(setup):
    e, template, layout, photos = setup
    photo = photos[0]
    _write_entry(photo.path, layout, photo.plate)
    st = photo.path.stat()
    os.utime(photo.path, (st.st_atime, st.st_mtime + 120))
    assert not spots.is_detected(photo.path, layout)


def test_status_and_estimate_report_without_measuring(project, setup, capsys, monkeypatch):
    path, _ = project
    e, template, layout, photos = setup
    _write_entry(photos[0].path, layout, photos[0].plate)
    assert cli.main(["status", str(path)]) == cli.EXIT_OK
    assert "spots located on 1 of 7" in capsys.readouterr().out
    import spotting_timecourse as tc

    monkeypatch.setattr(tc, "measure_all", lambda *a, **k: pytest.fail("measured"))
    assert cli.main(["detect", str(path), "--estimate"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "1 already located, 6 to do" in out


def test_detect_measures_only_what_is_missing_with_the_pipelines_jobs(
        project, setup, monkeypatch, tmp_path):
    path, _ = project
    e, template, layout, photos = setup
    _write_entry(photos[0].path, layout, photos[0].plate)
    import spotting_timecourse as tc

    seen = {}

    def measure_all(jobs, cache, workers, timing=False):
        seen["jobs"] = jobs
        return [], []

    monkeypatch.setattr(tc, "measure_all", measure_all)
    status = tmp_path / "status.txt"
    monkeypatch.setenv(cli.STATUS_ENV, str(status))
    assert cli.main(["detect", str(path), "--workers", "1"]) == cli.EXIT_OK
    measured = {Path(job[0]).name for job in seen["jobs"]}
    assert len(seen["jobs"]) == 6 and str(photos[0].path) not in {j[0] for j in seen["jobs"]}
    assert measured == {"a.jpg", "b.jpg"}
    # The lab's layout: the pipeline's classic job, so the cache key matches a run's.
    assert all(len(job) == 3 for job in seen["jobs"])
    assert status.read_text() == "0"
