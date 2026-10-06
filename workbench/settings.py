"""What the workbench remembers between sittings, in `.workbench.json`.

Recent files, the window's size and place, the sidebar's width, whether Home
opens at startup, and which documents were open -- conveniences, every one.
So the file is read tolerantly (anything missing or malformed is simply not
remembered) and written atomically (a crash mid-write leaves the old file,
never half of a new one).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

#: The project folder: beside the .exe when packaged, the repository otherwise.
ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parents[1])

SETTINGS_FILE = ROOT / ".workbench.json"

#: Recent files kept per document kind.
RECENT_LIMIT = 12


class Settings:
    def __init__(self, path: Path | None = SETTINGS_FILE, *,
                 read_only: bool = False) -> None:
        """`read_only` for a self-test: read nothing, write nothing, so a
        check of the program never disturbs the person's own session."""
        self.path = path
        self.read_only = read_only or path is None
        self.data: dict = {} if self.read_only else self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        if self.read_only:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass                 # remembering is a convenience; never fail for it

    # -- plain values --------------------------------------------------------

    def get(self, key: str, default=None):
        value = self.data.get(key, default)
        return default if value is None else value

    def set(self, key: str, value) -> None:
        self.data[key] = value

    # -- recent files ----------------------------------------------------------

    def add_recent(self, kind: str, path: Path) -> None:
        entries = [e for e in self._recent_entries()
                   if not (e["kind"] == kind and _same(e["path"], path))]
        entries.insert(0, {"kind": kind, "path": str(path),
                           "opened": datetime.now().isoformat(timespec="seconds")})
        kept, counts = [], {}
        for e in entries:
            counts[e["kind"]] = counts.get(e["kind"], 0) + 1
            if counts[e["kind"]] <= RECENT_LIMIT:
                kept.append(e)
        self.data["recent"] = kept

    def recent(self, kind: str) -> list[tuple[Path, datetime | None]]:
        """(path, when opened) for this kind, newest first, still on disk."""
        out = []
        for e in self._recent_entries():
            if e["kind"] != kind:
                continue
            path = Path(e["path"])
            if not path.exists():
                continue
            try:
                when = datetime.fromisoformat(e.get("opened", ""))
            except (TypeError, ValueError):
                when = None
            out.append((path, when))
        return out

    def _recent_entries(self) -> list[dict]:
        raw = self.data.get("recent")
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict)
                and isinstance(e.get("kind"), str)
                and isinstance(e.get("path"), str)]

    # -- the session -----------------------------------------------------------

    def session(self) -> list[tuple[str, Path]]:
        raw = self.data.get("session")
        if not isinstance(raw, list):
            return []
        out = []
        for e in raw:
            if (isinstance(e, dict) and isinstance(e.get("kind"), str)
                    and isinstance(e.get("path"), str)):
                out.append((e["kind"], Path(e["path"])))
        return out

    def set_session(self, documents: list[tuple[str, Path]]) -> None:
        self.data["session"] = [{"kind": k, "path": str(p)} for k, p in documents]


def _same(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)
