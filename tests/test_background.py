"""Frozen ImageJ outputs and application integration, requiring no Java/FIJI."""
from pathlib import Path
import sys

import numpy as np
import pytest
import tifffile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import spotting_quant as sq
from spotting_background import subtract_background


@pytest.mark.parametrize("case", ["flat", "noise", "small_radius", "corners", "spots", "tiny"])
def test_matches_frozen_imagej(case):
    with np.load(Path(__file__).with_name("background_reference.npz")) as ref:
        src = ref[case + "_input"]
        original = src.copy()
        got = subtract_background(src, float(ref[case + "_radius"]))
        np.testing.assert_array_equal(got, ref[case + "_expected"])
        np.testing.assert_array_equal(src, original)
        assert got.dtype == np.float32


def test_batch_and_all_mode_aliases_match_without_subprocess(tmp_path, monkeypatch):
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("background subtraction must not launch an external process")
    monkeypatch.setattr(subprocess, "run", forbidden)
    with np.load(Path(__file__).with_name("background_reference.npz")) as ref:
        src = ref["spots_input"]
        expected = ref["spots_expected"]
    path = tmp_path / "plate with spaces.tif"
    tifffile.imwrite(path, src)
    outputs = sq.subtract_background_batch([(path, 128), (path, 128.1)], tmp_path / "batch")
    assert len(outputs) == 1
    np.testing.assert_array_equal(tifffile.imread(next(iter(outputs.values()))), expected)
    assert sq.MeasureOptions().bg_mode == "python"
    for mode in ("python", "fiji", "paraboloid"):
        got, iterations, spread = sq.subtract_background(
            src, ball_radius=128, bg_centers=[], bg_radius=3, mode=mode)
        np.testing.assert_array_equal(got, expected)
        assert iterations == 1
    # Negative noise is preserved, rather than clipped to zero.
    assert expected.min() < 0


@pytest.mark.parametrize("radius", [0, -1, np.nan, np.inf])
def test_rejects_invalid_radius(radius):
    with pytest.raises(ValueError, match="radius"):
        subtract_background(np.ones((5, 5)), radius)


def test_rejects_nonfinite_image():
    with pytest.raises(ValueError, match="finite"):
        subtract_background(np.full((5, 5), np.nan), 20)
