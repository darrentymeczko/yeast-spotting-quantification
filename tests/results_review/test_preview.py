"""The review preview uses the same PyPrism renderer as exported figures."""

from __future__ import annotations

import pytest
from conftest import needs_engine

from results_review.export import CSV_NAME, draw_preview
from results_review.rebuild import Frame

pytestmark = needs_engine


def png_bytes(size=(8, 8)) -> bytes:
    """Return a small, valid PNG for the SheetView state-machine tests."""
    import io
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def frame():
    import pandas as pd

    rows = []
    for rep in ("rep1", "rep2", "rep3", "rep4"):
        for col, strain in ((1, "WT BY"), (2, "ΔSOD2")):
            rows.append({
                "experiment": "Set01 GLU 20 Hours",
                "treatment": "Set01 GLU 20 Hours", "set": "TC",
                "plate": 1 if rep in ("rep1", "rep2") else 2,
                "image": "p.JPG", "replicate": rep, "dilution_row": 2,
                "dilution": "middle", "strain_col": col, "strain": strain,
                "raw_growth": 10.0 if col == 1 else 6.0,
                "artifact": False, "excluded": False,
                "is_control": col == 1, "control_raw": 10.0,
                "control_mean": 10.0, "relative_growth": 1.0 if col == 1 else 0.6,
                "control_ok": True, "outlier": False,
                "raw_growth_original": 10.0 if col == 1 else 6.0,
                "manual": False, "excluded_source": "", "outlier_source": "",
                "edit_note": "",
            })
    return Frame(medium="GLU", candidate=None,
                 experiment="Set01 GLU 20 Hours", tidy=pd.DataFrame(rows),
                 control_col=1)


def test_preview_returns_the_pyprism_graph(frame, tmp_path):
    graph, publication_path = draw_preview(frame, tmp_path)
    assert publication_path is True
    assert graph is not None and graph.exists()
    assert graph.name == "spotting_Set01_GLU_20_Hours.png"
    assert (tmp_path / "GLU" / CSV_NAME).exists()
    assert (tmp_path / "GLU" / "figures" /
            "spotting_paired_ttests.csv").exists()


def test_corrections_change_the_graph(frame, tmp_path):
    graph, _ = draw_preview(frame, tmp_path)
    before = graph.read_bytes()
    frame.tidy.loc[(frame.tidy["replicate"] == "rep1") &
                   (frame.tidy["strain_col"] == 2), "excluded"] = True
    graph, _ = draw_preview(frame, tmp_path)
    assert graph.read_bytes() != before


def test_previous_graph_is_cleared_before_a_failed_redraw(frame, monkeypatch,
                                                          tmp_path):
    import spotting_batch as sb

    graph, _ = draw_preview(frame, tmp_path)
    assert graph.exists()
    monkeypatch.setattr(sb, "run_plots", lambda *_args, **_kwargs: None)
    got, ok = draw_preview(frame, tmp_path)
    assert got is None and not ok
    assert not graph.exists()
    assert (tmp_path / "GLU" / "plot_error.log").exists()


def test_empty_frame_draws_nothing(frame, tmp_path):
    frame.tidy["excluded"] = True
    graph, ok = draw_preview(frame, tmp_path)
    assert graph is None and not ok


def test_preview_stays_in_scratch(frame, tmp_path):
    draw_preview(frame, tmp_path / "scratch")
    assert not (tmp_path / "chosen").exists()
    assert not (tmp_path / "best").exists()


def test_preview_can_keep_the_original_highlighted_spot_panel(frame, tmp_path):
    from PIL import Image

    original = tmp_path / "original.png"
    source = Image.new("RGB", (1000, 600), (30, 60, 90))
    # A conspicuous right panel makes it clear that this is replaced while the
    # original left-side spot panel is retained byte-for-byte.
    for x in range(475, 1000):
        for y in range(63, 579):
            source.putpixel((x, y), (220, 20, 20))
    source.save(original)

    combined, ok = draw_preview(frame, tmp_path / "scratch",
                                original_sheet=original)
    assert ok and combined.name.startswith("review_")
    with Image.open(combined) as image:
        assert image.size == source.size
        assert image.getpixel((100, 300)) == (30, 60, 90)
        assert image.getpixel((750, 300)) != (220, 20, 20)


def test_clearing_an_automatic_outlier_restores_it_to_the_redraw(frame,
                                                                 tmp_path):
    import pandas as pd

    mutant = frame.tidy["strain_col"] == 2
    frame.tidy.loc[mutant, "relative_growth"] = [6.0, 1.1, 1.2, 1.3]
    frame.tidy.loc[mutant, "outlier"] = [True, False, False, False]
    draw_preview(frame, tmp_path)
    stats_path = (tmp_path / "GLU" / "figures" /
                  "spotting_paired_ttests.csv")
    assert int(pd.read_csv(stats_path).iloc[0]["n"]) == 3

    # This is the state rebuild._flag_outliers writes for a manual override.
    # `cleared` is the durable review decision.  It is authoritative even at
    # the CSV boundary if an older/stale boolean still says True.
    frame.tidy.loc[mutant, "outlier"] = [True, False, False, False]
    frame.tidy.loc[mutant, "outlier_source"] = ["cleared", "", "", ""]
    draw_preview(frame, tmp_path)
    assert int(pd.read_csv(stats_path).iloc[0]["n"]) == 4
