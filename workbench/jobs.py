"""Long runs -- measuring an experiment, re-exporting reviews -- inside the window.

Standalone, a run gets a console window of its own. In the workbench it runs
the same command as a separate process (so a crash in the measurement code
cannot take the window down, and its worker processes work as they always
have), but with its output piped back here: shown live in a log tab, parsed
for progress, kept in `.workbench/logs/`, and reported when it ends.

    StreamSplitter   bytes-as-text -> committed lines and the live line
    Job              one run: its state, output, progress and result folder
    JobRunner        starts, reads, queues and stops jobs

The pipeline reports progress the console way, rewriting one line in place:
`print(f"\\r    {done}/{total}", end="")`. The splitter turns a carriage return
into "replace the live line", which is what a console does with it, so the
log reads as the console would have.
"""

from __future__ import annotations

import codecs
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from uikit.host import JobSpec

from .settings import ROOT

LOG_DIR = ROOT / ".workbench" / "logs"

#: Lines of output kept in memory per job; the log file keeps everything.
MAX_LINES = 20000

#: Environment that only makes sense for a console of its own. A piped job
#: must not get it: SPOTTING_NEW_CONSOLE would make the child open a visible
#: console after all, and the pauses would wait for an Enter nobody can press.
CONSOLE_ONLY_ENV = ("SPOTTING_NEW_CONSOLE", "EXPERIMENT_GUI_RUN_PAUSE",
                    "SPOTTING_PAUSE", "EXPERIMENT_GUI_RUN_STATUS")

_PROGRESS = re.compile(r"(\d+)\s*/\s*(\d+)")
_COUNTER_LINE = re.compile(r"^\s*\[?(\d+)\s*/\s*(\d+)(?:\]|\s|$)")
_RESULTS = re.compile(r"^\s*results folder:\s*(.+?)\s*$", re.IGNORECASE)


class StreamSplitter:
    """Decoded output in, events out: ("line", text) for a finished line and
    ("live", text) for the line still being written (or rewritten).

    A carriage return starts the live line over, as on a console. One at the
    very end of a chunk is held back: on Windows a newline arrives as "\\r\\n",
    and the two halves can land in different reads.
    """

    def __init__(self) -> None:
        self._line = ""
        self._held_cr = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        # Whole pieces at a time, not character by character: this runs on a
        # thread of the window's own process, and a Python loop over every
        # character of a busy run's output was time the window waited for.
        events: list[tuple[str, str]] = []
        if self._held_cr:
            text = "\r" + text
            self._held_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            self._held_cr = True
        pieces = text.replace("\r\n", "\n").split("\n")
        live_changed = False
        last = len(pieces) - 1
        for i, piece in enumerate(pieces):
            if "\r" in piece:
                # Each carriage return starts the line over: only what
                # follows the last one is left.
                self._line = piece.rsplit("\r", 1)[1]
                live_changed = True
            elif piece:
                self._line += piece
                live_changed = True
            if i < last:
                events.append(("line", self._line))
                self._line = ""
                live_changed = False
        if live_changed:
            events.append(("live", self._line))
        return events

    def flush(self) -> list[tuple[str, str]]:
        """At the end of the output: whatever is left is a line."""
        self._held_cr = False
        if self._line:
            line, self._line = self._line, ""
            return [("line", line)]
        return []


class Job:
    """One run. A `uikit.host.JobHandle`: `poll()` and `stop()`."""

    QUEUED, RUNNING, DONE, FAILED, STOPPED = (
        "queued", "running", "done", "failed", "stopped")

    def __init__(self, runner: "JobRunner", spec: JobSpec, owner=None) -> None:
        self.runner = runner
        self.spec = spec
        self.title = spec.title
        self.kind = spec.kind
        self.owner = owner
        self.state = self.QUEUED
        self.returncode: int | None = None
        self.created = time.time()
        self.started: float | None = None
        self.ended: float | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LINES)
        #: Lines committed so far, including any `lines` has since dropped:
        #: how a log view tells what is new without copying them all.
        self.committed = 0
        self.live = ""
        #: (done, total) from the latest counter seen, if any.
        self.progress: tuple[int, int] | None = None
        #: The last thing it said that was not just a counter.
        self.phase = ""
        self.result_dir: Path | None = None
        self.log_path: Path | None = None
        self.process: subprocess.Popen | None = None
        self.stop_requested = False
        self.error: str = ""

    # -- JobHandle -------------------------------------------------------------

    def poll(self) -> int | None:
        return self.returncode if self.finished else None

    def stop(self) -> None:
        self.runner.stop(self)

    # -- state -----------------------------------------------------------------

    @property
    def finished(self) -> bool:
        return self.state in (self.DONE, self.FAILED, self.STOPPED)

    @property
    def active(self) -> bool:
        return self.state in (self.QUEUED, self.RUNNING)

    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        return (self.ended or time.time()) - self.started

    def describe(self) -> str:
        """One short line of where it has got to."""
        if self.state == self.QUEUED:
            return "waiting for the run before it"
        if self.state == self.RUNNING:
            if self.progress is not None:
                done, total = self.progress
                return f"{done}/{total}  ·  {format_elapsed(self.elapsed())}"
            return f"running  ·  {format_elapsed(self.elapsed())}"
        if self.state == self.DONE:
            return f"finished in {format_elapsed(self.elapsed())}"
        if self.state == self.STOPPED:
            return "stopped"
        return f"failed (exit code {self.returncode})" if not self.error else self.error

    # -- what the output says ----------------------------------------------------

    def _take(self, kind: str, text: str) -> None:
        if kind == "line":
            self.lines.append(text)
            self.committed += 1
            self.live = ""
            self._read(text, committed=True)
        else:
            self.live = text
            self._read(text, committed=False)

    def _read(self, text: str, *, committed: bool) -> None:
        found = _RESULTS.match(text)
        if found and committed:
            self.result_dir = Path(found.group(1))
            return
        # Only the counter line itself ("    12/96", optionally followed by a
        # note) is progress; "n/m" inside a sentence is not.
        counter = _COUNTER_LINE.match(text)
        if counter:
            done, total = int(counter.group(1)), int(counter.group(2))
            if 0 < total and 0 <= done <= total:
                self.progress = (done, total)
        stripped = text.strip()
        if committed and stripped and not _PROGRESS.fullmatch(stripped):
            self.phase = stripped


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def job_argv(argv: list) -> list:
    """The command for a piped job: python.exe rather than pythonw.exe, which
    has no stdout to pipe."""
    argv = [str(a) for a in argv]
    if argv:
        exe = Path(argv[0])
        if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
            argv[0] = str(exe.parent / "python.exe")
    return argv


def job_env(spec: JobSpec) -> dict:
    env = dict(os.environ if spec.env is None else spec.env)
    for name in CONSOLE_ONLY_ENV:
        env.pop(name, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class JobRunner:
    """Starts jobs, reads their output on threads, and hands every event to
    the UI thread through one queue, drained by `pump`.

    Experiment runs are taken one at a time: two measuring at once would
    fight over the same processors and the same measurement cache, and each
    would take longer than the two one after the other.
    """

    #: Kinds of which only one runs at a time; the rest queue.
    EXCLUSIVE = ("experiment-run",)

    def __init__(self, on_change: Callable[[Job, set], None],
                 log_dir: Path | None = LOG_DIR) -> None:
        self.on_change = on_change
        self.log_dir = log_dir
        self.jobs: list[Job] = []
        self._events: "queue.Queue[tuple[Job, tuple]]" = queue.Queue()

    # -- starting --------------------------------------------------------------

    def start(self, spec: JobSpec, owner=None) -> Job:
        job = Job(self, spec, owner)
        self.jobs.append(job)
        if not self._must_wait(job):
            self._launch(job)       # raises OSError if it cannot start at all
        self.on_change(job, {"state"})
        return job

    def _must_wait(self, job: Job) -> bool:
        return job.kind in self.EXCLUSIVE and any(
            j is not job and j.kind == job.kind and j.state == Job.RUNNING
            for j in self.jobs)

    def _launch(self, job: Job) -> None:
        spec = job.spec
        flags = 0
        if sys.platform == "win32":
            # No console window -- for it, or for the worker processes it
            # starts, which inherit this hidden one instead of each opening
            # their own. A process group of its own, so Stop can end the lot.
            # Below normal priority, which its workers inherit too: a run
            # keeping every core busy must not make the window wait its turn.
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        job.process = subprocess.Popen(
            job_argv(spec.argv), cwd=spec.cwd, env=job_env(spec),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0, close_fds=True,
            creationflags=flags)
        job.state = Job.RUNNING
        job.started = time.time()
        job.log_path = self._log_path(job)
        threading.Thread(target=self._read, args=(job,), daemon=True,
                         name=f"job-reader-{job.title}").start()

    def _log_path(self, job: Job) -> Path | None:
        if self.log_dir is None:
            return None
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", job.title).strip("_")[:60] or "job"
        return self.log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}.log"

    # -- reading (on the job's own thread) ----------------------------------------

    def _read(self, job: Job) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        splitter = StreamSplitter()
        log = None
        if job.log_path is not None:
            try:
                job.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = open(job.log_path, "w", encoding="utf-8", newline="")
                log.write(f"# {job.title}\n# {subprocess.list2cmdline(job_argv(job.spec.argv))}\n\n")
            except OSError:
                log = None
        stream = job.process.stdout
        try:
            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError:
                    break
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if log is not None:
                    log.write(text)
                    log.flush()
                events = splitter.feed(text)
                if events:
                    self._events.put((job, events))
            tail = decoder.decode(b"", final=True)
            events = splitter.feed(tail) + splitter.flush()
            if events:
                self._events.put((job, events))
        finally:
            code = job.process.wait()
            if log is not None:
                log.write(f"\n\n# exit code {code}\n")
                log.close()
            try:
                stream.close()
            except OSError:
                pass
            self._events.put((job, [("exit", code)]))

    # -- on the UI thread ----------------------------------------------------------

    def pump(self, limit: int = 4000) -> None:
        """Apply what the readers have queued: each read's events arrive as
        one batch. Bounded, so a job printing furiously cannot freeze the
        window; the rest waits for the next pump."""
        changed: dict[int, tuple[Job, set]] = {}
        taken = 0
        while taken < limit:
            try:
                job, events = self._events.get_nowait()
            except queue.Empty:
                break
            taken += len(events)
            entry = changed.setdefault(id(job), (job, set()))
            for kind, value in events:
                if kind == "exit":
                    self._finished(job, int(value))
                    entry[1].add("state")
                else:
                    job._take(kind, value)
                    entry[1].add(kind)
        for job, kinds in changed.values():
            self.on_change(job, kinds)

    def _finished(self, job: Job, code: int) -> None:
        job.returncode = code
        job.ended = time.time()
        if job.live:
            job.lines.append(job.live)
            job.live = ""
        if job.stop_requested:
            job.state = Job.STOPPED
        else:
            job.state = Job.DONE if code == 0 else Job.FAILED
        self._start_next(job.kind)

    def _start_next(self, kind: str) -> None:
        for job in self.jobs:
            if job.kind == kind and job.state == Job.QUEUED and not self._must_wait(job):
                try:
                    self._launch(job)
                except OSError as exc:
                    job.state = Job.FAILED
                    job.error = f"could not start: {exc}"
                    job.ended = time.time()
                self.on_change(job, {"state"})
                return

    # -- stopping ----------------------------------------------------------------

    def stop(self, job: Job) -> None:
        if job.finished:
            return
        job.stop_requested = True
        if job.state == Job.QUEUED:
            job.state = Job.STOPPED
            job.ended = time.time()
            self.on_change(job, {"state"})
            return
        kill_tree(job.process)

    def running(self) -> list[Job]:
        return [j for j in self.jobs if j.active]


def kill_tree(process: subprocess.Popen | None) -> None:
    """End a process and everything it started: a run's worker processes
    would otherwise carry on measuring with nobody to report to."""
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
