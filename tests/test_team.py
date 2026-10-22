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
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    assert main(["project", str(repo), "--team"]) == 0
    assert team.INSTALL_COMMAND in capsys.readouterr().out
    profile = tmp_path / "profile"
    (profile / "plugins").mkdir(parents=True)
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.chdir(repo)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL   plugin turned on for the team" in out and team.INSTALL_COMMAND in out
    (profile / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {team.PLUGIN_ID: [{"scope": "project"}]}}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ok     plugin turned on for the team" in out and "FAIL   hook PreToolUse" not in out
    with pytest.raises(SystemExit):
        main(["project", str(tmp_path / "nowhere"), "--team"])
