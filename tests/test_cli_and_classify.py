import json

import pytest

from model_router.classify import classify_rules
from model_router.cli import main


def test_rules_place_common_spanish_and_english_prompts():
    assert classify_rules("Revisa este script SQL antes de producción").kind == "review"
    assert classify_rules("Revisa este script SQL antes de producción").risk == "high"
    assert classify_rules("clasifica estos tickets en 5 categorías").kind == "classify"
    assert classify_rules("Write a LinkedIn post about data migrations").kind == "write"
    assert classify_rules("Extrae los campos de esta factura a JSON").kind == "extract"
    assert classify_rules("¿Qué hora es en Tokio?").kind == "chat"
    assert classify_rules("Migra el pipeline paso a paso a Dagster").kind == "agentic"
    assert classify_rules("Es crítico: borra los duplicados").risk == "critical"


def test_plan_command_needs_no_api(capsys):
    assert main(["plan", "Implementa una función que parsee fechas DD/MM/YYYY", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "inicio:" in out and "escalera:" in out and "verificador" in out


def test_plan_claude_code_prints_frontmatter_values(capsys):
    main(["plan", "resume este texto", "--kind", "summarize", "--risk", "low", "--claude-code"])
    assert capsys.readouterr().out == "model: haiku\neffort: medium\n"


def test_bench_simulate_prints_table(capsys):
    assert main(["bench", "--simulate", "--tasks", "12", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "SIMULACION" in out and "G router" in out


def test_cells_command_lists_lattice(capsys):
    assert main(["cells"]) == 0
    assert "fable/max" in capsys.readouterr().out


def test_stats_on_empty_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    assert main(["stats"]) == 0
    assert "sin tareas" in capsys.readouterr().out
    assert json.loads("{}") == {}


def test_search_prompts_are_their_own_kind():
    from model_router.classify import classify_rules

    assert classify_rules("Explora el repo y lista los módulos que tocan la base de datos").kind == "search"
    assert classify_rules("Find where the config is loaded").kind == "search"
    assert classify_rules("Busca errores en este módulo").kind == "review"
    assert classify_rules("Implementa una función que devuelva una lista").kind == "code"


def test_bench_file_runs_tasks_in_parallel(tmp_path, monkeypatch, capsys):
    from model_router import cli
    from model_router.testing import FakeClient, Reply

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_live_client", lambda args: FakeClient(script={"*": [Reply("1: POSITIVO")] * 6}))
    spec = tmp_path / "t.json"
    spec.write_text(json.dumps({"tasks": [
        {"id": f"c{i}", "kind": "classify", "risk": "low", "prompt": "clasifica", "check_regex": "POSITIVO"} for i in range(3)
    ]}))
    assert main(["bench", str(spec), "--yes", "--jobs", "2", "--strategies", "router"]) == 0
    out = capsys.readouterr().out
    assert out.count(" solved $") == 3 and "tareas 3" in out
    assert len(list((tmp_path / "bench").glob("*.jsonl"))) == 1


def test_doctor_reports_missing_hooks(tmp_path, monkeypatch, capsys):
    import stat

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text("#!/bin/sh\necho 9.9.9\n")
    claude.chmod(claude.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(fake_bin))
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setenv("ROUTER_CLAUDE_CONFIG_DIR", str(profile))
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
    monkeypatch.setenv("ROUTER_CLAUDE_CONFIG_DIR", str(profile))
    (profile / "settings.json").write_text(json.dumps({
        "model": "fable", "modelSettings": {"claude-fable-5-1": {"effortLevel": "low"}},
    }))
    main(["doctor"])
    assert "chat principal: model fable, effort low" in capsys.readouterr().out


def test_an_unknown_model_is_a_one_line_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonet")
    with pytest.raises(SystemExit, match="modelo desconocido 'sonet'.*haiku, sonnet, opus, fable"):
        main(["plan", "clasifica esto", "--kind", "classify", "--risk", "low"])
    monkeypatch.delenv("MODEL_ROUTER_MODELS")
    with pytest.raises(SystemExit, match="modelo desconocido 'opsu'"):
        main(["install", "--settings", str(tmp_path / "s.json"), "--no-skill", "--models", "opsu"])
    assert not (tmp_path / "s.json").exists()


def test_stats_skips_corrupt_lines_in_the_logs_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    good = json.dumps({"cell": "opus/low", "kind": "code", "risk": "medium"})
    (tmp_path / "hook.jsonl").write_text(good + "\n{\"cell\": \"opus/l\n" + good + "\n")
    task = {"type": "task", "task_id": "t1", "kind": "code", "status": "solved", "attempts": [], "usage": {}}
    (tmp_path / "ledger.jsonl").write_text(json.dumps(task) + "\n{\"type\": \"ta\n")
    assert main(["stats"]) == 0
    out, err = capsys.readouterr()
    assert "opus/low" in out and "tareas 1" in out
    assert "1 líneas ilegibles" in err


def test_uninstall_command_only_takes_profile_flags(tmp_path, monkeypatch, capsys):
    from model_router import install as mod

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "hook_binary", lambda: tmp_path / "router-hook")
    settings = tmp_path / "profile" / "settings.json"
    assert main(["install", "--settings", str(settings), "--no-skill"]) == 0
    assert "hooks" in json.loads(settings.read_text())
    assert main(["uninstall", "--settings", str(settings), "--no-skill"]) == 0
    assert "hooks" not in json.loads(settings.read_text())
    with pytest.raises(SystemExit):
        main(["uninstall", "--settings", str(settings), "--models", "opus"])
    capsys.readouterr()
