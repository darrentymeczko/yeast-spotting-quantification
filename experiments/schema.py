"""Reading and writing experiment JSON.

The file records who was spotted, on what, and where the photos are -- the
answers a photograph cannot supply. Like a plate template it is meant to be read
by a human and checked against the bench, because the critical failure mode is
still a mislabelled sample; unlike a template it has no grid to mirror, so plain
indented JSON is enough.

`ensure_ascii=False` throughout is not cosmetic: the strain names in this lab are
written with real Greek deltas ("ΔATX1") and escaping them would make the file
unreadable to the person who has to confirm it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import (
    DILUTION_MODES,
    KIND,
    MODES,
    PHOTO_TOPS,
    SCHEMA_VERSION,
    Condition,
    Experiment,
)
from .profiles import ProfileError, profile_from_dict, profile_to_dict


class ExperimentError(ValueError):
    """An experiment file could not be read. Messages always name the JSON path."""


_MISSING = object()


def _get(d: dict, key: str, path: str, types: type | tuple[type, ...], *, default=_MISSING):
    if key not in d or d[key] is None:
        if default is _MISSING:
            raise ExperimentError(f"{path}: missing required key {key!r}")
        return default
    value = d[key]
    if not isinstance(value, types):
        wanted = types if isinstance(types, tuple) else (types,)
        names = " or ".join(t.__name__ for t in wanted)
        raise ExperimentError(
            f"{path}.{key}: expected {names}, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# dict <-> Experiment
# ---------------------------------------------------------------------------


def condition_to_dict(c: Condition) -> dict:
    d: dict = {"code": c.code, "label": c.label}
    if c.control_slot is not None:
        d["control_slot"] = c.control_slot
    if c.exclude:
        d["exclude"] = list(c.exclude)
    if c.dilution:
        d["dilution"] = dict(c.dilution)
    return d


def condition_from_dict(raw: dict, path: str) -> Condition:
    if not isinstance(raw, dict):
        raise ExperimentError(f"{path}: expected an object, got {type(raw).__name__}")
    code = _get(raw, "code", path, str)
    if not code.strip():
        raise ExperimentError(f"{path}.code: must not be blank")

    dilution = _get(raw, "dilution", path, dict, default=None)
    if dilution is not None:
        mode = dilution.get("mode")
        if mode not in DILUTION_MODES:
            raise ExperimentError(
                f"{path}.dilution.mode: expected one of "
                f"{', '.join(DILUTION_MODES)}, got {mode!r}"
            )

    exclude = _get(raw, "exclude", path, list, default=[])
    for i, slot in enumerate(exclude):
        if not isinstance(slot, int):
            raise ExperimentError(
                f"{path}.exclude[{i}]: expected a sample slot number, "
                f"got {type(slot).__name__}"
            )

    return Condition(
        code=code,
        label=_get(raw, "label", path, str, default=""),
        control_slot=_get(raw, "control_slot", path, int, default=None),
        exclude=tuple(exclude),
        dilution=dilution,
    )


def to_dict(e: Experiment) -> dict:
    d: dict = {
        "schema_version": e.schema_version,
        "kind": KIND,
        "id": e.id,
        "revision": e.revision,
        "name": e.name,
    }
    if e.description:
        d["description"] = e.description
    if e.created:
        d["created"] = e.created

    d["template"] = {"id": e.template_id, "path": e.template_path}
    d["mode"] = e.mode
    d["strains"] = list(e.strains)
    d["control_slot"] = e.control_slot
    d["conditions"] = [condition_to_dict(c) for c in e.conditions]

    photos: dict = {
        "root": e.photo_root,
        "experiment_top": e.photo_top,
        "profile": profile_to_dict(e.profile),
    }
    if e.set_key is not None:
        photos["set_key"] = e.set_key
    if e.picks:
        photos["picks"] = {
            code: dict(sorted(slots.items()))
            for code, slots in sorted(e.picks.items())
        }
    if e.overrides:
        photos["overrides"] = {k: dict(v) for k, v in sorted(e.overrides.items())}
    if e.ignored:
        photos["ignored"] = sorted(e.ignored)
    d["photos"] = photos
    return d


def _migrate(d: dict) -> dict:
    version = d.get("schema_version")
    if version is None:
        raise ExperimentError("$: missing required key 'schema_version'")
    if not isinstance(version, int):
        raise ExperimentError("$.schema_version: expected an integer")
    if version > SCHEMA_VERSION:
        raise ExperimentError(
            f"$.schema_version: {version} is newer than this tool understands "
            f"(highest supported is {SCHEMA_VERSION}); update the Experiment Designer"
        )
    return d


def from_dict(d: dict) -> Experiment:
    if not isinstance(d, dict):
        raise ExperimentError(f"$: expected a JSON object, got {type(d).__name__}")
    d = _migrate(d)

    mode = _get(d, "mode", "$", str)
    if mode not in MODES:
        raise ExperimentError(
            f"$.mode: expected one of {', '.join(MODES)}, got {mode!r}"
        )

    template = _get(d, "template", "$", dict, default={})
    strains_raw = _get(d, "strains", "$", list, default=[])
    strains: list[str | None] = []
    for i, s in enumerate(strains_raw):
        if s is None:
            strains.append(None)
        elif isinstance(s, str):
            strains.append(s.strip() or None)
        else:
            raise ExperimentError(
                f"$.strains[{i}]: expected a name or null, got {type(s).__name__}"
            )

    conditions = [
        condition_from_dict(raw, f"$.conditions[{i}]")
        for i, raw in enumerate(_get(d, "conditions", "$", list, default=[]))
    ]
    seen: set[str] = set()
    for c in conditions:
        if c.code in seen:
            raise ExperimentError(
                f"$.conditions: {c.code!r} appears more than once; condition "
                f"codes name result files and must be unique"
            )
        seen.add(c.code)

    photos = _get(d, "photos", "$", dict, default={})
    photo_top = _get(
        photos, "experiment_top", "$.photos", str, default="top"
    ).strip().lower()
    if photo_top not in PHOTO_TOPS:
        raise ExperimentError(
            "$.photos.experiment_top: expected one of "
            f"{', '.join(PHOTO_TOPS)}, got {photo_top!r}"
        )
    try:
        profile = profile_from_dict(
            _get(photos, "profile", "$.photos", dict, default={}), "$.photos.profile"
        )
    except ProfileError as exc:
        raise ExperimentError(str(exc)) from exc

    picks_raw = _get(photos, "picks", "$.photos", dict, default={})
    picks: dict[str, dict[str, str]] = {}
    for code, slots in picks_raw.items():
        if not isinstance(slots, dict):
            raise ExperimentError(
                f"$.photos.picks[{code!r}]: expected an object of plate -> file, "
                f"got {type(slots).__name__}"
            )
        for plate, relpath in slots.items():
            if not isinstance(relpath, str):
                raise ExperimentError(
                    f"$.photos.picks[{code!r}][{plate!r}]: expected a file path, "
                    f"got {type(relpath).__name__}"
                )
            picks.setdefault(str(code), {})[str(plate)] = relpath

    overrides_raw = _get(photos, "overrides", "$.photos", dict, default={})
    overrides: dict[str, dict] = {}
    for key, value in overrides_raw.items():
        if not isinstance(value, dict):
            raise ExperimentError(
                f"$.photos.overrides[{key!r}]: expected an object, "
                f"got {type(value).__name__}"
            )
        overrides[str(key)] = dict(value)

    return Experiment(
        name=_get(d, "name", "$", str, default="Untitled"),
        template_id=_get(template, "id", "$.template", str, default=""),
        template_path=_get(template, "path", "$.template", str, default=""),
        strains=strains,
        control_slot=_get(d, "control_slot", "$", int, default=None),
        conditions=conditions,
        mode=mode,
        photo_top=photo_top,
        photo_root=_get(photos, "root", "$.photos", str, default=""),
        set_key=_get(photos, "set_key", "$.photos", str, default=None),
        profile=profile,
        picks=picks,
        overrides=overrides,
        ignored=[str(p) for p in _get(photos, "ignored", "$.photos", list, default=[])],
        id=_get(d, "id", "$", str, default=""),
        revision=_get(d, "revision", "$", int, default=1),
        description=_get(d, "description", "$", str, default=""),
        created=_get(d, "created", "$", str, default=""),
        schema_version=SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Text and files
# ---------------------------------------------------------------------------


def dumps_experiment(e: Experiment) -> str:
    return json.dumps(to_dict(e), indent=2, ensure_ascii=False) + "\n"


def loads_experiment(text: str) -> Experiment:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExperimentError(f"not valid JSON: {exc}") from exc
    return from_dict(raw)


def load(path: Path) -> Experiment:
    # utf-8-sig so a file hand-edited in Notepad or written by PowerShell, which
    # add a UTF-8 BOM, still loads. Writing stays BOM-free.
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ExperimentError(f"could not read {path}: {exc}") from exc
    try:
        return loads_experiment(text)
    except ExperimentError as exc:
        raise ExperimentError(f"{path}: {exc}") from exc


def save(e: Experiment, path: Path, *, bump_revision: bool = True) -> None:
    """Write atomically -- these files live on OneDrive."""
    if bump_revision:
        e.revision += 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps_experiment(e), encoding="utf-8")
    os.replace(tmp, path)
