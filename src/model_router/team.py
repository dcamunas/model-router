"""The plugin for everyone who works in a repository: declared once in its `.claude/settings.json`, installed by each.

Claude Code reads two keys of a repository's settings once a teammate trusts the folder:
`extraKnownMarketplaces` registers the catalogue the plugin comes from, and `enabledPlugins` turns the plugin
on. It does not install a plugin hosted elsewhere on its own: each person runs the install command once (see
INSTALL_COMMAND), and the plugin builds its environment with uv at the next session. `enable` writes the two
keys, keeping everything else in the file; `router doctor` says when a repository has the plugin on and this
profile does not have it installed.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any  # Any: Claude Code settings, decoded JSON

from .paths import read_regular

MARKETPLACE = "dcamunas"
MARKETPLACE_REPO = "dcamunas/claude-plugins"
PLUGIN_ID = f"model-router@{MARKETPLACE}"  # the id Claude Code uses once installed from the marketplace
INSTALL_COMMAND = f"claude plugin install {PLUGIN_ID} --scope project"
MAX_SETTINGS_BYTES = 1024 * 1024


class SettingsError(ValueError):
    """Signal that a repository's settings file cannot be changed safely: a link, not JSON, or not a JSON object."""


def settings_path(project: str) -> Path:
    """Return the repository's shared Claude Code settings, the file a team commits."""
    return Path(project) / ".claude" / "settings.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {}
    raw = read_regular(path, MAX_SETTINGS_BYTES)
    if raw is None:
        raise SettingsError(f"{path} is not a regular file (a link, or too big): change it by hand")
    try:
        data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError as exc:
        raise SettingsError(f"{path} is not valid JSON ({exc}): fix it first") from exc
    if not isinstance(data, dict):
        raise SettingsError(f"{path} is not a JSON object: fix it first")
    return data


def enable(project: str) -> list[str]:
    """Declare the marketplace and turn the plugin on in the repository's settings, keeping every other key; return
    what changed (nothing when both were there already).

    Raises:
        SettingsError: the settings file cannot be changed safely.
    """
    path = settings_path(project)
    data = _read(path)
    changed = []
    marketplaces = data.setdefault("extraKnownMarketplaces", {})
    plugins = data.setdefault("enabledPlugins", {})
    if not isinstance(marketplaces, dict) or not isinstance(plugins, dict):
        raise SettingsError(f"{path}: extraKnownMarketplaces and enabledPlugins must be objects")
    if MARKETPLACE not in marketplaces:
        marketplaces[MARKETPLACE] = {"source": {"source": "github", "repo": MARKETPLACE_REPO}}
        changed.append(f"marketplace {MARKETPLACE} ({MARKETPLACE_REPO})")
    if plugins.get(PLUGIN_ID) is not True:
        plugins[PLUGIN_ID] = True
        changed.append(f"plugin {PLUGIN_ID} enabled")
    if changed:
        _write(path, data)
    return changed


def _write(path: Path, data: dict[str, Any]) -> None:
    """Replace the file in one step, so a teammate's session never reads half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        Path(tmp).replace(path)
    except BaseException:  # an interrupt too: never leave a half-written temporary file next to the settings
        Path(tmp).unlink(missing_ok=True)
        raise


def enabled_in(project: str | None) -> bool:
    """Say whether the repository's settings turn the plugin on (False when they cannot be read)."""
    if not project:
        return False
    try:
        plugins = _read(settings_path(project)).get("enabledPlugins")
    except SettingsError:
        return False
    return isinstance(plugins, dict) and plugins.get(PLUGIN_ID) is True


def installed_in(profile: Path) -> bool:
    """Say whether the Claude profile at `profile` has the plugin installed, at any scope."""
    try:
        data = json.loads((profile / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    return isinstance(plugins, dict) and bool(plugins.get(PLUGIN_ID))
