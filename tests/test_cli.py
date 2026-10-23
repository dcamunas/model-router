import io
import json
import stat

import pytest

from model_router import cli
from model_router.cli import main
from model_router.paths import claude_profile, write_config
from model_router.testing import FakeClient, Reply


@pytest.fixture
def home(state):
    """The router's state folder in the test's profile."""
    return state


def test_plan_command_needs_no_api(capsys):
    assert main(["plan", "Implement a function that parses DD/MM/YYYY dates", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "start:" in out and "ladder:" in out and "verifier" in out


def test_plan_at_critical_risk_prints_one_judge_on_another_model(capsys):
    argv = ["plan", "Assess whether we should migrate the billing database", "--kind", "analysis", "--risk", "critical"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "verification full" in out and "verifier (full): opus/xhigh" in out


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


def test_stats_says_there_are_no_tasks_on_an_empty_home(home, capsys):
    assert main(["stats"]) == 0
    assert "no tasks" in capsys.readouterr().out


def test_run_prints_the_answer_the_attempts_and_records_the_task(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("def f(): return 42")]}))
    assert main(["run", "Implement f returning 42", "--check-regex", "return 42"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("def f(): return 42\n")
    assert "classification: code / medium" in out
    assert "pass (checker)" in out and "status solved" in out and "retries 0" in out
    assert err == ""
    records = [json.loads(line) for line in (home / "ledger.jsonl").read_text().splitlines()]
    assert len(records) == 1 and records[0]["status"] == "solved" and records[0]["attempts"][0]["cell"] == "opus/low"
    assert "prompt" not in records[0] and "answer" not in records[0]


def test_run_json_gives_the_result_and_the_answer(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply('{"a": 1}')]}))
    assert main(["run", "give me a JSON object", "--kind", "extract", "--risk", "low", "--check-json", "a", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "solved" and data["answer"] == '{"a": 1}' and len(data["attempts"]) == 1
    assert data["attempts"][0]["reason"] == "valid JSON"


def test_run_exits_1_and_warns_when_nothing_passes(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("no")]}))
    assert main(["run", "classify this", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"]) == 1
    out, err = capsys.readouterr()
    assert "status unsolved" in out and "does not match /POSITIVE/" in out
    assert "did not pass verification" in err


def test_attempts_table_fits_the_terminal_width(monkeypatch):
    from model_router.runner import Router
    from model_router.task import Task, contains_checker

    client = FakeClient(script={"*": [Reply("42")]})
    result = Router(client).run(Task(prompt="f", kind="code", risk="medium", checker=contains_checker("42")))
    result.attempts[0].reason = "x" * 200
    wide = cli._attempts_table(result, width=400)
    assert max(len(line) for line in wide.splitlines()) < 400 and "x" * cli.REASON_COLUMN_CHARS in wide
    narrow = cli._attempts_table(result, width=100)
    assert max(len(line) for line in narrow.splitlines()) <= 100 and "x" * 24 not in narrow
    tiny = cli._attempts_table(result, width=50)  # narrower than the fixed columns: the reason keeps its minimum
    assert "x" * cli.MIN_REASON_CHARS in tiny and "x" * (cli.MIN_REASON_CHARS + 1) not in tiny
    monkeypatch.setenv("COLUMNS", "100")
    assert cli._attempts_table(result) == narrow  # the terminal's width, through COLUMNS


def test_judge_command_verifies_a_file_and_exits_1_on_a_fail(home, monkeypatch, capsys):
    answer = home / "answer.md"
    answer.write_text("The notice.")
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(judge={"*": [Reply(parsed=_verdict(True))]}))
    assert main(["judge", "write the notice", "--kind", "write", "--risk", "critical", "--answer", str(answer)]) == 0
    out = capsys.readouterr().out
    assert "verdict pass" in out and "external" in out and "pass (judge, fable/low)" in out  # one judge, never two
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(judge={"*": [Reply(parsed=_verdict(False))]}))
    assert main(["judge", "write the notice", "--kind", "write", "--risk", "high", "--answer", str(answer), "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "unsolved" and data["attempts"][0]["verdicts"][0]["passed"] is False
    main(["stats"])
    assert "tasks 2 · solved 1" in capsys.readouterr().out
    assert main(["judge", "x", "--answer", str(home / "nope.md")]) == 2 and "cannot access" in capsys.readouterr().err
    answer.write_text("   ")
    assert main(["judge", "x", "--answer", str(answer)]) == 2 and "answer is empty" in capsys.readouterr().err


def _verdict(passed: bool):
    from model_router.verify import Verdict

    return Verdict(passed=passed, confidence=0.9, reason="because")


def test_a_missing_task_or_system_file_is_a_one_line_error(home, capsys):
    assert main(["plan", "--file", str(home / "nope.md")]) == 2 and "cannot access" in capsys.readouterr().err
    assert main(["plan", "x", "--system", str(home / "nope.txt")]) == 2 and "cannot access" in capsys.readouterr().err


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


def test_stats_skips_corrupt_lines_in_the_logs_and_says_so(home, capsys):
    good = json.dumps({"cell": "opus/low", "kind": "code", "risk": "medium"})
    (home / "hook.jsonl").write_text(good + "\n{\"cell\": \"opus/l\n" + good + "\n")
    task = {"type": "task", "task_id": "t1", "kind": "code", "status": "solved", "attempts": [], "usage": {}}
    (home / "ledger.jsonl").write_text(json.dumps(task) + "\n{\"type\": \"ta\n")
    assert main(["stats"]) == 0
    out, err = capsys.readouterr()
    assert "opus/low" in out and "tasks 1" in out
    assert "1 unreadable lines" in err


def test_config_shows_and_sets_the_models_the_judge_and_the_reader(home, capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"profile for `router run`: {claude_profile()}" in out and "haiku, sonnet, opus, fable" in out
    assert "judge for full verification: opus/xhigh (default)" in out and "read-only agent for reading tasks: on" in out
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
    assert "judge for full verification: opus/xhigh (via config.json)" in capsys.readouterr().out  # capped at xhigh
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

    for cmd in ("plan", "run", "judge", "stats", "doctor", "cells", "config", "project", "limits", "agents"):
        with _pytest.raises(SystemExit) as exc:
            main([cmd, "--help"])
        assert exc.value.code == 0, cmd
        assert "usage: router " + cmd in capsys.readouterr().out


def test_stats_leaves_out_records_of_a_verification_mode_this_router_lacks(home, capsys):
    from model_router.ledger import Ledger, summarize
    from model_router.runner import Router
    from model_router.task import Task

    ledger = Ledger(home / "ledger.jsonl")
    client = FakeClient(script={"*": [Reply("x")]}, judge={"*": [Reply(parsed=_verdict(True))]})
    Router(client, ledger=ledger).run(Task(prompt="x", kind="write", risk="critical"))
    foreign = {**ledger.tasks()[0], "task_id": "foreign", "verify": "twice"}
    with (home / "ledger.jsonl").open("a") as handle:
        handle.write(json.dumps({"type": "task", **foreign}) + "\n")
    assert summarize(ledger.tasks()).tasks == 1
