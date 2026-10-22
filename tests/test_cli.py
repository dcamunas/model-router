import json
import re
import stat

import pytest

from model_router import cli
from model_router.cli import main
from model_router.policy import Policy
from model_router.testing import FakeClient, Reply


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    return tmp_path


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


def _task_id(run_output: str) -> str:
    match = re.search(r"· id (\w+)", run_output)
    assert match is not None
    return match.group(1)


def test_feedback_override_moves_a_count_only_when_the_verdict_changes(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")]}))
    main(["run", "classify", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"])
    task_id = _task_id(capsys.readouterr().out)
    counts = lambda: json.loads((home / "feedback.json").read_text())["classify"]["haiku/low"]  # noqa: E731
    assert counts() == {"n": 1, "pass": 1, "n_first": 1, "pass_first": 1}
    main(["feedback", task_id, "--ok"])  # already a pass: nothing moves
    assert counts() == {"n": 1, "pass": 1, "n_first": 1, "pass_first": 1}
    main(["feedback", task_id, "--ko"])
    main(["feedback", task_id, "--ko"])  # repeating a correction does not drain other tasks' passes
    assert counts() == {"n": 1, "pass": 0, "n_first": 1, "pass_first": 0}
    main(["feedback", task_id, "--ok"])
    assert counts() == {"n": 1, "pass": 1, "n_first": 1, "pass_first": 1}
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("hello")]}))
    main(["run", "hi", "--kind", "chat", "--risk", "low"])  # accepted without any verification
    task_id = _task_id(capsys.readouterr().out)
    main(["feedback", task_id, "--ko"])
    assert "chat" not in json.loads((home / "feedback.json").read_text())  # nothing was ever counted for it


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
    with pytest.raises(SystemExit, match="cannot access"):
        main(["judge", "x", "--answer", str(home / "nope.md")])
    answer.write_text("   ")
    with pytest.raises(SystemExit, match="answer is empty"):
        main(["judge", "x", "--answer", str(answer)])


def _verdict(passed: bool):
    from model_router.verify import Verdict

    return Verdict(passed=passed, confidence=0.9, reason="because")


def test_a_missing_task_or_system_file_is_a_one_line_error(home):
    with pytest.raises(SystemExit, match="cannot access"):
        main(["plan", "--file", str(home / "nope.md")])
    with pytest.raises(SystemExit, match="cannot access"):
        main(["plan", "x", "--system", str(home / "nope.txt")])


def test_bench_without_a_terminal_to_confirm_is_cancelled(home, monkeypatch, capsys):
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [{"id": "c1", "kind": "chat", "risk": "low", "prompt": "hi"}]}))

    def no_terminal(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert main(["bench", str(spec)]) == 1
    assert "cancelled" in capsys.readouterr().out


def test_doctor_survives_a_profile_whose_settings_have_an_odd_shape(home, monkeypatch, capsys):
    profile = home / "p"
    profile.mkdir()
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    (profile / "settings.json").write_text("[]")
    main(["doctor"])
    assert "not a JSON object" in capsys.readouterr().out
    (profile / "settings.json").write_text(json.dumps({"hooks": [], "enabledPlugins": [], "modelSettings": 3, "model": "opus"}))
    main(["doctor"])
    out = capsys.readouterr().out
    assert "hook PreToolUse(Agent)" in out and "main chat: model opus" in out


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
    assert "FAIL   hook SubagentStop" in out and "FAIL   read-only agent router-reader" in out  # an older install
    data = json.loads((profile / "settings.json").read_text())
    data["hooks"]["SubagentStop"] = [{"hooks": [{"type": "command", "command": str(hook)}]}]
    (profile / "settings.json").write_text(json.dumps(data))
    (profile / "agents").mkdir()
    (profile / "agents" / "router-reader.md").write_text("---\nname: router-reader\n---\nmodel router")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FAIL   hook" not in out and "ok     skill router" in out and "model opus" in out
    assert "ok     read-only agent router-reader" in out


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


def test_an_unknown_model_is_said_in_the_plan_and_refused_by_install(home, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonet")
    assert main(["plan", "classify this", "--kind", "classify", "--risk", "low"]) == 0
    assert "does not know, ignored: sonet" in capsys.readouterr().out
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
    assert "judge for full verification: opus/xhigh (default)" in out
    assert main(["config", "--models", "haiku, opus", "--profile-dir", str(home / "p")]) == 0
    cfg = json.loads((home / "config.json").read_text())
    assert cfg == {"claude_config_dir": str((home / "p").resolve()), "models": ["haiku", "opus"]}
    assert main(["config", "--models", "all"]) == 0
    assert "models" not in json.loads((home / "config.json").read_text())
    assert main(["config", "--judge", "opus/max"]) == 0
    assert json.loads((home / "config.json").read_text())["judge"] == "opus/max"
    main(["doctor"])  # exits 1 here because the profile directory does not exist; the judge line is what matters
    assert "judge for full verification: opus/xhigh (via config.json)" in capsys.readouterr().out  # capped at xhigh
    with pytest.raises(SystemExit, match="unknown model 'opsu'"):
        main(["config", "--models", "opsu"])
    with pytest.raises(SystemExit, match="unknown effort"):
        main(["config", "--judge", "opus/turbo"])
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


def _ok_verdict():
    from model_router.catalog import Usage
    from model_router.verify import Verdict

    return Reply(parsed=Verdict(passed=True, confidence=0.9, reason="fine"), usage=Usage(2000, 100))


def test_bench_dry_run_validates_the_file_and_calls_nothing(home, monkeypatch, capsys):
    def no_client(_args):
        raise AssertionError("a dry run must not build a client")

    monkeypatch.setattr(cli, "_live_client", no_client)
    spec = home / "t.yaml"
    spec.write_text("tasks:\n  - id: a\n    kind: classify\n    risk: low\n    prompt: classify this\n"
                    "    check_regex: POSITIVE\n  - id: b\n    kind: write\n    risk: critical\n    prompt: write it\n")
    argv = ["bench", str(spec), "--dry-run", "--strategies", "router,haiku/low", "--verify", "auto", "--repeat", "3"]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "2 tasks (1 with a checker) x 2 strategies x 3 repeat(s) · eval " in out and "dry run" in out
    assert "haiku/low: verification full, none" in out
    assert not (home / "bench").exists()


def test_bench_repeat_records_eval_and_repeat_in_the_ledger_and_the_index(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")] * 8}))
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "low", "prompt": f"classify {i}", "check_regex": "POSITIVE"} for i in range(2)
    ]}))
    assert main(["bench", str(spec), "--yes", "--strategies", "haiku/low", "--repeat", "2"]) == 0
    assert "tasks 4" in capsys.readouterr().out
    files = list((home / "bench").glob("*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert records[0]["type"] == "bench" and records[0]["strategy"] == "haiku/low" and records[0]["repeat"] == 2
    tasks = {r["task_id"]: r for r in records if r["type"] == "task"}
    assert sorted(tasks) == ["c0.r1", "c0.r2", "c1.r1", "c1.r2"]
    assert {r["repeat"] for r in tasks.values()} == {1, 2} and len({r["eval_id"] for r in tasks.values()}) == 1
    assert tasks["c0.r1"]["fingerprint"] == tasks["c0.r2"]["fingerprint"] != tasks["c1.r1"]["fingerprint"]
    import sqlite3

    with sqlite3.connect(home / "router.db") as conn:
        rows = conn.execute("SELECT DISTINCT strategy, eval_id FROM task").fetchall()
    assert rows == [("haiku/low", tasks["c0.r1"]["eval_id"])]


def test_bench_verify_sets_the_mode_of_fixed_cells(home, monkeypatch, capsys):
    client = FakeClient(script={"*": [Reply("draft")]}, judge={"*": [_ok_verdict()]})
    monkeypatch.setattr(cli, "_live_client", lambda args: client)
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [{"id": "w", "kind": "write", "risk": "critical", "prompt": "write it"}]}))
    assert main(["bench", str(spec), "--yes", "--strategies", "opus/medium", "--verify", "auto"]) == 0
    capsys.readouterr()
    assert main(["bench", str(spec), "--yes", "--strategies", "opus/medium"]) == 0
    sources = []
    for path in sorted((home / "bench").glob("*.jsonl")):  # one file, or two when the second runs a second later
        records = [json.loads(line) for line in path.read_text().splitlines()]
        sources += [r["attempts"][0]["verdict_source"] for r in records if r["type"] == "task"]
    assert sorted(sources) == ["judge", "none"]


def test_stats_reads_the_index_and_matches_the_ledger(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")]}))
    main(["run", "classify", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"])
    capsys.readouterr()
    assert main(["stats"]) == 0
    from_db = capsys.readouterr().out
    assert main(["stats", "--source", "ledger"]) == 0
    assert capsys.readouterr().out == from_db and "tasks 1 · solved 1" in from_db
    assert (home / "router.db").exists()
    with (home / "ledger.jsonl").open("a") as fh:  # a line written behind the index's back
        fh.write(json.dumps({"type": "task", "task_id": "x", "kind": "chat", "risk": "low", "status": "unsolved",
                             "attempts": [], "retries": 0}) + "\n")
    main(["stats"])
    assert "tasks 2 · solved 1" in capsys.readouterr().out


def test_db_status_and_rebuild(home, monkeypatch, capsys):
    assert main(["db"]) == 0
    assert "not built yet" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")]}))
    main(["run", "classify", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"])
    capsys.readouterr()
    assert main(["db", "rebuild"]) == 0
    assert "rebuilt" in capsys.readouterr().out
    assert main(["db"]) == 0
    out = capsys.readouterr().out
    assert "1 tasks" in out and "in sync" in out


def test_a_broken_index_does_not_stop_a_run_and_stats_rebuilds_it(home, monkeypatch, capsys):
    (home / "router.db").write_bytes(b"garbage")
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")]}))
    assert main(["run", "classify", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVE"]) == 0
    captured = capsys.readouterr()
    assert "1: POSITIVE" in captured.out and "warning: the index was not updated" in captured.err
    assert main(["stats"]) == 0
    assert "tasks 1 · solved 1" in capsys.readouterr().out
    assert main(["db"]) == 0 and "in sync" in capsys.readouterr().out


def test_calibrate_reports_from_the_index_and_apply_makes_the_policy_start_from_it(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVE")] * 40}))
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "medium", "prompt": f"classify {i}", "check_regex": "POSITIVE"}
        for i in range(6)
    ]}))
    assert main(["bench", str(spec), "--yes", "--matrix", "--cells", "haiku/low,sonnet/low"]) == 0
    capsys.readouterr()
    assert main(["calibrate"]) == 0
    out = capsys.readouterr().out
    assert "12 first attempts with an independent verdict" in out and "compared with the built-in table" in out
    assert "classify x medium: haiku/medium -> haiku/low (100% of 6" in out and "--apply writes" in out
    assert main(["plan", "classify this", "--kind", "classify", "--risk", "medium"]) == 0
    assert "start: haiku/medium" in capsys.readouterr().out
    assert main(["calibrate", "--apply"]) == 0
    assert "saved" in capsys.readouterr().out
    cal_id = json.loads((home / "config.json").read_text())["calibration"]
    assert (home / "calibrations" / f"{cal_id}.json").exists()
    assert main(["plan", "classify this", "--kind", "classify", "--risk", "medium"]) == 0
    out = capsys.readouterr().out
    assert "start: haiku/low" in out and f"(calibration {cal_id})" in out
    ok, line = cli._calibration_check()
    assert ok and line.startswith(f"calibration: {cal_id} (made ") and "1 changes to the table) via config.json" in line
    main(["run", "classify this", "--kind", "classify", "--risk", "medium", "--check-regex", "POSITIVE"])
    capsys.readouterr()
    last = json.loads((home / "ledger.jsonl").read_text().splitlines()[-1])
    assert last["calibration_id"] == cal_id and last["plan"][0] == "haiku/low"
    with_cal, builtin = cli._parse_strategy(f"router@{cal_id}")[1], cli._parse_strategy("router@builtin")[1]
    assert isinstance(with_cal, Policy) and isinstance(builtin, Policy)
    assert with_cal.calibration_id == cal_id and builtin.calibration_id == "builtin"
    with pytest.raises(SystemExit, match="cannot be loaded"):
        cli._parse_strategy("router@nope")
    assert main(["calibrate"]) == 0
    assert f"compared with calibration {cal_id}" in capsys.readouterr().out
    assert main(["config"]) == 0 and f"calibration: {cal_id}" in capsys.readouterr().out
    assert main(["config", "--calibration", "none"]) == 0
    assert "calibration: none" in capsys.readouterr().out
    assert main(["plan", "classify this", "--kind", "classify", "--risk", "medium"]) == 0
    assert "start: haiku/medium" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="cannot access"):
        main(["config", "--calibration", "never-made"])
    monkeypatch.setenv("MODEL_ROUTER_CALIBRATION", "never-made")
    ok, line = cli._calibration_check()
    assert not ok and "cannot be loaded" in line and "MODEL_ROUTER_CALIBRATION" in line


def test_bench_matrix_lists_every_cell_and_max_usd_guards_the_spend(home, monkeypatch, capsys):
    from model_router.catalog import Usage

    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [{"id": "w", "kind": "write", "risk": "critical", "prompt": "write it"}]}))
    assert main(["bench", str(spec), "--matrix", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "x 20 strategies" in out and "fable/max: verification full" in out
    assert main(["bench", str(spec), "--matrix", "--cells", "haiku/low,opus/low", "--verify", "none", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "x 2 strategies" in out and "haiku/low: verification none" in out and "opus/low: verification none" in out
    assert main(["bench", str(spec), "--yes", "--matrix", "--max-usd", "0"]) == 1
    assert "over --max-usd" in capsys.readouterr().out and not (home / "bench").exists()
    big = Reply("draft", usage=Usage(2_000_000, 1000))  # far over the estimate, so the cap trips after the first cell
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [big] * 4}))
    argv = ["bench", str(spec), "--yes", "--matrix", "--cells", "haiku/low,opus/low", "--verify", "none", "--max-usd", "0.05"]
    assert main(argv) == 1
    out = capsys.readouterr().out
    assert "== haiku/low ==" in out and "stopped before opus/low" in out and "== opus/low ==" not in out
    assert "--max-usd $0.050 reached" in out


def test_calibrate_classifier_compares_rules_and_haiku_with_the_eval_labels(home, monkeypatch, capsys):
    from model_router.classify import LlmClassification

    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "medium", "prompt": f"classify ticket {i}"} for i in range(3)
    ]}))
    assert main(["calibrate", "--classifier", str(spec)]) == 0
    out = capsys.readouterr().out
    assert "rules: kind right on 3/3, risk right on 0/3; 3 misses" in out and "haiku:" not in out
    guess = Reply(parsed=LlmClassification(kind="classify", risk="medium", reason="a ticket"))
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(judge={"*": [guess] * 3}))  # structured: the judge table
    assert main(["calibrate", "--classifier", str(spec), "--llm"]) == 0
    out = capsys.readouterr().out
    assert "haiku: kind right on 3/3, risk right on 3/3; 0 misses" in out and "cost of the Haiku calls" in out


def test_every_subcommand_prints_its_help(capsys):
    import pytest as _pytest

    for cmd in ("plan", "run", "judge", "bench", "calibrate", "stats", "db", "feedback", "doctor", "cells", "install",
                "uninstall", "config"):
        with _pytest.raises(SystemExit) as exc:
            main([cmd, "--help"])
        assert exc.value.code == 0, cmd
        assert "usage: router " + cmd in capsys.readouterr().out


def test_a_task_file_cannot_ask_for_a_verification_mode_the_router_lacks(home, capsys):
    spec = home / "t.yaml"
    spec.write_text("tasks:\n  - id: w\n    kind: write\n    risk: critical\n    prompt: write it\n    verify: twice\n")
    with pytest.raises(SystemExit, match="not a router mode"):
        main(["bench", str(spec), "--dry-run"])


def test_doctor_names_the_listed_models_it_does_not_know(home, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonet")
    main(["doctor"])
    out = capsys.readouterr().out
    assert "available models: haiku" in out and "does not know, ignored: sonet" in out


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
