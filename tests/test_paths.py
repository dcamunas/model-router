"""Each Claude profile keeps its own router state, the old shared folder is reported, and the plugin's `router`
command finds the environment of its own profile."""

import os
import shutil
import subprocess
from pathlib import Path

from model_router.cli import main
from model_router.paths import home

ROOT = Path(__file__).resolve().parents[1]


def test_the_state_lives_in_the_profile_in_use_and_two_profiles_share_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_HOME")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "personal"))
    personal = home()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))
    assert personal == tmp_path / "personal" / "model-router" and home() == tmp_path / "work" / "model-router"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert home() == tmp_path / ".claude" / "model-router"
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "elsewhere"))
    assert home() == tmp_path / "elsewhere"


def _doctor(capsys) -> list[str]:
    main(["doctor"])
    return [line for line in capsys.readouterr().out.splitlines() if line.startswith("FAIL")]


def test_doctor_says_how_to_move_the_old_shared_folder_and_catches_a_move_that_nested_it(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MODEL_ROUTER_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    state = tmp_path / "profile" / "model-router"
    (tmp_path / ".model-router").mkdir()
    assert any("no longer read" in line and f"`mv {tmp_path / '.model-router'} {state}`" in line for line in _doctor(capsys))
    state.mkdir(parents=True)  # a session ran before the move: a plain mv would nest the old folder
    assert any("merge by hand" in line for line in _doctor(capsys))
    (tmp_path / ".model-router").rename(state / ".model-router")
    fails = _doctor(capsys)
    assert not any("no longer read" in line for line in fails) and any("moved inside the new one" in line for line in fails)


def test_a_profile_recorded_in_a_moved_folder_is_ignored_and_reported(tmp_path, monkeypatch, capsys):
    from model_router.cli_client import resolve_profile
    from model_router.paths import write_config

    monkeypatch.delenv("MODEL_ROUTER_HOME")
    monkeypatch.delenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))
    write_config(claude_config_dir=str(tmp_path / "personal"))  # what the old shared folder carried
    assert resolve_profile()[0] == tmp_path / "work"
    assert any("records the profile" in line and "ignored" in line for line in _doctor(capsys))


def _cached_plugin(profile: Path) -> Path:
    """Lay the plugin's `router` out as Claude Code caches a marketplace plugin, and return it."""
    bin_dir = profile / "plugins" / "cache" / "a-market" / "model-router" / "1.0.0" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "router", bin_dir / "router")
    return bin_dir / "router"


def _fake_cli(venv_bin: Path, says: str) -> None:
    venv_bin.mkdir(parents=True)
    cli = venv_bin / "router"
    cli.write_text(f'#!/bin/sh\necho "{says} $*"\n')
    cli.chmod(0o755)


def test_the_plugin_command_runs_the_cli_of_its_own_profile(tmp_path):
    router = _cached_plugin(tmp_path / "work")
    env = {"PATH": os.environ["PATH"]}
    missing = subprocess.run([str(router), "doctor"], capture_output=True, text=True, env=env, check=False)
    assert missing.returncode == 1 and "not ready yet" in missing.stderr
    _fake_cli(tmp_path / "work" / "plugins" / "data" / "model-router-a-market" / "venv" / "bin", "work")
    _fake_cli(tmp_path / "personal" / "plugins" / "data" / "model-router-a-market" / "venv" / "bin", "personal")
    ran = subprocess.run([str(router), "limits", "--check", "fable"], capture_output=True, text=True, env=env, check=False)
    assert ran.stdout.strip() == "work limits --check fable"
    _fake_cli(tmp_path / "hook-data" / "venv" / "bin", "hook")
    ran = subprocess.run([str(router)], capture_output=True, text=True, check=False,
                         env={**env, "CLAUDE_PLUGIN_DATA": str(tmp_path / "hook-data")})
    assert ran.stdout.strip() == "hook"
    link = tmp_path / "elsewhere" / "router"
    link.parent.mkdir()
    link.symlink_to(router)
    assert subprocess.run([str(link), "x"], capture_output=True, text=True, env=env, check=False).stdout.strip() == "work x"


def test_the_command_of_a_plugin_loaded_from_a_folder_uses_its_venv_or_says_so(tmp_path):
    folder = tmp_path / "checkout"
    (folder / "bin").mkdir(parents=True)
    shutil.copy(ROOT / "bin" / "router", folder / "bin" / "router")
    env = {"PATH": os.environ["PATH"]}
    missing = subprocess.run([str(folder / "bin" / "router")], capture_output=True, text=True, env=env, check=False)
    assert missing.returncode == 1 and "uv sync" in missing.stderr
    _fake_cli(folder / ".venv" / "bin", "checkout")
    ran = subprocess.run([str(folder / "bin" / "router"), "stats"], capture_output=True, text=True, env=env, check=False)
    assert ran.stdout.strip() == "checkout stats"
