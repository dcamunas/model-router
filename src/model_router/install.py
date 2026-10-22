"""`router install` / `router uninstall`: wire the router into a Claude Code profile or repo.

Idempotent. `install` adds four hooks (PreToolUse on Agent, SessionStart, SubagentStop, StopFailure) that run the `router-hook`
entry point installed next to this interpreter, copies the `router` skill into the profile's skills
directory (or links it, for development), and records in ~/.model-router/config.json the profile
that `router run` will use and, when asked, the models this installation may route to. `uninstall`
removes the hooks and the skill and leaves config.json alone. Other settings are preserved and
previous router entries are replaced. Everything is validated before anything is written.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any  # Any: settings.json, decoded

from .catalog import MODELS, parse_models
from .paths import CONFIG_DIR_ENV, read_config, save_config

SKILL_SRC = Path(__file__).resolve().parent / "claude_code" / "router"
HOOK_NAME = "router-hook"
HOOK_EVENTS = ("PreToolUse", "SessionStart", "SubagentStop", "StopFailure")


def default_settings() -> Path:
    """Return the settings.json of the active Claude Code profile ($CLAUDE_CONFIG_DIR or ~/.claude)."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return base / "settings.json"


def hook_binary() -> Path | None:
    """Locate the `router-hook` executable of the environment running `router`: a project venv or a
    `uv tool` environment, then the PATH, so a plain `pip install --user` also works.
    """
    for sub, name in (("bin", HOOK_NAME), ("Scripts", HOOK_NAME + ".exe")):
        candidate = Path(sys.prefix) / sub / name
        if candidate.exists():
            return candidate
    found = shutil.which(HOOK_NAME)
    return Path(found).resolve() if found else None


def hook_entries(hook_bin: Path) -> dict[str, list[dict[str, Any]]]:
    """Return the settings.json entries that run `hook_bin` on PreToolUse(Agent), SessionStart, SubagentStop
    (which measures what each subagent spent) and StopFailure (which reads a spent usage limit and its reset).
    """
    cmd = {"type": "command", "command": str(hook_bin), "timeout": 10, "statusMessage": "router: picking a model"}
    return {
        "PreToolUse": [{"matcher": "Agent", "hooks": [cmd]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(hook_bin), "timeout": 10}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": str(hook_bin), "timeout": 30}]}],
        "StopFailure": [{"hooks": [{"type": "command", "command": str(hook_bin), "timeout": 60}]}],
    }


def _is_router(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(isinstance(h, dict) and HOOK_NAME in str(h.get("command", "")) for h in hooks)


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise SystemExit(f"{path} is not valid JSON ({exc}); fix it first, Claude Code ignores the whole file") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object; fix it first, Claude Code ignores the whole file")
    return data


def _rewrite_hooks(path: Path, entries: dict[str, list[dict[str, Any]]] | None) -> None:
    """Replace the router's hook entries in `path` with `entries`, or remove them when `entries` is None."""
    data = _read_settings(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"`hooks` in {path} is not an object; fix it first")
    for event in HOOK_EVENTS:
        present = hooks.get(event, [])
        if not isinstance(present, list):
            raise SystemExit(f"`hooks.{event}` in {path} is not a list; fix it first")
        kept = [e for e in present if not _is_router(e)]
        if entries is not None:
            kept += entries.get(event, [])
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def add_hooks(path: Path, hook_bin: Path) -> None:
    """Add the router hooks to the settings file at `path`, replacing previous router entries."""
    _rewrite_hooks(path, hook_entries(hook_bin))
    print(f"hooks in {path}")


def remove_hooks(path: Path) -> None:
    """Remove the router hooks from the settings file at `path`, keeping every other setting."""
    _rewrite_hooks(path, None)
    print(f"hooks removed from {path}")


def _is_ours(path: Path) -> bool:
    skill = path / "SKILL.md"
    return skill.is_file() and "name: router" in skill.read_text(encoding="utf-8")[:200]


def _check_replaceable(target: Path) -> None:
    """Stop with a message when `target` exists and is not the router skill (a copy or a symlink)."""
    if target.is_symlink():
        return
    if target.is_dir() and not _is_ours(target):
        raise SystemExit(f"{target} exists and is not the router skill; remove it by hand")
    if target.exists() and not target.is_dir():
        raise SystemExit(f"{target} exists and is not a directory; remove it by hand")


def _clear_skill(target: Path) -> None:
    _check_replaceable(target)
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)


def copy_skill(skills_dir: Path, *, link: bool = False) -> None:
    """Copy the `router` skill into `skills_dir`, or symlink it to the package with `link`, replacing
    a previous copy. Refuses to replace a skill that is not the router's.
    """
    target = skills_dir / "router"
    _clear_skill(target)
    skills_dir.mkdir(parents=True, exist_ok=True)
    if link:
        target.symlink_to(SKILL_SRC, target_is_directory=True)
        print(f"skill linked: {target} -> {SKILL_SRC}")
    else:
        shutil.copytree(SKILL_SRC, target)
        print(f"skill copied to {target}")


def remove_skill(skills_dir: Path) -> None:
    """Remove the `router` skill from `skills_dir`; a skill that is not the router's is left alone."""
    target = skills_dir / "router"
    _clear_skill(target)
    print(f"skill removed from {target}")


def model_names(models: str) -> list[str] | None:
    """Return the short names in `models` ('haiku,opus', any order) in catalogue order, or None for
    'all', which lifts the restriction.

    Raises:
        UnknownModelError: a name outside the catalogue, so a typo never silently changes the lattice.
    """
    if models.strip().lower() == "all":
        return None
    return [MODELS[m].short for m in parse_models(models)]


def remember(
    profile: Path | None, models: list[str] | None, *, reset_models: bool = False, judge: str | None = None,
    calibration: str | None = None,
) -> None:
    """Record in config.json the Claude profile for `router run`, the models this installation may route
    to (`reset_models` removes that restriction), the judge cell for full verification and the active
    calibration (an empty string removes it, so the code tables decide). A None value leaves its key untouched.
    """
    cfg = read_config()
    lines = _apply_config(cfg, profile, models, reset_models=reset_models, judge=judge, calibration=calibration)
    if not lines:
        return
    path = save_config(cfg)
    for line in lines:
        print(f"{line} (in {path})")


def _apply_config(
    cfg: dict[str, Any], profile: Path | None, models: list[str] | None, *, reset_models: bool, judge: str | None,
    calibration: str | None = None,
) -> list[str]:
    """Apply the requested changes to `cfg` in place and return one line per change, for the user."""
    lines = []
    if profile is not None:
        cfg["claude_config_dir"] = str(profile)
        lines.append(f"profile for `router run`: {profile}")
    if models is not None or reset_models:
        if models is not None:
            cfg["models"] = models
        else:
            cfg.pop("models", None)
        lines.append("available models: " + ", ".join(cfg.get("models") or [spec.short for spec in MODELS.values()]))
    if judge is not None:
        cfg["judge"] = judge
        lines.append(f"judge for full verification: {judge}")
    if calibration is not None:
        if calibration:
            cfg["calibration"] = calibration
        else:
            cfg.pop("calibration", None)
        lines.append(f"calibration: {calibration or 'none, the built-in tables decide'}")
    return lines


def install(
    settings: Path,
    *,
    skills_dir: Path | None = None,
    no_skill: bool = False,
    link: bool = False,
    profile_dir: Path | None = None,
    models: str | None = None,
) -> int:
    """Wire the router into the profile (or repo) whose settings file is `settings` and return 0.

    The model list, the hook binary and the skill target are checked before anything is written, so
    a failed install leaves the settings as they were.

    Raises:
        UnknownModelError: `models` names a model outside the catalogue.
        SystemExit: the hook binary is missing, the settings file is not JSON, or a foreign skill is
            in the way; the message says what to do.
    """
    settings = settings.expanduser()
    names = model_names(models) if models is not None else None
    hook_bin = hook_binary()
    if hook_bin is None:
        raise SystemExit("cannot find `router-hook` next to this interpreter or on the PATH: install the package "
                         "(uv tool install ..., or uv sync in the repo) and run `router install` again")
    skills = (skills_dir or settings.parent / "skills").expanduser()
    if not no_skill:
        _check_replaceable(skills / "router")
    add_hooks(settings, hook_bin)
    if not no_skill:
        copy_skill(skills, link=link)
        copy_agent(settings.parent / "agents")
    profile = profile_dir or (settings.parent if settings == default_settings() else None)
    if profile is not None:
        profile = profile.expanduser().resolve()
    elif models is None:
        print(f"profile for `router run` not set (pass --profile-dir or export {CONFIG_DIR_ENV})")
    remember(profile, names, reset_models=models is not None and names is None)
    print("active in new sessions; in an open session, /hooks reloads the configuration")
    return 0


def uninstall(settings: Path, *, skills_dir: Path | None = None, no_skill: bool = False) -> int:
    """Remove the router hooks and skill from the profile whose settings file is `settings` and
    return 0. config.json is kept, so a later `install` finds the same profile and models.
    """
    settings = settings.expanduser()
    skills = (skills_dir or settings.parent / "skills").expanduser()
    if not no_skill:
        _check_replaceable(skills / "router")  # before touching the settings, so a foreign skill stops everything
    remove_hooks(settings)
    if not no_skill:
        remove_skill(skills)
        remove_agent(settings.parent / "agents")
    return 0


AGENT_SRC = SKILL_SRC.parent / "agents" / "reader.md"
AGENT_NAME = "router-reader"  # the hook looks for this name in the profile's agents directory


def copy_agent(agents_dir: Path) -> None:
    """Copy the read-only `reader` agent into `agents_dir` as `router-reader`, the name the hook routes to when it
    does not run from the plugin; a file of that name that is not the router's is left alone.
    """
    target = agents_dir / f"{AGENT_NAME}.md"
    if target.exists() and "model router" not in target.read_text(encoding="utf-8").lower():
        print(f"{target} exists and is not the router's: the read-only agent was not installed")
        return
    agents_dir.mkdir(parents=True, exist_ok=True)
    text = AGENT_SRC.read_text(encoding="utf-8").replace("\nname: reader\n", f"\nname: {AGENT_NAME}\n", 1)
    target.write_text(text, encoding="utf-8")
    print(f"read-only agent {AGENT_NAME}: {target} (sessions started from now on load it)")


def remove_agent(agents_dir: Path) -> None:
    """Remove the router's read-only agent from `agents_dir`; one of that name that is not the router's stays."""
    target = agents_dir / f"{AGENT_NAME}.md"
    if target.is_file() and "model router" in target.read_text(encoding="utf-8").lower():
        target.unlink()
