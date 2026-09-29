"""Reading and writing plate-template JSON.

The stored form is a dense grid of short string tokens, one JSON line per plate
row, so the file looks like the plate it describes. That is the whole point:
the critical failure mode for this tool is a mislabelled sample, and the only
real defence is a human reading the file and confirming it matches the bench.

The emitter therefore hand-formats the `cells` arrays -- and only those. Every
scalar still goes through `json.dumps`, so quoting and escaping are never this
module's problem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import (
    KIND,
    SCHEMA_VERSION,
    Cell,
    DilutionSpec,
    PlateSlot,
    Template,
    TokenError,
)


class TemplateError(ValueError):
    """A template file could not be read. Messages always name the JSON path."""


_MISSING = object()


def _get(d: dict, key: str, path: str, types: type | tuple[type, ...], *, default=_MISSING):
    if key not in d or d[key] is None:
        if default is _MISSING:
            raise TemplateError(f"{path}: missing required key {key!r}")
        return default
    value = d[key]
    if not isinstance(value, types):
        wanted = types if isinstance(types, tuple) else (types,)
        names = " or ".join(t.__name__ for t in wanted)
        raise TemplateError(
            f"{path}.{key}: expected {names}, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# dict <-> Template
# ---------------------------------------------------------------------------


def to_dict(t: Template) -> dict:
    d: dict = {
        "schema_version": t.schema_version,
        "kind": KIND,
        "id": t.id,
        "revision": t.revision,
        "name": t.name,
    }
    if t.description:
        d["description"] = t.description
    if t.created:
        d["created"] = t.created

    d["grid"] = {"rows": t.rows, "cols": t.cols}
    d["sample_slots"] = t.sample_slots()

    dilution: dict = {"levels": t.dilution.levels}
    if t.dilution.fold is not None:
        dilution["fold"] = t.dilution.fold
    if t.dilution.labels:
        dilution["labels"] = list(t.dilution.labels)
    d["dilution"] = dilution

    d["plate_slots"] = [
        {
            "id": p.id,
            "label": p.label,
            "control_slot": p.control_slot,
            "cells": [[cell.token for cell in row] for row in p.cells],
        }
        for p in t.plates
    ]
    return d


def _migrate(d: dict) -> dict:
    version = d.get("schema_version")
    if version is None:
        raise TemplateError("$: missing required key 'schema_version'")
    if not isinstance(version, int):
        raise TemplateError("$.schema_version: expected an integer")
    if version > SCHEMA_VERSION:
        raise TemplateError(
            f"$.schema_version: {version} is newer than this tool understands "
            f"(highest supported is {SCHEMA_VERSION}); update the Plate Template Designer"
        )
    return d


def from_dict(d: dict) -> Template:
    if not isinstance(d, dict):
        raise TemplateError(f"$: expected a JSON object, got {type(d).__name__}")
    d = _migrate(d)

    grid = _get(d, "grid", "$", dict)
    rows = _get(grid, "rows", "$.grid", int)
    cols = _get(grid, "cols", "$.grid", int)
    if rows < 1 or cols < 1:
        raise TemplateError(f"$.grid: rows and cols must be at least 1, got {rows}x{cols}")

    dil_raw = _get(d, "dilution", "$", dict)
    dilution = DilutionSpec(
        levels=_get(dil_raw, "levels", "$.dilution", int),
        fold=_get(dil_raw, "fold", "$.dilution", (int, float), default=None),
        labels=list(_get(dil_raw, "labels", "$.dilution", list, default=[])),
    )
    if dilution.levels < 1:
        raise TemplateError(
            f"$.dilution.levels: must be at least 1, got {dilution.levels}"
        )

    plates: list[PlateSlot] = []
    for i, raw in enumerate(_get(d, "plate_slots", "$", list)):
        path = f"$.plate_slots[{i}]"
        if not isinstance(raw, dict):
            raise TemplateError(f"{path}: expected an object, got {type(raw).__name__}")
        plate_id = _get(raw, "id", path, str)
        cells_raw = _get(raw, "cells", path, list)
        if len(cells_raw) != rows:
            raise TemplateError(
                f"{path}.cells: has {len(cells_raw)} rows but $.grid.rows is {rows}"
            )
        cells: list[list[Cell]] = []
        for r, row in enumerate(cells_raw):
            if not isinstance(row, list):
                raise TemplateError(
                    f"{path}.cells[{r}]: expected a list of tokens, "
                    f"got {type(row).__name__}"
                )
            if len(row) != cols:
                raise TemplateError(
                    f"{path}.cells[{r}]: has {len(row)} tokens but $.grid.cols is {cols}"
                )
            built: list[Cell] = []
            for c, tok in enumerate(row):
                if not isinstance(tok, str):
                    raise TemplateError(
                        f"{path}.cells[{r}][{c}]: expected a string token, "
                        f"got {type(tok).__name__}"
                    )
                try:
                    built.append(Cell.from_token(tok))
                except TokenError as exc:
                    raise TemplateError(f"{path}.cells[{r}][{c}]: {exc}") from exc
            cells.append(built)
        plates.append(
            PlateSlot(
                id=plate_id,
                label=_get(raw, "label", path, str, default=""),
                control_slot=_get(raw, "control_slot", path, int, default=None),
                cells=cells,
            )
        )

    return Template(
        name=_get(d, "name", "$", str, default="Untitled"),
        rows=rows,
        cols=cols,
        dilution=dilution,
        plates=plates,
        id=_get(d, "id", "$", str, default=""),
        revision=_get(d, "revision", "$", int, default=1),
        description=_get(d, "description", "$", str, default=""),
        created=_get(d, "created", "$", str, default=""),
        schema_version=SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

_IND = "  "


def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _emit_cells(rows: list[list[str]], level: int) -> str:
    """One grid row per physical line, tokens column-aligned.

    Padding goes *after* the comma so the alignment spaces never sit between a
    token and its separator. It is ordinary JSON whitespace either way.
    """
    if not rows:
        return "[]"
    width = max((len(tok) for row in rows for tok in row), default=1)
    field = width + 4  # two quotes, one comma, at least one trailing space
    inner = _IND * (level + 1)

    out = ["["]
    for i, row in enumerate(rows):
        parts = []
        for j, tok in enumerate(row):
            text = _scalar(tok)
            parts.append((text + ",").ljust(field) if j < len(row) - 1 else text)
        tail = "," if i < len(rows) - 1 else ""
        out.append(f"{inner}[{''.join(parts)}]{tail}")
    out.append(_IND * level + "]")
    return "\n".join(out)


def _emit(value, level: int, key: str | None = None) -> str:
    if key == "cells" and isinstance(value, list):
        return _emit_cells(value, level)
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = _IND * (level + 1)
        items = [
            f"{inner}{_scalar(k)}: {_emit(v, level + 1, k)}" for k, v in value.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + _IND * level + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(x, (dict, list)) for x in value):
            return "[" + ", ".join(_scalar(x) for x in value) + "]"
        inner = _IND * (level + 1)
        items = [inner + _emit(x, level + 1) for x in value]
        return "[\n" + ",\n".join(items) + "\n" + _IND * level + "]"
    return _scalar(value)


def dumps_template(t: Template) -> str:
    return _emit(to_dict(t), 0) + "\n"


def loads_template(text: str) -> Template:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"not valid JSON: {exc}") from exc
    return from_dict(raw)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def load(path: Path) -> Template:
    # utf-8-sig so a file hand-edited in Notepad or written by PowerShell, which
    # add a UTF-8 BOM, still loads. Writing stays BOM-free.
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TemplateError(f"could not read {path}: {exc}") from exc
    try:
        return loads_template(text)
    except TemplateError as exc:
        raise TemplateError(f"{path}: {exc}") from exc


def save(t: Template, path: Path, *, bump_revision: bool = True) -> None:
    """Write atomically -- these files live on OneDrive."""
    if bump_revision:
        t.revision += 1
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dumps_template(t), encoding="utf-8")
    os.replace(tmp, path)
