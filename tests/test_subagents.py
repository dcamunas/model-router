import json

from model_router.hook import decide, handle
from model_router.subagents import load, prompt_hash, read_transcript, record, summary_rows


def _turn(msg_id, *, model="claude-opus-5-5", cache_read=1000, cache_write=100, output=10, tools=0, at="10:00:00"):
    content = [{"type": "tool_use", "name": "Read", "id": f"t{i}", "input": {}} for i in range(tools)]
    content.append({"type": "text", "text": "..."})
    usage = {"input_tokens": 2, "output_tokens": output, "cache_read_input_tokens": cache_read,
             "cache_creation_input_tokens": cache_write}
    # a transcript writes one entry per content block, each repeating the message's usage
    return [{"timestamp": f"2026-10-22T{at}Z", "message": {"role": "assistant", "id": msg_id, "model": model,
                                                           "content": [block], "usage": usage}} for block in content]


def _transcript(path, prompt, turns):
    lines = [{"timestamp": "2026-10-22T10:00:00Z", "message": {"role": "user", "content": prompt}}]
    for turn in turns:
        lines += turn
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    return path


def test_a_transcript_is_counted_once_per_message_with_its_peak_context_tools_and_time(tmp_path):
    path = _transcript(tmp_path / "agent.jsonl", "Review the parser", [
        _turn("m1", cache_read=1000, cache_write=500, tools=2, at="10:00:05"),
        _turn("m2", cache_read=3000, cache_write=100, tools=1, at="10:01:05"),
        _turn("m3", cache_read=4000, cache_write=0, output=300, at="10:02:00"),
    ])
    u = read_transcript(path)
    assert (u.turns, u.tool_calls, u.cache_read_tokens, u.cache_write_tokens, u.output_tokens) == (3, 3, 8000, 600, 320)
    assert u.peak_context == 4002 and u.seconds == 120 and u.prompt == "Review the parser"
    assert u.model == "claude-opus-5-5" and u.tokens == 6 + 320 + 8000 + 600
    cost = u.cost()
    assert cost is not None and cost > 0


def test_a_finished_subagent_is_recorded_next_to_the_hook_s_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    prompt = "kind: review, risk: high\nReview the parser for off-by-one errors"
    out = handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s1", "cwd": "/tmp",
                  "tool_input": {"subagent_type": "general-purpose", "description": "review", "prompt": prompt}})
    assert out is not None
    agent = _transcript(tmp_path / "agent.jsonl", prompt, [_turn("m1"), _turn("m2", cache_read=5000)])
    event = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1", "agent_type": "general-purpose",
             "effort": {"level": "medium"}, "agent_transcript_path": str(agent)}
    assert handle(event) is None
    rec = load(tmp_path / "model-router" / "subagents.jsonl")[0]
    assert rec["routed"] == "opus/medium" and (rec["kind"], rec["risk"]) == ("review", "high")
    assert (rec["model"], rec["effort"], rec["turns"], rec["cache_read_tokens"]) == ("opus", "medium", 2, 6000)
    assert rec["prompt_hash"] == prompt_hash(prompt) and rec["cost_usd"] > 0 and "prompt" not in rec
    alone = _transcript(tmp_path / "other.jsonl", "classify these tickets as bug or billing", [_turn("x")])
    handle({**event, "agent_id": "a2", "agent_transcript_path": str(alone)})
    rec = load(tmp_path / "model-router" / "subagents.jsonl")[1]
    assert rec["routed"] is None and rec["kind"] == "classify"  # the hook left it alone: kind read from the prompt
    handle({**event, "agent_id": "a3", "agent_transcript_path": str(tmp_path / "missing.jsonl")})  # quiet
    monkeypatch.setenv("MODEL_ROUTER_HOOK_LOG", "0")
    handle({**event, "agent_id": "a4"})
    assert len(load(tmp_path / "model-router" / "subagents.jsonl")) == 2


def test_stats_groups_subagents_by_agent_and_cell_most_spent_first(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    base = {"agent_type": "general-purpose", "turns": 10, "peak_context": 50_000, "cache_read_tokens": 400_000}
    lines = [{**base, "model": "opus", "effort": "medium", "cost_usd": 1.0},
             {**base, "model": "opus", "effort": "medium", "cost_usd": 3.0},
             {**base, "model": "haiku", "effort": "low", "cost_usd": 0.01, "agent_type": "model-router:reader"}]
    (tmp_path / "model-router" / "subagents.jsonl").write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    rows = summary_rows(load(tmp_path / "model-router" / "subagents.jsonl"))
    assert rows[0][:5] == ["general-purpose", "opus/medium", 2, 10.0, "50.0K"] and rows[0][-1] == "$4.00"
    assert rows[1][1] == "haiku/low"
    main(["stats"])
    out = capsys.readouterr().out
    assert "subagents, measured from their transcripts" in out and "model-router:reader" in out


def test_record_without_a_known_model_has_no_cost():
    from model_router.subagents import SubagentUsage

    usage = SubagentUsage(model="some-new-model", turns=1, output_tokens=5)
    assert record({"agent_type": "x"}, usage, None)["cost_usd"] is None
    assert decide({"subagent_type": "Explore", "prompt": "x"})[0] is None  # untouched agents are still measured later


def test_a_first_reading_is_completed_once_the_transcript_settles(tmp_path, monkeypatch):
    from model_router.subagents import finalize

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    agent = _transcript(tmp_path / "agent.jsonl", "Review the parser", [_turn("m1")])  # the last turn is not in yet
    handle({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1", "agent_type": "general-purpose",
            "agent_transcript_path": str(agent)})
    log = tmp_path / "model-router" / "subagents.jsonl"
    assert load(log)[0]["turns"] == 1 and load(log)[0]["final"] is False
    _transcript(agent, "Review the parser", [_turn("m1"), _turn("m2", output=500)])  # Claude Code writes the end
    assert finalize(log) == 0  # still fresh: it may be written to
    later = agent.stat().st_mtime + 60
    assert finalize(log, now=later) == 1
    rec = load(log)[0]
    assert rec["final"] is True and rec["turns"] == 2 and rec["output_tokens"] == 510
    agent.unlink()
    assert finalize(log, now=later) == 0  # final records are left alone, and a vanished transcript is no error


def test_finalize_keeps_records_appended_meanwhile_survives_bad_ones_and_counts_a_subagent_once(tmp_path):
    from model_router.subagents import append, finalize, summary_rows

    log = tmp_path / "model-router" / "subagents.jsonl"
    agent = _transcript(tmp_path / "agent.jsonl", "Review", [_turn("m1"), _turn("m2")])
    base = {"agent_type": "general-purpose", "model": "opus", "effort": "low", "session": "s", "final": False}
    append(log, {**base, "agent_id": "bad", "transcript": ""})  # Path("") is a directory: never read, made final
    append(log, {**base, "agent_id": "dir", "transcript": str(tmp_path)})
    append(log, {**base, "agent_id": "a1", "transcript": str(agent), "turns": 1, "cost_usd": 0.1})
    assert finalize(log, now=agent.stat().st_mtime + 60) == 3
    recs = load(log)
    assert all(r["final"] for r in recs) and recs[2]["turns"] == 2 and recs[2]["context_cost_usd"] is not None
    append(log, {**base, "agent_id": "a1", "final": True, "turns": 5, "cost_usd": 1.0})  # resumed: a later record
    rows = summary_rows(load(log))
    assert rows[0][2] == 3  # three subagents, a1 once (its last record)


def test_an_old_config_naming_unknown_models_never_stops_the_router(tmp_path, monkeypatch, capsys):
    from model_router.catalog import available_models
    from model_router.cli import main

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    (tmp_path / "model-router" / "config.json").write_text(json.dumps({"judge": "gpt/high", "models": ["luna"]}))
    assert len(available_models()) == 4  # only unknown names listed: every Claude model instead of none
    assert main(["plan", "review the payment code", "--risk", "high"]) == 0
    out = capsys.readouterr().out
    assert "judge setting cannot be used" in out and "opus/xhigh instead" in out


def test_load_recent_reads_only_the_end_and_skips_a_cut_line(tmp_path):
    from model_router.subagents import load_recent

    log = tmp_path / "subagents.jsonl"
    log.write_text("".join(json.dumps({"agent_id": f"a{i}", "pad": "x" * 50}) + "\n" for i in range(1000)))
    recent = load_recent(log, max_bytes=2000)
    assert recent and recent[-1]["agent_id"] == "a999" and len(recent) < 40
    assert all("agent_id" in r for r in recent)  # the line cut in half at the start is not misread
    assert load_recent(tmp_path / "missing.jsonl") == []
