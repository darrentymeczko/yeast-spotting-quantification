"""Multiple panels must never share a strain map, control or candidate pool."""

from pathlib import Path

import pytest

from experiments import intake, schema
from experiments.gui.controller import ExperimentController
from experiments.model import Condition, Experiment, StrainGroup, QUANTIFY
from experiments.profiles import FacetRule, NamingProfile, capture_tree
from experiments.validate import validate
from plate_template.schema import load


@pytest.fixture
def grouped(tmp_path):
    for group in ("A", "B"):
        for plate in (1, 2):
            path = tmp_path / group / "16 Hours" / "Glucose" / f"Plate {plate}" / "photo.jpg"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"fixture")
    template = load(Path("plate_template/templates/lab_standard_8x6.json"))
    profile = capture_tree()
    profile = NamingProfile("Grouped", (*profile.rules,
                            FacetRule("set", "segment", 0, (r"^(.+)$",), "raw")), profile.aliases)
    e = Experiment(name="Two panels", photo_root=str(tmp_path),
                   template_id=template.id, conditions=[Condition("GLU", "Glucose")],
                   profile=profile, strain_groups={
                       "A": StrainGroup([f"A{i}" for i in range(8)], 1),
                       "B": StrainGroup([f"B{i}" for i in range(8)], 2),
                   })
    files, _ = intake.scan_images(tmp_path)
    ctl = ExperimentController(e)
    ctl.template, ctl.files = template, files
    ctl.reresolve()
    return ctl


def test_group_roundtrip_and_old_files(grouped):
    ctl = grouped
    ctl.select_group("B")
    ctl.set_condition_control("GLU", 3)
    ctl.set_condition_exclude("GLU", [4])
    ctl.set_pick("GLU", "1", ctl.candidates()[0])
    ctl.set_plate_dilution("GLU", "1", 2)
    e = schema.loads_experiment(schema.dumps_experiment(ctl.experiment))
    assert e.for_group("B").control_for("GLU") == 3
    assert e.for_group("B").exclude_for("GLU") == (4,)
    assert e.for_group("B").dilution_for("GLU", "1") == 2
    assert e.for_group("A").control_for("GLU") == 1
    assert not e.for_group("A").picks
    assert schema.from_dict(schema.to_dict(Experiment())).strain_groups == {}
    old = schema.to_dict(Experiment(strains=["legacy WT"]))
    old["schema_version"] = 1
    assert schema.from_dict(old).strains == ["legacy WT"]
    assert schema.to_dict(e)["schema_version"] >= 2


def test_add_group_preserves_first_and_starts_second_blank():
    ctl = ExperimentController(Experiment(strains=["WT", "Mutant"], control_slot=1,
                                          conditions=[Condition("GLU", control_slot=2)]))
    ctl.add_strain_group("Second", first_key="First")
    assert ctl.experiment.for_group("First").strains == ["WT", "Mutant"]
    assert ctl.panel_experiment.strains == [None, None]
    assert ctl.panel_experiment.control_for("GLU") is None
    ctl.set_strain(1, "Other WT")
    ctl.set_control(1)
    assert ctl.experiment.for_group("First").strain(1) == "WT"
    ctl.undo()
    assert ctl.panel_experiment.control_slot is None
    ctl.redo()
    assert ctl.panel_experiment.control_slot == 1
    restored = schema.from_dict(ctl.snapshot())
    assert restored.for_group("Second").strain(1) == "Other WT"


def test_undo_group_creation_and_active_selection_are_safe():
    ctl = ExperimentController(Experiment(strains=["WT"]))
    ctl.add_strain_group("B", first_key="A")
    ctl.undo()
    assert ctl.panel_experiment is ctl.experiment
    assert ctl.active_group is None
    ctl.redo()
    assert ctl.panel_experiment.set_key == "A"


@pytest.mark.parametrize("name", ["", "A", "bad|name", "bad\nname"])
def test_invalid_group_names_cannot_partially_mutate(name):
    ctl = ExperimentController(Experiment(strains=["WT"]))
    before = ctl.snapshot()
    with pytest.raises(ValueError):
        ctl.add_strain_group(name, first_key="A")
    assert ctl.snapshot() == before


def test_validation_and_photo_assignment_are_group_specific(grouped):
    ctl = grouped
    assert not [i for i in validate(ctl.experiment, ctl.template) if i.is_error]
    assert not [i for i in intake.check_resolution(ctl.experiment, ctl.resolution, ctl.template) if i.is_error]
    path = ctl.files[0].relpath
    ctl.override(path, "set", "Unknown")
    assert any(i.code == "unassigned_strain_group" for i in ctl.issues())
    ctl.override(path, "set", None)
    ctl.experiment.profile.rules = tuple(r for r in ctl.experiment.profile.rules if r.facet != "set")
    ctl.reresolve()
    assert all("set" in r.missing for r in ctl.resolution.rows)
    ctl.override_many([f.relpath for f in ctl.files], "set", "A")
    assert all(r.set_key == "A" for r in ctl.resolution.rows)
    assert any(i.code == "no_photos_resolved" and "'B'" in i.message for i in ctl.issues())


def test_group_picks_and_dilutions_never_cross_panels(grouped):
    ctl = grouped
    ctl.set_mode(QUANTIFY)
    for key, level in (("A", 0), ("B", 2)):
        ctl.select_group(key)
        for row in ctl.resolution.rows:
            if row.set_key == key:
                ctl.set_pick("GLU", str(row.plate), row.relpath)
                ctl.set_plate_dilution("GLU", str(row.plate), level)
    assert ctl.experiment.for_group("A").dilution_for("GLU", "1") == 0
    assert ctl.experiment.for_group("B").dilution_for("GLU", "1") == 2
    assert not [i for i in ctl.issues() if i.is_error]
    with pytest.raises(ValueError, match="selected strain group"):
        ctl.set_pick("GLU", "1", ctl.experiment.for_group("A").pick("GLU", "1"))
    ctl.experiment.strain_groups["B"].picks["GLU"]["1"] = ctl.experiment.for_group("A").pick("GLU", "1")
    assert any(i.code == "wrong_group_pick" for i in ctl.issues())


def test_every_group_is_validated_even_when_another_is_selected(grouped):
    ctl = grouped
    ctl.select_group("A")
    ctl.experiment.strain_groups["B"].control_slot = None
    assert any(i.is_error and "'B'" in i.message and "control" in i.message for i in ctl.issues())


def test_timecourse_splits_before_scoring_and_review_loads_each_panel(grouped, tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from experiments import run
    from results_review import discovery, rebuild
    from tests.results_review.test_run_info import CANDIDATES
    _, tc = run._pipeline()
    seen = []

    def score(tree, args, cfg, **kwargs):
        shots = kwargs["shots"]
        assert len(shots) == 2
        assert all(s.path.relative_to(Path(grouped.experiment.photo_root)).parts[0] == tree.set_hint for s in shots)
        seen.append((tree.set_hint, cfg["strains"], cfg["media"]["GLU"]["control_col"]))
        args.out.mkdir(parents=True)
        (args.out / discovery.CANDIDATES_CSV).write_text(
            CANDIDATES.replace("_9.JPG,_9_1.JPG", "photo.jpg,photo.jpg"), encoding="utf-8")
        return 0

    monkeypatch.setattr(tc, "run_one", score)
    monkeypatch.setattr(run, "_configure_timecourse_workers", lambda *args: None)
    output = tmp_path / "results"
    assert run.run(grouped.experiment, outdir=output, res=grouped.resolution, template=grouped.template) == 0
    assert seen == [("A", [f"A{i}" for i in range(8)], 1), ("B", [f"B{i}" for i in range(8)], 2)]
    paths = discovery.list_sets(tmp_path)
    assert len(paths) == 2
    reviews = {discovery.load_set(p).label: discovery.load_set(p) for p in paths}
    for key, control in (("A", 1), ("B", 2)):
        info = reviews[f"Two panels - {key}"].experiment
        assert info.strain(1) == f"{key}0"
        assert info.control_for("GLU") == control
        assert info.pipeline_config["from_set"] == key
        assert info.pipeline_config["media"]["GLU"]["control_col"] == control
        rebuilt = rebuild.pipeline_candidate(Path(grouped.experiment.photo_root),
                                              reviews[f"Two panels - {key}"].candidates[0],
                                              info.pipeline_config)
        assert all(s.path.relative_to(tmp_path).parts[0] == key for s in rebuilt["plates"])


def test_quantified_rows_use_the_right_panel_and_control(grouped):
    pytest.importorskip("numpy")
    from experiments import run
    from tests.experiments.test_many_levels import FakePlate
    for key, control in (("A", 1), ("B", 2)):
        e = grouped.experiment.for_group(key)
        e.set_dilution_for("GLU", "1", 1)
        e.set_dilution_for("GLU", "2", 1)
        tidy = run.build_tidy(e, grouped.template, "GLU", [("1", FakePlate(1)), ("2", FakePlate(2))])
        assert set(tidy["strain"]) == {f"{key}{i}" for i in range(8)}
        assert set(tidy["set"]) == {key}
        assert set(tidy.loc[tidy.is_control, "strain_col"]) == {control}
        assert tidy.loc[tidy.is_control, "relative_growth"].mean() == pytest.approx(1)


def test_missing_assignments_block_direct_run_even_without_gui(grouped, monkeypatch, tmp_path):
    from experiments import run
    grouped.override(grouped.files[0].relpath, "set", "Not defined")
    monkeypatch.setattr(run, "_pipeline", lambda: pytest.fail("must reject before scoring"))
    with pytest.raises(run.RunError, match="no defined strain group"):
        run.run(grouped.experiment, outdir=tmp_path / "results", res=grouped.resolution, template=grouped.template)


def test_gui_switches_panel_conditions_and_plate_picks(app_tk_root, grouped):
    import tkinter as tk
    from experiments.app import ExperimentApp
    win = tk.Toplevel(app_tk_root)
    win.withdraw()
    try:
        app = ExperimentApp(win, grouped.experiment)
        app.controller.template = grouped.template
        app.controller.files = grouped.files
        app.controller.reresolve()
        app.controller.select_group("B")
        app.refresh()
        assert app.panel._rows[0][1].get() == "B0"
        assert app.panel.control.get() == 2
        assert app.conditions.group_selector.selected.get() == "B"
        assert app.plates.group_selector.selected.get() == "B"
        assert all(path.startswith("B/") for path in app.controller.candidates())
        app.controller.select_group("A")
        app.refresh()
        assert app.panel._rows[0][1].get() == "A0"
        assert "All strain groups" in app.run_details.cget("text")
    finally:
        win.destroy()


def test_manual_multiple_groups_choice_creates_panels_and_assigns_photos(app_tk_root, tmp_path, monkeypatch):
    import tkinter as tk
    from experiments.app import ExperimentApp
    from experiments.gui import groups, data
    from experiments.gui.data import _MULTIPLE_GROUPS
    (tmp_path / "first.jpg").write_bytes(b"x")
    (tmp_path / "second.jpg").write_bytes(b"x")
    e = Experiment(photo_root=str(tmp_path), strains=["WT", "Mutant"], control_slot=1)
    win = tk.Toplevel(app_tk_root)
    win.withdraw()
    try:
        app = ExperimentApp(win, e)
        replies = iter(["First panel", "Second panel"])
        monkeypatch.setattr(groups, "_ask_text", lambda *a, **kw: next(replies))
        app.data.simple_rules["set"]["selected"].set(_MULTIPLE_GROUPS)
        app.data._apply_choices()
        assert list(app.controller.experiment.strain_groups) == ["First panel", "Second panel"]
        assert app.panel._rows[0][1].get() == ""
        assert app.data.simple_rules["set"]["selected"].get() == _MULTIPLE_GROUPS
        monkeypatch.setattr(data, "_ask_text", lambda *a, **kw: "Second panel")
        app.data.table.selection_set("second.jpg")
        app.data._correct("set")
        assert app.controller.experiment.overrides["second.jpg"]["set"] == "Second panel"
        app.controller.rescan()
        assert next(r for r in app.controller.resolution.rows if r.relpath == "second.jpg").set_key == "Second panel"
    finally:
        win.destroy()


def test_handpicked_run_writes_separate_csvs_with_group_strains(grouped, tmp_path, monkeypatch):
    pd = pytest.importorskip("pandas")
    from experiments import run
    from experiments.model import DATA_OUTPUT
    from results_review.discovery import load_run_info
    from tests.experiments.test_many_levels import FakePlate
    ctl = grouped
    ctl.set_mode(QUANTIFY)
    ctl.set_output(DATA_OUTPUT)
    for key in ("A", "B"):
        ctl.select_group(key)
        for row in ctl.resolution.rows:
            if row.set_key == key:
                ctl.set_pick("GLU", str(row.plate), row.relpath)
                ctl.set_plate_dilution("GLU", str(row.plate), 1)
    sb, _ = run._pipeline()
    monkeypatch.setattr(sb, "PROJECT_ROOT", tmp_path)
    measured = []

    def measure(ref, *_args):
        measured.append((ref.set_id, ref.path.relative_to(tmp_path).parts[0]))
        data = FakePlate(ref.plate)
        data.ref = ref
        return data

    monkeypatch.setattr(sb, "measure", measure)
    output = tmp_path / "results"
    assert run.run(ctl.experiment, outdir=output, res=ctl.resolution, template=ctl.template) == 0
    assert measured == [("A", "A"), ("A", "A"), ("B", "B"), ("B", "B")]
    csvs = list(output.rglob("spotting_results_normalized.csv"))
    assert len(csvs) == 2
    for csv in csvs:
        tidy = pd.read_csv(csv)
        key = tidy["set"].iloc[0]
        assert set(tidy["strain"]) == {f"{key}{i}" for i in range(8)}
        assert load_run_info(csv.parent).strain(1) == f"{key}0"


def test_review_uses_manual_assignments_and_never_falls_back_to_other_groups(grouped, tmp_path, monkeypatch):
    from experiments import run
    from results_review import discovery, rebuild
    from results_review.gui.controller import ReviewController
    from tests.results_review.test_run_info import CANDIDATES
    ctl = grouped
    # Put B's two photos in A through manual corrections, then save A's run.
    ctl.override_many([r.relpath for r in ctl.resolution.rows], "set", "B")
    b_paths = [r.relpath for r in ctl.resolution.rows if r.relpath.startswith("B/")]
    ctl.override_many(b_paths, "set", "A")
    output = tmp_path / "results"
    run.write_handoff(ctl.experiment.for_group("A"), intake.group_resolution(ctl.resolution, "A"), output)
    (output / discovery.CANDIDATES_CSV).write_text(
        CANDIDATES.replace("_9.JPG,_9_1.JPG", "photo.jpg,photo.jpg"), encoding="utf-8")
    review = ReviewController(discovery.load_set(output))
    monkeypatch.setattr(rebuild, "tree_candidates", lambda *_a, **_kw: pytest.fail("must not re-scan names"))
    candidate = review.run.candidates[0]
    got = rebuild.pipeline_candidate(review.capture_root, candidate, review.cfg)
    assert all(s.path.relative_to(tmp_path).parts[0] == "B" for s in got["plates"])
    # Missing saved photos must not select an identically named one from A.
    review.cfg["resolved_photos"][0]["relpath"] = "missing/photo.jpg"
    with pytest.raises(rebuild.RebuildError, match="missing"):
        rebuild.pipeline_candidate(review.capture_root, candidate, review.cfg)


def test_ambiguous_saved_names_are_refused_in_review(grouped, tmp_path):
    from experiments import run
    from results_review import discovery, rebuild
    from tests.results_review.test_run_info import CANDIDATES
    output = tmp_path / "results"
    run.write_handoff(grouped.experiment.for_group("A"), intake.group_resolution(grouped.resolution, "A"), output)
    (output / discovery.CANDIDATES_CSV).write_text(
        CANDIDATES.replace("_9.JPG,_9_1.JPG", "photo.jpg,photo.jpg"), encoding="utf-8")
    review = discovery.load_set(output)
    cfg = review.experiment.pipeline_config
    cfg["resolved_photos"].append(dict(cfg["resolved_photos"][0]))
    with pytest.raises(rebuild.RebuildError, match="exactly one"):
        rebuild.pipeline_candidate(tmp_path, review.candidates[0], cfg)
