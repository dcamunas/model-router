import json
import subprocess
import sys

from model_router.hook import decide, handle


def agent_event(**tool_input):
    return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1", "cwd": "/tmp",
            "tool_input": {"description": "task", "prompt": "Review this SQL script before production", **tool_input}}


def routed(event) -> dict:
    out = handle(event)
    assert out is not None
    return out


def decided(tool_input, chat_model=None) -> tuple[dict, dict]:
    updated, record = decide(tool_input, chat_model)
    assert updated is not None
    return updated, record


def test_fills_model_and_effort_for_general_purpose_subagent(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    out = routed(agent_event())
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert updated["model"] == "opus" and updated["effort"] == "medium"  # review x high -> opus/medium
    assert updated["prompt"].startswith("Review")
    assert out["systemMessage"].startswith("router: opus/medium")
    log = (tmp_path / "hook.jsonl").read_text().strip().splitlines()
    assert len(log) == 1 and json.loads(log[0])["cell"] == "opus/medium"


def test_keeps_an_explicit_model_and_skips_custom_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    assert handle(agent_event(model="sonnet")) is None
    assert handle(agent_event(subagent_type="fork")) is None
    assert handle(agent_event(subagent_type="Explore")) is None
    assert handle(agent_event(subagent_type="migration-curator-g3w")) is None
    assert not (tmp_path / "hook.jsonl").exists()


def test_only_model_is_set_when_claude_chose_an_effort():
    updated, record = decided({"prompt": "classify these tickets", "effort": "high"})
    assert updated["model"] == "haiku" and updated["effort"] == "high" and record["set"] == ["model"]


def test_session_start_adds_context_and_other_events_are_silent():
    out = routed({"hook_event_name": "SessionStart", "source": "startup"})
    assert "Model router active" in out["hookSpecificOutput"]["additionalContext"]
    assert handle({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert handle({"hook_event_name": "PostToolUse", "tool_name": "Agent"}) is None


def test_entry_point_never_fails_on_garbage(tmp_path, monkeypatch):
    env = {"MODEL_ROUTER_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for payload in ("not json", "{}", json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input": None})):
        proc = subprocess.run([sys.executable, "-m", "model_router.hook"], input=payload, text=True,
                              capture_output=True, env=env)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""


def test_entry_point_routes_a_real_payload(tmp_path):
    env = {"MODEL_ROUTER_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    payload = json.dumps(agent_event(prompt="Write a LinkedIn post about data migrations"))
    proc = subprocess.run([sys.executable, "-m", "model_router.hook"], input=payload, text=True, capture_output=True, env=env)
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"  # write x medium -> sonnet/medium


def test_hook_env_switch_disables_it_without_uninstalling(tmp_path):
    env = {"MODEL_ROUTER_HOME": str(tmp_path), "MODEL_ROUTER_HOOK": "0", "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, "-m", "model_router.hook"], input=json.dumps(agent_event()), text=True,
                          capture_output=True, env=env)
    assert proc.returncode == 0 and proc.stdout == ""


def _transcript(tmp_path, *lines):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return str(path)


def test_main_model_reads_last_non_sidechain_assistant_entry(tmp_path):
    from model_router.hook import main_model

    path = _transcript(
        tmp_path,
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "role": "assistant"}},
        {"type": "assistant", "isSidechain": True, "message": {"model": "claude-fable-5-1"}},
    )
    assert main_model(path) == "opus"
    assert main_model(str(tmp_path / "missing.jsonl")) is None
    assert main_model(None) is None


def test_critical_review_crosses_to_another_model_than_the_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    prompt = "Review this critical design of the payments migration before running it"
    event = agent_event(prompt=prompt)
    event["transcript_path"] = _transcript(tmp_path, {"type": "assistant", "message": {"model": "claude-opus-5-5"}})
    out = routed(event)
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert (updated["model"], updated["effort"]) == ("fable", "low")  # review x critical = opus/high, crossed
    assert out["systemMessage"].endswith("cross-checked against the chat")
    record = json.loads((tmp_path / "hook.jsonl").read_text().strip().splitlines()[-1])
    assert record["cell"] == "fable/low" and "another model than the chat (opus)" in record["cross"]


def test_a_strange_transcript_line_does_not_stop_the_routing(tmp_path, monkeypatch):
    from model_router.hook import main_model

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    path = _transcript(tmp_path, {"type": "assistant", "message": {"model": "claude-opus-5-5"}},
                       {"type": "assistant", "message": "assistant"}, ["assistant"])
    assert main_model(path) == "opus"  # the odd lines are skipped, the last good one counts
    event = agent_event()
    event["transcript_path"] = path
    assert routed(event)["hookSpecificOutput"]["updatedInput"]["model"] == "opus"


def test_a_log_that_cannot_be_written_does_not_stop_the_routing(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "state"
    blocker.write_text("not a directory")
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(blocker))
    out = routed(agent_event())
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"
    assert "not logged" in capsys.readouterr().err


def test_review_stays_put_when_chat_is_another_model_or_risk_is_not_critical(tmp_path):
    critical = "Review this critical design of the payments migration"
    updated, record = decided({"prompt": critical}, chat_model="fable")
    assert updated["model"] == "opus" and updated["effort"] == "high" and "cross" not in record
    updated, record = decided({"prompt": critical}, chat_model=None)
    assert updated["model"] == "opus" and "cross" not in record
    updated, record = decided({"prompt": "Review this SQL script before production"}, chat_model="opus")
    assert updated["model"] == "opus" and updated["effort"] == "medium" and "cross" not in record


def test_an_external_judge_never_becomes_a_subagents_model(tmp_path, monkeypatch):
    from model_router.catalog import load_external

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "judge": "gpt/high", "external": {"gpt": {"provider": "openai", "id": "gpt-5.5", "input": 5, "output": 30}}}))
    try:
        load_external(json.loads((tmp_path / "config.json").read_text()))
        updated, record = decided({"prompt": "Review this critical design of the payments migration"}, chat_model="opus")
        assert updated["model"] == "opus" and "cross" not in record
    finally:
        load_external({})


def test_without_fable_the_hook_stays_on_available_models(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus")
    updated, record = decided({"prompt": "Analyse the critical design of the payments migration"})
    assert updated["model"] == "opus" and record["cell"] == "opus/medium"  # fable/low degraded, then the floor
    updated, record = decided({"prompt": "Review this critical design of the payments migration"}, chat_model="opus")
    assert updated["model"] == "opus" and "cross" not in record  # no other model can take the cross-check
