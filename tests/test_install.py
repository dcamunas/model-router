import json

import pytest

from model_router import install as mod
from model_router.catalog import UnknownModelError
from model_router.install import install, uninstall


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


def test_settings_of_an_unexpected_shape_stop_install_with_one_line(home):
    profile = home / "profile"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text("[]")
    with pytest.raises(SystemExit, match="not a JSON object"):
        install(settings, no_skill=True)
    settings.write_text(json.dumps({"hooks": []}))
    with pytest.raises(SystemExit, match="`hooks`.*not an object"):
        install(settings, no_skill=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": "x"}}))
    with pytest.raises(SystemExit, match="not a list"):
        uninstall(settings, no_skill=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": ["x", {"hooks": "y"}]}}))
    assert install(settings, no_skill=True) == 0  # odd entries are kept as they are, the router's are added
    assert _settings(home)["hooks"]["PreToolUse"][:2] == ["x", {"hooks": "y"}]


def test_uninstall_leaves_the_hooks_when_a_foreign_skill_is_in_the_way(home, capsys):
    profile = home / "profile"
    profile.mkdir()
    settings = profile / "settings.json"
    settings.write_text("{}")
    assert install(settings, no_skill=True) == 0
    foreign = profile / "skills" / "router"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: other\n---\n")
    with pytest.raises(SystemExit, match="not the router skill"):
        uninstall(settings)
    assert "PreToolUse" in _settings(home)["hooks"]  # nothing was removed
    assert (foreign / "SKILL.md").exists()


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
    assert "available models: haiku, opus" in out


def test_install_is_idempotent_and_uninstall_removes_hooks_and_skill_but_keeps_config(home):
    profile = home / "profile"
    install(profile / "settings.json", profile_dir=profile, models="opus")
    install(profile / "settings.json", profile_dir=profile)
    assert len(_settings(home)["hooks"]["PreToolUse"]) == 1
    assert uninstall(profile / "settings.json") == 0
    assert "hooks" not in _settings(home)
    assert not (profile / "skills" / "router").exists()
    assert json.loads((home / "state" / "config.json").read_text())["models"] == ["opus"]


def test_link_mode_symlinks_the_skill_and_models_all_resets(home):
    profile = home / "profile"
    install(profile / "settings.json", profile_dir=profile, models="sonnet", link=True)
    assert (profile / "skills" / "router").is_symlink()
    install(profile / "settings.json", profile_dir=profile, models="all")
    assert not (profile / "skills" / "router").is_symlink()
    assert "models" not in json.loads((home / "state" / "config.json").read_text())


def test_a_bad_model_or_a_foreign_skill_stops_the_install_before_anything_is_written(home):
    profile = home / "profile"
    settings = profile / "settings.json"
    with pytest.raises(UnknownModelError, match="sonet"):
        install(settings, profile_dir=profile, no_skill=True, models="sonet")
    assert not settings.exists()
    foreign = profile / "skills" / "router"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: other\n---\n")
    with pytest.raises(SystemExit, match="not the router skill"):
        install(settings, profile_dir=profile)
    assert not settings.exists() and (foreign / "SKILL.md").exists()
    with pytest.raises(SystemExit, match="not the router skill"):
        uninstall(settings)
    assert (foreign / "SKILL.md").exists()


def test_missing_hook_binary_is_a_clear_error(home, monkeypatch):
    monkeypatch.setattr(mod, "hook_binary", lambda: None)
    with pytest.raises(SystemExit, match="router-hook"):
        install(home / "profile" / "settings.json")


def test_install_copies_the_read_only_agent_under_its_profile_name_and_uninstall_removes_it(tmp_path, monkeypatch):
    from model_router.install import copy_agent, remove_agent

    agents = tmp_path / "agents"
    copy_agent(agents)
    text = (agents / "router-reader.md").read_text()
    assert "\nname: router-reader\n" in text and "tools: Read, Grep, Glob, WebFetch, WebSearch" in text
    remove_agent(agents)
    assert not (agents / "router-reader.md").exists()
    (agents / "router-reader.md").write_text("---\nname: router-reader\n---\nsomeone else's agent")
    copy_agent(agents)
    remove_agent(agents)
    assert "someone else's agent" in (agents / "router-reader.md").read_text()  # never overwritten or removed
