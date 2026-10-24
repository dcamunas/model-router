"""Risk floors a repository sets in `.claude/model-router.json`: they raise, never lower, read only the head of a
brief, skip what cannot be used, and reach the hook's decision."""

import json

from model_router import floors
from model_router.hook import decide


def _repo(tmp_path, monkeypatch, rules) -> str:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude").mkdir()
    if rules is not None:
        (repo / ".claude" / "model-router.json").write_text(json.dumps({"v": 1, "floors": rules}))
    return str(repo)


def test_a_floor_raises_the_risk_and_never_lowers_it(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, [{"match": "producci[oó]n", "risk": "critical", "why": "reaches production"},
                                         {"match": "migration", "risk": "high"},
                                         {"match": "typo", "risk": "low"}])
    rules = floors.load(repo)
    assert len(rules.rules) == 3 and not rules.problems
    assert floors.apply("low", "fix the migration", floors=rules) == floors.Raised("high", rules.rules[1])
    assert floors.apply("medium", "deploy to PRODUCCIÓN and the migration", floors=rules).risk == "critical"
    assert floors.apply("critical", "a typo in the migration", floors=rules) == floors.Raised("critical")  # never down
    assert floors.apply("medium", "nothing to see", floors=rules) == floors.Raised("medium")
    assert rules.rules[0].label == "reaches production" and rules.rules[1].label == "migration"


def test_only_the_description_and_the_head_of_the_brief_are_searched():
    brief = "kind: search, risk: low, access: read\nList the files.\n\n\nfoo\nbar\nmaterial: production logs follow"
    text = floors.head(brief, "list files")
    assert "List the files" in text and "list files" in text and "production" not in text


def test_rules_that_cannot_be_used_are_skipped_and_said(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, [{"match": "(", "risk": "high"}, {"match": "x", "risk": "huge"},
                                         {"match": "", "risk": "high"}, "text", {"match": "ok", "risk": "high"},
                                         {"match": "y" * 300, "risk": "high"}, {"match": "a{99999999999}", "risk": "high"}])
    rules = floors.load(repo)
    assert [r.match for r in rules.rules] == ["ok"] and len(rules.problems) == 6
    assert any("floor 1" in p and "valid pattern" in p for p in rules.problems)


def test_no_file_no_floors_and_a_bad_file_says_why(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, None)
    assert floors.load(repo) == floors.Floors(None) and floors.load(None) == floors.Floors(None)
    path = floors.floors_path(repo)
    path.write_text("{not json")
    assert "not valid JSON" in floors.load(repo).problems[0]
    path.write_text(json.dumps({"v": 2, "floors": []}))
    assert "expected" in floors.load(repo).problems[0]
    path.write_text(json.dumps({"v": True, "floors": []}))
    assert "expected" in floors.load(repo).problems[0]
    path.write_text('{"v": 1, "floors": ' + "[" * 20000)
    assert "not valid JSON" in floors.load(repo).problems[0]  # nesting deep enough to exhaust the parser
    path.unlink()
    path.symlink_to(tmp_path / "missing.json")
    assert "regular file" in floors.load(repo).problems[0]  # a dangling link is said, not ignored
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"v": 1, "floors": [{"match": "x", "risk": "critical"}]}))
    path.symlink_to(target)
    loaded = floors.load(repo)
    assert loaded.rules == () and "regular file" in loaded.problems[0]  # a link is never followed


def test_the_hook_starts_a_subagent_on_the_floor_of_its_repository(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch, [{"match": "migraci[oó]n", "risk": "critical", "why": "customer data"}])
    brief = "kind: code, risk: low, access: write\nAdapta la migración de clientes"
    updated, record = decide({"subagent_type": "general-purpose", "prompt": brief}, cwd=repo)
    assert updated is not None and record["risk"] == "critical" and record["floor"] == "low -> critical: customer data"
    assert updated["model"] in ("opus", "fable")  # the critical floor, opus/medium or above
    updated, record = decide({"subagent_type": "general-purpose", "prompt": "kind: code, risk: low\nRename a variable"},
                             cwd=repo)
    assert record["risk"] == "low" and "floor" not in record


def test_router_project_and_doctor_show_the_floors_and_how_often_each_fired(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    repo = _repo(tmp_path, monkeypatch, [{"match": "migration", "risk": "critical", "why": "customer data"},
                                         {"match": "(", "risk": "high"}])
    home = tmp_path / "profile" / "model-router"
    home.mkdir(parents=True)
    (home / "hook.jsonl").write_text(json.dumps({"project": repo, "floor": "low -> critical: customer data",
                                                 "floor_match": "migration"}) + "\n"
                                     + "{cut short\n")
    monkeypatch.chdir(repo)
    assert main(["project", repo]) == 0
    out = capsys.readouterr().out
    assert "risk floors" in out and "customer data" in out and "not used: floor 2" in out
    assert [line for line in out.splitlines() if "customer data" in line][0].split()[-1] == "1"
    main(["doctor"])
    assert "FAIL   risk floors in" in capsys.readouterr().out


def test_router_plan_in_a_repository_follows_its_floors(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    repo = _repo(tmp_path, monkeypatch, [{"match": "migration", "risk": "critical"}])
    monkeypatch.chdir(repo)
    assert main(["plan", "Summarise the migration notes", "--kind", "summarize", "--risk", "low"]) == 0
    captured = capsys.readouterr()
    assert "raised to critical" in captured.err and "table summarize x critical" in captured.out


def test_a_pattern_that_takes_too_long_counts_as_not_matching_and_is_named(tmp_path, monkeypatch):
    import time

    repo = _repo(tmp_path, monkeypatch, [{"match": "(a+)+$", "risk": "critical", "why": "slow"},
                                         {"match": "migration", "risk": "high"}])
    started = time.monotonic()
    raised = floors.apply("low", "a" * 60 + "b migration", floors=floors.load(repo))
    assert time.monotonic() - started < 1
    assert raised.risk == "high" and raised.unsearched == ("slow",)
    updated, record = decide({"subagent_type": "general-purpose", "prompt": "kind: code, risk: low\n" + "a" * 60 + "b"},
                             cwd=repo)
    assert updated is not None and record["floor_unsearched"] == ["slow"]


def test_without_a_timer_the_rules_are_not_searched(tmp_path, monkeypatch):
    import threading

    repo = _repo(tmp_path, monkeypatch, [{"match": "migration", "risk": "high"}])
    out = []
    worker = threading.Thread(target=lambda: out.append(floors.apply("low", "migration", floors=floors.load(repo))))
    worker.start()
    worker.join()
    assert out[0] == floors.Raised("low", None, ("migration",))


def test_a_plan_given_no_text_is_not_raised_by_a_floor(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    repo = _repo(tmp_path, monkeypatch, [{"match": "declared|risk", "risk": "critical"}])
    monkeypatch.chdir(repo)
    assert main(["plan", "--kind", "summarize", "--risk", "low", "--claude-code"]) == 0
    assert capsys.readouterr().out == "model: haiku\neffort: medium\n"

