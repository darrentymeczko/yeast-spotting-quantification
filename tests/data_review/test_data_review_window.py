"""The window, alone and as a workbench tab, on a small real photo folder."""

from __future__ import annotations

import time
import tkinter as tk

import pytest

from data_review import flags as ff
from data_review.app import DataReviewApp, flags_path_for


def pump(root, until, timeout=30.0):
    """Run the event loop until `until()` holds -- the window loads on threads."""
    end = time.monotonic() + timeout
    while not until():
        root.update()
        time.sleep(0.02)
        if time.monotonic() > end:
            raise AssertionError("timed out waiting for the window")


@pytest.fixture
def window(tk_root, project, monkeypatch, tmp_path):
    from data_review import spots

    # Nothing is detected: the cache is an empty folder of this test's own.
    monkeypatch.setattr(spots, "cache_dir", lambda: tmp_path / "cache")
    path, _ = project
    top = tk.Toplevel(tk_root)
    app = DataReviewApp(top, flags_path_for(path))
    pump(top, lambda: app.ctl.loaded and app.ctl.layout is not None
         and app.view.pixels is not None)
    yield app, path
    if top.winfo_exists():
        app.ctl._saved = app.ctl.flags.to_dict()    # nothing to ask about
        app.host.close()


def test_either_file_opens_the_same_review(project):
    path, _ = project
    assert flags_path_for(path) == ff.sidecar_for(path)
    assert flags_path_for(ff.sidecar_for(path)) == ff.sidecar_for(path)


def test_it_opens_on_the_first_photo_and_says_spots_are_not_located(window):
    app, _ = window
    assert len(app.ctl.photos) == 7
    assert app.ctl.current is app.ctl.photos[0]
    assert "not located" in app.banner.cget("text")
    assert "spots located on 0 of 7" in app.counts_label.cget("text")
    assert len(app._list_ids) == 7
    status = app.tree.set(app._list_ids[app.ctl.photos[0].relpath], "status")
    assert status == "not located"


def test_flag_the_plate_pass_the_next_and_save(window):
    app, path = window
    first = app.ctl.current
    app._toggle_plate()
    app.update()
    assert app.ctl.flags.plate_reason(first.relpath) == "smeared or wet plate"
    assert "Whole plate flagged" in app.banner.cget("text")
    assert app.tree.set(app._list_ids[first.relpath], "status") == "⚑ plate"
    app._step(1)
    second = app.ctl.current
    app._looks_good()
    assert app.ctl.flags.is_reviewed(second.relpath)
    assert app.ctl.current is app.ctl.photos[2]
    assert app._save()
    saved = ff.load(ff.sidecar_for(path))
    assert saved.plate_reason(first.relpath) and second.relpath in saved.reviewed
    app._undo()
    app._undo()
    assert not app.ctl.flags.plate_reason(first.relpath)


def test_clicking_a_spot_flags_it_with_the_chosen_reason(window):
    app, _ = window
    app.spot_reason.set("bubble or debris")
    app._clicked((2, 3))
    app.update()
    rel = app.ctl.current.relpath
    assert app.ctl.flags.spot_reason(rel, 3, 4) == "bubble or debris"
    assert app.flag_list.size() == 1
    app._clicked((2, 3))
    assert not app.ctl.flags.spot_reason(rel, 3, 4)


def test_closing_with_unsaved_flags_asks(window, monkeypatch):
    from tkinter import messagebox

    app, _ = window
    app.ctl.set_plate("smeared")
    monkeypatch.setattr(messagebox, "askyesnocancel", lambda *a, **k: None)
    assert app._can_close() is False
    monkeypatch.setattr(messagebox, "askyesnocancel", lambda *a, **k: False)
    assert app._can_close() is True


def test_detection_is_offered_as_a_job_through_the_host(window, monkeypatch):
    from tkinter import messagebox

    app, path = window
    started = []

    class Job:
        def poll(self):
            return 0

    monkeypatch.setattr(messagebox, "askokcancel", lambda *a, **k: True)
    monkeypatch.setattr(app.host, "run_job",
                        lambda spec: started.append(spec) or Job())
    app._detect()
    (spec,) = started
    assert spec.argv[-3:] == ["data-review-cli", "detect", str(path.resolve())]
    assert spec.kind == "experiment-run"       # queues with measurement runs


# --- in the workbench ------------------------------------------------------------


def test_the_workbench_knows_a_data_review_when_it_sees_one(project):
    from workbench.registry import BY_KEY, data_review_path, kind_of
    from workbench.shell import Shell

    path, _ = project
    side = data_review_path(path)
    ff.save(side, ff.DataFlags())
    assert kind_of(side) == "data-review"
    assert kind_of(path) == "experiment"
    assert Shell.display_name("data-review", side) == "Synthetic"
    assert BY_KEY["data-review"].step == 3


def test_it_opens_as_a_tab_beside_its_experiment(tk_root, project, tmp_path,
                                                 monkeypatch):
    from data_review import spots
    from workbench.settings import Settings
    from workbench.shell import Shell

    monkeypatch.setattr(spots, "cache_dir", lambda: tmp_path / "cache")
    path, _ = project
    top = tk.Toplevel(tk_root)
    top.withdraw()
    shell = Shell(top, Settings(tmp_path / "workbench.json"), restore=False)
    try:
        experiment = shell.open_document("experiment", path)
        review = shell.open_document("data-review", ff.sidecar_for(path))
        assert None not in (experiment, review)
        assert shell.documents == [experiment, review]
        # One file, one tab: asking again brings the same one forward.
        assert shell.open_document("data-review", ff.sidecar_for(path)) is review
        labels = [shell.new_menu.entrycget(i, "label")
                  for i in range(shell.new_menu.index("end") + 1)]
        assert "Review data" not in labels
    finally:
        for doc in shell.documents:
            app = getattr(doc.app, "ctl", None)
            if app is not None:
                app._saved = app.flags.to_dict()
        shell.exit(force=True)
