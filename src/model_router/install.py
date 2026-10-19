"""`router install` / `router uninstall`: wire the router into a Claude Code profile or repo.

Idempotent. `install` adds two hooks (PreToolUse on Agent, SessionStart) that run the `router-hook`
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
from typing import Any

from .catalog import MODELS, parse_models
from .paths import read_config, save_config

SKILL_SRC = Path(__file__).resolve().parent / "claude_code" / "router"
HOOK_NAME = "router-hook"
HOOK_EVENTS = ("PreToolUse", "SessionStart")


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
    """Return the settings.json entries that run `hook_bin` on PreToolUse(Agent) and SessionStart."""
    cmd = {"type": "command", "command": str(hook_bin), "timeout": 10, "statusMessage": "router: eligiendo modelo"}
    return {
        "PreToolUse": [{"matcher": "Agent", "hooks": [cmd]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(hook_bin), "timeout": 10}]}],
    }


def _is_router(entry: dict[str, Any]) -> bool:
    return any(HOOK_NAME in str(h.get("command", "")) for h in entry.get("hooks", []))


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise SystemExit(f"{path} no es JSON válido ({exc}); arréglalo antes, Claude Code lo ignora entero") from exc


def _rewrite_hooks(path: Path, entries: dict[str, list[dict[str, Any]]] | None) -> None:
    """Replace the router's hook entries in `path` with `entries`, or remove them when `entries` is None."""
    data = _read_settings(path)
    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        kept = [e for e in hooks.get(event, []) if not _is_router(e)]
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
    print(f"hooks en {path}")


def remove_hooks(path: Path) -> None:
    """Remove the router hooks from the settings file at `path`, keeping every other setting."""
    _rewrite_hooks(path, None)
    print(f"hooks quitados de {path}")


def _is_ours(path: Path) -> bool:
    skill = path / "SKILL.md"
    return skill.is_file() and "name: router" in skill.read_text(encoding="utf-8")[:200]


def _check_replaceable(target: Path) -> None:
    """Stop with a message when `target` exists and is not the router skill (a copy or a symlink)."""
    if target.is_symlink():
        return
    if target.is_dir() and not _is_ours(target):
        raise SystemExit(f"{target} existe y no es la skill del router; quítala a mano")
    if target.exists() and not target.is_dir():
        raise SystemExit(f"{target} existe y no es una carpeta; quítala a mano")


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
        print(f"skill enlazada: {target} -> {SKILL_SRC}")
    else:
        shutil.copytree(SKILL_SRC, target)
        print(f"skill copiada en {target}")


def remove_skill(skills_dir: Path) -> None:
    """Remove the `router` skill from `skills_dir`; a skill that is not the router's is left alone."""
    target = skills_dir / "router"
    _clear_skill(target)
    print(f"skill quitada de {target}")


def model_names(models: str) -> list[str] | None:
    """Return the short names in `models` ('haiku,opus', any order) in catalogue order, or None for
    'all', which lifts the restriction.

    Raises:
        UnknownModelError: a name outside the catalogue, so a typo never silently changes the lattice.
    """
    if models.strip().lower() == "all":
        return None
    return [MODELS[m].short for m in parse_models(models)]


def remember(profile: Path | None, models: list[str] | None, *, reset_models: bool = False) -> None:
    """Record in config.json the Claude profile for `router run` and the models this installation may
    route to; `reset_models` removes that restriction. A None value leaves its key untouched.
    """
    if profile is None and models is None and not reset_models:
        return
    cfg = read_config()
    if profile is not None:
        cfg["claude_config_dir"] = str(profile)
    if models is not None:
        cfg["models"] = models
    elif reset_models:
        cfg.pop("models", None)
    path = save_config(cfg)
    if profile is not None:
        print(f"perfil para `router run`: {profile} (en {path})")
    if models is not None or reset_models:
        shown = ", ".join(cfg.get("models") or [spec.short for spec in MODELS.values()])
        print(f"modelos disponibles: {shown} (en {path})")


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
        raise SystemExit("no encuentro `router-hook` junto a este intérprete ni en el PATH: instala el paquete "
                         "(uv tool install ..., o uv sync en el repo) y vuelve a ejecutar `router install`")
    skills = (skills_dir or settings.parent / "skills").expanduser()
    if not no_skill:
        _check_replaceable(skills / "router")
    add_hooks(settings, hook_bin)
    if not no_skill:
        copy_skill(skills, link=link)
    profile = profile_dir or (settings.parent if settings == default_settings() else None)
    if profile is not None:
        profile = profile.expanduser().resolve()
    elif models is None:
        print("perfil para `router run` sin fijar (pasa --profile-dir o exporta ROUTER_CLAUDE_CONFIG_DIR)")
    remember(profile, names, reset_models=models is not None and names is None)
    print("activo en las sesiones nuevas; en una sesión abierta, /hooks recarga la configuración")
    return 0


def uninstall(settings: Path, *, skills_dir: Path | None = None, no_skill: bool = False) -> int:
    """Remove the router hooks and skill from the profile whose settings file is `settings` and
    return 0. config.json is kept, so a later `install` finds the same profile and models.
    """
    settings = settings.expanduser()
    remove_hooks(settings)
    if not no_skill:
        remove_skill((skills_dir or settings.parent / "skills").expanduser())
    return 0
