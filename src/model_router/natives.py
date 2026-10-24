"""The native Claude Code settings the router builds on, written into the profile's settings.json by the router itself.

A plugin's own settings only carry `agent` and `subagentStatusLine`, so the SessionStart hook writes these into
`<profile>/settings.json`:

    autoCompactWindow    400000 (docs/claude-code.md has the replay that chose it)
    statusLine           `router statusline` from the plugin's environment ($CLAUDE_PLUGIN_DATA)

Each key at most once per profile, and only when the settings do not have it already. `natives.json` in the state
folder records every key the router looked at, with who set it (`router` or `user`): a key in it is never touched
again, so a value the person changes or removes stays as they left it. A settings.json that is not a JSON object is
left alone. `router config --natives off` stops it.

0.23 also wrote `env.CLAUDE_CODE_SUBAGENT_MODEL=sonnet`. The skills that run in their own subsession, such as
/code-review, took it and reviewed on Sonnet where the router sends reviews to Opus, so it is taken out again while
it still holds the value the router wrote, with the switch on or off: it only undoes the router's own write.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any  # Any: decoded JSON (settings.json)

from .paths import claude_profile, home, locked, read_config, read_json, replace_file

RECORD = "natives.json"
AUTO_COMPACT_WINDOW = 400_000
KEYS = ("autoCompactWindow", "statusLine")
SAID = {"autoCompactWindow": f"autoCompactWindow {AUTO_COMPACT_WINDOW}", "statusLine": "the status line"}
RETIRED = {"env.CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}  # written by an earlier version: key (a dot: inside that object)


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
    return out


def apply() -> tuple[list[str], list[str]]:
    """Write the wanted keys the settings lack and the router never looked at before, and take out the retired ones it
    wrote; return the keys it set and the keys it took out.

    Raises:
        OSError: settings.json or the record could not be read or written.
    """
    if settings_path().is_symlink() and not settings_path().exists():
        return [], []  # a link to nothing: never create its target
    path = settings_path().resolve()  # a settings.json linked from a dotfiles repository stays a link
    with locked(home() / RECORD):
        before = _stamp(path)
        data: Any = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            return [], []
        seen = read_json(home() / RECORD)
        record = dict(seen)
        done = [key for key, value in wanted().items() if key not in seen and _set(data, seen, key, value)] \
            if enabled() else []
        undone = _retire(data, seen)
        if done or undone:
            if _stamp(path) != before:  # Claude Code or the person wrote it meanwhile: theirs wins, try next session
                return [], []
            mode = path.stat().st_mode & 0o777 if path.exists() else None
            replace_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)
        if seen != record:
            replace_file(home() / RECORD, json.dumps(seen, indent=2) + "\n")
    return done, undone


def _set(data: dict[str, Any], seen: dict[str, Any], key: str, value: Any) -> bool:
    """Put `value` at `key` in the settings when the person has no value of their own, recording who holds the key
    in `seen`; return whether the router set it.
    """
    if key in data:
        seen[key] = "user"
        return False
    data[key] = value
    seen[key] = "router"
    return True


def _retire(data: dict[str, Any], seen: dict[str, Any]) -> list[str]:
    """Take out each retired key the router wrote, while it still holds the value it wrote, and mark it retired in
    `seen` either way; return the keys taken out. A value the person changed stays theirs.
    """
    out = []
    for key, value in RETIRED.items():
        if seen.get(key) != "router":
            continue
        parent, _, leaf = key.rpartition(".")
        holder = data.get(parent) if parent else data
        if isinstance(holder, dict) and holder.get(leaf) == value:
            del holder[leaf]
            if parent and not holder:
                del data[parent]
            out.append(key)
        seen[key] = "retired"
    return out


def _stamp(path: Path) -> tuple[int, int] | None:
    """Return the modification time and size of `path`, or None when it does not exist."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def notice(done: list[str], undone: list[str]) -> str:
    """Return the one-line notice for the keys just set or taken out, or an empty string."""
    parts = []
    if done:
        said = [SAID[key] for key in done]
        parts.append("set " + (", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0]))
    if undone:
        parts.append("took out " + ", ".join(f"{key.rpartition('.')[2]}={RETIRED[key]}" for key in undone)
                     + " (written by router 0.23)")
    if not parts:
        return ""
    return (f"router: {'; '.join(parts)} in settings.json, from the next session on "
            "(once; `router config --natives off` stops it)")


def report() -> list[str]:
    """Return one line per key for `router doctor`: set by the router, set by the user, or not set."""
    if not enabled():
        return ["native settings: off (`router config --natives on` turns them on)"]
    data = read_json(settings_path())
    seen = read_json(home() / RECORD)
    lines = []
    for key in KEYS:
        if key not in data:
            lines.append(f"{key}: not set" + (" (removed after the router set it; it stays so)"
                                               if seen.get(key) == "router" else ""))
        else:
            lines.append(f"{key}: set by {'the router' if seen.get(key) == 'router' else 'the user'}")
    return lines
