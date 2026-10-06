"""Candidates are counted significant with the experiment's own test.

Scoring used to run an uncorrected one-sample t-test per strain whatever the
experiment chose, so on Set12 K-OAC37C a sheet said "8 of 23 strains p<0.05"
above a graph that, under the chosen ANOVA + Tukey, marked two. The panel here
reproduces that shape: one strain moved a little but very consistently, one
that is all over the place, and one that is clearly down. A t-test calls the
consistent strain; an ANOVA pools the noisy strain's spread and does not.

The engine is stubbed wherever a photograph would be read.
"""

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")
pytest.importorskip("scipy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import spotting_batch as sb  # noqa: E402
import spotting_plots as sp  # noqa: E402
import spotting_timecourse as tc  # noqa: E402

STRAINS = ["WT", "steady", "noisy", "sick", "e", "f", "g", "h"]
#: Growth relative to WT, per strain slot, for replicates 1-4.
GROWTH = {
    1: [1.0, 1.0, 1.0, 1.0],
    2: [0.85, 0.86, 0.84, 0.855],     # down 15%, every replicate
    3: [0.3, 2.5, 1.0, 0.6],          # no effect, enormous spread
    4: [0.1, 0.11, 0.09, 0.1],        # down 90%
    5: [1.02, 0.97, 1.03, 0.99],
    6: [0.98, 1.03, 1.01, 0.97],
    7: [1.01, 0.99, 0.98, 1.02],
    8: [0.97, 1.02, 1.0, 1.01],
}


class Plate:
    """`PlateData` stand-in for the classic layout: replicate
    (plate - 1) * 2 + 1 + row // 3, slot = column + 1, WT reading 10."""

    def __init__(self, plate, rows=6, cols=8):
        self.ref = sb.PhotoRef(Path(f"p{plate}.JPG"), "TC", plate, "TC")
        self.net = np.array([[10.0 * GROWTH[c + 1][(plate - 1) * 2 + r // 3]
                              for c in range(cols)] for r in range(rows)])
        self.rim = np.zeros((rows, cols), bool)
        self.bg_samples = np.ones(5)
        self.radius = 50.0
        self.centers = np.zeros((rows, cols, 2))


@pytest.fixture(autouse=True)
def plates(monkeypatch):
    monkeypatch.setattr(tc, "_cached_measure",
                        lambda path, plate, rows, cache_dir, layout=None:
                        Plate(plate))


def candidate():
    shots = [tc.Shot(Path(f"40 Hours/GLY/Plate {p}/_9.JPG"), 40.0, "40 Hours",
                     "GLY", "Glycerol", p) for p in (1, 2)]
    return tc.candidates(shots)[0]


def score(statistics):
    level = sb.classic_layout().levels[0]
    return tc.score_candidate(candidate(), level, Path("c"), STRAINS, 1,
                              statistics=statistics)


T_TEST = {"statistical_test": "t_test", "p_adjust": "none", "alpha": 0.05,
          "posthoc": "none", "extra_references": (), "all_pairs": False}
TUKEY = dict(T_TEST, statistical_test="anova", posthoc="tukey")


def test_the_panel_is_one_the_two_tests_disagree_on():
    assert set(score(T_TEST)["_strain_sigs"]) == {"steady", "sick"}
    assert set(score(TUKEY)["_strain_sigs"]) == {"sick"}


def test_the_count_is_the_chosen_tests():
    m = score(TUKEY)
    assert m["n_significant"] == 1
    assert m["_strain_sigs"] == {"sick": -1}


def test_the_count_is_the_brackets_the_graph_draws(tmp_path):
    """Drawn from the frame the candidate's sheet graph is drawn from."""
    level = sb.classic_layout().levels[0]
    tidy = sb.build_tidy_level([Plate(1), Plate(2)], STRAINS, 1, level,
                               experiment="x", treatment="x", set_label="TC")
    csv = tmp_path / "x.csv"
    tidy.to_csv(csv, index=False, encoding="utf-8-sig")
    for stats in (T_TEST, TUKEY, dict(TUKEY, posthoc="dunnett"),
                  dict(T_TEST, p_adjust="holm")):
        sp.draw(csv, tmp_path / "figures", **stats)
        name = ("spotting_paired_ttests.csv"
                if stats["statistical_test"] == "t_test"
                else "spotting_anova.csv")
        table = pd.read_csv(tmp_path / "figures" / name)
        marked = {r.group2 for r in table.itertuples()
                  if r.group1 == "WT" and r.group2 != "all strains"
                  and r.p_adj <= stats["alpha"]}
        assert set(score(stats)["_strain_sigs"]) == marked, stats


def test_no_statistics_is_the_renderers_defaults():
    assert score(None)["_strain_sigs"] == score(T_TEST)["_strain_sigs"]


def test_an_omnibus_anova_counts_no_strain():
    m = score(dict(TUKEY, posthoc="none"))
    assert m["n_significant"] == 0 and m["_strain_sigs"] == {}


def test_alpha_is_the_chosen_cutoff():
    loose = score(dict(TUKEY, alpha=0.5))["_strain_sigs"]
    assert len(loose) >= len(score(TUKEY)["_strain_sigs"])
    assert score(dict(TUKEY, alpha=1e-12))["_strain_sigs"] == {}


def test_a_deferred_test_gives_the_same_answer():
    m = tc.score_candidate(candidate(), sb.classic_layout().levels[0],
                           Path("c"), STRAINS, 1, statistics=TUKEY, test=False)
    assert m["n_significant"] is None
    assert tc.significant_strains(m["_tested"], TUKEY) == \
        score(TUKEY)["_strain_sigs"]


def test_a_slow_test_is_spread_over_workers(monkeypatch):
    """The pool path returns what one-at-a-time does, in order."""
    m = tc.score_candidate(candidate(), sb.classic_layout().levels[0],
                           Path("c"), STRAINS, 1, test=False)
    # Two are tested here to time the second; the pool takes the other three.
    groups = [None] + [m["_tested"]] * 4 + [None]
    serial = tc.significance_all(groups, TUKEY, workers=1)
    monkeypatch.setattr(tc, "PARALLEL_TEST_S", 0.0)
    pooled = tc.significance_all(groups, TUKEY, workers=2)
    assert pooled == serial == [{}] + [{"sick": -1}] * 4 + [{}]
