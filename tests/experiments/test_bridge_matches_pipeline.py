"""The bridge must keep agreeing with the pipeline it drives.

`experiments/run.py` hands the pipeline hand-built records and a hand-built
argument namespace instead of going through `discover()` and `argparse`. That is
deliberate -- it is what lets any folder layout be read -- but it means the two
can drift apart silently. A renamed `Shot` field or a new flag read inside
`run_one` would not raise here; it would produce a differently-configured run.

So the agreement is asserted, in the same spirit as
`tests/results_review/test_matches_pipeline.py`.

These tests need the pipeline importable (numpy, pandas, scipy); they skip
rather than fail where it is not.
"""

import sys
from argparse import Namespace
from dataclasses import fields
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

pytest.importorskip("numpy")
pytest.importorskip("pandas")
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

tc = pytest.importorskip("spotting_timecourse")
sb = pytest.importorskip("spotting_batch")

from experiments import intake, run  # noqa: E402
from experiments.model import Condition, Experiment, TIMECOURSE  # noqa: E402
from experiments.profiles import capture_tree  # noqa: E402


def experiment(tmp_path) -> Experiment:
    return Experiment(
        name="Set09",
        strains=[None, "WT BY", "ΔMCR1", "ΔNQM1", "ΔCTT1", "ΔUBP2", "ΔNTG1", None],
        control_slot=2,
        conditions=[
            Condition("GLU", "Glucose"),
            Condition("K-OAc", "Potassium Acetate", control_slot=3, exclude=(5,)),
        ],
        mode=TIMECOURSE,
        photo_root=str(tmp_path),
        profile=capture_tree(),
    )


def photos(tmp_path):
    for hours in (16, 40):
        for medium in ("Glucose", "Potassium Acetate"):
            for plate in (1, 2):
                d = tmp_path / f"{hours} Hours" / medium / f"Plate {plate} (Rep 1+2)"
                d.mkdir(parents=True)
                (d / "_9.JPG").write_bytes(b"x")
    files, _ = intake.scan_images(tmp_path)
    return files


# --- the argument namespace -------------------------------------------------


def test_the_bridge_supplies_every_argument_run_one_reads():
    """A new flag read inside run_one must be added to RUN_ONE_ARGS."""
    import inspect
    import re

    source = inspect.getsource(tc.run_one)
    needed = set(re.findall(r"args\.([a-zA-Z_]+)", source))
    missing = needed - set(run.RUN_ONE_ARGS)
    assert not missing, (
        f"run_one reads {sorted(missing)}, which experiments/run.py does not "
        f"supply; add them to RUN_ONE_ARGS with the pipeline's own defaults"
    )


def test_the_bridge_defaults_match_the_pipelines_own():
    """An experiment run and run_timecourse.bat must configure the run alike."""
    parser_defaults = {}
    for action in tc.build_parser()._actions if hasattr(tc, "build_parser") else []:
        parser_defaults[action.dest] = action.default

    if not parser_defaults:  # the parser is built inside main()
        import inspect
        import re

        source = inspect.getsource(tc)
        for dest, default in [("rank_by", '"combined"'), ("figures", '"all"'),
                              ("workers", "0")]:
            assert re.search(rf'default={re.escape(default)}', source), (
                f"could not confirm the pipeline default for {dest}"
            )
        assert run.RUN_ONE_ARGS["rank_by"] == "combined"
        assert run.RUN_ONE_ARGS["figures"] == "all"
        assert run.RUN_ONE_ARGS["workers"] == 0
        return

    for key, value in run.RUN_ONE_ARGS.items():
        if key in parser_defaults and key != "out":
            assert value == parser_defaults[key], (
                f"{key}: bridge uses {value!r}, the pipeline defaults to "
                f"{parser_defaults[key]!r}"
            )


def test_estimate_and_workers_reach_the_namespace(tmp_path):
    args = run._timecourse_args(tmp_path, estimate=True, workers=3)
    assert args.out == tmp_path and args.estimate is True and args.workers == 3


def test_an_unset_override_does_not_clobber_the_default(tmp_path):
    args = run._timecourse_args(tmp_path, workers=None)
    assert args.workers == run.RUN_ONE_ARGS["workers"]


# --- the records ------------------------------------------------------------


def test_shots_carry_every_field_the_pipeline_declares(tmp_path):
    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    shots = run.to_shots(e, res)
    assert shots
    declared = {f.name for f in fields(tc.Shot)}
    assert declared == {"path", "tp_hours", "tp_label", "medium", "medium_label",
                        "plate"}
    for shot in shots:
        for name in declared:
            assert getattr(shot, name) is not None


def test_shots_feed_straight_into_the_pipelines_own_candidate_builder(tmp_path):
    """The real check: the pipeline pairs our records without adaptation."""
    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    cands = tc.candidates(run.to_shots(e, res))
    assert len(cands) == 4  # 2 media x 2 timepoints, one pairing each
    for c in cands:
        assert c["plate1"].plate == 1 and c["plate2"].plate == 2
        assert c["medium"] in {"GLU", "K-OAc"}


def test_photos_of_an_undeclared_condition_are_left_out(tmp_path):
    e = experiment(tmp_path)
    e.conditions = [Condition("GLU", "Glucose")]
    res = intake.resolve(e, photos(tmp_path))
    assert {s.medium for s in run.to_shots(e, res)} == {"GLU"}


def test_the_timepoint_label_is_the_folder_name_the_pipeline_would_use(tmp_path):
    """Result folders and figure files are named from it, so it must match."""
    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    assert {s.tp_label for s in run.to_shots(e, res)} == {"16 Hours", "40 Hours"}


def test_photo_refs_group_the_way_spotting_batch_groups_them(tmp_path):
    e = experiment(tmp_path)
    e.set_key = "9"
    res = intake.resolve(e, photos(tmp_path))
    combos = run.to_photo_refs(e, res)
    assert set(combos) == {"9|GLU", "9|K-OAc"}
    for refs in combos.values():
        assert [r.plate for r in refs] == [1, 1, 2, 2]
        assert all(isinstance(r, sb.PhotoRef) for r in refs)


# --- the config -------------------------------------------------------------


def test_the_config_is_read_by_the_pipelines_own_medium_cfg(tmp_path):
    """`medium_cfg` is the single place the per-condition control is resolved."""
    e = experiment(tmp_path)
    cfg = run.to_pipeline_config(e)
    assert tc.medium_cfg(cfg, "GLU") == (2, [])
    assert tc.medium_cfg(cfg, "K-OAc") == (3, [5])


def test_the_config_looks_like_a_timecourse_config_file(tmp_path):
    cfg = run.to_pipeline_config(experiment(tmp_path))
    assert set(cfg) >= {"strains", "control_col", "exclude", "media"}
    assert cfg["strains"][1] == "WT BY"
    assert cfg["control_col"] == 2


# --- dilution geometry vs the pipeline's hardcoded arithmetic ---------------
#
# `spotting_batch` derives the rows of a dilution level from the lab's own
# design: three levels spotted twice down six rows. `experiments.geometry`
# reads them off the plate template instead, which is what lets a design
# declare any number of levels. For the lab standard the two must agree
# EXACTLY -- otherwise every existing result would shift.


def lab_template():
    from plate_template import presets

    return presets.lab_standard_8x6()


@pytest.mark.parametrize("index", [0, 1, 2])
@pytest.mark.parametrize("plate_id", ["1", "2"])
def test_rows_for_a_level_match_the_hardcoded_table(index, plate_id):
    from experiments import geometry

    classic = sb.DILUTION_ORDER[index]
    # `sb.DILUTIONS` is 0-based; `quant_rows` is 1-based, the conversion the
    # pipeline itself does at spotting_batch.py:275 and :511.
    want = tuple(r + 1 for r in sb.DILUTIONS[classic])
    assert geometry.rows_for(lab_template(), plate_id, index) == want


def test_geometry_rows_are_one_based_like_quant_rows():
    """The engine reads `quant_rows` as 1-based; 0 would silently be dropped.

    `roi_radius_for_rows` filters on `1 <= r <= N_ROWS`, so a 0-based row 0
    vanishes and the ROI is sized from the remaining row alone.
    """
    from experiments import geometry

    rows = geometry.rows_for(lab_template(), "1", 0)
    assert min(rows) >= 1
    assert all(1 <= r <= sq_rows() for r in rows)


def sq_rows() -> int:
    import spotting_quant as sq

    return sq.N_ROWS


def test_the_default_grid_constants_still_agree():
    """Re-declared rather than imported, so assert they have not drifted.

    These are the engine's DEFAULT lattice, not a requirement: `detect_grid`
    takes the grid size as an argument.
    """
    import spotting_quant as sq

    from experiments import geometry

    assert (geometry.DEFAULT_ROWS, geometry.DEFAULT_COLS) == (sq.N_ROWS, sq.N_COLS)


def test_the_template_grid_reaches_the_engine(tmp_path):
    """A 12x16 design must be looked for at a 12x16 pitch, not as a sparse 8x6."""
    from dataclasses import replace

    import spotting_quant as sq

    from experiments import geometry
    from tests.experiments.test_geometry import custom

    t = custom(4, 1, rows=12, cols=16)
    rows, cols = geometry.grid_shape(t)
    opts = replace(sq.MeasureOptions(), n_rows=rows, n_cols=cols)
    assert opts.grid_shape == (12, 16)
    assert sq.expected_pitch(1000.0, *opts.grid_shape) < sq.expected_pitch(1000.0)


def test_the_classic_level_names_still_agree():
    from experiments import geometry

    assert list(geometry.CLASSIC_LABELS) == sb.DILUTION_ORDER


def test_replicate_numbering_matches_the_hardcoded_formula():
    """`(plate - 1) * 2 + 1 + row // 3`, but read off the template."""
    from experiments import geometry

    t = lab_template()
    for plate_id in geometry.plate_ids(t):
        for index, _ in geometry.levels(t):
            for r, _c, placement in geometry.cells_for(t, plate_id, index):
                assert placement.replicate == (int(plate_id) - 1) * 2 + 1 + r // 3


def test_level_labels_match_the_hardcoded_row_mapping():
    """`DILUTION_ORDER[row % 3]`, but read off the template."""
    from experiments import geometry

    t = lab_template()
    for plate_id in geometry.plate_ids(t):
        for index, _ in geometry.levels(t):
            for r, _c, placement in geometry.cells_for(t, plate_id, index):
                assert (geometry.level_label(t, placement.dilution)
                        == sb.DILUTION_ORDER[r % 3])


# --- the handoff ------------------------------------------------------------


def test_the_handoff_records_the_resolved_run(tmp_path):
    import json

    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    out = tmp_path / "Results"
    path = run.write_handoff(e, res, out)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["kind"] == "spotting_experiment_run"
    assert payload["strains"][1] == "WT BY"
    assert payload["controls"] == {"GLU": 2, "K-OAc": 3}
    assert len(payload["photos"]) == 8
    assert payload["photos"][0]["plate"] in (1, 2)


def test_the_handoff_records_the_cache_version_tag(tmp_path):
    """Cache invalidation is manual, so a results folder says what made it."""
    import json

    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    path = run.write_handoff(e, res, tmp_path / "Results")
    tag = json.loads(path.read_text(encoding="utf-8"))["cache_key_version"]
    assert tag.startswith("v") and tag[1:].isdigit()


def test_the_handoff_is_written_atomically(tmp_path):
    e = experiment(tmp_path)
    res = intake.resolve(e, photos(tmp_path))
    out = tmp_path / "Results"
    run.write_handoff(e, res, out)
    assert not list(out.glob("*.tmp"))


def test_intake_image_extensions_match_the_engine():
    """Re-declared rather than imported, so assert they still agree."""
    import spotting_quant as sq

    assert intake.IMAGE_EXTS == sq.IMAGE_EXTS
