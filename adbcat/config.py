"""Persistent settings: query history, saved filters and display options."""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "adbcat"
CONFIG_FILE = CONFIG_DIR / "config.json"

COLUMNS = ("date", "time", "pid", "uid", "package", "tag")
DEFAULTS = {
    "history": [],
    "saved": [],  # [{"name": str, "query": str}]
    "columns": {"date": True, "time": True, "pid": True, "uid": False, "package": True, "tag": True},
    "mine": [],
    "wrap": False,
    "tag_width": 23,
    "package_width": 28,
    "max_lines": 200_000,
}


class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULTS))
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                for k, v in loaded.items():
                    if k == "columns" and isinstance(v, dict):
                        self.data["columns"].update(v)
                    elif k in DEFAULTS:
                        self.data[k] = v
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass

    def add_history(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        hist = self.data["history"]  # mutated in place: the filter input holds a reference
        if query in hist:
            hist.remove(query)
        hist.append(query)
        del hist[:-100]
