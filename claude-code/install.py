#!/usr/bin/env python3
"""Enable or disable the router inside a Claude Code profile or repo. Idempotent, standard library only.

    python3 claude-code/install.py                       # profile in $CLAUDE_CONFIG_DIR or ~/.claude
    python3 claude-code/install.py --settings ~/.claude/settings.json
    python3 claude-code/install.py --settings .claude/settings.json --no-skill   # a repo
    python3 claude-code/install.py --uninstall

It adds two hooks (PreToolUse on Agent, SessionStart) that call the `router-hook` entry point of this
project's virtualenv, and links the `router` skill into the profile's skills directory. Existing
settings are preserved; previous router entries are replaced.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROUTER_HOME = Path(os.environ.get("MODEL_ROUTER_HOME", "~/.model-router")).expanduser()
HOOK_BIN = REPO / ".venv" / "bin" / "router-hook"
SKILL_SRC = REPO / "claude-code" / "router"
MARK = "router-hook"


def default_settings() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return base / "settings.json"


def hook_entries() -> dict[str, list[dict]]:
    cmd = {"type": "command", "command": str(HOOK_BIN), "timeout": 10, "statusMessage": "router: eligiendo modelo"}
    return {
        "PreToolUse": [{"matcher": "Agent", "hooks": [cmd]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(HOOK_BIN), "timeout": 10}]}],
    }


def _is_router(entry: dict) -> bool:
    return any(MARK in str(h.get("command", "")) for h in entry.get("hooks", []))


def update_settings(path: Path, *, uninstall: bool) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    for event, entries in hook_entries().items():
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
    print(f"{'quitado de' if uninstall else 'hooks en'} {path}")


def update_skill(skills_dir: Path, *, uninstall: bool) -> None:
    link = skills_dir / "router"
    if link.is_symlink() or link.exists():
        if not link.is_symlink():
            sys.exit(f"{link} existe y no es un enlace; quítalo a mano")
        link.unlink()
    if uninstall:
        print(f"skill quitada de {link}")
        return
    skills_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(SKILL_SRC, target_is_directory=True)
    print(f"skill enlazada: {link} -> {SKILL_SRC}")


def remember_profile(profile: Path) -> None:
    """Tell `router run` which Claude profile to use when no ROUTER_CLAUDE_CONFIG_DIR is exported."""
    cfg = ROUTER_HOME / "config.json"
    data = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    data["claude_config_dir"] = str(profile)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"perfil para `router run`: {profile} (en {cfg})")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--settings", type=Path, default=default_settings(), help="settings.json a tocar")
    p.add_argument("--skills-dir", type=Path, help="carpeta de skills (por defecto, junto al settings)")
    p.add_argument("--no-skill", action="store_true", help="solo hooks, sin enlazar la skill")
    p.add_argument("--profile-dir", type=Path,
                   help="perfil de Claude que usara `router run` (por defecto, el del settings si es un perfil)")
    p.add_argument("--uninstall", action="store_true")
    args = p.parse_args()
    if not args.uninstall and not HOOK_BIN.exists():
        sys.exit(f"falta {HOOK_BIN}: ejecuta `uv sync` en {REPO} primero")
    update_settings(args.settings.expanduser(), uninstall=args.uninstall)
    if not args.no_skill:
        update_skill((args.skills_dir or args.settings.expanduser().parent / "skills").expanduser(), uninstall=args.uninstall)
    if not args.uninstall:
        profile = args.profile_dir or (args.settings.expanduser().parent if args.settings == default_settings() else None)
        if profile is not None:
            remember_profile(profile.expanduser().resolve())
        else:
            print("perfil para `router run` sin fijar (pasa --profile-dir o exporta ROUTER_CLAUDE_CONFIG_DIR)")
        print("activo en las sesiones nuevas; en una sesión abierta, /hooks recarga la configuración")
    return 0


if __name__ == "__main__":
    sys.exit(main())
