"""The Claude Code plugin around the package: manifests in sync with pyproject, the skill reachable
from the manifest, and the hook bootstrap script run as Claude Code would run it."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hooks" / "router-hook.sh"


def _version() -> str:
    match = re.search(r'^version = "(.*)"$', (ROOT / "pyproject.toml").read_text(), re.M)
    assert match is not None
    return match.group(1)


VERSION = _version()


def test_the_manifest_names_the_plugin_and_the_version_of_pyproject():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "model-router" and plugin["version"] == VERSION
    skills = ROOT / plugin["skills"]
    assert plugin["skills"].startswith("./") and (skills / "router" / "SKILL.md").is_file()
    assert [(ROOT / agent).is_file() for agent in plugin["agents"]] == [True]
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for entries in hooks.values() for e in entries for h in e["hooks"]]
    assert all('"${CLAUDE_PLUGIN_ROOT}"/hooks/router-hook.sh' in c for c in commands)
    assert hooks["PreToolUse"][0]["matcher"] == "Agent" and "--bootstrap" not in commands[0]
    assert commands[1].endswith("--bootstrap")
    assert os.access(SCRIPT, os.X_OK)


def run_hook(data: Path, event: dict, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "CLAUDE_PLUGIN_ROOT": str(ROOT), "CLAUDE_PLUGIN_DATA": str(data), "CLAUDE_CONFIG_DIR": str(data / "profile")}
    return subprocess.run([str(SCRIPT), *args], input=json.dumps(event), text=True, capture_output=True,
                          env={**base, **(env or {})}, timeout=300)


def test_outside_a_plugin_or_before_bootstrap_the_hook_is_silent(tmp_path):
    proc = subprocess.run([str(SCRIPT)], input="{}", text=True, capture_output=True, env={"PATH": os.environ["PATH"]})
    assert proc.returncode == 0 and proc.stdout == "" and "CLAUDE_PLUGIN_ROOT" in proc.stderr
    proc = run_hook(tmp_path, {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": {"prompt": "x"}})
    assert proc.returncode == 0 and proc.stdout == "" and "not ready yet" in proc.stderr
    assert not (tmp_path / "venv").exists()


def test_bootstrap_without_uv_explains_and_exits_0(tmp_path):
    empty = tmp_path / "nobin"
    empty.mkdir()
    proc = run_hook(tmp_path / "data", {"hook_event_name": "SessionStart"}, "--bootstrap",
                    env={"PATH": str(empty), "HOME": str(tmp_path), "MODEL_ROUTER_UV": str(tmp_path / "no-uv")})
    assert proc.returncode == 0 and proc.stdout == "" and "cannot find uv" in proc.stderr


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_bootstrap_builds_the_venv_once_and_then_routes(tmp_path):
    data = tmp_path / "data"
    proc = run_hook(data, {"hook_event_name": "SessionStart", "source": "startup"}, "--bootstrap")
    assert proc.returncode == 0, proc.stderr
    assert "Model router active" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert (data / "installed-version").read_text() == VERSION
    pinned = (ROOT / ".python-version").read_text().strip()
    out = subprocess.run([str(data / "venv" / "bin" / "python"), "--version"], capture_output=True, text=True).stdout
    assert out.startswith(f"Python {pinned}"), out  # the plugin's pin, whatever the session's directory says
    venv_mtime = (data / "venv" / "bin" / "router-hook").stat().st_mtime
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "cwd": "/",
             "tool_input": {"subagent_type": "general-purpose",
                            "prompt": "risk: critical. Analyse the design of the payments migration"}}
    proc = run_hook(data, event)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "fable"
    (data / "profile" / "model-router").mkdir(parents=True, exist_ok=True)
    (data / "profile" / "model-router" / "config.json").write_text(json.dumps({"models": ["haiku", "sonnet", "opus"]}))
    proc = run_hook(data, event)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "opus"  # no Fable here
    proc = run_hook(data, {"hook_event_name": "SessionStart"}, "--bootstrap")
    assert proc.returncode == 0 and (data / "venv" / "bin" / "router-hook").stat().st_mtime == venv_mtime  # not rebuilt
    (data / "installed-version").write_text("0.0.0")
    proc = run_hook(data, {"hook_event_name": "SessionStart"}, "--bootstrap")
    assert proc.returncode == 0 and (data / "installed-version").read_text() == VERSION  # rebuilt on a version change
    (data / "installed-version").write_text("999.0.0")  # a newer session installed a newer version
    proc = run_hook(data, event)
    assert proc.returncode == 0 and "updatedInput" in proc.stdout and "a new session uses it" in proc.stderr
    venv_mtime = (data / "venv" / "bin" / "router-hook").stat().st_mtime
    proc = run_hook(data, {"hook_event_name": "SessionStart", "source": "compact"}, "--bootstrap")
    assert proc.returncode == 0 and "Model router active" in proc.stdout  # the old session still gets its context
    assert (data / "installed-version").read_text() == "999.0.0"  # and never puts its older version back
    assert (data / "venv" / "bin" / "router-hook").stat().st_mtime == venv_mtime



@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_bootstrap_rebuilds_a_venv_built_on_another_python(tmp_path):
    data = tmp_path / "data"
    fake = data / "venv" / "bin" / "python"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\nexit 1\n")  # an interpreter that fails the version check
    fake.chmod(0o755)
    proc = run_hook(data, {"hook_event_name": "SessionStart", "source": "startup"}, "--bootstrap")
    assert proc.returncode == 0, proc.stderr
    assert "Model router active" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert b"exit 1" not in fake.read_bytes() and (data / "venv" / "bin" / "router-hook").is_file()
