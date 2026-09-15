from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME = Path.home() / ".local" / "share" / "gh-work-track"
DEFAULT_DB_DIR = "lance"


def data_home() -> Path:
    override = os.environ.get("GH_WORK_TRACK_HOME")
    if override:
        return Path(override).expanduser()
    return DEFAULT_HOME


def db_path(override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    return data_home() / DEFAULT_DB_DIR
