"""Where the review's slow work runs: processes of its own.

Rebuilding a medium, redrawing its graph and exporting all compute -- pandas,
the statistics, matplotlib, PIL -- and on a thread of the window's own process
that computing held the whole window up, every tab of the workbench with it
(`uikit.tasks` says why). So each runs in a worker process instead, while the
window's thread only waits for the answer:

    engine   rebuilds, graph redraws and exports. Kept between calls, so the
             measurements it has read stay cached for the next rebuild. A
             second process starts only if a call comes while it is busy --
             another review tab exporting, say.
    stats    a strain summary's p-values, so they never queue behind an
             export.

Each function here has the signature of the one it stands in for.
"""

from __future__ import annotations

from uikit import tasks

#: Imported by `warm`, so the first strain summary finds them loaded.
STATS_MODULES = ("results_review.rebuild", "spotting_plots")


def engine() -> tasks.Worker:
    return tasks.worker("review", processes=2)


def stats() -> tasks.Worker:
    return tasks.worker("review-stats")


def rebuild(*args, **kwargs):
    from . import rebuild as rb

    return engine().call(rb.rebuild, *args, **kwargs)


def draw_preview(*args, **kwargs):
    from . import export as ex

    return engine().call(ex.draw_preview, *args, **kwargs)


def export_review(*args, **kwargs):
    from . import export as ex

    return engine().call(ex.export_review, *args, **kwargs)


def summarize(*args, **kwargs):
    from . import rebuild as rb

    return stats().call(rb.summarize, *args, **kwargs)


def warm() -> None:
    """Start the statistics process now, without waiting for it: the first
    p-values are wanted only once the first rebuild has landed. (The engine
    is not warmed: the window's first rebuild follows at once and, arriving
    while a warm-up was still importing, would start a second process.)"""
    stats().warm(*STATS_MODULES)


def release() -> None:
    """Let go of everything the review holds: its processes, and with them
    every measurement they cached. For when the last review closes."""
    tasks.shutdown("review", "review-stats")
    from . import rebuild as rb

    rb.release_memory()
