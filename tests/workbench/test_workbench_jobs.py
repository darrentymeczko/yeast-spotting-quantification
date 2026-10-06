"""Runs inside the workbench: output streamed, parsed, stoppable, reported.

The measurement pipeline reports progress the console way -- one line rewritten
with carriage returns -- and the workbench has to read that from a pipe on
Windows, where a newline arrives as two bytes that can be split across reads.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import tkinter as tk
from pathlib import Path

import pytest

from uikit.host import JobSpec
from workbench.jobs import (CONSOLE_ONLY_ENV, Job, JobRunner, StreamSplitter,
                            job_env)

ROOT = Path(__file__).resolve().parents[2]


# --- reading the output ------------------------------------------------------------


def test_newlines_commit_lines():
    s = StreamSplitter()
    assert s.feed("one\ntwo\n") == [("line", "one"), ("line", "two")]


def test_a_carriage_return_rewrites_the_live_line():
    s = StreamSplitter()
    assert s.feed("\r    1/5\r    2/5") == [("live", "    2/5")]
    assert s.feed("\r    5/5\r\n") == [("line", "    5/5")]


def test_a_crlf_split_between_reads_is_one_newline():
    s = StreamSplitter()
    assert s.feed("abc\r") == [("live", "abc")]
    assert s.feed("\nnext") == [("line", "abc"), ("live", "next")]


def test_what_is_left_at_the_end_is_a_line():
    s = StreamSplitter()
    s.feed("no newline")
    assert s.flush() == [("line", "no newline")]
    assert s.flush() == []


class CharByChar(StreamSplitter):
    """The splitter as it was first written, one character at a time: the
    reference the faster one has to agree with."""

    def feed(self, text):
        events = []
        if self._held_cr:
            text = "\r" + text
            self._held_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            self._held_cr = True
        live_changed = False
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\n":
                events.append(("line", self._line))
                self._line = ""
                live_changed = False
            elif ch == "\r":
                if i + 1 < n and text[i + 1] == "\n":
                    i += 1
                    events.append(("line", self._line))
                    self._line = ""
                    live_changed = False
                else:
                    self._line = ""
                    live_changed = True
            else:
                self._line += ch
                live_changed = True
            i += 1
        if live_changed:
            events.append(("live", self._line))
        return events


def test_reading_whole_pieces_says_what_reading_characters_did():
    import random

    rng = random.Random(7)
    for _ in range(400):
        text = "".join(rng.choice(["a", "b", " ", "\r", "\n", "\r\n", "12/40"])
                       for _ in range(rng.randrange(0, 60)))
        cuts = sorted(rng.sample(range(len(text) + 1), min(len(text) + 1, 4)))
        chunks = [text[a:b] for a, b in zip([0] + cuts, cuts + [len(text)])]
        fast, slow = StreamSplitter(), CharByChar()
        for chunk in chunks:
            assert fast.feed(chunk) == slow.feed(chunk), repr(chunks)
        assert fast.flush() == slow.flush(), repr(chunks)


def test_progress_results_folder_and_phase_are_read_from_the_output():
    job = Job(JobRunner(lambda *_: None, log_dir=None), JobSpec("t", ["x"]))
    job._take("line", "  GLU|16 Hours: measuring 4 plate(s) ...")
    job._take("live", "    12/40")
    job._take("line", "  results folder: C:\\Results\\Timecourse\\Set09")
    assert job.progress == (12, 40)
    assert job.result_dir == Path("C:\\Results\\Timecourse\\Set09")
    assert job.phase.startswith("GLU|16 Hours")


def test_a_piped_job_gets_none_of_the_console_only_environment():
    spec = JobSpec("t", ["x"], env={name: "1" for name in CONSOLE_ONLY_ENV}
                   | {"KEEP": "yes"})
    env = job_env(spec)
    assert not set(CONSOLE_ONLY_ENV) & set(env)
    assert env["KEEP"] == "yes"
    assert env["PYTHONUNBUFFERED"] == "1" and env["PYTHONIOENCODING"] == "utf-8"


# --- a real child process ---------------------------------------------------------------


CHILD = textwrap.dedent("""
    import sys, time
    print("hello Δ")
    for i in range(1, 6):
        print(f"\\r    {i}/5", end="", flush=True)
        time.sleep(0.02)
    print()
    print("  results folder: X")
    sys.exit(3)
""")


def _wait(runner: JobRunner, job: Job, timeout: float = 60) -> None:
    end = time.time() + timeout
    while not job.finished:
        runner.pump()
        assert time.time() < end, "the job never finished"
        time.sleep(0.02)


def test_a_job_streams_its_output_and_reports_its_exit_code(tmp_path):
    changes = []
    runner = JobRunner(lambda job, kinds: changes.append(set(kinds)),
                       log_dir=tmp_path)
    job = runner.start(JobSpec("child", [sys.executable, "-c", CHILD],
                               cwd=str(tmp_path)))
    _wait(runner, job)
    assert job.state == Job.FAILED and job.poll() == 3
    assert "hello Δ" in job.lines
    assert job.progress == (5, 5)
    assert job.result_dir == Path("X")
    assert job.log_path is not None and "hello" in job.log_path.read_text(
        encoding="utf-8")
    assert any("state" in kinds for kinds in changes)


PRIORITY_CHILD = textwrap.dedent("""
    import ctypes
    from ctypes import wintypes
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.GetPriorityClass.argtypes = (wintypes.HANDLE,)
    print("priority", k.GetPriorityClass(k.GetCurrentProcess()))
""")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_a_run_gives_way_to_the_window(tmp_path):
    runner = JobRunner(lambda *_: None, log_dir=None)
    job = runner.start(JobSpec("child", [sys.executable, "-c", PRIORITY_CHILD],
                               cwd=str(tmp_path)))
    _wait(runner, job)
    assert f"priority {subprocess.BELOW_NORMAL_PRIORITY_CLASS}" in job.lines


def test_runs_of_an_exclusive_kind_queue_one_behind_the_other(tmp_path):
    runner = JobRunner(lambda *_: None, log_dir=None)
    quick = [sys.executable, "-c", "import time; time.sleep(0.3)"]
    first = runner.start(JobSpec("a", quick, kind="experiment-run"))
    second = runner.start(JobSpec("b", quick, kind="experiment-run"))
    assert first.state == Job.RUNNING and second.state == Job.QUEUED
    _wait(runner, first)
    assert second.state in (Job.RUNNING, Job.DONE)
    _wait(runner, second)
    assert second.state == Job.DONE


def test_stopping_a_queued_job_never_starts_it():
    runner = JobRunner(lambda *_: None, log_dir=None)
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    first = runner.start(JobSpec("a", slow, kind="experiment-run"))
    second = runner.start(JobSpec("b", slow, kind="experiment-run"))
    second.stop()
    assert second.state == Job.STOPPED and second.process is None
    first.stop()
    _wait(runner, first)
    assert first.state == Job.STOPPED


def _alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259                        # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process tree")
def test_stop_ends_the_worker_processes_too():
    """A run's measuring happens in worker processes. Stopping only the
    parent would leave them measuring with nobody to report to."""
    parent = textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen([{sys.executable!r}, "-c",
                                  "import time; time.sleep(60)"])
        print(f"worker {{child.pid}}", flush=True)
        time.sleep(60)
    """)
    runner = JobRunner(lambda *_: None, log_dir=None)
    job = runner.start(JobSpec("tree", [sys.executable, "-c", parent]))
    end = time.time() + 30
    while not any(line.startswith("worker ") for line in job.lines):
        runner.pump()
        assert time.time() < end
        time.sleep(0.05)
    worker = int(next(l for l in job.lines if l.startswith("worker ")).split()[1])
    assert _alive(worker)
    job.stop()
    _wait(runner, job)
    assert job.state == Job.STOPPED
    end = time.time() + 10
    while _alive(worker) and time.time() < end:
        time.sleep(0.1)
    assert not _alive(worker)


# --- the experiment runner says where its results go ------------------------------------


def test_the_experiment_runner_prints_its_results_folder(tmp_path, monkeypatch, capsys):
    from argparse import Namespace

    from experiments import cli, run as runner, schema, validate, intake
    from experiments.model import Experiment

    path = tmp_path / "e.spotexp.json"
    schema.save(Experiment(name="Set77"), path, bump_revision=False)
    seen = {}
    monkeypatch.setattr(runner, "prepare", lambda e: object())
    monkeypatch.setattr(runner, "default_results_dir",
                        lambda e: tmp_path / "Results" / e.name)
    monkeypatch.setattr(runner, "run",
                        lambda e, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(cli, "_load_template", lambda e, base: None)
    monkeypatch.setattr(validate, "validate", lambda e, t: [])
    monkeypatch.setattr(intake, "check_resolution", lambda e, r, *a, **k: [])

    args = Namespace(file=str(path), out=None, estimate=False, workers=None,
                     force=False)
    assert cli.cmd_run(args) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"results folder: {tmp_path / 'Results' / 'Set77'}" in out
    assert seen["outdir"] == tmp_path / "Results" / "Set77"


# --- in the window -------------------------------------------------------------------------


@pytest.fixture
def shell(tk_root, tmp_path, monkeypatch):
    from results_review import links
    from workbench.settings import Settings
    from workbench.shell import Shell

    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    window = tk.Toplevel(tk_root)
    window.withdraw()
    sh = Shell(window, Settings(tmp_path / "workbench.json"), restore=False,
               log_dir=tmp_path / "logs")
    yield sh
    try:
        sh.exit(force=True)
    except tk.TclError:
        pass


def _finish(shell, job, timeout=60):
    end = time.time() + timeout
    while not job.finished:
        shell.runner.pump()
        shell.root.update()
        assert time.time() < end
        time.sleep(0.02)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("suffix", [".spotexp.json", ".json", ".SPOTEXP.JSON"])
def test_saved_experiment_name_reaches_run_prompt_and_progress(
        shell, tmp_path, monkeypatch, existing, suffix):
    from experiments import app as app_module, schema
    from experiments.model import Experiment

    path = tmp_path / f"Heat stress{suffix}"
    if existing:
        schema.save(Experiment(), path)
    doc = shell.open_document("experiment", path if existing else None)
    app = doc.app
    monkeypatch.setattr(app_module, "default_experiment_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module.filedialog, "asksaveasfilename",
                        lambda **kw: str(path))
    if not existing:
        assert app.save_file()
    monkeypatch.setattr(app.controller, "issues", lambda: [])
    monkeypatch.setattr(app, "_pipeline_python", lambda env: Path(sys.executable))
    prompts = []
    monkeypatch.setattr(app_module.messagebox, "askokcancel",
                        lambda title, message, **kw: prompts.append(message) or True)

    # Exercise the real job/status UI without starting the measurement pipeline.
    def launch(job):
        job.state = Job.RUNNING
        job.started = time.time()

    monkeypatch.setattr(shell.runner, "_launch", launch)
    app._run_quantification()

    assert app.controller.experiment.name == "Heat stress"
    assert schema.load(path).name == "Heat stress"
    assert not app.controller.dirty
    assert "for Heat stress?" in prompts[0]
    job = shell.runner.jobs[-1]
    assert job.title.startswith("Heat stress")
    assert "Heat stress" in shell.status_job.cget("text")
    assert "Untitled" not in shell.status_job.cget("text")
    job.state = Job.DONE


def test_a_run_the_program_started_is_reported_when_it_ends(shell):
    job = shell.run_job(JobSpec("Child run", [sys.executable, "-c", CHILD]))
    assert shell.jobs_panel.winfo_manager()           # the panel appears
    _finish(shell, job)
    assert shell.toasts.toasts, "nobody said it had finished"
    assert "did not finish" in str(shell.toasts.toasts[-1].winfo_children()[1]
                                   .cget("text"))


def test_a_runs_log_opens_in_a_tab_of_its_own(shell):
    job = shell.run_job(JobSpec("Child run", [sys.executable, "-c", CHILD]))
    doc = shell.open_job(job)
    assert doc is not None and doc.kind == "job" and shell.active is doc
    _finish(shell, job)
    shell.root.update()
    text = doc.app.text.get("1.0", "end")
    assert "hello Δ" in text and "5/5" in text
    assert shell.open_job(job) is doc                  # one tab per run


def test_a_long_runs_log_adds_only_what_is_new_and_keeps_the_latest(shell):
    from workbench import jobs_ui

    job = Job(shell.runner, JobSpec("Chatty run", ["x"]))
    job.state = Job.RUNNING
    shell.runner.jobs.append(job)
    doc = shell.open_job(job)
    log = doc.app
    total = 0
    for _ in range(4):
        for _ in range(jobs_ui.VIEW_LINES // 2):
            job._take("line", f"line {total}")
            total += 1
        job._take("live", f"    {total}/99999")
        log.update_log()
    lines = log.text.get("1.0", "end-1c").split("\n")
    assert len(lines) <= jobs_ui.VIEW_LINES + jobs_ui.VIEW_SLACK + 1
    assert lines[-2:] == [f"line {total - 1}", f"    {total}/99999"]
    numbers = [int(l.split()[1]) for l in lines[:-1]]
    assert numbers == list(range(numbers[0], total))     # none lost or doubled
    job.state = Job.DONE


def test_a_documents_notice_about_its_run_offers_the_runs_next_steps(shell):
    owner = shell.open_document("plate")
    job = shell.run_job(JobSpec("Owned run", [sys.executable, "-c", "print(1)"]),
                        owner=owner)
    _finish(shell, job)
    shell.notify("Finished", "It finished.", owner=owner)
    toast = shell.toasts.toasts[-1]
    links_text = [w.cget("text") for w in toast.winfo_children()[-1].winfo_children()]
    assert "Show the log" in links_text
