import json

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
