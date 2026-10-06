"""The "how long will this take?" estimate.

It used to be uncached photos x 70 s / workers. On Set11Test (2026-10-05) that
said 19 min for a run that took 31: measuring ran ~80 s a photo, and the nine
minutes of comparison sheets afterwards were never counted at all.
"""

from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("pandas")

import spotting_batch as sb  # noqa: E402
import spotting_estimate as est  # noqa: E402
import spotting_timecourse as tc  # noqa: E402


@pytest.fixture
def rates_file(tmp_path, monkeypatch):
    """Every test gets its own rates file; the real one is never touched."""
    path = tmp_path / "run_rates.json"
    monkeypatch.setattr(est, "RATES_FILE", path)
    return path


# --- the defaults reproduce the run they were measured on ---------------------


def test_set11_is_estimated_at_the_31_minutes_it_took(rates_file):
    # 128 uncached photos on 8 workers; 128 sheets counted before scoring.
    measure, _ = est.measure_s(8)
    figures, _ = est.figure_seconds(128, 128, 128, 128, 8)
    total = 16 * measure + figures + est.OVERHEAD_S
    assert 29 * 60 <= total <= 34 * 60


def test_the_figure_model_matches_the_figure_pass_it_was_fitted_to():
    # graphs 36 s + subtractions 110 s + sheets 398 s, for what was drawn.
    assert est._figure_model(115, 113, 115, 115, 8) == pytest.approx(544, rel=0.03)


def test_a_pairings_further_dilutions_cost_far_less_than_its_first():
    one = est._figure_model(10, 20, 10, 10, 1)
    three = est._figure_model(30, 20, 10, 30, 1)
    assert three - one < one / 2


def test_no_work_is_no_time():
    assert est._figure_model(0, 0, 0, 0, 8) == 0


# --- each finished run corrects the next estimate -----------------------------


def test_a_recorded_measurement_replaces_the_default(rates_file):
    assert est.measure_s(8) == (est.MEASURE_S, False)
    est.record_measure(load=16, wall=16 * 100.0, workers=8)
    assert est.measure_s(8) == (pytest.approx(100.0), True)


def test_rates_are_kept_per_worker_count(rates_file):
    est.record_measure(load=16, wall=16 * 100.0, workers=8)
    assert est.measure_s(4) == (est.MEASURE_S, False)


def test_a_new_run_moves_the_rate_halfway(rates_file):
    est.record_measure(load=16, wall=16 * 100.0, workers=8)
    est.record_measure(load=16, wall=16 * 60.0, workers=8)
    assert est.measure_s(8)[0] == pytest.approx(80.0)


def test_a_run_too_small_to_trust_is_not_recorded(rates_file):
    est.record_measure(load=est.MIN_PHOTOS_TO_RECORD - 1, wall=500.0, workers=8)
    est.record_figures(5, 5, 5, est.MIN_SHEETS_TO_RECORD - 1, 8, wall=500.0)
    assert not rates_file.exists()


def test_an_absurd_rate_is_ignored(rates_file):
    est.record_measure(load=16, wall=16 * 0.5, workers=8)
    est.record_measure(load=16, wall=16 * 10_000.0, workers=8)
    assert est.measure_s(8) == (est.MEASURE_S, False)


def test_a_recorded_figure_pass_scales_the_model(rates_file):
    base = est._figure_model(115, 113, 115, 115, 8)
    est.record_figures(115, 113, 115, 115, 8, wall=2 * base)
    got, timed = est.figure_seconds(115, 113, 115, 115, 8)
    assert timed and got == pytest.approx(2 * base, rel=0.01)


def test_other_computers_rates_are_left_alone(rates_file, monkeypatch):
    monkeypatch.setattr(est, "_host", lambda: "lab-pc")
    est.record_measure(load=16, wall=16 * 100.0, workers=8)
    monkeypatch.setattr(est, "_host", lambda: "laptop")
    assert est.measure_s(8) == (est.MEASURE_S, False)
    est.record_measure(load=16, wall=16 * 120.0, workers=8)
    monkeypatch.setattr(est, "_host", lambda: "lab-pc")
    assert est.measure_s(8)[0] == pytest.approx(100.0)


def test_an_unreadable_rates_file_means_defaults(rates_file):
    rates_file.write_text("not json", encoding="utf-8")
    assert est.measure_s(8) == (est.MEASURE_S, False)
    est.record_measure(load=16, wall=16 * 100.0, workers=8)   # and recovers
    assert est.measure_s(8)[0] == pytest.approx(100.0)


def test_durations_round_up():
    assert est.format_minutes(20) == "under a minute"
    assert est.format_minutes(61) == "2 min"
    assert est.format_minutes(31.4 * 60) == "32 min"
    assert est.format_minutes(65 * 60) == "1 h 05 min"
    assert est.format_phase(20) == "<1 min"
    assert est.format_phase(600) == "~10 min"


# --- what the pipeline counts --------------------------------------------------


def _jobs(tmp_path, n, cached=()):
    """`n` classic measurement jobs; the indices in `cached` have an entry."""
    jobs = []
    for i in range(n):
        photo = tmp_path / f"p{i}.JPG"
        photo.write_bytes(b"x" * (i + 1))
        job = (str(photo), (1, 4), str(tmp_path))
        if i in cached:
            path, rows, cache_dir, n_rows, n_cols, _ = tc._job_parts(job)
            key = sb._cache_key(photo, tc._job_opts(rows, n_rows, n_cols))
            (tmp_path / f"{key}.npz").write_bytes(b"")
        jobs.append(job)
    return jobs


def test_the_busiest_worker_is_what_the_wall_clock_waits_for(tmp_path):
    # Dealt round-robin to 4 workers: worker 0 gets jobs 0, 4, 8 -- all
    # uncached -- and the others' jobs are all cached bar job 11. An even
    # share (4 / 4 = 1) would say a third of the real time.
    jobs = _jobs(tmp_path, 12, cached={1, 2, 3, 5, 6, 7, 9, 10})
    assert tc.busiest_load(jobs, 4) == (4, 3)


def test_one_worker_measures_everything_in_turn(tmp_path):
    jobs = _jobs(tmp_path, 6, cached={0})
    assert tc.busiest_load(jobs, 1) == (5, 5)


def test_the_estimate_splits_work_exactly_as_measuring_does(tmp_path):
    jobs = _jobs(tmp_path, 10)
    assert tc._measure_chunks(jobs, 4) == [jobs[i::4] for i in range(4)]


def _cands(n_pairings, plates=2, media=("GLU",)):
    cands = []
    for medium in media:
        for i in range(n_pairings):
            shots = tuple(tc.Shot(Path(f"{medium}/{i}/plate{p}.JPG"), 16.0 + i,
                                  f"{16 + i} Hours", medium, medium, p)
                          for p in range(1, plates + 1))
            cands.append({"medium": medium, "plates": shots})
    return cands


def test_every_candidate_gets_a_sheet_per_dilution_level():
    graphs, photos, pairings, sheets = tc.figure_workload(
        _cands(5), sb.classic_layout(), "all")
    assert (graphs, photos, pairings, sheets) == (15, 10, 5, 15)


def test_no_figures_means_no_figure_work():
    assert tc.figure_workload(_cands(5), sb.classic_layout(), "none") == (0, 0, 0, 0)


def test_the_best_n_per_medium_caps_the_sheets():
    graphs, photos, pairings, sheets = tc.figure_workload(
        _cands(5, media=("GLU", "GLY")), sb.classic_layout(), "2")
    assert (sheets, pairings, photos) == (4, 4, 8)


def test_the_estimate_counts_the_figures(capsys, rates_file):
    seconds = tc.print_estimate(16, (128, 128, 128, 128), 8)
    out = capsys.readouterr().out
    assert "figures" in out and "128 comparison sheet(s)" in out
    assert "default rates" in out
    assert seconds > 16 * est.MEASURE_S + 8 * 60
