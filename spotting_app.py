"""Spotting Quantification -- every stage of the pipeline behind one program.

    py spotting_app.py               the workbench: every tool in one window
    py spotting_app.py <stage> ...   run one stage directly

    workbench     the workbench window         (run_workbench.bat)
    plate         Plate Template Designer      (run_plate_designer.bat)
    experiment    Experiment Designer          (run_experiment_designer.bat)
    data-review   Data Review                  (run_data_review.bat)
    spotting      Spotting quantification      (run_spotting.bat)
    timecourse    Time course                  (run_timecourse.bat)
    review        Results Review               (run_review.bat; --apply re-exports)

The workbench is the program people use. The single-tool stages are how each
tool still runs on its own; `spotting` and `timecourse` are the older console
pipelines, kept for scripting -- measuring is done from an experiment now.

    plate-cli, experiment-cli, data-review-cli, montage, pptx, quant
                  the command-line tools that sit behind the stages

Everything after the stage name goes to that stage untouched, exactly as the
matching .bat file passes it on. Dragging folders onto the program runs the time
course on them, as it does for run_timecourse.bat.

The .bat files still work and are unchanged. This file also runs from source:
`py spotting_app.py timecourse ...`, or bare for the launcher window.

Packaging lives outside this file. If it is packaged again -- PyInstaller
--onedir feeding an Inno Setup installer is the shape that suits distribution
-- the frozen branches here still hold: the project folder becomes the one
holding the program, and this project's own .py files are used ahead of any
built-in copy, so an edit does not need a repackage.

Packaged, the program has no console of its own: the launcher and the windows
would otherwise each leave an empty terminal behind them. The two stages that
ask questions (`spotting`, `timecourse`) open a console when they start, and
hold it open at the end the way the .bat files' `pause` does.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import traceback
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

FROZEN = bool(getattr(sys, "frozen", False))

#: The project folder: the photos, `Results/`, `.spotting_cache/`. Beside the
#: .exe when packaged, so the .exe is a drop-in for the .bat files.
ROOT = (Path(sys.executable).resolve().parent if FROZEN
        else Path(__file__).resolve().parent)

#: The folders holding this program's own code, as they sit in the project.
CODE_DIRS = ("src", "uikit", "workbench", "plate_template", "experiments",
             "data_review", "results_review")


def _use_local_source() -> bool:
    """Put the project's own .py files ahead of anything built into the .exe.

    The packaged program carries the scientific stack -- numpy, scipy, pandas,
    matplotlib, numba -- which is 99% of its size and almost never changes. This
    project's own code is under 1% of it and changes constantly, so freezing it
    in would mean rebuilding to see every edit. Instead the .exe reads it from
    disk, and editing a file is all it takes for the next run to use it.

    A copy is still built in, so the .exe handed to someone on its own works
    with nothing beside it. Local source simply wins when it is there, and the
    launcher says which of the two is in use, so the answer is never a guess.
    """
    present = [ROOT / name for name in CODE_DIRS]
    if not all(p.is_dir() for p in present):
        return False                       # nothing beside it: use the built-in
    for path in (ROOT / "src", ROOT):
        if str(path) in sys.path:
            sys.path.remove(str(path))
        sys.path.insert(0, str(path))
    return True


#: True when the code being run is the .py files in the project folder. Always
#: so from source; for the .exe, only when those folders sit beside it.
LOCAL_SOURCE = _use_local_source()

APP_TITLE = "Spotting Quantification"
ERROR_LOG = ROOT / "spotting_error.log"

#: Set by the launcher on the stages that need a console of their own.
NEW_CONSOLE_ENV = "SPOTTING_NEW_CONSOLE"
#: Set by the workbench on a job it shows in a console window of its own, so
#: the window is held open at the end instead of vanishing with the output.
PAUSE_ENV = "SPOTTING_PAUSE"

#: Whether the process was handed real stdio. Recorded at import, before
#: `_ensure_stdio` papers over a missing one.
HAD_STDOUT = sys.stdout is not None
HAD_STDIN = sys.stdin is not None

# ---------------------------------------------------------------------------
# The stages. Each imports its module when it is run, not before, so opening
# the workbench does not cost a numpy import.
# ---------------------------------------------------------------------------


def _workbench(argv):
    from workbench.app import main
    return main(argv)


def _plate(argv):
    from plate_template.app import main
    return main(argv)


def _experiment(argv):
    from experiments.app import main
    return main(argv)


def _data_review(argv):
    from data_review.app import main
    return main(argv)


def _spotting(argv):
    import spotting_batch
    return spotting_batch.main(argv)


def _timecourse(argv):
    import spotting_timecourse
    return spotting_timecourse.main(argv)


def _review(argv):
    # `--apply` re-exports every reviewed set without opening the window.
    if "--apply" in argv:
        from results_review.cli import main
        return main([a for a in argv if a != "--apply"])
    from results_review.app import main
    return main(argv)


def _plate_cli(argv):
    from plate_template.cli import main
    return main(argv)


def _experiment_cli(argv):
    from experiments.cli import main
    return main(argv)


def _data_review_cli(argv):
    from data_review.cli import main
    return main(argv)


def _montage(argv):
    import spotting_montage
    return spotting_montage.main(argv)


def _pptx(argv):
    import spotting_pptx
    return spotting_pptx.main(argv)


def _quant(argv):
    import spotting_quant
    return spotting_quant.main(argv)


@dataclass(frozen=True)
class Stage:
    key: str
    title: str
    blurb: str
    run: Callable[[list], object]
    #: "window"  -- a GUI, no console.
    #: "console" -- asks questions; gets a console of its own, held open at the end.
    #: "tool"    -- prints and returns; writes to the terminal that started it.
    kind: str
    banner: str = ""


STAGES = [
    Stage("workbench", "Spotting Quantification",
          "Every tool in one window: plate templates, experiments and their "
          "review, as tabs, with Home to start from.",
          _workbench, "window"),
    Stage("plate", "Plate Template Designer",
          "Lay out the plate: grid size, which cell holds which sample, "
          "replicate and dilution, and the control on each plate.",
          _plate, "window"),
    Stage("experiment", "Experiment Designer",
          "Say who was on the plate: the strain in each sample slot, what it "
          "grew on, the control, and where the photographs are.",
          _experiment, "window"),
    Stage("data-review", "Data Review",
          "Flip through every photograph an experiment imported and flag "
          "bad plates and bad spots, before the statistics are run.",
          _data_review, "window"),
    Stage("spotting", "Spotting assay quantification",
          "Quantify photos you chose by eye. Put them in the \"Spotting "
          "Assays\" folder, named <set>.<plate><TREATMENT>.JPG.",
          _spotting, "console",
          "Spotting assay quantification"),
    Stage("timecourse", "Time course",
          "Score every timepoint x photo x dilution in a raw capture tree, "
          "and pick the best set for each medium.",
          _timecourse, "console",
          "Spotting time course -- scoring photo sets"),
    Stage("review", "Results Review",
          "Look through every candidate the time course scored, keep the "
          "winner or choose another, and correct individual spots.",
          _review, "window"),
    Stage("plate-cli", "", "", _plate_cli, "tool"),
    Stage("experiment-cli", "", "", _experiment_cli, "tool"),
    Stage("data-review-cli", "", "", _data_review_cli, "tool"),
    Stage("montage", "", "", _montage, "tool"),
    Stage("pptx", "", "", _pptx, "tool"),
    Stage("quant", "", "", _quant, "tool"),
]
BY_KEY = {s.key: s for s in STAGES}

#: How this program was actually started, so the help text tells the reader to
#: type what they typed -- correct from source and if it is ever packaged.
INVOCATION = (Path(sys.executable).name if FROZEN
              else f"py {Path(__file__).name}")

USAGE = f"""\
{APP_TITLE}

  {INVOCATION}                 open the workbench: every tool in one window
  {INVOCATION} <stage> [...]   run one stage; arguments pass through

Stages:
  workbench      the workbench window (what running with no stage opens)
  plate          Plate Template Designer, on its own
  experiment     Experiment Designer, on its own
  data-review    Data Review, on its own  (flag bad plates and spots first)
  review         Results Review, on its own  (--apply re-exports, no window)

Console pipelines, kept for scripting (measure from an experiment instead):
  spotting       Spotting assay quantification
  timecourse     Time course (drag folders onto the program to run these)

Command-line tools:
  plate-cli      experiment-cli      data-review-cli      montage      pptx      quant

  --selftest     check that every stage loads, then exit
  --help         this text

The photo folder, Results\\ and the saved answers live in the folder holding
the program: {ROOT}
"""


# ---------------------------------------------------------------------------
# Console handling (Windows). The packaged program is a windowed one.
# ---------------------------------------------------------------------------

def _open_console(interactive: bool) -> bool:
    """Make sure stdio goes somewhere a person can see. True if the console is
    one this call created, so that it will vanish when the program exits.

    Left alone when stdio is already real -- a redirect, a pipe, a terminal that
    handed its handles over -- so scripting the tools keeps working.
    """
    forced = os.environ.pop(NEW_CONSOLE_ENV, "") == "1"
    if not forced and HAD_STDOUT and (HAD_STDIN or not interactive):
        return False
    if sys.platform != "win32":
        return False

    import ctypes

    kernel32 = ctypes.windll.kernel32
    owned = False
    if not kernel32.GetConsoleWindow():
        # A tool started from a terminal writes into that terminal. A stage that
        # asks questions cannot share it with the shell reading the same keys.
        if not (not interactive and kernel32.AttachConsole(-1)):
            if not kernel32.AllocConsole():
                return False
            owned = True
    if owned:
        kernel32.SetConsoleTitleW(APP_TITLE)
    _bind_console_stdio()
    return owned


def _bind_console_stdio() -> None:
    """Point sys.stdin/out/err at the console, the way the interpreter itself
    does for a console program, so accented strain names survive.

    `_WindowsConsoleIO` is what CPython uses for a real console and is the one
    that gets the delta characters right. It is private, so if it ever moves,
    fall back to opening the console device by name: plainer, still correct,
    and far better than a stage dying for want of a nicety.
    """
    import io

    try:
        from _io import _WindowsConsoleIO

        out_raw = io.BufferedWriter(_WindowsConsoleIO("CONOUT$", "w"))
        in_raw = io.BufferedReader(_WindowsConsoleIO("CONIN$", "r"))
    except Exception:
        out_raw = open("CONOUT$", "wb", buffering=0)
        in_raw = open("CONIN$", "rb", buffering=0)

    sys.stdout = sys.stderr = io.TextIOWrapper(
        out_raw, encoding="utf-8", errors="replace", newline="\n",
        line_buffering=True, write_through=True)
    sys.stdin = io.TextIOWrapper(in_raw, encoding="utf-8", errors="replace")


def _ensure_stdio() -> None:
    """A windowed program has no stdout or stderr. Code that writes to either
    (a stray `sys.stderr.write`, a worker process) must not crash on it."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _pause() -> None:
    try:
        input("\n  Press Enter to close this window ... ")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def _exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)          # `raise SystemExit("message")`
    return 1


def _report_crash(stage: Stage, text: str) -> None:
    """A window has no console to print a traceback to. Silence is the one
    outcome a launcher must never produce, so it is written down and shown."""
    try:
        ERROR_LOG.write_text(
            f"{datetime.now():%Y-%m-%d %H:%M:%S}  {stage.key}\n{text}",
            encoding="utf-8")
    except OSError:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            APP_TITLE,
            f"{stage.title or stage.key} could not start.\n\n"
            f"{text.strip().splitlines()[-1]}\n\n"
            f"Details were written to:\n{ERROR_LOG}")
        root.destroy()
    except Exception:
        pass


def run_stage(stage: Stage, argv: list) -> int:
    owned = False
    if stage.kind != "window":
        owned = _open_console(interactive=stage.kind == "console")
    if stage.kind == "console":
        # The .bat files' banner and `chcp 65001`, which is what stdio above does.
        print(f"\n {'=' * 60}\n  {stage.banner}\n {'=' * 60}")

    try:
        rc = stage.run(argv)
    except SystemExit as exc:
        rc = _exit_code(exc)
    except Exception:
        text = traceback.format_exc()
        if stage.kind == "window":
            _report_crash(stage, text)
        else:
            print(text, file=sys.stderr)
        rc = 1
    rc = int(rc or 0)

    if stage.kind == "console":
        print()
        print(f"  *** Finished with errors (exit code {rc}) ***" if rc
              else "  Done.")
    if _pause_requested() or owned:
        _pause()
    return rc


def _pause_requested() -> bool:
    """Asked for by whoever started this in a console window of its own.
    Taken off the environment, so nothing this starts in turn pauses too."""
    return os.environ.pop(PAUSE_ENV, "") == "1"


# ---------------------------------------------------------------------------
# Starting a stage as a process of its own
# ---------------------------------------------------------------------------

def spawn_stage(stage: Stage, args: list = ()) -> subprocess.Popen:
    """Start a stage as its own process, so one crashing cannot take the others
    with it and several can be open at once."""
    cmd = [sys.executable] + ([] if FROZEN else [str(Path(__file__).resolve())])
    env = dict(os.environ)
    # The stage must outlive this window. Without this, a packaged program's
    # child borrows the launcher's unpacked files, and closing the launcher
    # deletes them from under it.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if stage.kind == "console":
        env[NEW_CONSOLE_ENV] = "1"
    return subprocess.Popen(
        cmd + [stage.key, *args], cwd=str(ROOT), env=env, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def source_note() -> str:
    """One line saying which copy of this program's code is in use."""
    if not FROZEN:
        return f"Running the source in {ROOT}"
    if LOCAL_SOURCE:
        return ("Running the .py files in this folder, so your edits apply "
                "straight away. No rebuild needed.")
    return ("Running the copy built into the .exe. Edits to the .py files "
            "will NOT apply unless those folders sit beside it.")


# ---------------------------------------------------------------------------
# Self-test: does the packaged program hold everything the stages reach for?
# ---------------------------------------------------------------------------

def selftest() -> int:
    """Import every stage and touch the parts a packaged build tends to lose:
    compiled numba code, matplotlib's fonts, the .pptx template, Tk itself."""
    import io

    failures: list = []

    def check(label: str, fn: Callable[[], object]) -> None:
        try:
            detail = fn()
            print(f"  ok    {label}" + (f"  ({detail})" if detail else ""))
        except Exception as exc:
            failures.append(label)
            print(f"  FAIL  {label}: {type(exc).__name__}: {exc}")

    def load(name: str) -> Callable[[], object]:
        def go():
            __import__(name)
        return go

    print(f"{APP_TITLE} self-test -- frozen={FROZEN}, root={ROOT}")
    print(f"{source_note()}\n")
    for name in ("uikit.theme", "uikit.host", "workbench.app", "workbench.shell",
                 "plate_template.app", "plate_template.cli", "experiments.app",
                 "experiments.cli", "experiments.run", "data_review.app",
                 "data_review.cli", "data_review.spots", "results_review.app",
                 "results_review.cli", "results_review.rebuild",
                 "results_review.export", "spotting_batch",
                 "spotting_timecourse", "spotting_timecourse_figures",
                 "spotting_montage", "spotting_pptx", "spotting_quant",
                 "spotting_plots"):
        check(f"import {name}", load(name))

    def numba_background():
        import numpy as np
        import spotting_background as bg

        img = np.random.default_rng(0).random((64, 64)) * 255
        out = bg.subtract_background(img, 10)
        assert out.shape == img.shape and np.isfinite(out).all()
        return "compiled and ran"

    def matplotlib_formats():
        # Each format is a separate backend, imported by name at save time. The
        # pipeline writes PNG and PDF; this is where a missing one shows up.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots()
        ax.plot([0, 1], [0, 1])
        ax.set_title("selftest Δ")
        for fmt in ("png", "pdf", "svg", "ps"):
            fig.savefig(io.BytesIO(), format=fmt)
        plt.close(fig)
        return "png, pdf, svg, ps"

    def pptx_template():
        from pptx import Presentation
        return f"{len(Presentation().slide_layouts)} layouts"

    def xlsx():
        import openpyxl
        buf = io.BytesIO()
        openpyxl.Workbook().save(buf)

    def skimage_label():
        import numpy as np
        from skimage.measure import label

        return f"{label(np.eye(4, dtype=bool)).max()} object(s)"

    def stats():
        import pyprism_plot  # noqa: F401
        from statsmodels.stats.multitest import multipletests
        multipletests([0.01, 0.04, 0.2], method="fdr_bh")

    def tk_root():
        import tkinter as tk

        root = tk.Tk()
        root.update()
        root.destroy()
        return f"Tcl {tk.TclVersion}"

    def default_template():
        from experiments import BUNDLE
        from plate_template.schema import load as load_template

        path = BUNDLE / "plate_template" / "templates" / "lab_standard_8x6.json"
        return load_template(path).name

    check("numba background subtraction", numba_background)
    check("matplotlib saves every format", matplotlib_formats)
    check("python-pptx default template", pptx_template)
    check("openpyxl writes a workbook", xlsx)
    check("scikit-image", skimage_label)
    check("statsmodels + pyprism_plot", stats)
    check("bundled plate template", default_template)

    def starts(key: str) -> Callable[[], object]:
        # The program starting itself as a separate process: packaged, that is
        # where unpacking, the environment reset and the missing stdio all
        # have to work at once.
        def go():
            rc = spawn_stage(BY_KEY[key], ["--selftest"]).wait(timeout=180)
            assert rc == 0, f"{key}'s own self-test exited {rc}"
            return f"started {key}, it exited 0"
        return go

    check("Tk opens", tk_root)
    check("the workbench window builds and opens documents", starts("workbench"))
    check("a single tool still runs on its own", starts("plate"))

    print()
    if failures:
        print(f"selftest FAILED: {len(failures)} problem(s)")
        return 1
    print("selftest OK")
    return 0


# ---------------------------------------------------------------------------

def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")   # for worker processes

    if not argv:
        return run_stage(BY_KEY["workbench"], [])
    head, rest = argv[0], argv[1:]

    if head in ("-h", "--help", "help", "/?"):
        _open_console(interactive=False)
        print(USAGE)
        return 0
    if head == "--selftest":
        owned = _open_console(interactive=False)
        pause = _pause_requested()
        rc = selftest()
        if owned or pause:
            _pause()
        return rc
    if head.lower() in BY_KEY:
        stage = BY_KEY[head.lower()]
        if stage.key == "review" and "--apply" in rest:
            stage = replace(stage, kind="tool")   # headless: it prints, no window
        return run_stage(stage, rest)
    if Path(head).is_dir():                     # folders dragged onto the program
        return run_stage(BY_KEY["timecourse"], argv)

    _open_console(interactive=False)
    print(f"Unknown stage {head!r}.\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    # A packaged program's worker processes are this same program started again.
    # Both lines must run before anything else, and in this order: the workers
    # exit inside freeze_support() and would otherwise meet a missing stdout.
    _ensure_stdio()
    multiprocessing.freeze_support()
    raise SystemExit(main())
