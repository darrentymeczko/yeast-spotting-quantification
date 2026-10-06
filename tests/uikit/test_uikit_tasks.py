"""Slow work off the UI thread, and output captured from one thread only."""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from uikit import tasks
from uikit.tasks import capture_output, run_in_thread


def test_capture_takes_only_this_threads_output(capsys):
    started, release = threading.Event(), threading.Event()

    def other_thread():
        started.set()
        release.wait(5)
        print("from the other thread")

    worker = threading.Thread(target=other_thread)
    worker.start()
    started.wait(5)
    with capture_output() as text:
        print("from this thread")
        release.set()
        worker.join(5)
    assert text.getvalue() == "from this thread\n"
    assert "from the other thread" in capsys.readouterr().out


def test_work_runs_off_the_ui_thread_and_lands_back_on_it(tk_root):
    ui = threading.get_ident()
    seen = {}

    def work():
        seen["worker"] = threading.get_ident()
        return 42

    def done(result, error):
        seen.update(result=result, error=error, done=threading.get_ident())

    run_in_thread(tk_root, work, done, poll_ms=10)
    end = time.time() + 5
    while "done" not in seen and time.time() < end:
        tk_root.update()
        time.sleep(0.01)
    assert seen["result"] == 42 and seen["error"] is None
    assert seen["worker"] != ui and seen["done"] == ui


def test_an_error_in_the_work_is_handed_back(tk_root):
    seen = {}
    run_in_thread(tk_root, lambda: 1 / 0,
                  lambda r, e: seen.update(error=e), poll_ms=10)
    end = time.time() + 5
    while "error" not in seen and time.time() < end:
        tk_root.update()
        time.sleep(0.01)
    assert isinstance(seen["error"], ZeroDivisionError)


def test_the_window_gets_the_interpreter_back_quickly():
    before = sys.getswitchinterval()
    try:
        tasks.favour_the_window()
        assert sys.getswitchinterval() <= tasks.SWITCH_INTERVAL
    finally:
        sys.setswitchinterval(before)


# --- work in a process of its own ----------------------------------------------
#
# What a worker runs has to be importable by name, so it is defined here at
# module level. The worker process imports this module to find it.


def where(value):
    return os.getpid(), threading.get_ident(), value


def counted(n, progress):
    for i in range(n):
        progress(f"step {i}")
    return n


def fails():
    raise LookupError("not in this set")


def priority_class():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetPriorityClass.argtypes = (wintypes.HANDLE,)
    return kernel32.GetPriorityClass(kernel32.GetCurrentProcess())


def sleeps(seconds):
    time.sleep(seconds)
    return seconds


@pytest.fixture
def process_worker(monkeypatch):
    """A real worker process -- the suite otherwise runs workers in-thread."""
    monkeypatch.delenv(tasks.MODE_ENV, raising=False)
    made = []

    def make(**kwargs):
        w = tasks.Worker("test", **kwargs)
        made.append(w)
        return w

    yield make
    for w in made:
        w.shutdown()


def test_the_work_runs_in_another_process_kept_between_calls(process_worker):
    w = process_worker()
    pid, _thread, value = w.call(where, {"a": [1, 2]})
    assert pid != os.getpid() and value == {"a": [1, 2]}
    assert w.call(where, 0)[0] == pid             # the same, still warm


def test_an_error_in_the_process_is_raised_here_as_itself(process_worker):
    w = process_worker()
    with pytest.raises(LookupError, match="not in this set"):
        w.call(fails)
    assert w.call(where, 1)[2] == 1               # and the process lives on


def test_progress_comes_back_in_order_and_stops_with_the_call(process_worker):
    w = process_worker()
    heard = []
    assert w.call(counted, 20, progress=heard.append) == 20
    time.sleep(0.3)                     # anything still in flight would land
    assert heard == [f"step {i}" for i in range(len(heard))]
    assert w._listeners == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_the_process_gives_way_to_the_window(process_worker):
    w = process_worker()
    assert w.call(priority_class) == tasks.BELOW_NORMAL_PRIORITY_CLASS


def test_what_cannot_be_sent_runs_here_instead(process_worker, capsys):
    w = process_worker()
    pid, thread, _ = w.call(lambda: where(None))
    assert pid == os.getpid() and thread == threading.get_ident()
    assert "running it here instead" in capsys.readouterr().err


def test_in_thread_mode_never_starts_a_process(monkeypatch):
    monkeypatch.setenv(tasks.MODE_ENV, "thread")
    w = tasks.Worker("test")
    heard = []
    assert w.call(counted, 2, progress=heard.append) == 2
    assert heard == ["step 0", "step 1"]
    assert w.call(where, 0)[0] == os.getpid() and not w.running


def test_shutting_down_abandons_the_call_and_frees_the_process(process_worker):
    w = process_worker()
    first = w.call(where, 0)[0]
    errors = []

    def wait():
        try:
            w.call(sleeps, 30)
        except Exception as exc:
            errors.append(exc)

    waiting = threading.Thread(target=wait)
    waiting.start()
    time.sleep(0.5)
    w.shutdown()
    waiting.join(10)
    assert not waiting.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], tasks.WorkerLost)
    assert not w.running
    assert w.call(where, 0)[0] != first           # a fresh one next time


def dies():
    os._exit(3)


def test_a_process_that_dies_mid_call_is_reported_and_replaced(process_worker):
    w = process_worker()
    first = w.call(where, 0)[0]
    with pytest.raises(tasks.WorkerLost):
        w.call(dies)                  # not retried here: it would kill us too
    assert w.call(where, 0)[0] not in (first, os.getpid())


def test_an_idle_worker_lets_its_process_go(process_worker):
    w = process_worker(idle_s=0.5)
    w.call(where, 0)
    assert w.running
    end = time.time() + 10
    while w.running and time.time() < end:
        time.sleep(0.1)
    assert not w.running
