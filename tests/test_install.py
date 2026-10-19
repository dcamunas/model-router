import json

import pytest

from model_router import install as mod
from model_router.install import install


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    hook = tmp_path / "env" / "bin" / "router-hook"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n")
    monkeypatch.setattr(mod, "hook_binary", lambda: hook)
    return tmp_path


def _settings(home):
    return json.loads((home / "profile" / "settings.json").read_text())


def test_install_writes_hooks_copies_skill_and_remembers_profile_and_models(home, capsys):
    profile = home / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": []}]}}))
    assert install(profile / "settings.json", profile_dir=profile, models="haiku, opus") == 0
    data = _settings(home)
    assert data["model"] == "opus" and "Stop" in data["hooks"]  # untouched
    pre = data["hooks"]["PreToolUse"]
    assert pre[0]["matcher"] == "Agent" and pre[0]["hooks"][0]["command"] == str(home / "env" / "bin" / "router-hook")
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("router-hook")
    skill = profile / "skills" / "router" / "SKILL.md"
    assert skill.is_file() and not skill.parent.is_symlink() and "name: router" in skill.read_text()
    cfg = json.loads((home / "state" / "config.json").read_text())
    assert cfg == {"claude_config_dir": str(profile.resolve()), "models": ["haiku", "opus"]}
    out = capsys.readouterr().out
    assert "modelos disponibles: haiku, opus" in out


def test_install_is_idempotent_and_uninstall_cleans_up(home):
    profile = home / "profile"
    install(profile / "settings.json", profile_dir=profile)
    install(profile / "settings.json", profile_dir=profile)
    assert len(_settings(home)["hooks"]["PreToolUse"]) == 1
    install(profile / "settings.json", uninstall=True)
    assert "hooks" not in _settings(home)
    assert not (profile / "skills" / "router").exists()


def test_link_mode_symlinks_the_skill_and_models_all_resets(home):
    profile = home / "profile"
    install(profile / "settings.json", profile_dir=profile, models="sonnet", link=True)
    assert (profile / "skills" / "router").is_symlink()
    install(profile / "settings.json", profile_dir=profile, models="all")
    assert not (profile / "skills" / "router").is_symlink()
    assert "models" not in json.loads((home / "state" / "config.json").read_text())


def test_refuses_a_foreign_skill_dir_and_bad_models(home):
    profile = home / "profile"
    foreign = profile / "skills" / "router"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: otra\n---\n")
    with pytest.raises(SystemExit):
        install(profile / "settings.json", profile_dir=profile)
    with pytest.raises(ValueError):
        install(profile / "settings.json", profile_dir=profile, no_skill=True, models="sonet")


def test_missing_hook_binary_is_a_clear_error(home, monkeypatch):
    monkeypatch.setattr(mod, "hook_binary", lambda: None)
    with pytest.raises(SystemExit, match="router-hook"):
        install(home / "profile" / "settings.json")
