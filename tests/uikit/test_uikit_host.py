"""The standalone host: a tool alone in its window, exactly as before.

Each tool builds into a host instead of owning its window. `StandaloneHost`
is what they get when they run on their own, and it has to reproduce the old
behaviour precisely -- the window's own menubar, title, close button asking
first, and a run opening a console of its own.
"""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk
from pathlib import Path

import pytest

from uikit import host as host_module
from uikit.host import ConsoleJob, JobSpec, StandaloneHost, as_host


@pytest.fixture
def window(tk_root):
    win = tk.Toplevel(tk_root)
    win.withdraw()
    yield win
    try:
        win.destroy()
    except tk.TclError:
        pass


def test_menus_go_on_the_windows_own_menubar(window):
    host = StandaloneHost(window, "Tool")
    file_menu = host.add_menu("File")
    host.add_menu("Help")
    menubar = window.nametowidget(window.cget("menu"))
    assert [menubar.entrycget(i, "label") for i in (1, 2)] == ["File", "Help"]
    assert menubar.entrycget(1, "menu") == str(file_menu)


def test_the_title_is_the_windows(window):
    host = StandaloneHost(window, "Tool")
    host.set_title("*e.json - Tool", tab="e")
    assert window.title() == "*e.json - Tool"


def test_the_frame_fills_the_window(window):
    host = StandaloneHost(window)
    assert host.frame.master is window
    assert host.frame.winfo_manager() == "pack"


def test_a_veto_keeps_the_window_open(window):
    host = StandaloneHost(window)
    disposed = []
    host.on_close(lambda: False)
    host.on_dispose(lambda: disposed.append(True))
    assert host.close() is False
    assert window.winfo_exists()
    assert disposed == []


def test_closing_disposes_once_then_destroys(window):
    host = StandaloneHost(window)
    disposed = []
    host.on_close(lambda: True)
    host.on_dispose(lambda: disposed.append(True))
    assert host.close() is True
    assert host.close() is True                   # a second close is harmless
    assert disposed == [True]
    assert not window.winfo_exists()


def test_the_close_button_goes_through_the_veto(window):
    host = StandaloneHost(window)
    host.on_close(lambda: False)
    window.tk.call(window.wm_protocol("WM_DELETE_WINDOW"))
    assert window.winfo_exists()


def test_shortcuts_are_bound_to_the_window_not_the_process(window, tk_root):
    host = StandaloneHost(window)
    host.bind_key("<Control-F12>", lambda _e: "break")
    assert window.bind("<Control-F12>")
    assert not tk_root.bind_all("<Control-F12>")


def test_a_standalone_host_never_opens_documents_itself(window):
    assert StandaloneHost(window).open_document("plate") is False


def test_as_host_wraps_a_window_and_passes_a_host_through(window):
    host = as_host(window, "Tool")
    assert isinstance(host, StandaloneHost)
    assert as_host(host) is host
    with pytest.raises(TypeError):
        as_host(object())


# --- a run in a console of its own ---------------------------------------------


def test_a_job_gets_a_console_and_a_status_file(monkeypatch):
    started = {}

    class Process:
        def poll(self):
            return None

    def popen(argv, **kwargs):
        started.update(argv=argv, **kwargs)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    spec = JobSpec("run", ["python", "x.py"], cwd="here", env={"A": "1"},
                   console_env={"SPOTTING_NEW_CONSOLE": "1"},
                   new_console=True, status_env="RUN_STATUS")
    job = host_module.start_console_job(spec)
    try:
        assert started["argv"] == ["python", "x.py"]
        assert started["cwd"] == "here"
        env = started["env"]
        assert env["A"] == "1" and env["SPOTTING_NEW_CONSOLE"] == "1"
        assert Path(env["RUN_STATUS"]).exists()
        assert started["stdout"] is subprocess.DEVNULL
        if sys.platform == "win32":
            assert started["creationflags"] == (
                subprocess.CREATE_NEW_CONSOLE
                | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    finally:
        job.discard()


def test_the_status_file_is_believed_before_the_process(tmp_path):
    """The console stays open for Enter after the run; the status file says
    when it actually finished."""

    class StillOpen:
        def poll(self):
            return None

    status = tmp_path / "status.txt"
    job = ConsoleJob("run", StillOpen(), status)
    assert job.poll() is None
    status.write_text("3", encoding="utf-8")
    assert job.poll() == 3
    assert not status.exists()
    assert job.poll() == 3                        # remembered, not re-read


def test_the_process_exiting_is_the_fallback(tmp_path):
    class Died:
        def poll(self):
            return 7

    job = ConsoleJob("run", Died(), tmp_path / "never-written.txt")
    assert job.poll() == 7
