"""Where the router keeps its state: ~/.model-router (MODEL_ROUTER_HOME moves it). `config.json`
holds what `router install` decides for this machine: the Claude profile the cli backend uses and,
optionally, the models this installation may route to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HOME_ENV = "MODEL_ROUTER_HOME"


def home() -> Path:
    """Return the state directory: $MODEL_ROUTER_HOME or ~/.model-router."""
    return Path(os.environ.get(HOME_ENV, "~/.model-router")).expanduser()


def config_path() -> Path:
    """Return the path of config.json inside the state directory."""
    return home() / "config.json"


def read_config() -> dict[str, Any]:
    """Return the contents of config.json, or an empty dict when it is missing or not a JSON object."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict[str, Any]) -> Path:
    """Write `data` as the whole config.json, creating the state directory, and return its path."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_config(**values: Any) -> Path:
    """Merge `values` into config.json and return its path."""
    return save_config({**read_config(), **values})
