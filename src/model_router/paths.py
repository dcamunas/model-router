"""Where the router keeps its state, how it reads and writes it, and the environment variables it reads.

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
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any  # Any: decoded JSON (config.json, logs)

MAIN_LEDGER = "ledger.jsonl"  # the ledger of `router run` and `router judge`
DECISION_LOG = "hook.jsonl"  # the hook's routing decisions
HOOK_ENV = "MODEL_ROUTER_HOOK"  # "0" disables the Claude Code hook without uninstalling it
HOOK_LOG_ENV = "MODEL_ROUTER_HOOK_LOG"  # "0" stops the hook's decisions log, its measurements and all learning
HOOK_QUIET_ENV = "MODEL_ROUTER_HOOK_QUIET"  # "1" hides the hook's one-line notice in the UI
CHILD_ENV = "MODEL_ROUTER_CHILD"  # "1" in every session the router starts itself (a worker, a judge): not a person's turn
NO_MCP = '{"mcpServers":{}}'  # the MCP configuration of every `claude -p` the router starts: none


def claude_profile() -> Path:
    """Return the Claude Code profile in use: $CLAUDE_CONFIG_DIR, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser().absolute()


def home() -> Path:
    """Return the state directory: `model-router` inside the Claude profile in use."""
    return claude_profile() / "model-router"


def logging_on() -> bool:
    """Say whether the hook keeps its decisions and measurements (MODEL_ROUTER_HOOK_LOG is not 0)."""
    return os.environ.get(HOOK_LOG_ENV, "1") != "0"


def quiet() -> bool:
    """Say whether the hook's one-line notices are hidden (MODEL_ROUTER_HOOK_QUIET=1)."""
    return os.environ.get(HOOK_QUIET_ENV) == "1"


def child_env(config_dir: Path | None) -> dict[str, str]:
    """Return the environment of a `claude -p` the router starts: on `config_dir` (the inherited profile when None),
    not nested in this session, marked as the router's own, with no claude.ai MCP servers.
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env[CHILD_ENV] = "1"  # hooks that watch a person's turns, and the limit probe, skip this session
    env.setdefault("ENABLE_CLAUDEAI_MCP_SERVERS", "false")
    return env


def config_path() -> Path:
    """Return the path of config.json inside the state directory."""
    return home() / "config.json"


def read_config() -> dict[str, Any]:
    """Return the contents of config.json, or an empty dict when it is missing or not a JSON object."""
    return read_json(config_path())


def write_config(**values: Any) -> Path:
    """Merge `values` into config.json (a None value removes its key) and return its path."""
    data = {k: v for k, v in {**read_config(), **values}.items() if v is not None}
    path = config_path()
    replace_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def read_json(path: Path) -> dict[str, Any]:
    """Return the JSON object in `path`, or an empty dict when it is missing, unreadable or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_jsonl(path: Path, *, tail_bytes: int | None = None) -> list[dict[str, Any]]:
    """Return the JSON objects of a log, one per line, in file order: only the last `tail_bytes` when given (its
    first line, cut in half, is dropped). A missing file is empty; a line cut short by two writers is skipped.
    """
    cut = False
    try:
        with path.open("rb") as fh:
            if tail_bytes is not None:
                size = fh.seek(0, os.SEEK_END)
                cut = size > tail_bytes
                fh.seek(max(0, size - tail_bytes))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()[1:] if cut else raw.splitlines()
    out = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def replace_file(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write `text` to `path` in one step: a fresh temporary file next to it (never a link someone planted) moved
    over it, so a reader sees the old file or the new one, never half of one. `mode` sets its permissions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            Path(temporary).chmod(mode)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)  # gone after the replace; left only by a failed write


@contextlib.contextmanager
def locked(log: Path) -> Generator[None]:
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
