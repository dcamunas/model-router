"""Native settings: written once per profile, only where missing, never over the person's own, never back."""

import json

import pytest

from model_router import natives
from model_router.cli import main
from model_router.hook import handle
from model_router.paths import claude_profile, write_config


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin data"))
    monkeypatch.delenv("CLAUDE_CODE_SUBAGENT_MODEL", raising=False)  # the session running the tests may set it
    monkeypatch.setattr(natives, "_claude_at_least", lambda version: True)
    return claude_profile() / "settings.json"


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_applies_the_missing_keys_once_and_keeps_the_rest(settings, tmp_path):
    _write(settings, {"model": "opus", "env": {"FOO": "1"}})
    assert natives.apply() == list(natives.KEYS)
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and data["autoCompactWindow"] == natives.AUTO_COMPACT_WINDOW
    assert data["env"] == {"FOO": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}
    assert data["statusLine"] == {"type": "command",
                                  "command": f"'{tmp_path}/plugin data/venv/bin/router' statusline"}
    assert natives.apply() == []


def test_never_overwrites_a_value_the_user_has(settings):
    _write(settings, {"autoCompactWindow": 200_000, "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"}})
    assert natives.apply() == ["statusLine"]
    data = json.loads(settings.read_text())
    assert data["autoCompactWindow"] == 200_000 and data["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] == "haiku"
    del data["autoCompactWindow"]
    _write(settings, data)
    assert natives.apply() == []  # the user's key was recorded: removing it later does not invite the router in
    assert "set by the user" in natives.report()[2]


def test_never_adds_back_a_key_the_user_removed(settings):
    natives.apply()
    data = json.loads(settings.read_text())
    del data["autoCompactWindow"], data["env"]
    _write(settings, data)
    assert natives.apply() == []
    assert "autoCompactWindow" not in json.loads(settings.read_text())
    assert natives.report()[0].startswith("autoCompactWindow: not set (removed")


def test_the_off_switch_writes_nothing(settings):
    write_config(natives=False)
    assert natives.apply() == [] and not settings.exists()
    assert natives.report() == ["native settings: off (`router config --natives on` turns them on)"]


def test_a_settings_file_that_is_not_an_object_is_left_alone(settings):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("[1, 2]")
    assert natives.apply() == [] and settings.read_text() == "[1, 2]"
    settings.write_text("{not json")
    with pytest.raises(ValueError):
        natives.apply()
    assert settings.read_text() == "{not json"


def test_the_status_line_waits_for_the_plugin_s_data_folder(settings, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    assert "statusLine" not in natives.apply()
    assert "statusLine" not in json.loads(settings.read_text())
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/data")
    assert natives.apply() == ["statusLine"]
    assert json.loads(settings.read_text())["statusLine"]["command"] == "/data/venv/bin/router statusline"


def test_session_start_says_it_once_and_never_in_the_router_s_own_sessions(settings, natives_hook, monkeypatch):
    monkeypatch.setattr("model_router.hook._natives", natives_hook)
    monkeypatch.setenv("MODEL_ROUTER_CHILD", "1")
    assert "systemMessage" not in (handle({"hook_event_name": "SessionStart"}) or {})
    assert not settings.exists()
    monkeypatch.delenv("MODEL_ROUTER_CHILD")
    out = handle({"hook_event_name": "SessionStart"}) or {}
    assert out["systemMessage"] == ("router: set autoCompactWindow 400000, the status line and "
                                    "CLAUDE_CODE_SUBAGENT_MODEL=sonnet in settings.json (once; "
                                    "`router config --natives off` stops it)")
    assert "Model router active" in out["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in (handle({"hook_event_name": "SessionStart"}) or {})


def test_a_failure_never_breaks_the_session_start(settings, natives_hook, monkeypatch, capsys):
    monkeypatch.setattr("model_router.hook._natives", natives_hook)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("{not json")
    out = handle({"hook_event_name": "SessionStart"}) or {}
    assert "additionalContext" in out["hookSpecificOutput"] and "systemMessage" not in out
    assert "native settings skipped" in capsys.readouterr().err


def test_config_switch_and_its_line(capsys):
    assert main(["config", "--natives", "off"]) == 0
    assert "native settings written once into settings.json: off" in capsys.readouterr().out
    assert main(["config", "--natives", "on"]) == 0
    assert "settings.json: on" in capsys.readouterr().out


def test_doctor_says_who_set_each_key(settings, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude binary: doctor still reads the settings
    _write(settings, {"autoCompactWindow": 300_000})
    natives.apply()
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ok     autoCompactWindow: set by the user" in out and "ok     statusLine: set by the router" in out
    assert "ok     env.CLAUDE_CODE_SUBAGENT_MODEL: set by the router" in out


def test_a_value_exported_in_the_shell_counts_as_the_user_s(settings, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "haiku")
    natives.apply()
    assert "env" not in json.loads(settings.read_text())
    assert json.loads((claude_profile() / "model-router" / natives.RECORD).read_text())[
        "env.CLAUDE_CODE_SUBAGENT_MODEL"] == "user"


def test_an_older_claude_code_gets_no_subagent_fallback_and_is_asked_again(settings, monkeypatch):
    monkeypatch.setattr(natives, "_claude_at_least", lambda version: False)
    natives.apply()
    assert "env" not in json.loads(settings.read_text())
    monkeypatch.setattr(natives, "_claude_at_least", lambda version: True)
    assert natives.apply() == ["env.CLAUDE_CODE_SUBAGENT_MODEL"]


def test_a_write_by_someone_else_meanwhile_wins(settings, monkeypatch):
    _write(settings, {"theme": "dark"})
    stamps = iter([(1, 10), (2, 12)])  # read, then changed before the router's write
    monkeypatch.setattr(natives, "_stamp", lambda path: next(stamps))
    assert natives.apply() == []
    assert json.loads(settings.read_text()) == {"theme": "dark"}
    assert not (claude_profile() / "model-router" / natives.RECORD).exists()  # tried again next session


def test_a_link_to_nothing_is_left_alone(settings, tmp_path):
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(tmp_path / "dotfiles" / "settings.json")
    assert natives.apply() == []
    assert not (tmp_path / "dotfiles").exists()
