"""Owns the review and every change to it.

Undo is a stack of whole-`Review` snapshots rather than inverse commands, for
the same reason `plate_template.gui.controller` does it: a review is a handful
of small dicts, so a snapshot costs nothing, while correct inverses for "set a
raw value", "clear an exclusion back to the pipeline's opinion" and "choose a
different candidate" would be three more chances to get an edge case subtly
wrong. Snapshots go through `to_dict`/`from_dict`, so the serialiser is
exercised on every single edit and a round-trip bug shows up in seconds of use
rather than on the next file load.

Deliberately free of tkinter. Everything here is testable without a display,
and rebuilding -- the one slow part -- is left to the caller to put on a thread.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .. import links, review as rv
from ..discovery import SetRun
from ..model import Candidate, Review, SpotEdit
from ..rebuild import Frame, RebuildError, rebuild


class UndoStack:
    def __init__(self, limit: int = 200) -> None:
        self.limit = limit
        self._undo: list[tuple[dict, str]] = []
        self._redo: list[tuple[dict, str]] = []

    def push(self, snapshot: dict, label: str) -> None:
        self._undo.append((snapshot, label))
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self, current: dict) -> "tuple[dict, str] | None":
        if not self._undo:
            return None
        snapshot, label = self._undo.pop()
        self._redo.append((current, label))
        return snapshot, label

    def redo(self, current: dict) -> "tuple[dict, str] | None":
        if not self._redo:
            return None
        snapshot, label = self._redo.pop()
        self._undo.append((current, label))
        return snapshot, label

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1][1] if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1][1] if self._redo else ""

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()


class ReviewController:
    def __init__(self, run: SetRun, review: "Review | None" = None,
                 on_change: "Callable[[], None] | None" = None) -> None:
        self.run = run
        self.review = rv.with_defaults(run, review or Review())
        self.on_change = on_change
        self.undo_stack = UndoStack()
        self._saved = self.review.to_dict()
        self.last_change = ""
        self._frames: dict[str, Frame] = {}
        self._errors: dict[str, str] = {}
        self.medium: str = (run.media[0] if run.media else "")

        self.capture_root: "Path | None" = links.resolve(
            run.label, self.review.capture_root, run.results_dir)
        self.cfg: "dict | None" = None
        self.config_error: str = ""
        self._load_cfg()

    # -- plumbing ------------------------------------------------------------

    def snapshot(self) -> dict:
        return self.review.to_dict()

    def _commit(self, before: dict, label: str, *, invalidate: str = "") -> bool:
        """Record the change if it actually changed anything."""
        if self.review.to_dict() == before:
            return False
        self.undo_stack.push(before, label)
        if invalidate:
            self._frames.pop(invalidate, None)
            self._errors.pop(invalidate, None)
        self.last_change = label
        self._notify()
        return True

    def _notify(self) -> None:
        if self.on_change:
            self.on_change()

    @property
    def dirty(self) -> bool:
        return self.review.to_dict() != self._saved

    # -- the capture tree ----------------------------------------------------

    def _load_cfg(self) -> None:
        """The strain panel, from the run that produced these results if it said.

        A folder produced through the experiment layer records the exact config
        the run used, and its photo folder need not contain a
        `timecourse_config.json` at all -- that file is written by the console
        pipeline, which is only one of the ways these results can be made. The
        recorded config is preferred whenever it is there, so the review
        normalises against the same control the run did.
        """
        self.cfg, self.config_error = None, ""
        info = getattr(self.run, "experiment", None)
        if info is not None and info.pipeline_config:
            self.cfg = info.pipeline_config
            return
        if self.capture_root is None:
            self.config_error = "no capture folder linked"
            return
        try:
            self.cfg = links.load_config(self.capture_root)
        except Exception as e:
            self.config_error = str(e)

    def link_photos(self, root: Path) -> tuple[bool, str]:
        """Point this set at a capture tree. Returns (ok, message)."""
        from ..rebuild import clear_cache

        root = Path(root)
        if not links.is_capture_tree(root):
            return False, (f"{root.name} does not look like a capture tree "
                           f"(expected timepoint / medium / plate folders).")
        clear_cache()
        self._frames.clear()
        self._errors.clear()
        self.capture_root = root
        self.review.capture_root = str(root)
        self._load_cfg()
        if self.config_error:
            return False, self.config_error
        links.remember(self.run.label, root)
        self._notify()
        return True, f"linked to {root}"

    @property
    def can_edit(self) -> bool:
        return self.capture_root is not None and self.cfg is not None

    # -- picks ---------------------------------------------------------------

    def candidates(self, medium: str = "") -> list[Candidate]:
        return self.run.for_medium(medium or self.medium)

    def chosen(self, medium: str = "") -> "Candidate | None":
        return rv.chosen_candidate(self.run, self.review, medium or self.medium)

    def is_default(self, medium: str = "") -> bool:
        return rv.is_default(self.run, self.review, medium or self.medium)

    def set_medium(self, medium: str) -> None:
        if medium != self.medium:
            self.medium = medium
            self._notify()

    def choose(self, cand: Candidate, reason: str = "") -> bool:
        """Pick a candidate for its medium.

        Edits are kept, not discarded: they are keyed on (replicate, column),
        which means the same spot in a different photo pairing, and silently
        dropping "column 8 replicate 3 is unusable" because the timepoint
        changed would throw away a judgement the person still holds. Anything
        that no longer matches is reported by `rebuild` instead.
        """
        before = self.snapshot()
        self.review.set_pick(cand.medium, cand, reason)
        return self._commit(before, f"Choose {cand.medium} {cand.label}",
                            invalidate=cand.medium)

    def reset_pick(self, medium: str = "") -> bool:
        medium = medium or self.medium
        best = self.run.pipeline_best(medium)
        if best is None:
            return False
        before = self.snapshot()
        self.review.set_pick(medium, best)
        return self._commit(before, f"Reset {medium} to the pipeline's pick",
                            invalidate=medium)

    # -- edits ---------------------------------------------------------------

    def _amend(self, replicate: str, strain_col: int, label: str,
               **changes) -> bool:
        medium = self.medium
        before = self.snapshot()
        self.review.amend(medium, replicate, int(strain_col), **changes)
        return self._commit(before, label, invalidate=medium)

    def set_raw(self, replicate: str, strain_col: int, strain: str,
                value: "float | None") -> bool:
        """Substitute a hand-measured grey value, or clear back to the measured one."""
        if value is None:
            return self._amend(replicate, strain_col,
                               f"Restore measured value for {strain} {replicate}",
                               raw_growth=None)
        return self._amend(replicate, strain_col,
                           f"Set {strain} {replicate} to {value:g}",
                           raw_growth=float(value))

    def set_excluded(self, replicate: str, strain_col: int, strain: str,
                     value: "bool | None") -> bool:
        """Drop or keep a spot. None means "defer to the pipeline again"."""
        if value is None:
            word = "Clear exclusion decision for"
        else:
            word = "Exclude" if value else "Keep"
        return self._amend(replicate, strain_col,
                           f"{word} {strain} {replicate}", excluded=value)

    def set_outlier(self, replicate: str, strain_col: int, strain: str,
                    value: "bool | None") -> bool:
        if value is None:
            word = "Clear outlier decision for"
        else:
            word = "Flag" if value else "Unflag"
        return self._amend(replicate, strain_col,
                           f"{word} {strain} {replicate}", outlier=value)

    def set_note(self, replicate: str, strain_col: int, strain: str,
                 note: str) -> bool:
        return self._amend(replicate, strain_col,
                           f"Note on {strain} {replicate}", note=note.strip())

    def set_statistics(self, statistical_test: str, p_adjust: str,
                       alpha: "float | None" = None) -> bool:
        """Choose the test used by review previews and exports."""
        if statistical_test not in {"t_test", "anova"}:
            raise ValueError("statistical_test must be 't_test' or 'anova'")
        if p_adjust not in {"none", "holm", "bonferroni", "sidak"}:
            raise ValueError("unknown multiple-testing correction")
        alpha = self.review.alpha if alpha is None else float(alpha)
        if not 0 < alpha < 1:
            raise ValueError("alpha must be between 0 and 1")
        before = self.snapshot()
        self.review.statistical_test = statistical_test
        self.review.p_adjust = p_adjust
        self.review.alpha = alpha
        test_label = "ratio paired t-tests" if statistical_test == "t_test" \
            else "one-way ANOVA"
        correction = (f" with {p_adjust} correction"
                      if statistical_test == "t_test" and p_adjust != "none"
                      else " without correction"
                      if statistical_test == "t_test" else "")
        return self._commit(before, f"Use {test_label}{correction}, α={alpha:g}")

    def revert_spot(self, replicate: str, strain_col: int, strain: str) -> bool:
        """Forget every correction to one spot."""
        medium = self.medium
        before = self.snapshot()
        self.review.set_edit(medium, SpotEdit(replicate, int(strain_col)))
        return self._commit(before, f"Revert {strain} {replicate}",
                            invalidate=medium)

    def revert_medium(self, medium: str = "") -> bool:
        medium = medium or self.medium
        before = self.snapshot()
        self.review.clear_medium(medium)
        return self._commit(before, f"Revert every edit on {medium}",
                            invalidate=medium)

    def edit_for(self, replicate: str, strain_col: int) -> "SpotEdit | None":
        return self.review.edit(self.medium, (str(replicate), int(strain_col)))

    # -- rebuilt data --------------------------------------------------------

    def frame(self, medium: str = "", force: bool = False) -> "Frame | None":
        """The rebuilt per-spot frame for a medium, or None if it cannot be built.

        Cached per medium and dropped whenever that medium's pick or edits
        change, so the table can never show numbers from a superseded decision.
        Slow the first time (it walks the capture tree); call it off the UI
        thread.
        """
        medium = medium or self.medium
        if force:
            self._frames.pop(medium, None)
            self._errors.pop(medium, None)
        if medium in self._frames:
            return self._frames[medium]
        if medium in self._errors or not self.can_edit:
            return None
        cand = self.chosen(medium)
        if cand is None:
            self._errors[medium] = (
                f"the candidate recorded for {medium} is not in this run's "
                f"results any more; choose one from the list")
            return None
        try:
            f = rebuild(self.capture_root, self.run.label, self.cfg, cand,
                        self.review.edits_for(medium))
        except RebuildError as e:
            self._errors[medium] = str(e)
            return None
        except Exception as e:                    # pragma: no cover - defensive
            self._errors[medium] = f"{type(e).__name__}: {e}"
            return None
        self._frames[medium] = f
        return f

    def cached_frame(self, medium: str = "") -> "Frame | None":
        """The rebuilt frame ONLY if it is already in hand.

        Used by every redraw, so that painting the window never blocks on
        reading a photograph. `frame()` is the one that does real work, and it
        belongs on a worker thread.
        """
        return self._frames.get(medium or self.medium)

    def is_pending(self, medium: str = "") -> bool:
        """Rebuildable, but not rebuilt yet -- i.e. worth showing a wait for."""
        medium = medium or self.medium
        return (self.can_edit and medium not in self._frames
                and medium not in self._errors)

    def error(self, medium: str = "") -> str:
        medium = medium or self.medium
        if not self.can_edit:
            return self.config_error or "no capture folder linked"
        return self._errors.get(medium, "")

    def frames(self) -> tuple[list[Frame], list[str]]:
        """Every medium's frame, for export. Returns (frames, problems)."""
        out, bad = [], []
        for medium in self.run.media:
            f = self.frame(medium)
            if f is None:
                bad.append(f"{medium}: {self.error(medium)}")
            else:
                out.append(f)
        return out, bad

    # -- saving --------------------------------------------------------------

    def save(self) -> Path:
        if self.capture_root is not None:
            self.review.capture_root = str(self.capture_root)
        path = rv.save_for(self.run, self.review)
        self._saved = self.review.to_dict()
        self._notify()
        return path

    # -- history -------------------------------------------------------------

    def _restore(self, snapshot: dict) -> None:
        self.review = Review.from_dict(snapshot)
        self._frames.clear()
        self._errors.clear()
        self._notify()

    def undo(self) -> "str | None":
        got = self.undo_stack.undo(self.snapshot())
        if got is None:
            return None
        snapshot, label = got
        self._restore(snapshot)
        return label

    def redo(self) -> "str | None":
        got = self.undo_stack.redo(self.snapshot())
        if got is None:
            return None
        snapshot, label = got
        self._restore(snapshot)
        return label
