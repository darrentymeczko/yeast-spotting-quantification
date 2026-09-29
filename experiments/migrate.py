"""Turning the pipelines' existing answer files into experiments.

Two files hold today's answers, and neither is an experiment:

    Spotting Assays/spotting_config.json    the strain panels ("sets") and, per
                                            set-and-medium combination, the
                                            control, the excluded slots and the
                                            dilution to score
    <SetNN>/timecourse_config.json          a per-capture-tree copy of the
                                            panel, plus the per-medium control
                                            this branch added

They overlap, disagree in places, and neither records where the photos are. The
migration folds them into one experiment per set: the main config supplies the
panel and the handpicked-quantification choices, the timecourse config supplies
the per-medium control, and the capture tree it sits in supplies the photo root.

Nothing is guessed. Where the two configs disagree about a control the
timecourse config wins -- it is the more specific statement, and the per-medium
control exists precisely because the set-level one is wrong on respiring media
-- and the disagreement is reported rather than silently resolved.

This module must stay importable headless -- no tkinter, no pipeline imports.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date
from pathlib import Path

from .model import QUANTIFY, TIMECOURSE, Condition, Experiment
from .profiles import capture_tree, flat_lab

#: `spotting_batch` wrote "combo" for what is now a condition-wide choice.
_DILUTION_MODES = {"combo": "condition", "plate": "plate"}

#: `spotting_timecourse.set_id_from_name`.
_SET_RE = re.compile(r"set[\s_-]*0*(\d+)", re.I)


class MigrationError(ValueError):
    """A config file could not be migrated. Messages name the file."""


def set_id_from_name(name: str) -> str | None:
    m = _SET_RE.search(name)
    return m.group(1) if m else None


def _read(path: Path) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise MigrationError(f"could not read {path}: {exc}") from exc
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MigrationError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(d, dict):
        raise MigrationError(f"{path}: expected a JSON object")
    return d


def _strains(raw) -> list[str | None]:
    out: list[str | None] = []
    for s in raw or []:
        out.append(s.strip() or None if isinstance(s, str) else None)
    return out


# ---------------------------------------------------------------------------
# The main config: one experiment per set
# ---------------------------------------------------------------------------


def from_spotting_config(
    path: Path, *, template_id: str = "", template_path: str = ""
) -> tuple[list[Experiment], list[str]]:
    """One experiment per set in `spotting_config.json`.

    The result is in QUANTIFY mode, because that is the pipeline this file
    belongs to: its dilution choices only mean anything for handpicked photos.
    """
    cfg = _read(path)
    sets = cfg.get("sets") or {}
    if not isinstance(sets, dict):
        raise MigrationError(f"{path}.sets: expected an object")
    combos = cfg.get("combo") or {}

    # "<set>|<MEDIUM>" -> the answers for that combination.
    by_set: dict[str, dict[str, dict]] = {}
    for key, entry in combos.items():
        if "|" not in str(key) or not isinstance(entry, dict):
            continue
        set_id, medium = str(key).split("|", 1)
        by_set.setdefault(set_id, {})[medium] = entry

    experiments: list[Experiment] = []
    notes: list[str] = []
    for set_id in sorted(sets, key=lambda s: (len(s), s)):
        raw = sets[set_id]
        if not isinstance(raw, dict):
            notes.append(f"set {set_id}: not an object, skipped")
            continue

        strains = _strains(raw.get("strains"))
        if not any(strains):
            notes.append(f"set {set_id}: no strain names, skipped")
            continue

        conditions = []
        for medium in sorted(by_set.get(set_id, {})):
            entry = by_set[set_id][medium]
            conditions.append(
                Condition(
                    code=medium,
                    label=medium,
                    control_slot=entry.get("control_col"),
                    exclude=tuple(int(s) for s in entry.get("exclude") or ()),
                    dilution=_dilution(entry.get("dilution")),
                )
            )
        if not conditions:
            notes.append(f"set {set_id}: no conditions recorded")

        # The same panel can have both a handpicked and a time-course
        # experiment, so the name says which this is. Without the suffix the
        # two migrations would write over each other, and the results folder
        # names would collide too.
        label = f"Set{int(set_id):02d}" if set_id.isdigit() else f"Set {set_id}"
        experiments.append(
            Experiment(
                name=f"{label} handpicked",
                template_id=template_id,
                template_path=template_path,
                strains=strains,
                control_slot=raw.get("control_col"),
                conditions=conditions,
                mode=QUANTIFY,
                # The flat pipeline reads every set out of one folder, so the
                # root is that folder and `set_key` picks this panel's photos.
                photo_root=str(Path(path).parent),
                set_key=set_id,
                profile=flat_lab(),
                id=str(uuid.uuid4()),
                created=date.today().isoformat(),
                description=f"Migrated from {Path(path).name}",
            )
        )
    return experiments, notes


def _dilution(raw) -> dict | None:
    """Translate a stored dilution choice, renaming the mode `combo` -> `condition`."""
    if not isinstance(raw, dict):
        return None
    mode = _DILUTION_MODES.get(raw.get("mode"))
    if mode is None:
        return None
    out: dict = {"mode": mode}
    if mode == "condition" and raw.get("choice"):
        out["choice"] = raw["choice"]
    elif mode == "plate" and isinstance(raw.get("choices"), dict):
        out["choices"] = {str(k): v for k, v in raw["choices"].items()}
    else:
        return None
    return out


# ---------------------------------------------------------------------------
# A capture tree: the timecourse experiment for one set
# ---------------------------------------------------------------------------


def from_capture_tree(
    root: Path,
    *,
    main_config: Path | None = None,
    template_id: str = "",
    template_path: str = "",
) -> tuple[Experiment, list[str]]:
    """The timecourse experiment for one `<SetNN>/` capture tree.

    `timecourse_config.json` beside the photos supplies the panel and the
    per-medium control; the main config is consulted only to fill in what the
    local file does not carry.
    """
    root = Path(root)
    local_path = root / "timecourse_config.json"
    local = _read(local_path) if local_path.exists() else {}
    notes: list[str] = []

    set_id = str(local.get("from_set") or "") or set_id_from_name(root.name) or ""
    strains = _strains(local.get("strains"))
    control = local.get("control_col")

    # Fall back to the main config for anything the local file lacks.
    if (not any(strains) or control is None) and set_id:
        source = Path(local.get("from_config") or "") if local.get("from_config") else None
        if main_config is not None:
            source = Path(main_config)
        if source and source.exists():
            entry = (_read(source).get("sets") or {}).get(set_id)
            if isinstance(entry, dict):
                if not any(strains):
                    strains = _strains(entry.get("strains"))
                    notes.append(f"strain panel taken from {source.name}")
                if control is None:
                    control = entry.get("control_col")

    if not any(strains):
        raise MigrationError(
            f"{root.name}: no strain panel in {local_path.name} or the main config"
        )

    base_exclude = tuple(int(s) for s in local.get("exclude") or ())
    conditions = []
    for code in sorted((local.get("media") or {})):
        entry = local["media"][code] or {}
        per_medium = entry.get("control_col")
        if per_medium is not None and control is not None and per_medium != control:
            # Reported, never silently resolved: this is exactly the case the
            # per-medium control was added for (WT BY does not grow on K-OAc),
            # so the more specific statement wins and says so.
            notes.append(
                f"{code}: control is slot {per_medium}, not the set's slot "
                f"{control}; keeping the per-medium one"
            )
        conditions.append(
            Condition(
                code=code,
                label=code,
                control_slot=per_medium,
                exclude=tuple(int(s) for s in entry.get("exclude") or base_exclude),
            )
        )
    if not conditions:
        notes.append("no per-medium block; conditions will be read from the photos")

    return (
        Experiment(
            name=root.name,
            template_id=template_id,
            template_path=template_path,
            strains=strains,
            control_slot=control,
            conditions=conditions,
            mode=TIMECOURSE,
            photo_root=str(root),
            set_key=None,
            profile=capture_tree(),
            id=str(uuid.uuid4()),
            created=date.today().isoformat(),
            description=f"Migrated from {local_path.name}" if local else
                        f"Migrated from the capture tree {root.name}",
        ),
        notes,
    )


def find_capture_trees(root: Path) -> list[Path]:
    """Folders under `root` that hold a `timecourse_config.json`."""
    root = Path(root)
    if (root / "timecourse_config.json").exists():
        return [root]
    try:
        subs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return []
    return [p for p in subs if (p / "timecourse_config.json").exists()]
