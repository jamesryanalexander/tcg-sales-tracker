from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "tcg_sales_tracker.sqlite3"
CONFIG_JSON_PATH = BASE_DIR / "config.json"


def _load_config_json() -> dict:
    if not CONFIG_JSON_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_JSON_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


_CONFIG_JSON = _load_config_json()


def _resolve(key: str, default: Optional[Any] = None) -> Optional[Any]:
    """Resolution order: env var -> config.json -> default. Same pattern as
    collection-tracker/config.py, kept here even though nothing needs it yet
    so a future tcgplayer.py API client has somewhere to put credentials."""
    if key in os.environ and os.environ[key] != "":
        return os.environ[key]
    if key in _CONFIG_JSON and _CONFIG_JSON[key] != "":
        return _CONFIG_JSON[key]
    return default


def get_max_upload_mb() -> int:
    return int(_resolve("MAX_UPLOAD_MB", 20))
