"""Where the router keeps its state and the environment variables that configure it.

State lives in the Claude profile in use, `<profile>/model-router`, where the profile is $CLAUDE_CONFIG_DIR or
~/.claude. Two profiles on one machine never share decisions, measurements, limits or settings, and `router run`
calls Claude on the profile in use. `config.json` holds what `router config` sets for that profile: the models
this installation may route to, the judge cell and the read-only agent switch. Every variable the router reads is
named here, all under the MODEL_ROUTER_ prefix.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any  # Any: config.json, decoded

MAIN_LEDGER = "ledger.jsonl"  # the ledger of `router run` and `router judge`
HOOK_ENV = "MODEL_ROUTER_HOOK"  # "0" disables the Claude Code hook without uninstalling it
HOOK_LOG_ENV = "MODEL_ROUTER_HOOK_LOG"  # "0" stops the hook's decisions log
HOOK_QUIET_ENV = "MODEL_ROUTER_HOOK_QUIET"  # "1" hides the hook's one-line notice in the UI
CHILD_ENV = "MODEL_ROUTER_CHILD"  # "1" in every session the router starts itself (a worker, a judge): not a person's turn


def claude_profile() -> Path:
    """Return the Claude Code profile in use: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser().absolute()


def home() -> Path:
    """Return the state directory: `model-router` inside the Claude profile in use."""
    return claude_profile() / "model-router"


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


def write_config(**values: Any) -> Path:
    """Merge `values` into config.json (a None value removes its key) and return its path."""
    data = {k: v for k, v in {**read_config(), **values}.items() if v is not None}
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


@contextlib.contextmanager
def locked(log: Path) -> Iterator[None]:
    """Hold an exclusive lock on `log`'s lock file: the hook's appends and `finalize`'s rewrite never interleave."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_regular(path: Path, max_bytes: int) -> bytes | None:
    """Return the bytes of `path` when it is a regular file of at most `max_bytes`, reached without following a
    link; None for anything else (missing, a link, a directory, a FIFO, too big, unreadable). For files that come
    with a repository, which anyone may have written.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, max_bytes + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return raw if len(raw) <= max_bytes else None
