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
