"""The packaged program: spotting_app.py.

Nothing here builds an .exe (that takes minutes and is checked by the build's
own `--selftest`). These are the properties that go wrong quietly:

  * a stage drifts from the .bat file it is meant to replace
  * arguments stop passing through, or an exit code is lost
  * the packaged program looks for the project folder in the wrong place
  * the .exe stops reading the .py files beside it, so edits silently do
    nothing -- the whole point of how it is built
  * the build names a file that has since moved
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import spotting_app as app  # noqa: E402


# --- a stage runs what its .bat file runs ------------------------------------

#: bat file -> (stage, names both must mention). If a launcher is pointed at a
#: different module, the .exe must follow, or the two quietly stop agreeing.
PAIRS = {
    "run_workbench.bat": ("workbench", ["workbench"]),
    "run_plate_designer.bat": ("plate", ["plate_template.app"]),
    "run_experiment_designer.bat": ("experiment", ["experiments.app"]),
    "run_data_review.bat": ("data-review", ["data_review.app"]),
    "run_spotting.bat": ("spotting", ["spotting_batch"]),
    "run_timecourse.bat": ("timecourse", ["spotting_timecourse"]),
    "run_review.bat": ("review", ["results_review.app", "results_review.cli"]),
}


@pytest.mark.parametrize("bat", sorted(PAIRS))
def test_a_stage_runs_what_its_bat_runs(bat):
    key, needles = PAIRS[bat]
    bat_text = (ROOT / bat).read_text(encoding="utf-8", errors="replace")
    stage_text = inspect.getsource(app.BY_KEY[key].run)
    for needle in needles:
        assert needle in bat_text, f"{bat} no longer runs {needle}"
        assert needle in stage_text, f"stage {key!r} does not run {needle}"


def test_every_bat_file_is_a_real_stage_and_stage_keys_are_unique():
    assert {s for s, _ in PAIRS.values()} <= set(app.BY_KEY)
    assert len({s.key for s in app.STAGES}) == len(app.STAGES)


def test_home_offers_each_tool_in_the_order_the_work_happens():
    """The workbench's Home cards are the four tools that still have a window
    of their own; the console pipelines are deliberately not among them."""
    from workbench.registry import KINDS

    assert [k.key for k in KINDS] == ["plate", "experiment", "data-review",
                                      "review"]
    assert [k.step for k in KINDS] == [1, 2, 3, 4]
    assert all(k.key in app.BY_KEY for k in KINDS)


# --- dispatch ------------------------------------------------------------------

@pytest.fixture
def seen(monkeypatch):
    """Replace every stage's runner with one that records its arguments."""
    calls: list[tuple[str, list]] = []
    for key, stage in list(app.BY_KEY.items()):
        def run(argv, _key=key):
            calls.append((_key, list(argv)))
            return 0
        monkeypatch.setitem(app.BY_KEY, key, app.replace(stage, run=run))
    return calls


def test_no_arguments_opens_the_workbench(seen):
    assert app.main([]) == 0
    assert seen == [("workbench", [])]


def test_arguments_pass_through_untouched(seen):
    assert app.main(["timecourse", "D:\\Set09", "--workers", "8"]) == 0
    assert seen == [("timecourse", ["D:\\Set09", "--workers", "8"])]


def test_a_stage_name_is_not_case_sensitive(seen):
    app.main(["Spotting", "--reask"])
    assert seen == [("spotting", ["--reask"])]


def test_dragged_folders_run_the_time_course(seen, tmp_path):
    a, b = tmp_path / "Set01", tmp_path / "Set02"
    a.mkdir(), b.mkdir()
    assert app.main([str(a), str(b)]) == 0
    assert seen == [("timecourse", [str(a), str(b)])]


def test_an_unknown_stage_is_an_error_not_a_launch(seen, capsys):
    assert app.main(["nonsense"]) == 2
    assert seen == []
    assert "Unknown stage" in capsys.readouterr().err


def test_help_lists_every_stage(capsys):
    assert app.main(["--help"]) == 0
    text = capsys.readouterr().out
    for stage in app.STAGES:
        assert stage.key in text, f"--help does not mention {stage.key}"


def test_review_apply_is_stripped_and_goes_to_the_headless_cli(monkeypatch):
    import results_review.cli
    import results_review.app

    got = {}
    monkeypatch.setattr(results_review.cli, "main",
                        lambda argv: got.setdefault("cli", list(argv)) and 0)
    monkeypatch.setattr(results_review.app, "main",
                        lambda argv: got.setdefault("app", list(argv)) and 0)
    app._review(["--apply", "Results/Timecourse/Set01"])
    app._review(["Results/Timecourse/Set01"])
    assert got == {"cli": ["Results/Timecourse/Set01"],
                   "app": ["Results/Timecourse/Set01"]}


# --- exit codes, as the .bat files report them ---------------------------------

def _stage(fn, kind="tool"):
    return app.Stage("t", "T", "", fn, kind)


def test_a_return_value_is_the_exit_code():
    assert app.run_stage(_stage(lambda argv: 3), []) == 3
    assert app.run_stage(_stage(lambda argv: None), []) == 0


def test_system_exit_carries_its_code():
    def fail(argv):
        raise SystemExit(2)                # what argparse does on a bad flag
    assert app.run_stage(_stage(fail), []) == 2


def test_system_exit_with_a_message_is_one_and_prints_it(capsys):
    def fail(argv):
        raise SystemExit("no such preset")
    assert app.run_stage(_stage(fail), []) == 1
    assert "no such preset" in capsys.readouterr().err


def test_a_crash_is_reported_not_raised(monkeypatch, capsys):
    def boom(argv):
        raise RuntimeError("kaput")
    assert app.run_stage(_stage(boom), []) == 1
    assert "kaput" in capsys.readouterr().err

    reported = []
    monkeypatch.setattr(app, "_report_crash", lambda s, t: reported.append(t))
    assert app.run_stage(_stage(boom, kind="window"), []) == 1
    assert reported and "kaput" in reported[0]     # a window shows it in a dialog


# --- where the packaged program looks for the project folder -------------------

@pytest.fixture
def packaged(monkeypatch, tmp_path):
    """Pretend to be the .exe, sitting in tmp_path; put everything back after."""
    exe = tmp_path / "SpottingQuantification.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    yield tmp_path
    monkeypatch.undo()
    for name in ("spotting_paths", "experiments", "data_review", "results_review"):
        importlib.reload(importlib.import_module(name))


def test_the_engine_uses_the_folder_the_exe_is_in(packaged):
    import spotting_paths

    importlib.reload(spotting_paths)
    assert spotting_paths.PROJECT_ROOT == packaged.resolve()


def test_the_experiment_layer_separates_user_files_from_shipped_ones(packaged):
    import experiments

    importlib.reload(experiments)
    assert experiments.REPO == packaged.resolve()
    # The shipped plate template is inside the bundle, not beside the exe.
    assert experiments.BUNDLE != experiments.REPO
    assert (experiments.BUNDLE / "plate_template" / "templates"
            / "lab_standard_8x6.json").exists()


def test_the_review_window_uses_the_folder_the_exe_is_in(packaged):
    import results_review

    importlib.reload(results_review)
    assert results_review.PROJECT_ROOT == packaged.resolve()


def test_the_data_review_uses_the_folder_the_exe_is_in(packaged):
    import data_review

    importlib.reload(data_review)
    assert data_review.PROJECT_ROOT == packaged.resolve()


def test_a_src_folder_beside_the_exe_is_used_ahead_of_the_bundled_engine(packaged):
    """The whole reason the .exe is built the way it is: an edit to src/ must
    take effect on the next run, with no rebuild. That only happens if the
    folder beside the .exe goes on sys.path ahead of the built-in copy."""
    import results_review

    src = str(packaged.resolve() / "src")
    (packaged / "src").mkdir()
    importlib.reload(results_review)
    assert src in sys.path
    sys.path.remove(src)


def test_no_src_beside_the_exe_falls_back_to_the_bundled_engine(packaged):
    """Handed to someone on its own, the .exe must still work."""
    import results_review

    importlib.reload(results_review)
    assert str(packaged.resolve() / "src") not in sys.path


# --- the .exe reads this project's code from disk -------------------------------

@pytest.fixture
def path_restored():
    """_use_local_source edits sys.path; hand it back exactly as it was."""
    before = list(sys.path)
    yield
    sys.path[:] = before


def _all_code_dirs(folder):
    for name in app.CODE_DIRS:
        (folder / name).mkdir()


def test_local_code_goes_ahead_of_everything_else(packaged, path_restored,
                                                  monkeypatch):
    _all_code_dirs(packaged)
    monkeypatch.setattr(app, "ROOT", packaged.resolve())
    assert app._use_local_source() is True
    # Ahead of the bundle, or the built-in copy would win and edits do nothing.
    assert sys.path[:2] == [str(packaged.resolve()), str(packaged.resolve() / "src")]


def test_a_partial_folder_is_not_treated_as_local_code(packaged, path_restored,
                                                       monkeypatch):
    """Half a source tree beside the .exe is someone's leftovers, not a
    checkout. Importing from it would half-shadow the bundle, which fails in a
    far more confusing way than not using it at all."""
    (packaged / "src").mkdir()
    (packaged / "experiments").mkdir()
    monkeypatch.setattr(app, "ROOT", packaged.resolve())
    before = list(sys.path)
    assert app._use_local_source() is False
    assert sys.path == before


def test_the_source_note_says_which_code_is_running(packaged, monkeypatch):
    monkeypatch.setattr(app, "FROZEN", True)
    monkeypatch.setattr(app, "LOCAL_SOURCE", True)
    assert "edits apply" in app.source_note()
    monkeypatch.setattr(app, "LOCAL_SOURCE", False)
    assert "NOT apply" in app.source_note()


def test_from_source_the_code_is_always_the_local_code():
    assert app.LOCAL_SOURCE is True
    assert app.FROZEN is False


def test_the_plate_designer_finds_templates_beside_the_exe(packaged):
    from plate_template.app import default_template_dir

    assert default_template_dir() == packaged.resolve() / "Plate Templates"


def test_from_source_the_project_folder_is_still_the_repo():
    import experiments
    import results_review
    import spotting_paths

    assert spotting_paths.PROJECT_ROOT == ROOT
    assert experiments.REPO == experiments.BUNDLE == ROOT
    assert results_review.PROJECT_ROOT == ROOT


# --- the build script ----------------------------------------------------------

def test_the_code_folders_the_exe_looks_for_all_exist():
    """CODE_DIRS decides whether the .exe reads local .py files. A folder
    renamed here and not there would send it silently back to the built-in
    copy, and edits would stop taking effect with nothing said."""
    for name in app.CODE_DIRS:
        assert (ROOT / name).is_dir(), f"{name}/ is gone or renamed"
