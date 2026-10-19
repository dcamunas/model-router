"""Where the router keeps its state: ~/.model-router (MODEL_ROUTER_HOME moves it). `config.json`
holds the few things decided at install time, today only the Claude profile for the cli backend.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def home() -> Path:
    return Path(os.environ.get("MODEL_ROUTER_HOME", "~/.model-router")).expanduser()


def config_path() -> Path:
    return home() / "config.json"


def read_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_config(**values: Any) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**read_config(), **values}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
