"""The flags file: the contract every other tool reads the data review through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_review import flags as ff
from data_review.flags import DataFlags


def test_the_review_sits_beside_its_experiment(tmp_path):
    exp = tmp_path / "Set09.spotexp.json"
    side = ff.sidecar_for(exp)
    assert side == tmp_path / "Set09.datareview.json"
    assert ff.is_flags_file(side) and not ff.is_flags_file(exp)
    assert ff.stem_of(side) == "Set09"
    # Found back from the review alone, by name or by what it records.
    assert ff.experiment_for(side) == exp
    assert ff.experiment_for(side, DataFlags(experiment="Other.spotexp.json")) \
        == tmp_path / "Other.spotexp.json"


def test_a_plain_json_experiment_still_gets_a_review_name(tmp_path):
    assert ff.sidecar_for(tmp_path / "odd.json") == tmp_path / "odd.datareview.json"


def test_round_trip_and_one_spelling_per_photo():
    f = DataFlags(experiment="E.spotexp.json", experiment_id="abc")
    f.set_plate("24 Hours\\GLU\\Plate 1\\a.jpg", "smeared")
    f.set_spot("24 Hours/GLU/Plate 2/a.jpg", 3, 5, "contamination")
    f.set_reviewed("48 Hours/GLU/Plate 1/a.jpg")
    back = DataFlags.from_dict(json.loads(json.dumps(f.to_dict())))
    assert back.to_dict() == f.to_dict()
    # Back slashes are the same photo as forward ones.
    assert back.plate_reason("24 Hours/GLU/Plate 1/a.jpg") == "smeared"
    assert back.spot_reason("24 Hours\\GLU\\Plate 2\\a.jpg", 3, 5) == "contamination"
    assert back.n_plates == 1 and back.n_spots == 1


def test_a_plate_flag_applies_to_every_spot_on_it():
    f = DataFlags()
    f.set_plate("p.jpg", "out of focus")
    f.set_spot("p.jpg", 1, 1, "bubble")
    assert f.reason_for("p.jpg", 1, 1) == "plate: out of focus; bubble"
    assert f.reason_for("p.jpg", 2, 2) == "plate: out of focus"
    assert f.reason_for("other.jpg", 1, 1) == ""


def test_clearing_leaves_nothing_behind():
    f = DataFlags()
    f.set_spot("p.jpg", 2, 3, "bubble")
    f.set_spot("p.jpg", 2, 3, None)
    f.set_plate("p.jpg", "x")
    f.set_plate("p.jpg", "")
    assert f.spots == {} and f.plates == {} and f.is_empty


def test_flagged_photos_count_as_looked_at():
    f = DataFlags()
    f.set_spot("p.jpg", 1, 1, "bubble")
    assert f.is_reviewed("p.jpg") and f.is_flagged("p.jpg")
    assert not f.is_reviewed("q.jpg")


def test_a_missing_file_is_an_empty_review(tmp_path):
    assert ff.load(tmp_path / "none.datareview.json").is_empty


def test_another_kind_of_file_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "x.datareview.json"
    path.write_text(json.dumps({"kind": "spotting_experiment"}))
    with pytest.raises(ValueError):
        ff.load(path)
    path.write_text(json.dumps({"kind": ff.KIND, "version": ff.VERSION + 1}))
    with pytest.raises(ValueError, match="newer"):
        ff.load(path)


def test_malformed_entries_are_dropped_and_the_rest_kept():
    f = DataFlags.from_dict({
        "kind": ff.KIND, "version": 1,
        "plates": {"a.jpg": {"reason": "smeared"}, "b.jpg": {"reason": " "},
                   "c.jpg": "nonsense"},
        "spots": {"a.jpg": [{"row": 1, "col": 2, "reason": "bubble"},
                            {"row": 0, "col": 2, "reason": "zero row"},
                            {"row": "x", "col": 2, "reason": "bad"},
                            {"row": 2, "col": 2}]},
        "reviewed": ["a.jpg", 7]})
    assert set(f.plates) == {"a.jpg"}
    assert f.spots_on("a.jpg") == {(1, 2): f.spots["a.jpg"][(1, 2)]}
    assert f.reviewed == {"a.jpg"}


def test_reading_beside_an_experiment_never_raises(tmp_path):
    exp = tmp_path / "E.spotexp.json"
    assert ff.load_for_experiment(None).is_empty
    assert ff.load_for_experiment(exp).is_empty
    ff.sidecar_for(exp).write_text("{ not json")
    assert ff.load_for_experiment(exp).is_empty


def test_saving_is_atomic_and_leaves_no_scratch(tmp_path):
    path = tmp_path / "deep" / "E.datareview.json"
    f = DataFlags()
    f.set_plate("a.jpg", "smeared")
    assert ff.save(path, f) == path
    assert ff.load(path).plate_reason("a.jpg") == "smeared"
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_a_reason_names_where_it_came_from():
    assert ff.describe("bubble") == "data review: bubble"
    assert ff.describe("") == ""
