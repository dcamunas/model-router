"""Where the router keeps its state and the environment variables that configure it.

State lives in ~/.model-router (MODEL_ROUTER_HOME moves it). `config.json` holds what `router
install` or `router config` decide for this machine: the Claude profile the cli backend uses and,
optionally, the models this installation may route to, the judge cell and the active calibration. Every
variable the router reads is named here, all under the MODEL_ROUTER_ prefix.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HOME_ENV = "MODEL_ROUTER_HOME"
MAIN_LEDGER = "ledger.jsonl"  # the ledger of `router run` and `router judge`; benches write their own under bench/
MODELS_ENV = "MODEL_ROUTER_MODELS"  # models this installation may route to, e.g. "haiku,sonnet,opus"
CONFIG_DIR_ENV = "MODEL_ROUTER_CLAUDE_CONFIG_DIR"  # Claude profile for the cli backend
JUDGE_ENV = "MODEL_ROUTER_JUDGE"  # the cell that judges in `full` mode, e.g. "opus/xhigh"
LEAN_ENV = "MODEL_ROUTER_LEAN"  # 0 keeps every subagent on the agent Claude chose: no switch to the read-only one
TRIBUNAL_ENV = "MODEL_ROUTER_TRIBUNAL"  # 0 turns the tribunal plugin's hooks off; every CLI call the router starts sets it
ARBITER_ENV = "MODEL_ROUTER_ARBITER"  # the cell that settles a tribunal's split; unset = the rule in policy.py
CALIBRATION_ENV = "MODEL_ROUTER_CALIBRATION"  # the calibration that decides: an id or a file; "none" = the code tables
CODEX_HOME_ENV = "MODEL_ROUTER_CODEX_HOME"  # the Codex home (ChatGPT account) for `codex` cells; never the ambient ~/.codex
HOOK_ENV = "MODEL_ROUTER_HOOK"  # "0" disables the Claude Code hook without uninstalling it
HOOK_LOG_ENV = "MODEL_ROUTER_HOOK_LOG"  # "0" stops the hook's decisions log
HOOK_QUIET_ENV = "MODEL_ROUTER_HOOK_QUIET"  # "1" hides the hook's one-line notice in the UI


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


@contextlib.contextmanager
def locked(log: Path) -> Iterator[None]:
    """Hold an exclusive lock on `log`'s lock file: the hook's appends and `finalize`'s rewrite never interleave."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
