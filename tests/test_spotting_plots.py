"""Statistics and files produced by the PyPrism spotting renderer."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import spotting_plots as plots


def _frame() -> pd.DataFrame:
    rows = []
    for rep, control, ratio in zip(range(4), [10, 12, 9, 11],
                                   [0.5, 0.6, 0.4, 0.5]):
        for col, name, value in ((1, "WT", 1.0), (2, "mutant", ratio)):
            rows.append({"treatment": "Set01 GLU", "replicate": rep,
                         "strain": name, "strain_col": col,
                         "relative_growth": value, "value": value,
                         "control_raw": control,
                         "is_control": col == 1, "artifact": False,
                         "excluded": False, "outlier": False,
                         "control_ok": True})
    return pd.DataFrame(rows)


def test_ratio_test_matches_log_one_sample_test():
    from scipy.stats import ttest_1samp

    frame = _frame()
    result = plots._ratio_tests(frame, "WT", "none").iloc[0]
    expected = ttest_1samp(np.log([0.5, 0.6, 0.4, 0.5]), 0).pvalue
    assert result["p"] == pytest.approx(expected)
    assert result["mean_ratio"] == pytest.approx(
        np.exp(np.mean(np.log([0.5, 0.6, 0.4, 0.5]))))


def test_draw_writes_png_pdf_and_compatible_stats(tmp_path):
    csv_path = tmp_path / "input.csv"
    _frame().to_csv(csv_path, index=False, encoding="utf-8-sig")
    made = plots.draw(csv_path, tmp_path / "figures")
    assert [path.name for path in made] == ["spotting_Set01_GLU.png"]
    assert made[0].stat().st_size > 1000
    assert made[0].with_suffix(".pdf").exists()
    stats = pd.read_csv(tmp_path / "figures" / "spotting_paired_ttests.csv")
    assert list(stats.columns) == ["treatment", "group1", "group2", "n",
                                   "mean_ratio", "p", "p_adj"]


@pytest.mark.parametrize("method", ["holm", "bonferroni", "sidak"])
def test_ratio_test_applies_selected_fwer_correction(method):
    from statsmodels.stats.multitest import multipletests

    frame = pd.concat([
        _frame(),
        _frame().assign(strain="mutant2", strain_col=3,
                        relative_growth=0.75, value=0.75),
    ], ignore_index=True)
    result = plots._ratio_tests(frame, "WT", method)
    expected = multipletests(result["p"], method=method)[1]
    assert result["p_adj"].to_numpy() == pytest.approx(expected)


def test_anova_is_omnibus_and_writes_its_own_table(tmp_path):
    csv_path = tmp_path / "input.csv"
    _frame().to_csv(csv_path, index=False, encoding="utf-8-sig")
    made = plots.draw(csv_path, tmp_path / "figures",
                      statistical_test="anova")
    assert made and made[0].exists()
    stats_path = tmp_path / "figures" / "spotting_anova.csv"
    assert stats_path.exists()
    stats = pd.read_csv(stats_path)
    assert stats.loc[0, "test"] == "one-way ANOVA (log relative growth)"
    assert stats.loc[0, "group2"] == "all strains"
    assert stats.loc[0, "n_groups"] == 2
    assert np.isfinite(stats.loc[0, "p"])
    assert not (tmp_path / "figures" / "spotting_paired_ttests.csv").exists()


# --- comparisons and post-hoc tests ------------------------------------------


def _panel(n_strains: int = 4, seed: int = 0) -> pd.DataFrame:
    """WT plus mutants with distinct growth, four replicates each."""
    rng = np.random.default_rng(seed)
    rows = []
    means = [1.0, 0.4, 0.9, 1.6, 0.2][:n_strains]
    for rep in range(4):
        for col, mu in enumerate(means, 1):
            v = 1.0 if col == 1 else float(mu * np.exp(rng.normal(0, 0.1)))
            rows.append({"treatment": "T", "replicate": f"rep{rep + 1}",
                         "strain": "WT" if col == 1 else f"m{col}",
                         "strain_col": col, "value": v, "relative_growth": v,
                         "is_control": col == 1})
    return pd.DataFrame(rows)


def test_default_comparisons_are_every_strain_against_the_control():
    names = ["WT", "a", "b", "c"]
    assert plots.comparison_pairs(names, "WT") == [
        ("WT", "a"), ("WT", "b"), ("WT", "c")]
    # An extra reference adds its own comparisons, without repeating WT-b.
    assert plots.comparison_pairs(names, "WT", ["b"]) == [
        ("WT", "a"), ("WT", "b"), ("WT", "c"), ("b", "a"), ("b", "c")]
    assert len(plots.comparison_pairs(names, "WT", all_pairs=True)) == 6
    # A reference missing from this panel is ignored.
    assert plots.comparison_pairs(names, "WT", ["zzz"]) == \
        plots.comparison_pairs(names, "WT")


def test_default_ratio_tests_are_unchanged_by_explicit_pairs():
    frame = _panel()
    default = plots._ratio_tests(frame, "WT", "holm")
    explicit = plots._ratio_tests(
        frame, "WT", "holm",
        plots.comparison_pairs(["WT", "m2", "m3", "m4"], "WT"))
    pd.testing.assert_frame_equal(default, explicit)


def test_a_strain_to_strain_t_test_pairs_by_replicate():
    from scipy.stats import ttest_1samp

    frame = _panel()
    got = plots._ratio_tests(frame, "WT", "none", [("m2", "m3")]).iloc[0]
    a = frame[frame.strain == "m2"].set_index("replicate")["value"]
    b = frame[frame.strain == "m3"].set_index("replicate")["value"]
    d = np.log(b) - np.log(a)
    assert got["p"] == pytest.approx(ttest_1samp(d, 0).pvalue)
    assert got["mean_ratio"] == pytest.approx(np.exp(d.mean()))


def test_the_correction_covers_every_requested_comparison():
    frame = _panel()
    small = plots._ratio_tests(frame, "WT", "bonferroni")
    big = plots._ratio_tests(frame, "WT", "bonferroni",
                             plots.comparison_pairs(["WT", "m2", "m3", "m4"],
                                                    "WT", all_pairs=True))
    first = big[(big.group1 == "WT") & (big.group2 == "m2")].iloc[0]
    assert first["p_adj"] >= small.iloc[0]["p_adj"]
    assert first["p_adj"] == pytest.approx(min(1.0, first["p"] * 6))


def test_dunnett_post_hoc_matches_scipy():
    from scipy.stats import dunnett

    frame = _panel()
    tests = plots._anova_test(frame, "WT", "dunnett")
    assert tests.iloc[0]["group2"] == "all strains"
    post = tests.iloc[1:]
    assert list(post["group2"]) == ["m2", "m3", "m4"]
    logs = plots._log_groups(frame, ["WT", "m2", "m3", "m4"])
    want = dunnett(logs["m2"], logs["m3"], logs["m4"], control=logs["WT"],
                   rng=np.random.default_rng(plots.DUNNETT_SEED)).pvalue
    assert post["p_adj"].to_numpy() == pytest.approx(want)
    # Seeded, so a redraw can never move a strain across the cutoff.
    again = plots._anova_test(frame, "WT", "dunnett").iloc[1:]
    assert list(again["p_adj"]) == list(post["p_adj"])


def test_tukey_post_hoc_matches_scipy_for_the_requested_pairs():
    from scipy.stats import tukey_hsd

    frame = _panel()
    names = ["WT", "m2", "m3", "m4"]
    pairs = [("m2", "m3")]
    post = plots._anova_test(frame, "WT", "tukey", pairs).iloc[1:]
    logs = plots._log_groups(frame, names)
    want = tukey_hsd(*[logs[n] for n in names]).pvalue[1, 2]
    assert post.iloc[0]["p_adj"] == pytest.approx(want)
    # Every pair, unbalanced groups included (Tukey-Kramer).
    uneven = frame.drop(index=frame[frame.strain == "m3"].index[:1])
    logs = plots._log_groups(uneven, names)
    every = plots.comparison_pairs(names, "WT", all_pairs=True)
    post = plots._anova_test(uneven, "WT", "tukey", every).iloc[1:]
    full = tukey_hsd(*[logs[n] for n in names]).pvalue
    want = [full[names.index(a), names.index(b)] for a, b in every]
    assert post["p_adj"].to_numpy() == pytest.approx(want, rel=1e-6)


@pytest.mark.parametrize("method", ["holm", "bonferroni", "sidak"])
def test_pooled_variance_post_hoc_is_corrected_over_the_pairs(method):
    from statsmodels.stats.multitest import multipletests

    post = plots._anova_test(_panel(), "WT", method).iloc[1:]
    assert post["p_adj"].to_numpy() == pytest.approx(
        multipletests(post["p"], method=method)[1])


def test_dunnett_cannot_compare_every_pair(tmp_path):
    csv_path = tmp_path / "input.csv"
    _panel().to_csv(csv_path, index=False)
    with pytest.raises(ValueError, match="Tukey"):
        plots.draw(csv_path, tmp_path / "figures", statistical_test="anova",
                   posthoc="dunnett", all_pairs=True)


def test_post_hoc_rows_reach_the_table_and_the_figure(tmp_path):
    csv_path = tmp_path / "input.csv"
    _panel().to_csv(csv_path, index=False)
    made = plots.draw(csv_path, tmp_path / "figures", statistical_test="anova",
                      posthoc="tukey", all_pairs=True)
    assert made and made[0].exists()
    stats = pd.read_csv(tmp_path / "figures" / "spotting_anova.csv")
    assert len(stats) == 1 + 6
    assert stats["test"].iloc[1] == "Tukey HSD post-hoc"


@pytest.mark.parametrize("stats", [
    {"statistical_test": "t_test"},
    {"statistical_test": "t_test", "p_adjust": "holm"},
    {"statistical_test": "anova", "posthoc": "tukey"},
    {"statistical_test": "anova", "posthoc": "dunnett"},
    {"statistical_test": "anova", "posthoc": "tukey", "all_pairs": True},
    {"statistical_test": "t_test", "p_adjust": "bonferroni",
     "extra_references": ("m3",)},
])
def test_vs_control_reads_the_p_values_the_graph_is_drawn_from(tmp_path, stats):
    """The family is every requested comparison, as on the graph."""
    csv_path = tmp_path / "input.csv"
    _panel(5).to_csv(csv_path, index=False)
    plots.draw(csv_path, tmp_path / "figures", **stats)
    name = ("spotting_paired_ttests.csv" if stats["statistical_test"] == "t_test"
            else "spotting_anova.csv")
    table = pd.read_csv(tmp_path / "figures" / name)
    want = {r.group2: r.p_adj for r in table.itertuples()
            if r.group1 == "WT" and r.group2 != "all strains"}
    got = plots.vs_control(_panel(5), "WT", **stats)
    assert {k: p for k, (p, _) in got.items()} == pytest.approx(want)


def test_vs_control_says_which_way_a_strain_moved():
    got = plots.vs_control(_panel(), "WT", statistical_test="anova",
                           posthoc="dunnett")
    assert got["m2"][1] < 1 < got["m4"][1]


def test_an_omnibus_anova_compares_no_strain_with_the_control():
    assert plots.vs_control(_panel(), "WT", statistical_test="anova") == {}


def test_the_test_is_named_as_chosen():
    assert plots.describe_test() == "ratio t-test, uncorrected, p <= 0.05"
    assert plots.describe_test("t_test", "holm", alpha=0.01) == \
        "ratio t-test, Holm, p <= 0.01"
    assert plots.describe_test("anova", posthoc="tukey") == \
        "ANOVA + Tukey HSD, p <= 0.05"
    assert plots.describe_test("anova") == "ANOVA, omnibus only, p <= 0.05"
    # A run's whole statistics dict goes straight in.
    assert plots.describe_test(
        statistical_test="anova", p_adjust="none", alpha=0.05,
        posthoc="dunnett", extra_references=(), all_pairs=False) == \
        "ANOVA + Dunnett, p <= 0.05"


def test_brackets_share_a_tier_only_when_they_do_not_touch():
    # Against the control: nothing can share, one tier each by reach.
    assert plots._stack_brackets([0, 0, 0], [3, 1, 2]) == [3, 1, 2]
    # Neighbours side by side share the bottom tier; a bridge goes above.
    assert plots._stack_brackets([0, 2, 0], [1, 3, 3]) == [1, 1, 2]
    # Touching at an end is overlapping.
    assert plots._stack_brackets([0, 1], [1, 2]) == [1, 2]
