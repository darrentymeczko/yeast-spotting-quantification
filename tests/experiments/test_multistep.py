"""Design, statistical independence, GUI persistence and additive run contract."""

from dataclasses import replace
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from experiments import intake, multistep, run, schema
from experiments.model import Condition, Experiment, MultiStepAnalysis
from experiments.gui.controller import ExperimentController
from plate_template.presets import lab_standard_8x6

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import spotting_multistep as analysis


def settings(**kw):
    return MultiStepAnalysis(enabled=True, dilution=0, hours=(24.,), **kw)


def synthetic(repeated=False, seed=83, n=12, technical=3):
    rng = np.random.default_rng(seed)
    hours = (12., 24., 36.) if repeated else (24.,)
    rows = []
    bio = rng.normal(0, .25, n)
    slope = rng.normal(0, .15, n)
    for slot in range((n + 2) // 3):
        for tech in range(technical):
            plate = f"{slot}-{tech}"
            plate_effect = rng.normal(0, .12)
            pair_effects = rng.normal(0, .15, 3)
            for hour in hours:
                plate_time = rng.normal(0, .1)
                for b in range(slot * 3, min((slot + 1) * 3, n)):
                    contrast = (.4 + bio[b] + plate_effect + pair_effects[b % 3]
                                + plate_time + (hour - 24) / 24 * (.3 + slope[b]) + rng.normal(0, .09))
                    control = np.exp(3 + rng.normal(0, .1))
                    for strain, col, value in [("WT", 1, control), ("mutant", 2, control * np.exp(contrast))]:
                        rows.append(dict(condition="GLU", strain=strain, strain_col=col,
                                         replicate=f"rep{b}", physical_plate=plate, hours=hour,
                                         raw_growth=value, detection_limit=.5, is_control=col == 1, qc_reason=""))
    return pd.DataFrame(rows)


def experiment():
    return Experiment(name="Synthetic", strains=["WT", "mutant"], control_slot=1,
                      conditions=[Condition("GLU")], multi_step=settings())


def resolution(hours=(24.,)):
    return intake.Resolution([intake.PhotoRow(
        relpath=f"{h:g}/p{p}-{t}.jpg", condition="GLU", timepoint=h, plate=p,
        status="ok") for h in hours for p in (1, 2) for t in ("A", "B")])


def assign(e, res):
    e.multi_step.plate_ids = {r.relpath: Path(r.relpath).stem.split("-")[-1] for r in res.rows}


def test_settings_roundtrip_legacy_defaults_and_undo():
    e = experiment()
    e.multi_step.plate_ids = {"x.jpg": "B"}
    e.multi_step.excluded_photos = {"y.jpg": "contamination"}
    assert schema.loads_experiment(schema.dumps_experiment(e)).multi_step == e.multi_step
    raw = schema.to_dict(e)
    raw.pop("multi_step")
    raw["schema_version"] = 2
    assert not schema.from_dict(raw).multi_step.enabled
    ctl = ExperimentController(e)
    before = ctl.snapshot()
    assert ctl.set_multi_step(replace(e.multi_step, method="hierarchical"))
    assert ctl.undo_stack.undo(ctl.snapshot())[0] == before


@pytest.mark.parametrize("changes", [{"hours": [float("nan")]}, {"hours": [24, 24]},
    {"dilution": True}, {"method": "bad"}, {"scope": "bad"}, {"enabled": "yes"},
    {"plate_ids": {"a": 1}}, {"excluded_photos": {"a": ""}}])
def test_invalid_settings_rejected(changes):
    with pytest.raises(ValueError):
        multistep.from_dict(changes)


def test_plate_labels_required_and_duplicates_rejected():
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    assert any("Assign technical" in v for v in multistep.design_errors(e, template, res))
    assign(e, res)
    assert not multistep.design_errors(e, template, res)
    e.multi_step.plate_ids[res.rows[1].relpath] = "A"
    assert any("duplicate photo" in v for v in multistep.design_errors(e, template, res))
    e.multi_step.excluded_photos[res.rows[1].relpath] = "duplicate photograph"
    assert not multistep.design_errors(e, template, res)


def test_repeated_labels_must_link_physical_plates_and_times_are_explicit():
    e, res = experiment(), resolution((24., 36.))
    e.multi_step.scope, e.multi_step.hours = "repeated", (24., 36.)
    e.multi_step.plate_ids = {r.relpath: r.relpath for r in res.rows}
    assert any("no physical" in v for v in multistep.design_errors(e, resolution=res))
    assign(e, res)
    assert not multistep.design_errors(e, resolution=res)
    e.multi_step.hours = (24., 48.)
    assert any("no included photo" in v for v in multistep.design_errors(e, resolution=res))


def test_two_step_matches_independent_biological_block_calculation():
    data = synthetic(n=4)
    result = analysis.analyze(data, settings())
    paired = data.pivot(index=["replicate", "physical_plate", "hours"], columns="strain", values="raw_growth")
    means = np.log(paired.mutant / paired.WT).groupby("replicate").mean()
    test = result.tests.iloc[0]
    assert test.biological_n == 4
    assert test.observation_n == 12
    assert test.ratio == pytest.approx(np.exp(means.mean()))
    assert test.p == pytest.approx(stats.ttest_1samp(means, 0).pvalue)
    assert test.df == 3


def test_more_technical_measurements_do_not_inflate_biological_n_or_weight():
    data = synthetic(n=4)
    extra = data[data.replicate == "rep0"].copy()
    extra.physical_plate += "-copy"
    a = analysis.analyze(data, settings()).tests.iloc[0]
    b = analysis.analyze(pd.concat([data, extra]), settings()).tests.iloc[0]
    assert b.biological_n == 4
    assert b.ratio == pytest.approx(a.ratio)
    assert b.p == pytest.approx(a.p)


def test_control_uncertainty_is_preserved_and_technical_failures_are_audited():
    data = synthetic(n=4)
    data.loc[(data.physical_plate == "0-0") & (data.strain == "WT"), "qc_reason"] = "glare"
    data.loc[(data.physical_plate == "0-1") & (data.replicate == "rep1") & (data.strain == "WT"), "raw_growth"] = .01
    result = analysis.analyze(data, settings())
    assert result.contrasts.included.sum() == 8
    assert result.contrasts.qc_reason.str.contains("glare").sum() == 3
    assert result.contrasts.qc_reason.str.contains("quantification limit").sum() == 1
    assert result.tests.biological_n.iloc[0] == 4
    assert len(result.observations) == len(data)


def test_censored_growth_is_not_treated_as_precise_or_silently_dropped():
    data = synthetic(n=4)
    data.loc[1, "raw_growth"] = 0
    result = analysis.analyze(data, settings())
    assert result.contrasts.included.all()
    assert result.contrasts.censored.sum() == 1
    assert result.tests.p.isna().all()
    assert result.diagnostics[0]["status"] == "descriptive only"


@pytest.mark.parametrize("method,scope", [("two_step", "technical"), ("hierarchical", "technical"),
                                          ("two_step", "repeated"), ("hierarchical", "repeated")])
def test_all_four_modes_recover_simulated_effect(method, scope):
    repeated = scope == "repeated"
    config = settings(method=method, scope=scope)
    if repeated:
        config.hours = (12., 24., 36.)
    result = analysis.analyze(synthetic(repeated), config)
    assert result.diagnostics[0]["status"] == "ok", result.diagnostics
    assert result.estimates.ratio.between(1, 2.5).all()
    assert (result.tests.p_holm >= result.tests.p).all()
    assert result.tests.biological_n.eq(12).all()
    if repeated:
        assert "trajectory vs control" in set(result.tests.test)
        assert len(result.estimates) == 3
    if method == "hierarchical":
        assert {"block", "physical_plate"} <= set(result.diagnostics[0]["covariance_terms"])


def test_nonconvergence_withholds_p_values(monkeypatch):
    def fail(*args):
        raise ValueError("Mixed model did not converge")
    monkeypatch.setattr(analysis, "_fit", fail)
    result = analysis.analyze(synthetic(), settings(method="hierarchical"))
    assert result.tests.p.isna().all()
    assert "converge" in result.diagnostics[0]["reason"]


def test_export_contains_separate_graph_and_preserves_previous_results(tmp_path):
    config = settings()
    result = analysis.analyze(synthetic(), config)
    previous = tmp_path / "spotting_results_normalized.csv"
    previous.write_text("original result")
    dest = analysis.write_results(result, tmp_path / "new", config)
    assert list(dest.glob("*-comparison.png"))
    assert list(dest.glob("*-comparison.svg"))
    assert pd.read_csv(dest / "tests.csv").biological_n.eq(12).all()
    assert json.loads((dest / "analysis.json").read_text())["settings"]["enabled"]
    assert previous.read_text() == "original result"
    with pytest.raises(FileExistsError):
        analysis.write_results(result, dest, config)


def test_measurement_bridge_visits_real_plates_once_not_candidate_combinations(monkeypatch):
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    assign(e, res)
    calls = []
    _, tc = run._pipeline()
    def measure(path, plate, rows, cache, layout):
        calls.append(str(path))
        net = np.full((6, 8), 20.)
        net[:, 1] = 10.
        return SimpleNamespace(net=net, rim=np.zeros((6, 8), dtype=bool), bg_samples=np.zeros(20))
    monkeypatch.setattr(tc, "_cached_measure", measure)
    data = run.measure_multi_step(e, res, template)
    assert len(calls) == 4
    assert data.physical_plate.nunique() == 4
    assert data.replicate.nunique() == 4
    assert len(data) == 16
    _, pairs = analysis.prepare_contrasts(data)
    assert np.allclose(pairs.log_ratio, np.log(.5))


def test_additional_analysis_is_opt_in_and_estimate_does_not_fit(monkeypatch, tmp_path):
    e, res = experiment(), resolution()
    template = lab_standard_8x6()
    assign(e, res)
    calls = []
    monkeypatch.setattr(run, "_run_timecourse", lambda *a, **k: calls.append("current") or 0)
    monkeypatch.setattr(run, "run_multi_step", lambda *a, **k: calls.append("additional"))
    run.run(e, res=res, template=template, outdir=tmp_path)
    assert calls == ["current", "additional"]
    calls.clear()
    run.run(e, res=res, template=template, outdir=tmp_path, estimate=True)
    assert calls == ["current"]
    calls.clear()
    e.multi_step.enabled = False
    run.run(e, res=res, template=template, outdir=tmp_path)
    assert calls == ["current"]


def test_full_bridge_writes_audited_graphs_alongside_current_result(monkeypatch, tmp_path):
    e, res, template = experiment(), resolution(), lab_standard_8x6()
    assign(e, res)
    _, tc = run._pipeline()
    def measure(path, plate, rows, cache, layout):
        net = np.full((6, 8), 20.)
        tech = 1 if "B" in str(path) else 0
        net[0, 1] = 10 + plate + tech
        net[3, 1] = 12 + plate + tech
        return SimpleNamespace(net=net, rim=np.zeros((6, 8), dtype=bool), bg_samples=np.zeros(20))
    monkeypatch.setattr(tc, "_cached_measure", measure)
    baseline = pd.DataFrame({"treatment": ["Synthetic GLU 24 Hours"] * 4,
                             "strain": ["mutant"] * 4, "relative_growth": [.6, .7, .65, .72]})
    def existing(*args, **kwargs):
        (tmp_path / "best").mkdir()
        baseline.to_csv(tmp_path / "best" / "spotting_results_normalized.csv", index=False)
        return 0
    monkeypatch.setattr(run, "_run_timecourse", existing)
    assert run.run(e, res=res, template=template, outdir=tmp_path) == 0
    pointer = json.loads((tmp_path / "multi_step" / "latest.json").read_text())
    dest = tmp_path / "multi_step" / pointer["directory"]
    assert len(pd.read_csv(dest / "observations.csv")) == 16
    assert len(pd.read_csv(dest / "paired_contrasts.csv")) == 8
    assert pd.read_csv(dest / "tests.csv").biological_n.iloc[0] == 4
    assert list(dest.glob("*-comparison.png"))
    assert (dest / "selection.json").exists()
    mapped = run._multi_step_baseline(e, res, tmp_path)
    assert mapped.hours.eq(24.).all() and mapped.treatment.eq("GLU").all()
    assert pd.read_csv(tmp_path / "best" / "spotting_results_normalized.csv").equals(baseline)


def test_missing_repeated_observation_preserves_identity_and_per_time_counts():
    config = settings(scope="repeated")
    config.hours = (12., 24., 36.)
    data = synthetic(repeated=True)
    data = data[~((data.replicate == "rep0") & (data.hours == 36.))]
    result = analysis.analyze(data, config)
    assert result.diagnostics[0]["status"] == "ok", result.diagnostics
    assert result.estimates.set_index("hours").loc[36., "biological_n"] == 11
    assert result.estimates.set_index("hours").loc[12., "biological_n"] == 12
    assert result.estimates.biological_n_total.eq(12).all()


def test_all_failed_measurements_still_export_descriptive_graph(tmp_path):
    data = synthetic(n=4)
    data["qc_reason"] = "contamination"
    result = analysis.analyze(data, settings())
    assert result.tests.empty
    assert result.diagnostics[0]["status"] == "descriptive only"
    dest = analysis.write_results(result, tmp_path / "failed", settings())
    assert list(dest.glob("*-comparison.png"))


def test_gui_edits_save_and_cancel_without_mutating_current_statistics(tk_root):
    from experiments.gui.multistep import MultiStepDialog
    ctl = ExperimentController(experiment())
    ctl.template = lab_standard_8x6()
    ctl.resolution = resolution((24., 36.))
    original = replace(ctl.experiment.statistics)
    dialog = MultiStepDialog(tk_root, ctl)
    dialog.withdraw()
    dialog.scope.set("Technical replicates + repeated measures")
    dialog.method.set("Full hierarchical analysis")
    dialog.hours.set("24, 36")
    dialog.save()
    assert ctl.experiment.multi_step.scope == "repeated"
    assert ctl.experiment.multi_step.method == "hierarchical"
    assert ctl.experiment.multi_step.hours == (24., 36.)
    assert ctl.experiment.statistics == original
    dialog = MultiStepDialog(tk_root, ctl)
    dialog.withdraw()
    dialog.enabled.set(False)
    dialog.destroy()
    assert ctl.experiment.multi_step.enabled


def test_dialog_draft_actions_have_local_undo_redo(tk_root, monkeypatch):
    from experiments.gui import multistep
    ctl = ExperimentController(experiment())
    ctl.template = lab_standard_8x6()
    ctl.resolution = resolution((24., 36.))
    original = ctl.snapshot()
    dialog = multistep.MultiStepDialog(tk_root, ctl)
    dialog.withdraw()
    path = dialog.table.get_children()[0]
    dialog.table.selection_set(path)
    monkeypatch.setattr(multistep.simpledialog, "askstring", lambda *a, **k: "A")
    actions = [dialog.assign, dialog.exclude, dialog.include,
               lambda: dialog.hours.set("24, 36"),
               lambda: dialog.enabled.set(not dialog.enabled.get()),
               lambda: dialog.scope.set("Technical replicates + repeated measures"),
               lambda: dialog.method.set("Full hierarchical analysis"),
               lambda: dialog.dilution.set(list(dialog.levels)[-1])]
    try:
        for action in actions:
            before = dialog._state()
            action()
            after = dialog._state()
            assert before != after
            dialog.undo()
            assert dialog._state() == before
            dialog.redo()
            assert dialog._state() == after
        assert ctl.snapshot() == original
    finally:
        dialog.destroy()


@pytest.mark.parametrize("size", ["800x560", "1080x720", "1400x900"])
def test_dialog_save_button_stays_visible_at_every_size(tk_root, size):
    import tkinter as tk
    from experiments.gui.multistep import MultiStepDialog
    parent = tk.Toplevel(tk_root)
    parent.deiconify()
    ctl = ExperimentController(experiment())
    ctl.template = lab_standard_8x6()
    dialog = MultiStepDialog(parent, ctl)
    try:
        dialog.geometry(size)
        dialog.update()
        button = dialog.save_button
        assert button.winfo_ismapped()
        assert button.winfo_rooty() >= dialog.winfo_rooty()
        assert (button.winfo_rooty() + button.winfo_height()
                <= dialog.winfo_rooty() + dialog.winfo_height())
    finally:
        dialog.destroy()
        parent.destroy()
