import json
import re
import stat

import pytest

from model_router import cli
from model_router.cli import main
from model_router.testing import FakeClient, Reply


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    return tmp_path


def test_plan_command_needs_no_api(capsys):
    assert main(["plan", "Implement a function that parses DD/MM/YYYY dates", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "start:" in out and "ladder:" in out and "verifier" in out


def test_plan_prints_both_judges_of_a_panel(capsys):
    argv = ["plan", "Assess whether we should migrate the billing database", "--kind", "analysis", "--risk", "critical"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "verification panel" in out and "verifier (panel): fable/low then fable/medium" in out


def test_plan_claude_code_prints_frontmatter_values(capsys):
    main(["plan", "summarise this text", "--kind", "summarize", "--risk", "low", "--claude-code"])
    assert capsys.readouterr().out == "model: haiku\neffort: medium\n"


def test_bench_without_a_file_runs_the_simulation_and_prints_the_table(capsys):
    assert main(["bench", "--simulate", "--tasks", "12", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "SIMULATION" in out and "router" in out


def test_bench_with_a_missing_file_is_a_one_line_error(tmp_path):
    with pytest.raises(SystemExit, match="cannot read"):
        main(["bench", str(tmp_path / "nope.yaml"), "--yes"])


def test_cells_lists_every_cell_of_the_lattice(capsys):
    assert main(["cells"]) == 0
    assert "fable/max" in capsys.readouterr().out


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


def test_feedback_overrides_the_verdict_in_the_ledger_and_the_feedback_store(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")]}))
    main(["run", "classify", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"])
    match = re.search(r"· id (\w+)", capsys.readouterr().out)
    assert match is not None
    task_id = match.group(1)
    assert main(["feedback", task_id, "--ko", "--note", "the label was NEGATIVE"]) == 0
    assert capsys.readouterr().out.strip() == f"{task_id}: marked as fail"
    main(["stats"])
    out = capsys.readouterr().out
    assert "tasks 1 · solved 0" in out
    store = json.loads((home / "feedback.json").read_text())
    assert store["classify"]["haiku/low"] == {"n": 1, "pass": 0, "n_first": 1, "pass_first": 0}
    with pytest.raises(SystemExit, match="no task nope"):
        main(["feedback", "nope", "--ok"])


def test_bench_file_runs_tasks_in_parallel(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")] * 6}))
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "low", "prompt": "classify", "check_regex": "POSITIVE"} for i in range(3)
    ]}))
    assert main(["bench", str(spec), "--yes", "--jobs", "2", "--strategies", "router"]) == 0
    out = capsys.readouterr().out
    assert out.count(" solved $") == 3 and "tasks 3" in out
    assert len(list((home / "bench").glob("*.jsonl"))) == 1


def _fake_claude(tmp_path, monkeypatch):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\necho 9.9.9\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(fake_bin))


def test_doctor_reports_missing_hooks(tmp_path, monkeypatch, capsys):
    _fake_claude(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    assert main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "ok     claude on PATH" in out and "9.9.9" in out
    assert "FAIL   hook PreToolUse(Agent)" in out and "FAIL   skill router" in out
    hook = tmp_path / "router-hook"
    hook.write_text("")
    (profile / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {
        "PreToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": str(hook)}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(hook)}]}]}}))
    (profile / "skills" / "router").mkdir(parents=True)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL   hook" not in out and "ok     skill router" in out and "model opus" in out


def test_doctor_shows_the_effort_of_the_chat_model_whatever_its_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude binary: doctor still reads the settings
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    (profile / "settings.json").write_text(json.dumps({
        "model": "fable", "modelSettings": {"claude-fable-5-1": {"effortLevel": "low"}},
    }))
    main(["doctor"])
    assert "main chat: model fable, effort low" in capsys.readouterr().out


def test_an_unknown_model_is_a_one_line_error_not_a_traceback(home, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonet")
    with pytest.raises(SystemExit, match="unknown model 'sonet'.*haiku, sonnet, opus, fable"):
        main(["plan", "classify this", "--kind", "classify", "--risk", "low"])
    monkeypatch.delenv("MODEL_ROUTER_MODELS")
    with pytest.raises(SystemExit, match="unknown model 'opsu'"):
        main(["install", "--settings", str(home / "s.json"), "--no-skill", "--models", "opsu"])
    assert not (home / "s.json").exists()


def test_stats_skips_corrupt_lines_in_the_logs_and_says_so(home, capsys):
    good = json.dumps({"cell": "opus/low", "kind": "code", "risk": "medium"})
    (home / "hook.jsonl").write_text(good + "\n{\"cell\": \"opus/l\n" + good + "\n")
    task = {"type": "task", "task_id": "t1", "kind": "code", "status": "solved", "attempts": [], "usage": {}}
    (home / "ledger.jsonl").write_text(json.dumps(task) + "\n{\"type\": \"ta\n")
    assert main(["stats"]) == 0
    out, err = capsys.readouterr()
    assert "opus/low" in out and "tasks 1" in out
    assert "1 unreadable lines" in err


def test_uninstall_command_only_takes_profile_flags(home, monkeypatch, capsys):
    from model_router import install as mod

    monkeypatch.setattr(mod, "hook_binary", lambda: home / "router-hook")
    settings = home / "profile" / "settings.json"
    assert main(["install", "--settings", str(settings), "--no-skill"]) == 0
    assert "hooks" in json.loads(settings.read_text())
    assert main(["uninstall", "--settings", str(settings), "--no-skill"]) == 0
    assert "hooks" not in json.loads(settings.read_text())
    with pytest.raises(SystemExit):
        main(["uninstall", "--settings", str(settings), "--models", "opus"])
    capsys.readouterr()


def test_config_shows_and_sets_the_profile_and_the_models_without_touching_hooks(home, capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "profile for `router run`: not set" in out and "haiku, sonnet, opus, fable" in out
    assert main(["config", "--models", "haiku, opus", "--profile-dir", str(home / "p")]) == 0
    cfg = json.loads((home / "config.json").read_text())
    assert cfg == {"claude_config_dir": str((home / "p").resolve()), "models": ["haiku", "opus"]}
    assert main(["config", "--models", "all"]) == 0
    assert "models" not in json.loads((home / "config.json").read_text())
    with pytest.raises(SystemExit, match="unknown model 'opsu'"):
        main(["config", "--models", "opsu"])
    assert not (home / "settings.json").exists()


def test_doctor_recognises_the_plugin_install(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    (profile / "settings.json").write_text(json.dumps({"enabledPlugins": {"model-router@dcamunas": True}}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "ok     plugin model-router@dcamunas enabled" in out and "hook PreToolUse" not in out
    assert "FAIL   plugin environment" in out and "needs uv" in out
    venv_hook = profile / "plugins" / "data" / "model-router-dcamunas" / "venv" / "bin" / "router-hook"
    venv_hook.parent.mkdir(parents=True)
    venv_hook.write_text("")
    main(["doctor"])
    assert "ok     plugin environment" in capsys.readouterr().out
