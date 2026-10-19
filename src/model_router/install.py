"""`router install` / `router uninstall`: wire the router into a Claude Code profile or repo.
Idempotent. Adds two hooks (PreToolUse on Agent, SessionStart) that run the `router-hook` entry point
installed next to this interpreter, copies the `router` skill into the profile's skills directory
(or links it with --link, for development), and records in ~/.model-router/config.json the profile
that `router run` will use and, with --models, the models this installation may route to.
Existing settings are preserved; previous router entries are replaced."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .catalog import MODELS, parse_models
from .paths import config_path, read_config

SKILL_SRC = Path(__file__).resolve().parent / "claude_code" / "router"
MARK = "router-hook"


def default_settings() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return base / "settings.json"


def hook_binary() -> Path | None:
    """The `router-hook` executable of the environment running `router`: a project venv or a
    `uv tool` environment. Falls back to PATH, so a plain `pip install --user` also works."""
    for sub, name in (("bin", MARK), ("Scripts", MARK + ".exe")):
        candidate = Path(sys.prefix) / sub / name
        if candidate.exists():
            return candidate
    found = shutil.which(MARK)
    return Path(found).resolve() if found else None


def hook_entries(hook_bin: Path) -> dict[str, list[dict]]:
    cmd = {"type": "command", "command": str(hook_bin), "timeout": 10, "statusMessage": "router: eligiendo modelo"}
    return {
        "PreToolUse": [{"matcher": "Agent", "hooks": [cmd]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(hook_bin), "timeout": 10}]}],
    }


def _is_router(entry: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in entry.get("hooks", []))


def update_settings(path: Path, hook_bin: Path | None, *, uninstall: bool) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError as exc:
        raise SystemExit(f"{path} no es JSON valido ({exc}); arreglalo antes, Claude Code lo ignora entero") from exc
    hooks = data.setdefault("hooks", {})
    for event, entries in hook_entries(hook_bin or Path(MARK)).items():
        kept = [e for e in hooks.get(event, []) if not _is_router(e)]
        if not uninstall:
            kept += entries
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{'hooks quitados de' if uninstall else 'hooks en'} {path}")


def _is_ours(path: Path) -> bool:
    skill = path / "SKILL.md"
    return skill.is_file() and "name: router" in skill.read_text(encoding="utf-8")[:200]


def update_skill(skills_dir: Path, *, uninstall: bool, link: bool = False) -> None:
    target = skills_dir / "router"
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        if not _is_ours(target):
            raise SystemExit(f"{target} existe y no es la skill del router; quitala a mano")
        shutil.rmtree(target)
    elif target.exists():
        raise SystemExit(f"{target} existe y no es una carpeta; quitala a mano")
    if uninstall:
        print(f"skill quitada de {target}")
        return
    skills_dir.mkdir(parents=True, exist_ok=True)
    if link:
        target.symlink_to(SKILL_SRC, target_is_directory=True)
        print(f"skill enlazada: {target} -> {SKILL_SRC}")
    else:
        shutil.copytree(SKILL_SRC, target)
        print(f"skill copiada en {target}")


def remember(profile: Path | None, models: str | None) -> None:
    """Record in config.json the Claude profile for `router run` and, with `models`, the models this
    installation may route to ('all' removes the restriction)."""
    cfg = read_config()
    if profile is not None:
        cfg["claude_config_dir"] = str(profile)
    if models is not None:
        if models.strip().lower() == "all":
            cfg.pop("models", None)
        else:
            cfg["models"] = [MODELS[m].short for m in parse_models(models)]
    if profile is None and models is None:
        return
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if profile is not None:
        print(f"perfil para `router run`: {profile} (en {path})")
    if models is not None:
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
    uninstall: bool = False,
) -> int:
    settings = settings.expanduser()
    hook_bin = None
    if not uninstall:
        hook_bin = hook_binary()
        if hook_bin is None:
            raise SystemExit("no encuentro `router-hook` junto a este interprete ni en el PATH: instala el paquete "
                             "(uv tool install ..., o uv sync en el repo) y vuelve a ejecutar `router install`")
    update_settings(settings, hook_bin, uninstall=uninstall)
    if not no_skill:
        update_skill((skills_dir or settings.parent / "skills").expanduser(), uninstall=uninstall, link=link)
    if uninstall:
        return 0
    profile = profile_dir or (settings.parent if settings == default_settings() else None)
    if profile is not None:
        profile = profile.expanduser().resolve()
    elif models is None:
        print("perfil para `router run` sin fijar (pasa --profile-dir o exporta ROUTER_CLAUDE_CONFIG_DIR)")
    remember(profile, models)
    print("activo en las sesiones nuevas; en una sesion abierta, /hooks recarga la configuracion")
    return 0
