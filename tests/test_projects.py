import json
from datetime import UTC, datetime, timedelta

from model_router import projects
from model_router.catalog import parse_cell
from model_router.hook import handle


def test_a_signature_ignores_the_declaration_line_and_tells_tasks_apart():
    task = "Review src/model_router/policy.py for bugs in the arbiter rule and report each with path:line."
    same = projects.signature("kind: review, risk: high, access: write\n" + task)
    assert projects.alike(projects.signature(task), same) == 1.0  # relaunched with another declaration: the same task
    other = projects.signature("Summarise the meeting notes from Tuesday into five bullet points for the team.")
    assert projects.alike(same, other) < 0.2 and projects.alike(None, same) == 0.0
    assert len(same) == projects.SIG_SIZE and all(isinstance(x, int) for x in same)


def test_a_relaunch_is_higher_soon_after_and_in_the_same_session():
    now = datetime.now(UTC)
    old = {"session": "s1", "agent_id": "a1", "model": "haiku", "effort": "medium", "agent_type": "general-purpose",
           "sig": projects.signature("Find where the config is loaded"), "at": (now - timedelta(minutes=5)).isoformat()}
    sig = projects.signature("Find where the config is loaded")
    assert projects.find_relaunch([old], "s1", sig, parse_cell("sonnet/low"), "general-purpose", now) == old
    assert projects.find_relaunch([old], "s1", sig, parse_cell("haiku/high"), "general-purpose", now) == old  # effort up
    assert projects.find_relaunch([old], "s1", sig, parse_cell("haiku/low"), "general-purpose", now) is None  # not higher
    assert projects.find_relaunch([old], "s2", sig, parse_cell("opus/low"), "general-purpose", now) is None  # another session
    late = now + timedelta(hours=1)
    assert projects.find_relaunch([old], "s1", sig, parse_cell("opus/low"), "general-purpose", late) is None
    reader = {**old, "agent_type": "model-router:reader"}
    assert projects.find_relaunch([reader], "s1", sig, parse_cell("haiku/medium"), "general-purpose", now) == reader


def test_failing_starts_go_up_on_their_own_and_cheaper_ones_are_only_proposed(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    project = str(tmp_path / "repo")
    records = [{"project": project, "routed": "haiku/medium", "kind": "review", "risk": "medium", "agent_id": f"a{i}"}
               for i in range(6)]
    records += [{"project": project, "routed": "sonnet/low", "kind": "search", "risk": "low", "agent_id": f"b{i}"}
                for i in range(10)]
    records += [{"project": "elsewhere", "routed": "haiku/medium", "kind": "review", "risk": "medium", "agent_id": "x"}]
    ev = projects.evidence(project, records, {"a0", "a1", "x"})
    assert ev[("review", "haiku/medium")]["n"] == 6 and ev[("review", "haiku/medium")]["fails"] == 2
    changes = projects.raise_failing(project, ev)
    assert [(c["kind"], c["risk"], c["from"]) for c in changes] == [("review", "medium", "haiku/medium")]
    tuned = projects.load_tuning(project)["start"]["review"]["medium"]
    assert parse_cell(tuned).capability > parse_cell("haiku/medium").capability
    assert projects.raise_failing(project, ev) == []  # moved once: the new cell's own evidence decides next
    assert projects.tuning_path(project).parent == tmp_path / "projects"  # never inside the repository
    proposals = projects.propose_lower(ev, {"high": "sonnet/low", "critical": "opus/medium"})
    assert [(c["kind"], c["from"]) for c in proposals] == [("search", "sonnet/low")]
    projects.apply(project, proposals)
    assert projects.load_tuning(project)["start"]["search"]["low"] == proposals[0]["to"]
    floored = projects.propose_lower({("search", "sonnet/low"): {"n": 12, "fails": 0, "risks": {"high"}}},
                                     {"high": "sonnet/low"})
    assert floored == []  # never below the risk's floor


def _agent_turn(prompt):
    return "\n".join(json.dumps(line) for line in [
        {"timestamp": "2026-10-22T10:00:00Z", "message": {"role": "user", "content": prompt}},
        {"timestamp": "2026-10-22T10:00:09Z", "message": {"role": "assistant", "id": "m1", "model": "claude-haiku-5-5",
         "content": [{"type": "text", "text": "done"}], "usage": {"input_tokens": 3, "output_tokens": 5,
         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50}}}]) + "\n"


def test_the_hook_learns_from_relaunches_and_starts_the_task_higher_in_that_project(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    cwd = str(repo / "src")

    def launch(prompt, **tool_input) -> dict:
        return handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1", "cwd": cwd,
                       "tool_input": {"subagent_type": "general-purpose", "prompt": prompt, **tool_input}}) or {}

    def finish(prompt, agent_id, model="haiku", effort="medium"):
        path = tmp_path / f"{agent_id}.jsonl"
        path.write_text(_agent_turn(prompt).replace("claude-haiku-5-5", f"claude-{model}-5-5" if model != "fable"
                                                    else "claude-fable-5-1"))
        handle({"hook_event_name": "SubagentStop", "session_id": "s1", "cwd": cwd, "agent_id": agent_id,
                "agent_type": "general-purpose", "effort": {"level": effort}, "agent_transcript_path": str(path)})

    first = None
    for i in range(6):
        prompt = f"kind: review, risk: medium\nReview module_{i}.py for off-by-one errors in the parser loop {i}."
        out = launch(prompt)
        first = first or out["hookSpecificOutput"]["updatedInput"]["model"]
        finish(prompt, f"a{i}")
        if i < 2:  # Claude relaunches the first two higher, setting the model itself
            launch(prompt, model="opus", effort="medium")
    log = [json.loads(line) for line in (tmp_path / "home" / "hook.jsonl").read_text().splitlines()]
    assert {e.get("relaunch_of") for e in log} >= {"a0", "a1"}
    assert all(r["project"] == str(repo) for r in (json.loads(x) for x in
               (tmp_path / "home" / "subagents.jsonl").read_text().splitlines()))
    started = handle({"hook_event_name": "SessionStart", "session_id": "s2", "cwd": cwd})
    assert started is not None and "review (medium) in repo now starts on" in started["systemMessage"]
    tuned = projects.load_tuning(str(repo))["start"]["review"]["medium"]
    out = launch("kind: review, risk: medium\nReview the tokenizer for missing cases.")
    last = json.loads((tmp_path / "home" / "hook.jsonl").read_text().splitlines()[-1])
    assert last["cell"] == tuned and out["hookSpecificOutput"]["updatedInput"]["model"] == parse_cell(tuned).spec.short
    assert parse_cell(tuned).capability > parse_cell(f"{first}/medium").capability or tuned.split("/")[0] != first
    again = handle({"hook_event_name": "SessionStart", "session_id": "s3", "cwd": cwd})
    assert again is not None and "systemMessage" not in again  # raised once; nothing new to say


def test_router_project_shows_the_evidence_proposes_and_resets(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    lines = [{"project": str(repo), "routed": "sonnet/low", "kind": "search", "risk": "low", "agent_id": f"b{i}",
              "final": True} for i in range(10)]
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "subagents.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    assert main(["project", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "search" in out and "sonnet/low" in out and "not applied" in out
    assert main(["project", str(repo), "--apply"]) == 0
    assert "applied:" in capsys.readouterr().out and projects.load_tuning(str(repo))["start"]["search"]["low"]
    assert main(["project", str(repo), "--reset"]) == 0
    assert projects.load_tuning(str(repo)) == {}
