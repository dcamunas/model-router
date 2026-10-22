import json
from datetime import UTC, datetime, timedelta

import pytest

from model_router import projects
from model_router.catalog import parse_cell
from model_router.hook import handle, start_in_force

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)  # a Thursday


@pytest.fixture(autouse=True)
def _frozen(freeze_clock):
    freeze_clock(NOW)


def test_a_signature_ignores_the_declaration_line_tells_tasks_apart_and_is_salted(tmp_path, monkeypatch):
    task = "Review src/model_router/policy.py for bugs in the arbiter rule and report each with path:line."
    same = projects.signature("kind: review, risk: high, access: write\n" + task)
    assert projects.alike(projects.signature(task), same) == 1.0
    other = projects.signature("Summarise the meeting notes from Tuesday into five bullet points for the team.")
    assert projects.alike(same, other) < 0.2 and projects.alike(None, same) == 0.0
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "elsewhere"))
    assert projects.alike(projects.signature(task), same) < 0.2  # another install's salt: not comparable


def _old(**kw):
    now = NOW
    base = {"session": "s1", "agent_id": "a1", "model": "haiku", "effort": "medium", "kind": "search", "risk": "low",
            "sig": projects.signature("Find where the config is loaded"), "at": (now - timedelta(minutes=5)).isoformat()}
    return {**base, **kw}


def _relaunch(old, cell, *, session="s1", kind="search", risk="low", now=NOW):
    return projects.find_relaunch([old], session, prompt_signature=projects.signature("Find where the config is loaded"),
                                  cell=parse_cell(cell), kind=kind, risk=risk, now=now)


def test_a_relaunch_is_the_same_task_higher_soon_after():
    old = _old()
    assert _relaunch(old, "sonnet/low") == old
    assert _relaunch(old, "haiku/high") == old  # the same model with more effort
    assert _relaunch(old, "haiku/medium") is None  # the same cell
    assert _relaunch(old, "opus/low", session="s2") is None
    assert _relaunch(old, "opus/low", risk="high") is None
    assert _relaunch(old, "opus/low", kind="code") is None
    assert _relaunch(old, "opus/low", now=NOW + timedelta(hours=1)) is None
    no_effort = _old(effort=None)  # the event gave no effort: the model's default
    assert _relaunch(no_effort, "sonnet/low") == no_effort


def _records(project, kind, risk, cell, n, prefix, *, chose=True, at=None):
    at = at or (NOW - timedelta(minutes=1)).isoformat()
    return [{"project": project, "routed": cell, "kind": kind, "risk": risk, "agent_id": f"{prefix}{i}",
             "router_chose": chose, "at": at} for i in range(n)]


def test_raises_only_the_cell_in_force_and_lowers_only_by_hand_back_to_where_it_came_from(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    project = str(tmp_path / "repo")
    current = {("review", "medium"): parse_cell("haiku/medium")}

    def in_force(kind, risk):
        return current.get((kind, risk), parse_cell("sonnet/low"))

    records = _records(project, "review", "medium", "haiku/medium", 6, "a")
    records += _records(project, "review", "medium", "haiku/medium", 3, "c", chose=False)  # Claude chose the effort
    records += _records(project, "review", "medium", "sonnet/medium", 6, "h")  # history: not the cell in force
    counts = projects.evidence(project, records, relaunched={"a0", "a1", "h0", "h1", "h2"})
    assert counts[("review", "medium", "haiku/medium")] == {"n": 6, "fails": 2}  # Claude's own choices do not count
    changes = projects.raise_failing(project, counts, current=in_force, local=counts)
    assert [(c["from"], c["to"]) for c in changes] == [("haiku/medium", changes[0]["to"])] and len(changes) == 1
    up = changes[0]["to"]
    entry = projects.load_tuning(project)["start"]["review"]["medium"]
    assert entry == {"cell": up, "base": "haiku/medium"} and parse_cell(up).capability > parse_cell("haiku/medium").capability
    current[("review", "medium")] = parse_cell(up)
    clean = projects.evidence(project, _records(project, "review", "medium", up, 10, "b"), relaunched=set())
    proposals = projects.propose_lower(project, clean, floors={"critical": "opus/medium"}, current=in_force)
    assert [(c["from"], c["to"]) for c in proposals] == [(up, "haiku/medium")]  # back to where it was raised from
    projects.apply(project, proposals)
    tuning = projects.load_tuning(project)
    assert tuning["start"]["review"]["medium"]["cell"] == "haiku/medium" and tuning["since"]
    current[("review", "medium")] = parse_cell("haiku/medium")
    old = projects.evidence(project, records, relaunched={"a0", "a1"}, since=tuning["since"])
    assert projects.raise_failing(project, old, current=in_force, local=old) == []  # the evidence before --apply no longer counts


def test_a_cheaper_start_must_really_be_cheaper_and_respect_the_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    project = str(tmp_path / "repo")
    for label in ("opus/high", "opus/medium", "sonnet/low", "fable/low"):
        counts = projects.evidence(project, _records(project, "agentic", "medium", label, 10, "x"), relaunched=set())
        for c in projects.propose_lower(project, counts, floors={}, current=lambda kind, risk, label=label: parse_cell(label)):
            assert parse_cell(c["to"]).expected_cost() <= parse_cell(label).expected_cost() * 0.8, (label, c)
    counts = projects.evidence(project, _records(project, "search", "high", "sonnet/low", 12, "f"), relaunched=set())
    assert projects.propose_lower(project, counts, floors={"high": "sonnet/low"},
                                  current=lambda kind, risk: parse_cell("sonnet/low")) == []


def test_reset_keeps_raises_from_coming_back_and_bad_tunings_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    project = str(tmp_path / "repo")
    records = _records(project, "review", "medium", "haiku/medium", 6, "a")
    counts = projects.evidence(project, records, relaunched={"a0", "a1", "a2"})
    assert projects.raise_failing(project, counts, current=lambda kind, risk: parse_cell("haiku/medium"), local=counts)
    projects.reset(project)
    tuning = projects.load_tuning(project)
    assert tuning["start"] == {}
    again = projects.evidence(project, records, relaunched={"a0", "a1", "a2"}, since=tuning["since"])
    assert projects.raise_failing(project, again, current=lambda kind, risk: parse_cell("haiku/medium"), local=again) == []
    projects.tuning_path(project).write_text(json.dumps({"start": {
        "review": {"medium": {"cell": "gpt-9/low"}, "high": "opus"}, "code": "x", "write": {"low": {"cell": 5}}}}))
    assert projects.load_tuning(project)["start"] == {}  # nothing usable: nothing applied, nothing crashes
    junk = projects.evidence(project, _records(project, "review", "medium", "gone-model/low", 6, "z"), relaunched={"z0", "z1"})
    assert projects.raise_failing(project, junk, current=lambda kind, risk: parse_cell("haiku/medium"), local=junk) == []


def test_a_tuning_is_ignored_once_the_table_no_longer_gives_the_cell_it_replaced():
    from model_router.policy import Policy
    from model_router.task import Task

    task = Task(prompt="x", kind="review", risk="medium")
    base = Policy(calibration=None).plan(task).start
    tuned = Policy(calibration=None, tuning={"name": "repo", "start": {"review": {"medium": {
        "cell": "fable/low", "base": base.label}}}}).plan(task)
    assert tuned.start.label == "fable/low" and any("tuned for repo" in r for r in tuned.rationale)
    stale = Policy(calibration=None, tuning={"name": "repo", "start": {"review": {"medium": {
        "cell": "haiku/low", "base": "sonnet/xhigh"}}}}).plan(task)
    assert stale.start == base and any("ignored" in r for r in stale.rationale)


def test_projects_are_git_repositories_resolved_and_nothing_else(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert projects.project_of(str(link / "src")) == str(repo.resolve())
    assert projects.project_of(str(tmp_path)) is None and projects.project_of(None) is None


def _transcript(prompt, model):
    return "\n".join(json.dumps(line) for line in [
        {"timestamp": "2026-10-22T10:00:00Z", "message": {"role": "user", "content": prompt}},
        {"timestamp": "2026-10-22T10:00:09Z", "message": {"role": "assistant", "id": "m1", "model": model,
         "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 3, "output_tokens": 5,
         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}}]) + "\n"


def test_the_hook_learns_from_model_relaunches_not_from_access_or_other_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    (tmp_path / "profile" / "agents").mkdir(parents=True)
    (tmp_path / "profile" / "agents" / "router-reader.md").write_text("---\nname: router-reader\n---\n")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cwd = str(repo)

    def launch(prompt, **tool_input) -> dict:
        return handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1", "cwd": cwd,
                       "tool_input": {"subagent_type": "general-purpose", "prompt": prompt, **tool_input}}) or {}

    def finish(prompt, agent_id, out):
        model = out["hookSpecificOutput"]["updatedInput"]["model"] if out else "opus"
        effort = out["hookSpecificOutput"]["updatedInput"].get("effort", "medium") if out else "medium"
        full = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
                "fable": "claude-fable-5-1"}[model]
        path = tmp_path / f"{agent_id}.jsonl"
        path.write_text(_transcript(prompt, full))
        handle({"hook_event_name": "SubagentStop", "session_id": "s1", "cwd": cwd, "agent_id": agent_id,
                "agent_type": "general-purpose", "effort": {"level": effort}, "agent_transcript_path": str(path)})

    for i in range(6):  # the reader could not edit: relaunched with access: write on the same cell, not a model fail
        prompt = f"kind: review, risk: medium, access: read\nReview module_{i}.py and the parser loop number {i}."
        finish(prompt, f"r{i}", launch(prompt))
        launch(prompt.replace("access: read", "access: write"))
    for i in range(6):  # a template for another file at another risk: another task
        finish(f"kind: review, risk: medium\nReview utils_{i}.py for off-by-one errors in the loop.", f"u{i}",
               launch(f"kind: review, risk: medium\nReview utils_{i}.py for off-by-one errors in the loop."))
        launch(f"kind: review, risk: high\nReview utils_{i}.py for off-by-one errors in the loop.")
    log = [json.loads(line) for line in (tmp_path / "home" / "hook.jsonl").read_text().splitlines()]
    assert not any(e.get("relaunch_of") for e in log)
    started = handle({"hook_event_name": "SessionStart", "session_id": "s2", "cwd": cwd})
    assert started is not None and "systemMessage" not in started  # nothing failed: nothing raised
    for i in range(6):  # now real model relaunches: the same task on a bigger model, twice in six
        prompt = f"kind: analysis, risk: medium\nAnalyse why the cache misses in module_{i}.py grow after a deploy."
        out = launch(prompt)
        finish(prompt, f"a{i}", out)
        if i < 2:
            launch(prompt, model="fable", effort="high")
    log = [json.loads(line) for line in (tmp_path / "home" / "hook.jsonl").read_text().splitlines()]
    assert {e.get("relaunch_of") for e in log} >= {"a0", "a1"}
    started = handle({"hook_event_name": "SessionStart", "session_id": "s3", "cwd": cwd})
    assert started is not None and "analysis (medium) in repo now starts on" in started["systemMessage"]
    tuning = projects.load_tuning(str(repo.resolve()))
    tuned = tuning["start"]["analysis"]["medium"]["cell"]
    assert start_in_force(tuning)("analysis", "medium").label == tuned
    out = launch("kind: analysis, risk: medium\nAnalyse the retry logic.")
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == parse_cell(tuned).spec.short


def test_router_project_shows_the_evidence_proposes_applies_and_resets(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = str(repo.resolve())
    (tmp_path / "home").mkdir()
    lines = [{**r, "final": True} for r in _records(project, "search", "low", "fable/low", 10, "b")]
    (tmp_path / "home" / "subagents.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    monkeypatch.setattr("model_router.hook.start_in_force", lambda tuning: lambda kind, risk: parse_cell("fable/low"))
    assert main(["project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "search" in out and "fable/low" in out and "not applied" in out
    assert main(["project", str(repo), "--apply"]) == 0
    assert "applied:" in capsys.readouterr().out and projects.load_tuning(project)["start"]["search"]["low"]
    assert main(["project", str(repo), "--reset"]) == 0
    assert projects.load_tuning(project)["start"] == {}
    import pytest

    with pytest.raises(SystemExit, match="not inside a git repository"):
        main(["project", str(tmp_path)])


def test_a_cheaper_start_needs_ten_clean_launches_not_nine(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    project = str(tmp_path / "repo")

    cells = projects.lattice()
    start = next(c for c in cells if projects.cheaper_below(c, cells) is not None)

    def in_force(kind, risk):
        return start

    nine = projects.evidence(project, _records(project, "agentic", "medium", start.label, projects.DOWN_N - 1, "x"),
                             relaunched=set())
    assert projects.propose_lower(project, nine, floors={}, current=in_force) == []
    ten = projects.evidence(project, _records(project, "agentic", "medium", start.label, projects.DOWN_N, "x"),
                            relaunched=set())
    assert len(projects.propose_lower(project, ten, floors={}, current=in_force)) == 1


def test_evidence_counts_the_whole_weeks_of_the_window_and_nothing_older():
    first = projects.window_start()
    assert first.weekday() == 0 and NOW - projects.WINDOW < first <= NOW - projects.WINDOW + timedelta(weeks=1)
    inside = _records("/r", "search", "low", "haiku/low", 1, "in", at=(first + timedelta(minutes=1)).isoformat())
    outside = _records("/r", "search", "low", "haiku/low", 1, "out", at=(first - timedelta(minutes=1)).isoformat())
    assert projects.evidence("/r", inside + outside, relaunched=set()) == {("search", "low", "haiku/low"): {"n": 1, "fails": 0}}


def test_router_plan_starts_where_the_hook_would_in_this_repository(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    table = start_in_force({})("search", "low")
    tuned = projects.step_up(table, projects.lattice())
    assert tuned is not None
    projects.apply(str(repo.resolve()), [{"kind": "search", "risk": "low", "from": table.label, "to": tuned.label,
                                          "what": "raised", "n": 6, "relaunched": 2}])
    monkeypatch.chdir(repo)
    assert main(["plan", "find where the config is loaded", "--kind", "search", "--risk", "low"]) == 0
    out = capsys.readouterr().out
    assert f"start: {tuned.label}" in out and "tuned for repo" in out
