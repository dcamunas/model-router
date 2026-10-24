import io
import json
import stat

import pytest

from model_router.cli import main
from model_router.paths import claude_profile, write_config


@pytest.fixture
def home(state):
    """The router's state folder in the test's profile."""
    return state


def test_plan_command_needs_no_api(capsys):
    assert main(["plan", "Implement a function that parses DD/MM/YYYY dates", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "classification:" in out and "start:" in out
    assert "ladder" not in out and "verif" not in out and "budget" not in out and "cost" not in out


def test_plan_claude_code_prints_frontmatter_values(capsys):
    main(["plan", "summarise this text", "--kind", "summarize", "--risk", "low", "--claude-code"])
    assert capsys.readouterr().out == "model: haiku\neffort: medium\n"


def test_plan_for_a_workflow_needs_no_text_when_kind_and_risk_are_declared(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("never read"))
    assert main(["plan", "--kind", "summarize", "--risk", "low", "--workflow"]) == 0
    assert capsys.readouterr().out == "{model: 'haiku', effort: 'medium'}\n"
    with pytest.raises(SystemExit):
        main(["plan", "--kind", "code", "--workflow", "--claude-code"])  # one shape at a time


def test_cells_lists_every_cell_of_the_lattice_and_never_max(capsys):
    assert main(["cells"]) == 0
    out = capsys.readouterr().out
    assert "fable/xhigh" in out and "/max" not in out


def test_stats_says_there_is_nothing_yet_on_an_empty_home(home, capsys):
    assert main(["stats"]) == 0
    assert "no hook decisions and no measured subagents" in capsys.readouterr().out


def test_a_missing_task_file_is_a_one_line_error(home, capsys):
    assert main(["plan", "--file", str(home / "nope.md")]) == 2 and "cannot access" in capsys.readouterr().err


def test_doctor_survives_a_profile_whose_settings_have_an_odd_shape(capsys):
    settings = claude_profile() / "settings.json"
    settings.write_text("[]")
    main(["doctor"])
    assert "not a JSON object" in capsys.readouterr().out
    settings.write_text(json.dumps({"hooks": [], "enabledPlugins": [], "modelSettings": 3, "model": "opus"}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL   plugin model-router@dcamunas is not enabled" in out and "main chat: model opus" in out


def _fake_claude(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\necho 9.9.9\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(fake_bin))


def test_doctor_reports_the_plugin_off_and_the_claude_version(tmp_path, monkeypatch, capsys):
    _fake_claude(tmp_path, monkeypatch)
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "ok     claude on PATH" in out and "9.9.9" in out
    assert f"ok     Claude profile: {claude_profile()}" in out
    assert "FAIL   plugin model-router@dcamunas is not enabled" in out and "claude plugin install" in out


def test_doctor_shows_the_effort_of_the_chat_model_whatever_its_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude binary: doctor still reads the settings
    (claude_profile() / "settings.json").write_text(json.dumps({
        "model": "fable", "modelSettings": {"claude-fable-5-1": {"effortLevel": "low"}},
    }))
    main(["doctor"])
    assert "main chat: model fable, effort low" in capsys.readouterr().out


def test_an_unknown_model_is_said_in_the_plan_and_the_doctor_and_refused_by_config(home, capsys):
    write_config(models=["haiku", "sonet"])
    assert main(["plan", "classify this", "--kind", "classify", "--risk", "low"]) == 0
    assert "does not know, ignored: sonet" in capsys.readouterr().out
    main(["doctor"])
    out = capsys.readouterr().out
    assert "available models: haiku" in out and "does not know, ignored: sonet" in out
    assert main(["config", "--models", "opsu"]) == 2 and "unknown model 'opsu'" in capsys.readouterr().err
    assert json.loads((home / "config.json").read_text())["models"] == ["haiku", "sonet"]


def test_stats_skips_corrupt_lines_in_the_hook_log(home, capsys):
    good = json.dumps({"cell": "opus/low", "kind": "code", "risk": "medium"})
    (home / "hook.jsonl").write_text(good + "\n{\"cell\": \"opus/l\n" + good + "\n")
    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "opus/low" in out and "opus/l " not in out
    assert out.splitlines()[3].split() == ["opus/low", "code", "medium", "2"]


def test_config_shows_and_sets_the_models_the_judge_and_the_reader(home, capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"Claude profile: {claude_profile()}" in out and "haiku, sonnet, opus, fable" in out
    assert "judge for critical reviews on the chat's model: opus/xhigh (default)" in out
    assert "read-only agent for reading tasks: on" in out
    assert main(["config", "--models", "haiku, opus", "--lean", "off"]) == 0
    assert json.loads((home / "config.json").read_text()) == {"models": ["haiku", "opus"], "lean": False}
    assert "read-only agent for reading tasks: off" in capsys.readouterr().out
    assert main(["config", "--lean", "on"]) == 0
    assert "lean" not in json.loads((home / "config.json").read_text())
    assert main(["config", "--models", "all"]) == 0
    assert "models" not in json.loads((home / "config.json").read_text())
    assert main(["config", "--judge", "opus/max"]) == 0
    assert json.loads((home / "config.json").read_text())["judge"] == "opus/max"
    main(["doctor"])  # exits 1 here because the plugin is not installed; the judge line is what matters
    out = capsys.readouterr().out
    assert "judge for critical reviews on the chat's model: opus/xhigh (via config.json)" in out  # capped at xhigh
    assert main(["config", "--models", "opsu"]) == 2 and "unknown model 'opsu'" in capsys.readouterr().err
    assert main(["config", "--judge", "opus/turbo"]) == 2 and "unknown effort" in capsys.readouterr().err
    assert not (home / "settings.json").exists()


def test_doctor_recognises_the_plugin_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    profile = claude_profile()
    (profile / "settings.json").write_text(json.dumps({"enabledPlugins": {"model-router@dcamunas": True}}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ok     plugin model-router@dcamunas enabled" in out
    assert "FAIL   plugin environment" in out and "needs uv" in out
    venv_hook = profile / "plugins" / "data" / "model-router-dcamunas" / "venv" / "bin" / "router-hook"
    venv_hook.parent.mkdir(parents=True)
    venv_hook.write_text("")
    main(["doctor"])
    assert "ok     plugin environment" in capsys.readouterr().out


def test_every_subcommand_prints_its_help(capsys):
    import pytest as _pytest

    for cmd in ("plan", "stats", "doctor", "cells", "config", "project", "limits", "agents", "usage", "statusline"):
        with _pytest.raises(SystemExit) as exc:
            main([cmd, "--help"])
        assert exc.value.code == 0, cmd
        assert "usage: router " + cmd in capsys.readouterr().out
    for gone in ("run", "judge"):
        with _pytest.raises(SystemExit) as exc:
            main([gone, "x"])
        assert exc.value.code == 2, gone

