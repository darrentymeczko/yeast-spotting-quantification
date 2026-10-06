"""Statistics chosen in the designer reach the figures and the review tool.

The designer repeats the review tool's choices so a test can be set before a
run. Repeating them means they can drift, so the agreement is asserted here.
"""

import json

import pytest

from experiments import model, run
from experiments.gui.controller import ExperimentController
from experiments.model import Experiment, Statistics
from experiments.validate import validate


def panel() -> Experiment:
    return Experiment(strains=["WT", "a", "b"], control_slot=1)


# --- agreement ----------------------------------------------------------------


def test_the_choices_match_the_review_tool():
    from results_review import model as review_model

    assert model.POSTHOC_METHODS == review_model.POSTHOC_METHODS
    review = review_model.Review()
    assert Statistics().review_dict() == {
        "test": review.statistical_test, "p_adjust": review.p_adjust,
        "alpha": review.alpha, "posthoc": review.posthoc,
        "extra_references": review.extra_references,
        "all_pairs": review.all_pairs,
    }


def test_the_choices_are_ones_the_plotting_stack_accepts():
    plots = pytest.importorskip("spotting_plots")

    assert set(model.POSTHOC_METHODS) == set(plots.POSTHOC_METHODS)
    assert set(model.P_ADJUST_METHODS) <= set(plots.P_ADJUST_METHODS)


def test_the_defaults_draw_exactly_what_the_pipeline_always_drew():
    import inspect

    sb = pytest.importorskip("spotting_batch")
    defaults = {k: p.default for k, p in
                inspect.signature(sb.run_plots).parameters.items()
                if p.default is not inspect.Parameter.empty}
    assert Statistics().plot_kwargs() == defaults


# --- validation ---------------------------------------------------------------


def test_a_comparison_strain_missing_from_the_panel_is_flagged():
    e = panel()
    e.statistics = Statistics(extra_references=("a", "gone"))
    found = [i for i in validate(e) if i.code == "unknown_reference"]
    assert len(found) == 1 and "'gone'" in found[0].message


def test_every_pair_needs_no_comparison_strains():
    e = panel()
    e.statistics = Statistics(all_pairs=True, extra_references=("gone",))
    assert not [i for i in validate(e) if i.code == "unknown_reference"]


# --- the controller -----------------------------------------------------------


def test_the_controller_refuses_dunnett_for_every_pair():
    ctl = ExperimentController(panel())
    ctl.set_statistics(test="anova", all_pairs=True)
    assert ctl.experiment.statistics.posthoc == "tukey"
    with pytest.raises(ValueError, match="Tukey"):
        ctl.set_statistics(posthoc="dunnett")


def test_the_controller_refuses_a_bad_cutoff():
    ctl = ExperimentController(panel())
    with pytest.raises(ValueError):
        ctl.set_statistics(alpha=0)
    assert ctl.experiment.statistics.alpha == 0.05


# --- the review tool opens on the same choices --------------------------------


def test_a_run_seeds_the_review_with_its_statistics(tmp_path):
    from results_review import review

    e = panel()
    e.statistics = Statistics(test="anova", posthoc="holm", alpha=0.01)
    run.seed_review_statistics(e, tmp_path)
    loaded = review.load(tmp_path / "review.json")
    assert (loaded.statistical_test, loaded.posthoc, loaded.alpha) == (
        "anova", "holm", 0.01)


def test_seeding_keeps_the_picks_and_edits_already_made(tmp_path):
    path = tmp_path / "review.json"
    path.write_text(json.dumps({
        "version": 1, "capture_root": "D:/x",
        "statistics": {"test": "t_test"},
        "chosen": {"GLU": {"timepoint": "40 Hours", "dilution": "middle"}},
    }), encoding="utf-8")
    e = panel()
    e.statistics = Statistics(p_adjust="holm")
    run.seed_review_statistics(e, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["chosen"]["GLU"]["timepoint"] == "40 Hours"
    assert data["capture_root"] == "D:/x"
    assert data["statistics"]["p_adjust"] == "holm"


# --- data-only output ---------------------------------------------------------


def test_output_round_trips_and_defaults_to_full():
    from experiments import schema

    e = panel()
    assert e.output == model.FULL_OUTPUT
    e.output = model.DATA_OUTPUT
    again = schema.loads_experiment(schema.dumps_experiment(e))
    assert again.output == model.DATA_OUTPUT

    d = schema.to_dict(panel())
    del d["output"]
    assert schema.from_dict(d).output == model.FULL_OUTPUT


def test_an_unknown_output_is_refused():
    from experiments import schema

    d = schema.to_dict(panel())
    d["output"] = "pdf"
    with pytest.raises(schema.ExperimentError, match=r"\$\.output"):
        schema.from_dict(d)


@pytest.mark.parametrize("output, figures", [
    (model.FULL_OUTPUT, "all"), (model.DATA_OUTPUT, "none"),
])
def test_a_data_only_time_course_draws_no_sheets(monkeypatch, tmp_path,
                                                 output, figures):
    from types import SimpleNamespace

    seen = {}

    def run_one(_tree, args, _cfg, **_kwargs):
        seen["figures"] = args.figures
        return 1                      # non-zero: no handoff to write

    tc = SimpleNamespace(Tree=lambda **_k: None, run_one=run_one)
    monkeypatch.setattr(run, "_pipeline", lambda: (None, tc))
    monkeypatch.setattr(run, "to_layout", lambda *_a: None)
    monkeypatch.setattr(run, "to_shots", lambda *_a: [object()])
    e = panel()
    e.output = output
    run._run_timecourse(e, None, tmp_path, estimate=False, workers=None,
                        template=object())
    assert seen["figures"] == figures


def test_the_controller_sets_the_output():
    ctl = ExperimentController(panel())
    assert ctl.set_output(model.DATA_OUTPUT)
    assert ctl.experiment.output == model.DATA_OUTPUT
    with pytest.raises(ValueError):
        ctl.set_output("pdf")
