"""Usage limits: read from the CLI's rate-limit event, kept until the exact reset, and acted on by the hook, the
plans and `router run`."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from model_router import clock, limits
from model_router.catalog import available_models
from model_router.hook import handle

NOW = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)  # Sat 24/10 15:00 in Madrid
REJECTED = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
    "status": "rejected", "resetsAt": int(RESET.timestamp()), "rateLimitType": "seven_day_overage_included",
    "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1792714800},
                       "seven_day_overage_included": {"utilization": 1, "resetsAt": int(RESET.timestamp())}}}})
ALLOWED = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed_warning", "rateLimitType": "seven_day", "unifiedWindows": {"seven_day": {"utilization": 0.83}}}})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    return tmp_path


def test_the_event_gives_the_exact_reset_of_a_refused_model():
    probe = limits.parse_events("not json\n" + REJECTED + "\n{\"type\": \"result\"}")
    assert probe.status == "rejected" and probe.until == RESET and probe.window == "seven_day_overage_included"
    assert probe.windows is not None and probe.windows["five_hour"]["utilization"] == 0.2
    assert limits.parse_events(ALLOWED).status == "allowed"
    assert limits.parse_events("").status == "unknown"


def test_a_limit_holds_until_the_exact_reset_and_no_longer(home, monkeypatch):
    limit = limits.record("fable", limits.parse_events(REJECTED))
    assert limit is not None and limit.until == RESET and "fable" in limits.active()
    assert "claude-fable-5-1" not in available_models() and "claude-fable-5-1" in available_models(with_limited=True)
    monkeypatch.setattr(clock, "now", lambda: RESET - timedelta(seconds=1))
    assert "fable" in limits.active()
    monkeypatch.setattr(clock, "now", lambda: RESET)
    assert limits.active() == {} and "claude-fable-5-1" in available_models()


def test_an_allowed_answer_clears_a_limit_and_an_unknown_one_holds_for_a_while(home, monkeypatch):
    limits.record("fable", limits.parse_events(REJECTED))
    assert limits.record("fable", limits.parse_events(ALLOWED)) is None and limits.active() == {}
    held = limits.record("opus", limits.Probe("unknown"))
    assert held is not None and held.until is None and "opus" in limits.active()
    monkeypatch.setattr(clock, "now", lambda: NOW + limits.RECHECK)
    assert limits.active() == {} and limits.stale() == ["opus"]


def test_a_spent_account_window_holds_every_model_and_the_router_keeps_routing(home):
    spent = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": int(RESET.timestamp()), "rateLimitType": "five_hour"}})
    limit = limits.record("haiku", limits.parse_events(spent))
    assert limit is not None and limit.model == limits.ALL_MODELS
    assert len(available_models()) == 4  # nowhere else to go: nothing is left out


def test_the_failure_text_names_the_model_or_the_account():
    assert limits.model_in_failure("You've reached your Fable limit. Switch to another model") == "fable"
    assert limits.model_in_failure("Claude usage limit reached") == limits.ALL_MODELS
    assert limits.model_in_failure("Connection reset") is None


def test_stop_failure_on_a_limit_probes_the_model_and_keeps_its_reset(home, monkeypatch):
    asked = []
    monkeypatch.setattr(limits, "probe", lambda model_id, **_: asked.append(model_id) or limits.parse_events(REJECTED))
    out = handle({"hook_event_name": "StopFailure", "error": "rate_limit", "agent_id": "a1",
                  "last_assistant_message": "You've reached your Fable limit. Switch to another model"})
    assert asked == ["claude-fable-5-1"] and out is not None and "fable limited until" in out["systemMessage"]
    assert limits.active()["fable"].until == RESET
    assert handle({"hook_event_name": "StopFailure", "error": "server_error"}) is None


def test_the_hook_routes_around_a_limited_model_and_refuses_it_when_named(home):
    limits.record("fable", limits.parse_events(REJECTED))
    brief = "kind: analysis, risk: critical, access: write\nAnalyse the design"
    out = handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "cwd": "/",
                  "tool_input": {"subagent_type": "general-purpose", "prompt": brief}})
    assert out is not None and out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"
    assert "fable limited until" in out["systemMessage"]
    out = handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "cwd": "/",
                  "tool_input": {"subagent_type": "general-purpose", "model": "fable", "prompt": "x"}})
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "usage limit until" in out["hookSpecificOutput"]["permissionDecisionReason"]
    context = handle({"hook_event_name": "SessionStart", "source": "startup", "cwd": "/"})
    assert context is not None and "Usage limits: fable until" in context["hookSpecificOutput"]["additionalContext"]


def test_router_run_keeps_the_limit_a_refused_call_hit(home, monkeypatch, tmp_path):
    from model_router.catalog import parse_cell
    from model_router.cli_client import CliClient
    from model_router.client import ClientUnavailableError

    monkeypatch.setattr(limits, "probe", lambda *_, **__: limits.parse_events(REJECTED))
    client = CliClient.__new__(CliClient)
    client.config_dir, client.binary = tmp_path, "claude"
    with pytest.raises(ClientUnavailableError, match="Fable limit"):
        client.to_completion({"is_error": True, "api_error_status": 429,
                              "result": "You've reached your Fable limit."}, parse_cell("fable/low"), None, 0.5)
    assert limits.active()["fable"].until == RESET


def test_router_limits_shows_each_model_until_its_reset_and_the_windows(home, capsys):
    from model_router.cli import main

    limits.record("fable", limits.parse_events(REJECTED))
    assert main(["limits"]) == 0
    out = capsys.readouterr().out
    assert "fable" in out and "seven_day_overage_included" in out and "five_hour" in out and "20%" in out
