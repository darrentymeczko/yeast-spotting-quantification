"""
spotting_estimate.py -- how long a run will take, before it starts.

The estimate used to be "uncached photos x 70 s / workers". On Set11Test
(2026-10-05: 128 uncached photos, 8 workers) that said 19 min, and the run took
31. The job log and the cache timestamps show where the other 12 went:

    measuring    20:29 -> 20:51   16 photos a worker at ~80 s, not 70
    scoring      20:51 -> 20:51   5 s
    figures      20:51 -> 21:00   graphs 36 s, subtractions 110 s, sheets 398 s

So the figure sheets -- drawn for every candidate by default -- were never
counted, and the per-photo figure had gone stale. The second is the recurring
failure: the measurement code and the machine both change, and a constant
quietly stops being true. Every real run therefore records what it took, per
computer, and the next estimate starts from that instead of from the defaults
below.

Nothing here imports the pipeline; it only does arithmetic on counts the
pipeline hands it, so it can be tested on its own.
"""

from __future__ import annotations

import json
import math
import os
import platform
import time
from pathlib import Path

from spotting_paths import PROJECT_ROOT

# Beside the measurement cache, which is also per program folder. The folder
# can sync between computers, so the rates inside are keyed by computer name.
RATES_FILE = PROJECT_ROOT / ".spotting_cache" / "run_rates.json"

# Worker-seconds per unit of work, measured on the Set11Test run above (8-core
# 7840U, 8 workers, one populated dilution level).
#
# One uncached photo while every worker is busy. Per-photo time RISES with the
# number of workers sharing memory bandwidth, so the serial default is lower;
# it has not been re-timed since the batched subtraction landed.
MEASURE_S = 80.0
MEASURE_SERIAL_S = 60.0
GRAPH_S = 2.4        # one candidate's PyPrism graph
SUBTRACT_S = 6.9     # one photo's display background subtraction
# The first sheet of a photo pairing loads its subtracted images and resamples
# the plate; the pairing's other dilutions reuse both (spotting_montage's block
# cache), so they cost only the compose.
PAIRING_S = 24.5
SHEET_S = 2.0
# Start-up, scoring every candidate, and the best candidates' tidy output.
OVERHEAD_S = 30.0

# Photos per background-subtraction batch in the figure pass; mirrors
# spotting_timecourse_figures.PHOTOS_PER_BACKGROUND_BATCH.
_SUBTRACT_BATCH = 4

# A run too small to say anything reliable about the rate is not recorded: a
# busiest worker with fewer photos than this, or fewer sheets than this.
MIN_PHOTOS_TO_RECORD = 4
MIN_SHEETS_TO_RECORD = 8
# A recorded value moves halfway toward each new run, so one odd run (a slow
# OneDrive download, another program hogging the CPU) cannot swing it far.
_BLEND = 0.5
# Anything outside this factor of the default is a broken run, not a slow
# computer, and is ignored.
_SANE = 4.0


def _host() -> str:
    return platform.node() or "this computer"


def _read(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def rates(path: Path | None = None) -> dict:
    """This computer's recorded rates, or {} if it has never finished a run."""
    mine = _read(path or RATES_FILE).get(_host())
    return mine if isinstance(mine, dict) else {}


def _update(change, path: Path | None = None) -> None:
    """Apply `change(mine)` to this computer's entry and save it atomically.

    Best effort: a run must never fail because its timing could not be saved.
    """
    path = Path(path or RATES_FILE)
    try:
        data = _read(path)
        mine = data.get(_host())
        mine = dict(mine) if isinstance(mine, dict) else {}
        change(mine)
        mine["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        data[_host()] = mine
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        pass


def _blend(old, new: float) -> float:
    try:
        old = float(old)
    except (TypeError, ValueError):
        return new
    return old + _BLEND * (new - old) if old > 0 else new


# --- measurement ------------------------------------------------------------


def default_measure_s(workers: int) -> float:
    return MEASURE_SERIAL_S if workers <= 1 else MEASURE_S


def measure_s(workers: int, path: Path | None = None) -> tuple[float, bool]:
    """(seconds per photo on the busiest worker, whether it was timed here)."""
    got = rates(path).get("measure", {})
    try:
        value = float(got[str(workers)])
        if value > 0:
            return value, True
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return default_measure_s(workers), False


def record_measure(load: int, wall: float, workers: int,
                   path: Path | None = None) -> None:
    """Record a finished measurement pass.

    `load` is the number of photos the BUSIEST worker measured -- the wall clock
    waits for that worker, so wall / load is the rate the estimate multiplies
    back by.
    """
    if load < MIN_PHOTOS_TO_RECORD or wall <= 0:
        return
    per = wall / load
    default = default_measure_s(workers)
    if not default / _SANE <= per <= default * _SANE:
        return

    def change(mine):
        table = mine.get("measure")
        table = dict(table) if isinstance(table, dict) else {}
        table[str(workers)] = round(_blend(table.get(str(workers)), per), 2)
        mine["measure"] = table

    _update(change, path)


# --- figures ----------------------------------------------------------------


def _figure_model(graphs: int, photos: int, pairings: int, sheets: int,
                  workers: int) -> float:
    """Default seconds for the figure pass, modelled on how it splits the work.

    Each phase deals its items round-robin across the workers, so the wall
    clock is the busiest worker's share.
    """
    w = max(1, workers)
    seconds = math.ceil(graphs / w) * GRAPH_S
    if photos:
        groups = math.ceil(photos / _SUBTRACT_BATCH)
        seconds += (math.ceil(groups / w) * min(photos, _SUBTRACT_BATCH)
                    * SUBTRACT_S)
    if pairings:
        seconds += math.ceil(pairings / w) * (PAIRING_S
                                              + SHEET_S * sheets / pairings)
    return seconds


def figure_seconds(graphs: int, photos: int, pairings: int, sheets: int,
                   workers: int, path: Path | None = None) -> tuple[float, bool]:
    """(seconds for the figure pass, whether it was scaled by a run here)."""
    base = _figure_model(graphs, photos, pairings, sheets, workers)
    try:
        scale = float(rates(path)["figures"])
        if scale > 0:
            return base * scale, True
    except (KeyError, TypeError, ValueError):
        pass
    return base, False


def record_figures(graphs: int, photos: int, pairings: int, sheets: int,
                   workers: int, wall: float, path: Path | None = None) -> None:
    """Record a finished figure pass as a scale on the default model.

    One factor rather than a rate per phase: the phases are timed together, and
    a slower computer is slower at all of them.
    """
    if sheets < MIN_SHEETS_TO_RECORD or wall <= 0:
        return
    base = _figure_model(graphs, photos, pairings, sheets, workers)
    if base <= 0:
        return
    scale = wall / base
    if not 1 / _SANE <= scale <= _SANE:
        return

    def change(mine):
        mine["figures"] = round(_blend(mine.get("figures"), scale), 3)

    _update(change, path)


# --- reporting --------------------------------------------------------------


def format_minutes(seconds: float) -> str:
    """'under a minute', '12 min', '1 h 05 min'. Rounds up, never down."""
    if seconds < 45:
        return "under a minute"
    minutes = max(1, math.ceil(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def format_phase(seconds: float) -> str:
    """'<1 min' or '~12 min', for one line of a breakdown."""
    return "<1 min" if seconds < 45 else f"~{format_minutes(seconds)}"
