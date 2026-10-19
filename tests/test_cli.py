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
    assert main(["plan", "Implementa una función que parsee fechas DD/MM/YYYY", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "inicio:" in out and "escalera:" in out and "verificador" in out


def test_plan_claude_code_prints_frontmatter_values(capsys):
    main(["plan", "resume este texto", "--kind", "summarize", "--risk", "low", "--claude-code"])
    assert capsys.readouterr().out == "model: haiku\neffort: medium\n"


def test_bench_without_a_file_runs_the_simulation_and_prints_the_table(capsys):
    assert main(["bench", "--simulate", "--tasks", "12", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "SIMULACIÓN" in out and "G router" in out


def test_cells_lists_every_cell_of_the_lattice(capsys):
    assert main(["cells"]) == 0
    assert "fable/max" in capsys.readouterr().out


def test_stats_says_there_are_no_tasks_on_an_empty_home(home, capsys):
    assert main(["stats"]) == 0
    assert "sin tareas" in capsys.readouterr().out


def test_run_prints_the_answer_the_attempts_and_records_the_task(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("def f(): return 42")]}))
    assert main(["run", "Implementa f que devuelva 42", "--check-regex", "return 42"]) == 0
    out, err = capsys.readouterr()
    assert out.startswith("def f(): return 42\n")
    assert "clasificación: code / medium" in out
    assert "OK (checker)" in out and "estado solved" in out and "reintentos 0" in out
    assert err == ""
    records = [json.loads(line) for line in (home / "ledger.jsonl").read_text().splitlines()]
    assert len(records) == 1 and records[0]["status"] == "solved" and records[0]["attempts"][0]["cell"] == "opus/low"
    assert "prompt" not in records[0] and "answer" not in records[0]


def test_run_json_gives_the_result_and_the_answer(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply('{"a": 1}')]}))
    assert main(["run", "dame un JSON", "--kind", "extract", "--risk", "low", "--check-json", "a", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "solved" and data["answer"] == '{"a": 1}' and len(data["attempts"]) == 1
    assert data["attempts"][0]["reason"] == "JSON válido"


def test_run_exits_1_and_warns_when_nothing_passes(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("no")]}))
    assert main(["run", "clasifica esto", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVO"]) == 1
    out, err = capsys.readouterr()
    assert "estado unsolved" in out and "no casa con /POSITIVO/" in out
    assert "no pasó la verificación" in err


def test_feedback_overrides_the_verdict_in_the_ledger_and_the_feedback_store(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVO")]}))
    main(["run", "clasifica", "--kind", "classify", "--risk", "low", "--check-regex", "POSITIVO"])
    match = re.search(r"· id (\w+)", capsys.readouterr().out)
    assert match is not None
    task_id = match.group(1)
    assert main(["feedback", task_id, "--ko", "--note", "la etiqueta era NEGATIVO"]) == 0
    assert capsys.readouterr().out.strip() == f"{task_id}: marcada como KO"
    main(["stats"])
    out = capsys.readouterr().out
    assert "tareas 1 · resueltas 0" in out
    store = json.loads((home / "feedback.json").read_text())
    assert store["classify"]["haiku/low"] == {"n": 1, "pass": 0, "n_first": 1, "pass_first": 0}
    with pytest.raises(SystemExit, match="no hay tarea nope"):
        main(["feedback", "nope", "--ok"])


def test_bench_file_runs_tasks_in_parallel(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVO")] * 6}))
    spec = home / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "low", "prompt": "clasifica", "check_regex": "POSITIVO"} for i in range(3)
    ]}))
    assert main(["bench", str(spec), "--yes", "--jobs", "2", "--strategies", "router"]) == 0
    out = capsys.readouterr().out
    assert out.count(" solved $") == 3 and "tareas 3" in out
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
    assert "ok     claude en PATH" in out and "9.9.9" in out
    assert "FALLO  hook PreToolUse(Agent)" in out and "FALLO  skill router" in out
    hook = tmp_path / "router-hook"
    hook.write_text("")
    (profile / "settings.json").write_text(json.dumps({"model": "opus", "hooks": {
        "PreToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": str(hook)}]}],
        "SessionStart": [{"hooks": [{"type": "command", "command": str(hook)}]}]}}))
    (profile / "skills" / "router").mkdir(parents=True)
    main(["doctor"])
    out = capsys.readouterr().out
    assert "FALLO  hook" not in out and "ok     skill router" in out and "model opus" in out


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
    assert "chat principal: model fable, effort low" in capsys.readouterr().out


def test_an_unknown_model_is_a_one_line_error_not_a_traceback(home, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonet")
    with pytest.raises(SystemExit, match="modelo desconocido 'sonet'.*haiku, sonnet, opus, fable"):
        main(["plan", "clasifica esto", "--kind", "classify", "--risk", "low"])
    monkeypatch.delenv("MODEL_ROUTER_MODELS")
    with pytest.raises(SystemExit, match="modelo desconocido 'opsu'"):
        main(["install", "--settings", str(home / "s.json"), "--no-skill", "--models", "opsu"])
    assert not (home / "s.json").exists()


def test_stats_skips_corrupt_lines_in_the_logs_and_says_so(home, capsys):
    good = json.dumps({"cell": "opus/low", "kind": "code", "risk": "medium"})
    (home / "hook.jsonl").write_text(good + "\n{\"cell\": \"opus/l\n" + good + "\n")
    task = {"type": "task", "task_id": "t1", "kind": "code", "status": "solved", "attempts": [], "usage": {}}
    (home / "ledger.jsonl").write_text(json.dumps(task) + "\n{\"type\": \"ta\n")
    assert main(["stats"]) == 0
    out, err = capsys.readouterr()
    assert "opus/low" in out and "tareas 1" in out
    assert "1 líneas ilegibles" in err


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
