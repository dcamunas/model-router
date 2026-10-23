"""The plugin turned on for a repository's team: settings merged, never overwritten, and doctor's check."""

import json

import pytest

from model_router import team
from model_router.cli import main


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_enable_writes_both_keys_and_keeps_everything_else(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".claude").mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}, "enabledPlugins": {"other@x": True},
                                    "extraKnownMarketplaces": {"x": {"source": {"source": "github", "repo": "x/y"}}}}))
    assert team.enable(str(repo)) == ["marketplace dcamunas (dcamunas/claude-plugins)", "plugin model-router@dcamunas enabled"]
    data = json.loads(settings.read_text())
    assert data["hooks"] == {"Stop": []} and data["enabledPlugins"] == {"other@x": True, "model-router@dcamunas": True}
    assert data["extraKnownMarketplaces"]["dcamunas"] == {"source": {"source": "github", "repo": "dcamunas/claude-plugins"}}
    assert "x" in data["extraKnownMarketplaces"]
    assert team.enable(str(repo)) == [] and team.enabled_in(str(repo))  # a second run changes nothing


def test_enable_keeps_the_file_mode_says_what_it_left_and_what_it_turned_back_on(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".claude").mkdir()
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": {team.PLUGIN_ID: False},
                                    "extraKnownMarketplaces": {"dcamunas": {"source": {"source": "git", "url": "x"}}}}))
    settings.chmod(0o644)
    changed = team.enable(str(repo))
    assert "left as it was" in changed[0] and changed[1].endswith("(it was turned off)")
    assert settings.stat().st_mode & 0o777 == 0o644
    data = json.loads(settings.read_text())
    assert data["extraKnownMarketplaces"]["dcamunas"]["source"]["url"] == "x" and data["enabledPlugins"][team.PLUGIN_ID]


def test_enable_creates_the_file_and_refuses_what_it_cannot_change_safely(tmp_path):
    repo = _repo(tmp_path)
    assert team.enable(str(repo)) and team.enabled_in(str(repo))
    settings = team.settings_path(str(repo))
    settings.write_text("{broken")
    with pytest.raises(team.SettingsError, match="not valid JSON"):
        team.enable(str(repo))
    settings.write_text("[]")
    with pytest.raises(team.SettingsError, match="JSON object"):
        team.enable(str(repo))
    settings.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    settings.symlink_to(target)
    with pytest.raises(team.SettingsError, match="regular file"):
        team.enable(str(repo))
    assert target.read_text() == "{}" and not team.enabled_in(str(repo))  # the link's target is left alone


def test_router_project_team_and_doctor_say_what_each_person_does(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    assert main(["project", str(repo), "--team"]) == 0
    assert team.INSTALL_COMMAND in capsys.readouterr().out
    profile = tmp_path / "profile"
    (profile / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.chdir(repo)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL   plugin turned on for the team" in out and team.INSTALL_COMMAND in out
    installed = profile / "plugins" / "installed_plugins.json"
    installed.write_text(json.dumps({"version": 2, "plugins": {team.PLUGIN_ID: [
        {"scope": "project", "projectPath": str(tmp_path / "another-repo")}]}}))
    main(["doctor"])
    assert "FAIL   plugin turned on for the team" in capsys.readouterr().out  # installed for another repository
    installed.write_text(json.dumps({"version": 2, "plugins": {team.PLUGIN_ID: [
        {"scope": "project", "projectPath": str(repo)}]}}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ok     plugin turned on for the team" in out and f"ok     plugin {team.PLUGIN_ID} enabled in" in out
    with pytest.raises(SystemExit):
        main(["project", str(tmp_path / "nowhere"), "--team"])
