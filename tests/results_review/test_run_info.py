"""Reading the `experiment.json` an experiment-layer run leaves behind.

The whole point of this file is that it is ADDITIVE. A results folder made by
the console pipeline has none of it and must keep opening exactly as before, and
a malformed one must degrade rather than make a reviewable folder unopenable --
so most of these tests are about what happens when it is absent or wrong.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from results_review import discovery, links, rebuild
from results_review.model import Candidate

CANDIDATES = (
    "medium,medium_label,timepoint,hours,plate1,plate2,dilution,control_n,"
    "median_CV,control_mean,control_CV,n_strains,n_significant,rank_score,"
    "ranked_by,best_set_score\n"
    "GLU,Glucose,16 Hours,16.0,_9.JPG,_9_1.JPG,least,4,0.05,10.7,0.34,7,7,1.75,"
    "combined,4.72\n"
)

HANDOFF = {
    "schema_version": 1,
    "kind": "spotting_experiment_run",
    "written": "2026-09-13T10:00:00",
    "experiment": {"name": "Set09"},
    "strains": [None, "WT BY", "ΔMCR1", "ΔNQM1", None],
    "controls": {"GLU": 2, "K-OAc": 3},
    "pipeline_config": {
        "strains": [None, "WT BY", "ΔMCR1", "ΔNQM1", None],
        "control_col": 2,
        "exclude": [],
        "media": {"GLU": {"control_col": 2, "exclude": []}},
    },
    "photo_root": "",
    "photos": [],
    "cache_key_version": "v23",
}


@pytest.fixture
def results(tmp_path):
    d = tmp_path / "Set09"
    d.mkdir()
    (d / "timecourse_candidates.csv").write_text(CANDIDATES, encoding="utf-8")
    return d


def write_handoff(results, **changes):
    payload = dict(HANDOFF)
    payload.update(changes)
    (results / "experiment.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


# --- absent, the ordinary case ---------------------------------------------


def test_a_folder_without_one_still_loads(results):
    run = discovery.load_set(results)
    assert run.experiment is None
    assert len(run.candidates) == 1


def test_reading_a_missing_file_is_none_not_an_error(results):
    assert discovery.load_run_info(results) is None


# --- present ----------------------------------------------------------------


def test_the_strain_panel_is_read_by_column(results):
    write_handoff(results)
    info = discovery.load_set(results).experiment
    assert info.strain(2) == "WT BY"
    assert info.strain(3) == "ΔMCR1"
    assert info.strain(1) is None      # an empty column
    assert info.strain(99) is None


def test_the_per_medium_control_is_read(results):
    write_handoff(results)
    info = discovery.load_run_info(results)
    assert info.control_for("GLU") == 2
    assert info.control_for("K-OAc") == 3
    assert info.control_for("GLY") is None


def test_the_run_metadata_is_read(results):
    write_handoff(results)
    info = discovery.load_run_info(results)
    assert info.name == "Set09"
    assert info.written.startswith("2026-09-13")
    assert info.cache_key_version == "v23"


def test_the_pipeline_config_comes_back_usable(results):
    write_handoff(results)
    cfg = discovery.load_run_info(results).pipeline_config
    assert cfg["control_col"] == 2
    assert cfg["media"]["GLU"]["control_col"] == 2


# --- degrading rather than failing ------------------------------------------


@pytest.mark.parametrize(
    "text", ["{not json", "[]", '"a string"', "null", ""]
)
def test_a_malformed_file_never_stops_the_folder_opening(results, text):
    (results / "experiment.json").write_text(text, encoding="utf-8")
    run = discovery.load_set(results)
    assert run.experiment is None
    assert len(run.candidates) == 1


def test_unexpected_field_types_are_dropped_not_trusted(results):
    write_handoff(results, strains=["ok", 7, None, {"x": 1}],
                  controls={"GLU": "two"})
    info = discovery.load_run_info(results)
    assert info.strains == {1: "ok"}
    assert info.controls == {}


def test_a_config_without_strains_is_not_offered(results):
    write_handoff(results, pipeline_config={"control_col": 2})
    assert discovery.load_run_info(results).pipeline_config == {}


def test_unknown_keys_from_a_newer_writer_are_ignored(results):
    write_handoff(results, something_new={"a": 1})
    assert discovery.load_run_info(results).name == "Set09"


# --- the photo link ---------------------------------------------------------


def test_the_recorded_photo_folder_is_used(results, tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    write_handoff(results, photo_root=str(photos))
    assert links.from_run_info(results) == photos
    assert links.resolve("Set09", "", results) == photos


def test_a_recorded_folder_that_has_moved_falls_back_to_guessing(results, tmp_path):
    write_handoff(results, photo_root=str(tmp_path / "gone"))
    assert links.from_run_info(results) is None


def test_the_reviews_own_record_still_wins(results, tmp_path):
    """It travels with the results, so it is the more authoritative statement."""
    recorded = tmp_path / "recorded"
    (recorded / "40 Hours" / "Glucose" / "Plate 1").mkdir(parents=True)
    stored = tmp_path / "stored"
    (stored / "40 Hours" / "Glucose" / "Plate 1").mkdir(parents=True)
    for d in (recorded, stored):
        (d / "40 Hours" / "Glucose" / "Plate 1" / "_9.JPG").write_bytes(b"x")

    write_handoff(results, photo_root=str(recorded))
    assert links.resolve("Set09", str(stored), results) == stored


def test_a_recorded_layout_orders_the_designs_own_level_names(results):
    """Names like "1:10" carry no order; the run's recorded layout supplies it."""
    names = ["neat", "1:10", "1:100", "1:1000"]
    csv = CANDIDATES.splitlines()[0] + "\n" + "\n".join(
        CANDIDATES.splitlines()[1].replace(",least,", f",{n},") for n in names) + "\n"
    (results / "timecourse_candidates.csv").write_text(csv, encoding="utf-8")
    write_handoff(results, dilution_layout={
        "n_rows": 6, "n_cols": 8,
        "levels": [{"name": n, "plates": {}} for n in reversed(names)]})
    run = discovery.load_set(results)
    ranks = {c.dilution: c.dilution_rank for c in run.candidates}
    assert ranks == {"1:1000": 0, "1:100": 1, "1:10": 2, "neat": 3}


def test_sheet_names_pad_past_ten_levels_like_the_pipeline(results):
    names = [f"level {i}" for i in range(1, 13)]
    write_handoff(results, dilution_layout={
        "levels": [{"name": n, "plates": {}} for n in names]})
    (results / "timecourse_candidates.csv").write_text(
        CANDIDATES.replace(",least,", ",level 11,"), encoding="utf-8")
    cand = discovery.load_set(results).candidates[0]
    assert discovery.sort_prefix(cand) == "0016.0h_d10_"


def test_without_a_recorded_layout_the_classic_order_stands(results):
    cand = discovery.load_set(results).candidates[0]
    assert cand.level_index == -1 and cand.dilution_rank == 0
    assert discovery.sort_prefix(cand) == "0016.0h_d0_"


def test_resolve_without_a_results_dir_behaves_as_before(results):
    assert links.resolve("Set09-nonexistent", "") is None


def test_rebuild_matches_a_declared_condition_to_its_folder_label(monkeypatch,
                                                                  tmp_path):
    """Experiment condition names must survive the legacy 8-char scanner code."""
    photo = tmp_path / "24 Hours" / "Glucose 37" / "Plate 1" / "_9_1.JPG"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"photo")
    shot = SimpleNamespace(path=photo, plate=1)
    found = {
        "medium": "GLUCOSE3", "medium_label": "Glucose 37",
        "tp_label": "24 Hours", "plates": (shot,), "plate1": shot,
    }
    requested = []

    def candidates(_root, plates=(1, 2)):
        requested.append(tuple(plates))
        return [found]

    monkeypatch.setattr(rebuild, "tree_candidates", candidates)
    cand = Candidate(
        medium="Glucose 37", medium_label="Glucose 37",
        timepoint="24 Hours", hours=24.0, plate1="_9_1.JPG", plate2="",
        dilution="level 1",
    )

    matched = rebuild.pipeline_candidate(Path(tmp_path), cand)

    assert matched["plates"] == (shot,)
    assert matched["medium"] == "Glucose 37"
    assert matched["medium_label"] == "Glucose 37"
    assert requested == [(1,)]
