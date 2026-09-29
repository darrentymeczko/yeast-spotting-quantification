"""Starting up: it must appear, and it must appear quickly.

The bug these exist for: the set chooser was built as a `Toplevel` of a
WITHDRAWN root. Tk propagates withdrawn state to a transient child, so the
dialog never mapped -- it sat at 1x1, invisible -- and `wait_window()` then
blocked on it forever. From the outside that is a launcher that opens a console
and does nothing at all, which is indistinguishable from a hang.

Nothing here opens the real chooser (it would block waiting for a click). They
check the property that was violated, and that the paths taken before a window
is drawn stay cheap.
"""

from __future__ import annotations

import time
import tkinter as tk

import pytest

from results_review import discovery, links


@pytest.fixture
def root(tk_root):
    """A withdrawn window, standing in for the hidden root that caused the bug."""
    w = tk.Toplevel(tk_root)
    w.withdraw()
    w.update_idletasks()
    yield w
    try:
        w.destroy()
    except tk.TclError:
        pass


# --- the bug ---------------------------------------------------------------

def test_a_withdrawn_window_is_never_accepted_as_a_parent(root):
    """The guard that stands between us and the hang."""
    from results_review.app import _is_usable_parent

    assert not _is_usable_parent(root), (
        "a withdrawn root must not be used as a dialog parent: Tk makes the "
        "transient child withdrawn too, and it never appears")
    assert not _is_usable_parent(None)


def test_transient_to_a_withdrawn_root_really_does_hide_the_child(root):
    """Pin the Tk behaviour itself, so the guard is not cargo-culted.

    If a future Tk stops doing this, this test fails and says so, rather than
    the guard quietly protecting against nothing.
    """
    child = tk.Toplevel(root)
    child.transient(root)
    child.update_idletasks()
    hidden = not child.winfo_ismapped()
    child.destroy()
    assert hidden, (
        "Tk no longer hides a transient child of a withdrawn master; the "
        "parent guard in app.choose_set can be revisited")


def test_a_visible_root_is_a_usable_parent(root):
    from results_review.app import _is_usable_parent

    root.deiconify()
    root.update()
    assert _is_usable_parent(root)


# --- speed -----------------------------------------------------------------

def test_listing_every_set_is_cheap():
    """This runs before the window is drawn, so it has to stay fast."""
    t = time.perf_counter()
    sets = discovery.list_sets()
    for p in sets:
        discovery.load_candidates(p / discovery.CANDIDATES_CSV)
    elapsed = time.perf_counter() - t
    assert elapsed < 2.0, (
        f"reading {len(sets)} candidate CSVs took {elapsed:.1f}s; the chooser "
        f"builds this list before anything is on screen")


def test_finding_the_photos_is_cheap():
    """`links.guess` walks OneDrive folders; it runs during window setup."""
    sets = discovery.list_sets()
    if not sets:
        pytest.skip("no results on this machine")
    t = time.perf_counter()
    for p in sets[:5]:
        links.guess(p.name)
    elapsed = time.perf_counter() - t
    assert elapsed < 5.0, (
        f"locating photos for 5 sets took {elapsed:.1f}s before any window "
        f"appears")


def test_importing_the_app_does_not_import_the_engine():
    """Startup must not pay for numpy, pandas and scikit-image.

    Measured at ~0.75 s for `spotting_timecourse` alone, and it is only needed
    once somebody actually edits something.
    """
    import subprocess
    import sys
    from pathlib import Path

    root_dir = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        "sys.path.insert(0, r'%s');"
        "import results_review.app;"
        "print(','.join(m for m in ('numpy','pandas','skimage',"
        "'spotting_timecourse','matplotlib') if m in sys.modules))"
        % root_dir
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    leaked = out.stdout.strip()
    assert not leaked, f"importing the app pulled in {leaked}"


# --- reopening the last set ------------------------------------------------

def test_the_last_set_round_trips(tmp_path, monkeypatch):
    """Double-clicking should land back where you left off."""
    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    assert links.last_set() is None

    results = tmp_path / "Set01"
    results.mkdir()
    (results / "timecourse_candidates.csv").write_text("medium\n",
                                                       encoding="utf-8")
    links.remember_set(results)
    assert links.last_set() == results


def test_a_last_set_that_has_gone_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    links.remember_set(tmp_path / "vanished")
    assert links.last_set() is None, (
        "a remembered folder that is no longer a results folder must fall "
        "back to the chooser, not fail to open")


def test_remembering_a_set_keeps_the_photo_links(tmp_path, monkeypatch):
    """The two things share one file; neither may clobber the other."""
    monkeypatch.setattr(links, "LINKS_FILE", tmp_path / "links.json")
    photos = tmp_path / "photos"
    photos.mkdir()
    links.remember("Set01", photos)
    links.remember_set(tmp_path)
    assert links.remembered("Set01") == photos


def test_an_old_flat_links_file_is_still_read(tmp_path, monkeypatch):
    """Upgrading must not lose folders somebody located by hand."""
    import json

    path = tmp_path / "links.json"
    photos = tmp_path / "photos"
    photos.mkdir()
    path.write_text(json.dumps({"Set01": str(photos)}), encoding="utf-8")
    monkeypatch.setattr(links, "LINKS_FILE", path)
    assert links.remembered("Set01") == photos
