"""Scanning, inferring and resolving, against synthesised copies of the real trees.

The layouts here are taken from the actual corpus, including the awkward ones:
a guest session two folders deeper than the canonical layout, lower-cased medium
and plate folders, and the `_9.JPG` / `_9_1.JPG` filenames that carry no
information at all.
"""

import pytest

from experiments import intake
from experiments.intake import ImageFile, check_resolution, infer_profile, resolve
from experiments.model import QUANTIFY, TIMECOURSE, Condition, Experiment
from experiments.profiles import FacetRule, NamingProfile


def files(*relpaths) -> list[ImageFile]:
    return [
        ImageFile(relpath=r, parts=tuple(r.split("/"))) for r in sorted(relpaths)
    ]


def capture_tree_files(sets=("Set01",), media=("Glucose", "Glycerol"),
                       hours=(16, 40), shots=2) -> list[ImageFile]:
    out = []
    for s in sets:
        for h in hours:
            for m in media:
                for plate in (1, 2):
                    for i in range(shots):
                        suffix = "" if i == 0 else f"_{i}"
                        out.append(
                            f"{s}/{h} Hours/{m}/Plate {plate} (Rep 1+2)/_9{suffix}.JPG"
                        )
    return files(*out)


def experiment(profile, mode=TIMECOURSE, **kw) -> Experiment:
    return Experiment(name="T", mode=mode, photo_root="/root", profile=profile, **kw)


# --- scanning ---------------------------------------------------------------


def test_scan_finds_images_at_any_depth_and_keys_them_with_forward_slashes(tmp_path):
    deep = tmp_path / "40 Hours" / "Glucose" / "Plate 1"
    deep.mkdir(parents=True)
    (deep / "_9.JPG").write_bytes(b"x")
    (tmp_path / "loose.png").write_bytes(b"x")
    (deep / "notes.txt").write_bytes(b"x")

    found, complaints = intake.scan_images(tmp_path)
    assert complaints == []
    assert [f.relpath for f in found] == [
        "40 Hours/Glucose/Plate 1/_9.JPG",
        "loose.png",
    ]
    assert found[0].parts == ("40 Hours", "Glucose", "Plate 1", "_9.JPG")


def test_scan_skips_dot_folders(tmp_path):
    hidden = tmp_path / ".spotting_cache"
    hidden.mkdir()
    (hidden / "a.png").write_bytes(b"x")
    found, _ = intake.scan_images(tmp_path)
    assert found == []


def test_scanning_a_non_folder_complains_rather_than_raising(tmp_path):
    found, complaints = intake.scan_images(tmp_path / "nope")
    assert found == []
    assert complaints and "not a folder" in complaints[0]


# --- inference --------------------------------------------------------------


def test_the_canonical_capture_tree_is_recognised_as_the_builtin():
    profile, why = infer_profile(capture_tree_files(), mode=TIMECOURSE)
    assert any("Capture tree" in line for line in why)
    assert intake.coverage(capture_tree_files(), profile, TIMECOURSE) == 1.0


def test_a_flat_folder_is_recognised_as_the_builtin():
    flat = files("1.1GLU.JPG", "1.2GLU.JPG", "2.1GLY.JPG", "2.2GLY.JPG")
    profile, why = infer_profile(flat, mode=QUANTIFY)
    assert any("Flat filenames" in line for line in why)
    assert intake.coverage(flat, profile, QUANTIFY) == 1.0


def test_an_unfamiliar_layout_is_inferred_structurally():
    """Facet order reversed and renamed: nothing a builtin would match."""
    odd = files(
        "Dextrose/Plate 1/tp 12h/img.jpg",
        "Dextrose/Plate 2/tp 12h/img.jpg",
        "Dextrose/Plate 1/tp 36h/img.jpg",
        "Dextrose/Plate 2/tp 36h/img.jpg",
        "Raffinose/Plate 1/tp 12h/img.jpg",
        "Raffinose/Plate 2/tp 12h/img.jpg",
    )
    profile, why = infer_profile(odd, mode=TIMECOURSE)
    assert intake.coverage(odd, profile, TIMECOURSE) == 1.0
    res = resolve(experiment(profile), odd)
    assert len(res.usable()) == len(odd)
    assert sorted(res.conditions()) == ["DEXTROSE", "RAFFINOS"]
    assert sorted({r.timepoint for r in res.usable()}) == [12.0, 36.0]


def test_plate_is_never_guessed_when_no_folder_names_one():
    """The one thing the pipeline refuses to infer, and so does this.

    Getting plate identity wrong silently mislabels biological replicates, so
    an unlabelled layout produces unresolved rows in front of the user rather
    than a confident guess.
    """
    nameless = files(
        "16 Hours/Glucose/A/_9.JPG",
        "16 Hours/Glucose/B/_9.JPG",
        "40 Hours/Glucose/A/_9.JPG",
        "40 Hours/Glucose/B/_9.JPG",
    )
    profile, why = infer_profile(nameless, mode=TIMECOURSE)
    assert profile.rule_for("plate") is None
    assert any("mislabel" in line for line in why)
    res = resolve(experiment(profile), nameless)
    assert res.usable() == []
    assert all("plate" in r.missing for r in res.unresolved())


def test_a_constant_number_is_not_read_as_a_timecourse():
    same = files(
        "2024/Glucose/Plate 1/_9.JPG",
        "2024/Glucose/Plate 2/_9.JPG",
        "2024/Glycerol/Plate 1/_9.JPG",
        "2024/Glycerol/Plate 2/_9.JPG",
    )
    profile, _ = infer_profile(same, mode=TIMECOURSE)
    assert profile.rule_for("timepoint") is None


def test_the_set_level_is_found_even_when_a_builtin_matches():
    many = capture_tree_files(sets=("Set01", "Set02"))
    profile, why = infer_profile(many, mode=TIMECOURSE)
    assert profile.rule_for("set") is not None
    assert any("names the set" in line for line in why)


def test_inference_reads_the_layout_from_the_commonest_depth():
    mixed = capture_tree_files() + files("stray.JPG")
    profile, why = infer_profile(mixed, mode=TIMECOURSE)
    assert any("deep" in line for line in why)
    res = resolve(experiment(profile), mixed)
    assert [r.relpath for r in res.unresolved()] == ["stray.JPG"]


def test_inference_of_an_empty_folder_says_so():
    profile, why = infer_profile([], mode=TIMECOURSE)
    assert profile.rules == ()
    assert why == ["no images found"]


# --- resolution -------------------------------------------------------------


def test_reshots_are_counted_not_parsed():
    """The filenames encode nothing; which re-shot a photo is follows from count."""
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=3)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    res = resolve(experiment(profile), tree)
    by_key = res.by_key()
    assert len(by_key) == 2  # two plates, one sitting
    for rows in by_key.values():
        assert sorted(r.shot for r in rows) == [1, 2, 3]


def test_an_override_beats_the_profile():
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    target = tree[0].relpath
    e = experiment(profile, overrides={target: {"plate": 2, "timepoint": 99}})
    row = next(r for r in resolve(e, tree).rows if r.relpath == target)
    assert row.plate == 2 and row.timepoint == 99.0


def test_an_override_survives_a_rescan_that_adds_photos():
    first = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(first, mode=TIMECOURSE)
    target = first[0].relpath
    e = experiment(profile, overrides={target: {"plate": 2}})

    later = first + capture_tree_files(media=("Glucose",), hours=(40,), shots=1)
    row = next(r for r in resolve(e, later).rows if r.relpath == target)
    assert row.plate == 2


def test_overrides_from_json_are_coerced_to_the_right_type():
    """Values read back out of a file arrive as strings."""
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    target = tree[0].relpath
    e = experiment(profile, overrides={target: {"plate": "2", "timepoint": "99"}})
    row = next(r for r in resolve(e, tree).rows if r.relpath == target)
    assert row.plate == 2 and row.timepoint == 99.0


def test_an_uncoercible_override_leaves_the_photo_unresolved():
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    target = tree[0].relpath
    e = experiment(profile, overrides={target: {"plate": "left one"}})
    row = next(r for r in resolve(e, tree).rows if r.relpath == target)
    assert row.status == "unresolved" and "plate" in row.missing


def test_ignoring_a_photo_beats_an_override():
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    target = tree[0].relpath
    e = experiment(profile, overrides={target: {"plate": 2}}, ignored=[target])
    row = next(r for r in resolve(e, tree).rows if r.relpath == target)
    assert row.status == "ignored"
    assert target not in [r.relpath for r in resolve(e, tree).usable()]


def test_set_key_selects_one_panel_out_of_a_shared_folder():
    many = capture_tree_files(sets=("Set01", "Set02"), media=("Glucose",),
                              hours=(16,), shots=1)
    profile, _ = infer_profile(many, mode=TIMECOURSE)
    e = experiment(profile, set_key="1")
    res = resolve(e, many)
    assert {r.set_key for r in res.usable()} == {"1"}
    assert {r.status for r in res.rows} == {"ok", "other_set"}


def test_a_sole_condition_is_applied_when_the_path_cannot_name_one():
    """One medium, no medium folder -- the common small case."""
    no_medium = files(
        "16 Hours/Plate 1/_9.JPG",
        "16 Hours/Plate 2/_9.JPG",
        "40 Hours/Plate 1/_9.JPG",
        "40 Hours/Plate 2/_9.JPG",
    )
    profile = NamingProfile(
        name="no condition",
        rules=(
            FacetRule("timepoint", "segment", -3, (r"(\d+)\s*h",), "hours"),
            FacetRule("plate", "segment", -2, (r"plate\s*(\d+)",), "int"),
        ),
    )
    e = experiment(profile, conditions=[Condition("GLU", "Glucose")])
    res = resolve(e, no_medium)
    assert len(res.usable()) == 4
    assert res.conditions() == ["GLU"]
    assert {r.condition_label for r in res.usable()} == {"Glucose"}


def test_a_sole_condition_is_not_applied_when_the_path_does_name_one():
    tree = capture_tree_files(media=("Glycerol",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = experiment(profile, conditions=[Condition("GLU", "Glucose")])
    assert resolve(e, tree).conditions() == ["GLY"]


def test_quantify_mode_does_not_require_a_timepoint():
    flat = files("1.1GLU.JPG", "1.2GLU.JPG")
    profile, _ = infer_profile(flat, mode=QUANTIFY)
    res = resolve(experiment(profile, mode=QUANTIFY), flat)
    assert len(res.usable()) == 2
    assert all(r.timepoint is None for r in res.usable())


# --- checking ---------------------------------------------------------------


def base_experiment(profile, **kw):
    return Experiment(
        name="T",
        mode=TIMECOURSE,
        photo_root="/root",
        profile=profile,
        strains=["WT BY", "ΔATX1"],
        control_slot=1,
        conditions=[Condition("GLU", "Glucose")],
        **kw,
    )


def test_photos_spanning_several_sets_are_an_error():
    many = capture_tree_files(sets=("Set01", "Set02"), media=("Glucose",),
                              hours=(16,), shots=1)
    profile, _ = infer_profile(many, mode=TIMECOURSE)
    e = base_experiment(profile)
    issues = check_resolution(e, resolve(e, many))
    bad = [i for i in issues if i.code == "multiple_sets"]
    assert bad and bad[0].is_error
    assert "never be pooled" in bad[0].message


def test_choosing_a_set_clears_the_error():
    many = capture_tree_files(sets=("Set01", "Set02"), media=("Glucose",),
                              hours=(16,), shots=1)
    profile, _ = infer_profile(many, mode=TIMECOURSE)
    e = base_experiment(profile, set_key="1")
    issues = check_resolution(e, resolve(e, many))
    assert not [i for i in issues if i.code == "multiple_sets"]


def test_sets_are_listed_in_reading_order():
    many = capture_tree_files(
        sets=tuple(f"Set{n:02d}" for n in range(1, 11)),
        media=("Glucose",), hours=(16,), shots=1,
    )
    profile, _ = infer_profile(many, mode=TIMECOURSE)
    e = base_experiment(profile)
    msg = next(
        i for i in check_resolution(e, resolve(e, many)) if i.code == "multiple_sets"
    ).message
    assert "1, 2, 3" in msg and "9, 10" in msg


def test_a_sitting_missing_a_plate_is_reported_and_skipped():
    tree = files(
        "16 Hours/Glucose/Plate 1/_9.JPG",
        "16 Hours/Glucose/Plate 2/_9.JPG",
        "40 Hours/Glucose/Plate 1/_9.JPG",  # no plate 2
    )
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = base_experiment(profile)
    issues = check_resolution(e, resolve(e, tree))
    incomplete = [i for i in issues if i.code == "incomplete_group"]
    assert incomplete and "no plate 2" in incomplete[0].message


def test_nothing_complete_is_an_error_not_a_warning():
    tree = files(
        "16 Hours/Glucose/Plate 1/_9.JPG",
        "40 Hours/Glucose/Plate 2/_9.JPG",
    )
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = base_experiment(profile)
    issues = check_resolution(e, resolve(e, tree))
    fatal = [i for i in issues if i.code == "no_complete_group"]
    assert fatal and fatal[0].is_error


def test_nothing_resolved_is_an_error():
    e = base_experiment(NamingProfile(name="empty"))
    res = resolve(e, files("a/b/c.JPG"))
    issues = check_resolution(e, res)
    assert issues[0].code == "no_photos_resolved" and issues[0].is_error


def test_a_declared_condition_with_no_photos_is_reported():
    tree = capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = base_experiment(profile)
    e.conditions.append(Condition("K-OAc", "Potassium Acetate"))
    codes = {i.code for i in check_resolution(e, resolve(e, tree))}
    assert "condition_without_photos" in codes


def test_conditions_found_in_a_brand_new_experiment_are_announced_once():
    tree = capture_tree_files(media=("Glucose", "Glycerol"), hours=(16,), shots=1)
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = base_experiment(profile)
    e.conditions.clear()
    issues = check_resolution(e, resolve(e, tree))
    found = [i for i in issues if i.code == "conditions_found"]
    assert len(found) == 1 and "GLU, GLY" in found[0].message
    assert not [i for i in issues if i.code == "undeclared_condition"]


def test_cloud_placeholders_are_reported_rather_than_read():
    tree = [
        ImageFile(f.relpath, f.parts, cloud_only=True)
        for f in capture_tree_files(media=("Glucose",), hours=(16,), shots=1)
    ]
    profile, _ = infer_profile(tree, mode=TIMECOURSE)
    e = base_experiment(profile)
    codes = {i.code for i in check_resolution(e, resolve(e, tree))}
    assert "photos_not_downloaded" in codes
