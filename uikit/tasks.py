"""Doing slow work without freezing the window.

    run_in_thread(widget, work, done)
        `work()` on a background thread; `done(result, error)` back on the
        UI thread once it has finished. For work that WAITS -- reading files,
        a worker process -- not for work that computes: see `worker`.

    worker(name).call(fn, *args, **kwargs)
        `fn(*args, **kwargs)` in a process of its own, kept warm between
        calls; for computing -- rebuilding numbers, drawing figures,
        exporting. Called from a `run_in_thread` work function.

    favour_the_window()
        Once, at startup: the window's thread gets the interpreter back
        quickly from any other thread that is busy.

    with capture_output() as text:
        Collect what THIS thread prints. `contextlib.redirect_stdout` swaps
        `sys.stdout` for the whole process, so in a window running several
        tools at once it would also swallow everything every other thread
        printed meanwhile. This routes by thread instead.

Why a thread is not enough for computing: every Tk call the window makes
needs Python's interpreter lock, and a thread crunching numbers holds it for
the whole switch interval at a time. Measured here, one busy thread turned a
30 ms redraw into three seconds -- the whole window lagging while a graph was
drawn. A process has an interpreter lock of its own.

Long runs with output of their own belong in a subprocess (`host.run_job`).
"""

from __future__ import annotations

import io
import itertools
import os
import pickle
import sys
import threading
import weakref
from contextlib import contextmanager
from typing import Callable


def run_in_thread(widget, work: Callable[[], object],
                  done: Callable[[object, BaseException | None], None],
                  *, poll_ms: int = 50) -> threading.Thread:
    box: dict = {}

    def go() -> None:
        try:
            box["result"] = work()
        except BaseException as exc:          # handed back, never lost
            box["error"] = exc

    thread = threading.Thread(target=go, daemon=True)
    thread.start()

    def check() -> None:
        if thread.is_alive():
            widget.after(poll_ms, check)
            return
        try:
            if not widget.winfo_exists():
                return                         # the window went away meanwhile
        except Exception:
            return
        done(box.get("result"), box.get("error"))

    widget.after(poll_ms, check)
    return thread


class _ThreadRouter:
    """Stands in for sys.stdout / sys.stderr, sending a write to the buffer
    of the thread that made it, if it is capturing, and on as usual if not."""

    def __init__(self, fallback) -> None:
        self.fallback = fallback
        self.buffers: dict[int, io.StringIO] = {}

    def write(self, text: str) -> int:
        buf = self.buffers.get(threading.get_ident())
        if buf is not None:
            return buf.write(text)
        if self.fallback is None:
            return len(text)
        return self.fallback.write(text)

    def flush(self) -> None:
        if threading.get_ident() not in self.buffers and self.fallback is not None:
            self.fallback.flush()

    def __getattr__(self, name):
        # encoding, isatty, fileno, reconfigure (which the pipeline calls) ...
        return getattr(self.fallback, name)


_lock = threading.Lock()


def _router(name: str) -> _ThreadRouter:
    with _lock:
        current = getattr(sys, name)
        if not isinstance(current, _ThreadRouter):
            current = _ThreadRouter(current)
            setattr(sys, name, current)
        return current


@contextmanager
def capture_output():
    """Everything this thread prints, to stdout and stderr, as one string."""
    buf = io.StringIO()
    me = threading.get_ident()
    routers = [_router("stdout"), _router("stderr")]
    for router in routers:
        router.buffers[me] = buf
    try:
        yield buf
    finally:
        for router in routers:
            router.buffers.pop(me, None)


# ---------------------------------------------------------------------------
# The window's thread first
# ---------------------------------------------------------------------------

#: Seconds a thread may keep the interpreter while another is waiting for it.
#: Python's default is 5 ms, and the window waits that long for EVERY Tk call
#: it makes while any other thread is busy. Below a millisecond it is
#: invisible; it costs nothing while the window is idle, because the switch
#: is only forced when somebody is waiting.
SWITCH_INTERVAL = 0.0005


def favour_the_window() -> None:
    """Call once, from a program that has a window."""
    sys.setswitchinterval(min(sys.getswitchinterval(), SWITCH_INTERVAL))


# ---------------------------------------------------------------------------
# Heavy work in a process of its own
# ---------------------------------------------------------------------------

#: "thread" runs every worker's calls on the calling thread instead -- the
#: test suite does, so that tests can stub what a worker calls.
MODE_ENV = "SPOTTING_WORKERS"

#: Windows' BELOW_NORMAL_PRIORITY_CLASS. Processes a worker starts in turn
#: (a pool drawing montages) inherit it.
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

_PROGRESS_QUEUE = None          # in a worker process: where progress goes


def lower_priority() -> None:
    """Give way to the window: this process, below normal priority."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(),
                                      BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(5)
    except Exception:                                # pragma: no cover
        pass


def _worker_started(progress_queue) -> None:
    """The first thing each worker process does."""
    global _PROGRESS_QUEUE
    _PROGRESS_QUEUE = progress_queue
    # Started from a pythonw window there is no stdout; a stray print in
    # the work must not crash it.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    os.environ.setdefault("MPLBACKEND", "Agg")
    lower_priority()


class _Relay:
    """`progress` as the work sees it in the worker process: each message
    goes back to the window's process, to the call that asked for it."""

    def __init__(self, token: int) -> None:
        self.token = token

    def __call__(self, message) -> None:
        if _PROGRESS_QUEUE is not None:
            try:
                _PROGRESS_QUEUE.put((self.token, message))
            except Exception:                        # pragma: no cover
                pass


def _run_pickled(blob: bytes):
    fn, args, kwargs = pickle.loads(blob)
    return fn(*args, **kwargs)


def _import(*modules: str) -> None:
    import importlib

    for name in modules:
        try:
            importlib.import_module(name)
        except Exception:                            # pragma: no cover
            pass


class WorkerLost(RuntimeError):
    """The worker process ended in the middle of a call."""


class Worker:
    """Heavy work in a process of its own, so the window never waits for it.

        w = worker("review")                  # shared by name; starts on first use
        frame = w.call(rebuild, root, ...)    # blocks the CALLING thread only
        w.call(export, ..., progress=say)     # progress relayed back as it comes
        w.shutdown()                          # ends it, freeing its memory

    Call it from a background thread (`run_in_thread`), never from the UI
    thread: it waits for the answer. While it waits it holds nothing the
    window needs.

    The process is kept between calls, so whatever the work keeps -- the
    modules it imported, measurements it cached -- is still there next
    time. It runs below normal priority: the window always gets the
    processor first. `processes` > 1 lets that many calls run at once; a
    second process is only started when a call arrives while the first is
    busy. `idle_s` ends the process after that long with nothing to do.

    `fn` must be importable by name (a module-level function), and its
    arguments and result picklable. Anything else -- and any machine where
    processes cannot be started, as some managed ones refuse -- is run on
    the calling thread instead, as all of it used to be.
    """

    def __init__(self, name: str, processes: int = 1,
                 idle_s: float | None = None) -> None:
        self.name = name
        self.processes = max(1, int(processes))
        self.idle_s = idle_s
        self.in_thread = os.environ.get(MODE_ENV, "").strip().lower() == "thread"
        self._lock = threading.Lock()
        self._pool = None
        self._queue = None
        self._busy = 0
        self._idle_timer: threading.Timer | None = None
        self._worked = False        # a call has come back from a process
        self._tokens = itertools.count(1)
        self._listeners: dict[int, Callable] = {}
        self._relay_lock = threading.Lock()
        self._warned: set[str] = set()

    # -- calling ---------------------------------------------------------------

    def call(self, fn: Callable, /, *args, progress: Callable | None = None,
             **kwargs):
        """`fn(*args, **kwargs)` in the worker process, and its result.

        `progress`, if given, is handed to `fn` as its `progress` argument;
        what `fn` reports through it is passed to `progress` here, on a
        thread of the worker's own, until the call returns.
        """
        if progress is not None:
            kwargs["progress"] = progress
        if self.in_thread:
            return fn(*args, **kwargs)
        token = 0
        sent = kwargs
        if progress is not None:
            token = next(self._tokens)
            sent = dict(kwargs, progress=_Relay(token))
        try:
            blob = pickle.dumps((fn, args, sent), protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            self._warn(fn, f"cannot be sent to a process ({type(exc).__name__}: "
                           f"{exc}); running it here instead")
            return fn(*args, **kwargs)

        from concurrent.futures import CancelledError
        from concurrent.futures.process import BrokenProcessPool

        if token:
            with self._relay_lock:
                self._listeners[token] = progress
        # Busy before the pool is even looked up, so the idle timer can never
        # end the pool this call is about to use.
        self._enter()
        try:
            try:
                pool = self._start()
            except Exception as exc:
                # Processes are blocked here: do the work as it always was.
                self.in_thread = True
                self._warn(fn, f"no worker process ({type(exc).__name__}: "
                               f"{exc}); running on a thread instead")
                return fn(*args, **kwargs)
            try:
                result = pool.submit(_run_pickled, blob).result()
            except (BrokenProcessPool, CancelledError, RuntimeError) as exc:
                # RuntimeError too: submitting to a pool `shutdown` has just
                # ended says so with one.
                if pool in _ENDED:
                    raise WorkerLost("stopped: the work was abandoned") from exc
                if not isinstance(exc, (BrokenProcessPool, CancelledError)):
                    raise                     # the work's own error
                self._discard(pool)
                if not self._worked:
                    # It never managed a single call: this machine will not
                    # let it run, rather than one call having killed it.
                    self.in_thread = True
                    self._warn(fn, "the worker process could not run; running "
                                   "on a thread instead")
                    return fn(*args, **kwargs)
                raise WorkerLost("the background process stopped before it "
                                 "finished; try again") from exc
            self._worked = True
            return result
        finally:
            if token:
                # Under the relay's lock: once this returns, nothing more is
                # reported for this call -- a late message can never land
                # after the caller has said it is done.
                with self._relay_lock:
                    self._listeners.pop(token, None)
            self._leave()

    def warm(self, *modules: str) -> None:
        """Start the process now, importing `modules`, without waiting for
        it: the first real call then finds it ready. Only for a worker whose
        first call is a while off -- one arriving while this is still
        importing would start a second process."""
        if self.in_thread:
            return

        def started(future) -> None:
            if not future.cancelled() and future.exception() is None:
                self._worked = True

        def go() -> None:
            try:
                self._start().submit(_import, *modules).add_done_callback(started)
            except Exception:
                pass

        threading.Thread(target=go, daemon=True,
                         name=f"worker-warm-{self.name}").start()

    # -- the process -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._pool is not None

    def _start(self):
        with self._lock:
            if self._pool is None:
                import multiprocessing
                from concurrent.futures import ProcessPoolExecutor

                context = multiprocessing.get_context("spawn")
                progress_queue = context.Queue()
                self._pool = ProcessPoolExecutor(
                    max_workers=self.processes, mp_context=context,
                    initializer=_worker_started, initargs=(progress_queue,))
                self._queue = progress_queue
                threading.Thread(target=self._relay, args=(progress_queue,),
                                 daemon=True,
                                 name=f"worker-progress-{self.name}").start()
            return self._pool

    def _relay(self, progress_queue) -> None:
        while True:
            try:
                item = progress_queue.get()
            except Exception:
                return
            if item is None:
                return
            token, message = item
            with self._relay_lock:
                listener = self._listeners.get(token)
                if listener is not None:
                    try:
                        listener(message)
                    except Exception:            # pragma: no cover
                        pass

    def _enter(self) -> None:
        with self._lock:
            self._busy += 1
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _leave(self) -> None:
        with self._lock:
            self._busy -= 1
            if self._busy or self.idle_s is None or self._pool is None:
                return
            timer = threading.Timer(self.idle_s, self._idle)
            timer.daemon = True
            self._idle_timer = timer
        timer.start()

    def _idle(self) -> None:
        with self._lock:
            if self._busy or self._pool is None:
                return
            pool, queue_ = self._pool, self._queue
            self._pool = self._queue = self._idle_timer = None
        _end(pool, queue_)

    def _discard(self, pool) -> None:
        with self._lock:
            if self._pool is not pool:
                return
            queue_, self._pool, self._queue = self._queue, None, None
        _end(pool, queue_)

    def shutdown(self) -> None:
        """End the process now, abandoning anything it is in the middle of,
        and free what it held. The next call starts a fresh one."""
        with self._lock:
            pool, queue_ = self._pool, self._queue
            self._pool = self._queue = None
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
        if pool is not None:
            _end(pool, queue_)

    def _warn(self, fn, text: str) -> None:
        name = getattr(fn, "__qualname__", repr(fn))
        if name in self._warned:
            return
        self._warned.add(name)
        try:
            print(f"[{self.name} worker] {name}: {text}", file=sys.stderr)
        except Exception:                            # pragma: no cover
            pass


#: Pools ended on purpose: a call cut short by one is abandoned, not retried.
_ENDED: "weakref.WeakSet" = weakref.WeakSet()


def _end(pool, progress_queue) -> None:
    _ENDED.add(pool)
    processes = list((getattr(pool, "_processes", None) or {}).values())
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:                                # pragma: no cover
        pass
    for process in processes:
        try:
            process.terminate()
        except Exception:                            # pragma: no cover
            pass
    if progress_queue is not None:
        try:
            progress_queue.put(None)                 # ends the relay thread
        except Exception:                            # pragma: no cover
            pass


_workers: dict[str, Worker] = {}
_workers_lock = threading.Lock()


def worker(name: str, processes: int = 1, idle_s: float | None = None) -> Worker:
    """The worker called `name`, made on first use. Tools share one by
    using the same name: two review tabs share one warm process."""
    with _workers_lock:
        got = _workers.get(name)
        if got is None:
            got = _workers[name] = Worker(name, processes, idle_s)
        return got


def shutdown(*names: str) -> None:
    """End the named workers' processes; with no names, every worker's."""
    with _workers_lock:
        chosen = [w for n, w in _workers.items() if not names or n in names]
    for w in chosen:
        w.shutdown()
