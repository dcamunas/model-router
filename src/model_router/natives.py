"""The native Claude Code settings the router builds on, written into the profile's settings.json by the router itself.

A plugin's own settings only carry `agent` and `subagentStatusLine`, so the SessionStart hook writes these into
`<profile>/settings.json`:

    autoCompactWindow                  400000 (docs/claude-code.md has the replay that chose it)
    statusLine                         `router statusline` from the plugin's environment ($CLAUDE_PLUGIN_DATA)
    env.CLAUDE_CODE_SUBAGENT_MODEL     sonnet, a fallback: a model the hook passes, or an agent's own, wins over it

Each key at most once per profile, and only when the settings do not have it already. `natives.json` in the state
folder records every key the router looked at, with who set it (`router` or `user`): a key in it is never touched
again, so a value the person changes or removes stays as they left it. A settings.json that is not a JSON object is
left alone. `router config --natives off` stops it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any  # Any: decoded JSON (settings.json)

from .paths import claude_profile, home, locked, read_config, read_json, replace_file

RECORD = "natives.json"
SUBAGENT_MODEL_SINCE = (2, 1, 251)  # before it, CLAUDE_CODE_SUBAGENT_MODEL overrode every other model choice
VERSION_SECONDS = 5.0
AUTO_COMPACT_WINDOW = 400_000
SUBAGENT_MODEL = "sonnet"
KEYS = ("autoCompactWindow", "statusLine", "env.CLAUDE_CODE_SUBAGENT_MODEL")  # a dot: an entry inside that object
SAID = {"autoCompactWindow": f"autoCompactWindow {AUTO_COMPACT_WINDOW}", "statusLine": "the status line",
        "env.CLAUDE_CODE_SUBAGENT_MODEL": f"CLAUDE_CODE_SUBAGENT_MODEL={SUBAGENT_MODEL}"}


def enabled() -> bool:
    """Say whether the router may write these settings (`router config --natives`, on by default)."""
    return read_config().get("natives") is not False


def settings_path() -> Path:
    """Return the profile's settings.json."""
    return claude_profile() / "settings.json"


def wanted() -> dict[str, Any]:
    """Return the value the router wants for each key; the status line only when $CLAUDE_PLUGIN_DATA says where the
    plugin's environment lives (it is set in hook processes and stays put across plugin updates).
    """
    out: dict[str, Any] = {"autoCompactWindow": AUTO_COMPACT_WINDOW}
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        router = shlex.quote(str(Path(data) / "venv" / "bin" / "router"))
        out["statusLine"] = {"type": "command", "command": f"{router} statusline"}
    out["env.CLAUDE_CODE_SUBAGENT_MODEL"] = SUBAGENT_MODEL
    return out


def apply() -> list[str]:
    """Write the wanted keys the settings lack and the router never looked at before; return the keys it set.

    Raises:
        OSError: settings.json or the record could not be read or written.
    """
    if not enabled() or (settings_path().is_symlink() and not settings_path().exists()):
        return []  # off, or a link to nothing: never create its target
    path = settings_path().resolve()  # a settings.json linked from a dotfiles repository stays a link
    with locked(home() / RECORD):
        before = _stamp(path)
        data: Any = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            return []
        seen = read_json(home() / RECORD)
        known = len(seen)
        done = [key for key, value in wanted().items() if key not in seen and _set(data, seen, key, value)]
        if done:
            if _stamp(path) != before:  # Claude Code or the person wrote it meanwhile: theirs wins, try next session
                return []
            mode = path.stat().st_mode & 0o777 if path.exists() else None
            replace_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)
        if len(seen) > known:
            replace_file(home() / RECORD, json.dumps(seen, indent=2) + "\n")
    return done


def _set(data: dict[str, Any], seen: dict[str, Any], key: str, value: Any) -> bool:
    """Put `value` at `key` in the settings when the person has no value of their own, recording who holds the key
    in `seen`; return whether the router set it.
    """
    holder = _holder(data, key)
    parent, _, leaf = key.rpartition(".")
    if holder is None or leaf in holder or (parent == "env" and leaf in os.environ):
        seen[key] = "user"  # in settings.json, or exported in the shell or another settings scope
        return False
    if key == "env.CLAUDE_CODE_SUBAGENT_MODEL" and not _claude_at_least(SUBAGENT_MODEL_SINCE):
        return False  # an older Claude Code would put it above the hook's choice; looked at again next time
    if parent and parent not in data:
        data[parent] = holder  # `env` appears only when something goes in it
    holder[leaf] = value
    seen[key] = "router"
    return True


def _stamp(path: Path) -> tuple[int, int] | None:
    """Return the modification time and size of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _claude_at_least(version: tuple[int, ...]) -> bool:
    """Say whether `claude --version` reports at least `version`; False when it cannot tell."""
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=VERSION_SECONDS,
                             check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return bool(found) and tuple(int(n) for n in found.groups()) >= version


def _holder(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Return the object that holds `key` in the settings (`env` for `env.X`; a new empty one, not yet in the
    settings, when there is none), or None when it is there but not an object.
    """
    parent = key.rpartition(".")[0]
    if not parent:
        return data
    holder = data.get(parent, {})
    return holder if isinstance(holder, dict) else None


def notice(done: list[str]) -> str:
    """Return the one-line notice for the keys just set, or an empty string."""
    if not done:
        return ""
    said = [SAID[key] for key in done]
    listed = ", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0]
    return f"router: set {listed} in settings.json (once; `router config --natives off` stops it)"


def report() -> list[str]:
    """Return one line per key for `router doctor`: set by the router, set by the user, or not set."""
    if not enabled():
        return ["native settings: off (`router config --natives on` turns them on)"]
    data = read_json(settings_path())
    seen = read_json(home() / RECORD)
    lines = []
    for key in KEYS:
        holder = _holder(data, key)
        if holder is None or key.rpartition(".")[2] not in holder:
            lines.append(f"{key}: not set" + (" (removed after the router set it; it stays so)"
                                               if seen.get(key) == "router" else ""))
        else:
            lines.append(f"{key}: set by {'the router' if seen.get(key) == 'router' else 'the user'}")
    return lines
