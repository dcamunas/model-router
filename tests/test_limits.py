"""Usage limits: read from the CLI's rate-limit event (through a fake `claude` that really runs), kept until the
exact reset, never lost to a failed probe, and acted on by the hook, the plans and `router run`."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from model_router import clock, limits
from model_router.catalog import MODELS, available_models, configured_models
from model_router.hook import handle

NOW = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)
RESET = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)  # Sat 24/10 15:00 in Madrid
CLAUDE_MODELS = {s.short: s.id for s in MODELS.values() if s.claude}
FABLE_MESSAGE = "You've reached your Fable limit. Switch to another model, or manage usage credits"


def _event(status: str, *, window: str = "seven_day_overage_included", reset: datetime | None = RESET) -> str:
    info = {"status": status, "rateLimitType": window,
            "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": 1792714800}}}
    if reset is not None:
        info["resetsAt"] = int(reset.timestamp())
    return json.dumps({"type": "rate_limit_event", "rate_limit_info": info})


REJECTED, ALLOWED = _event("rejected"), _event("allowed_warning", window="seven_day")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    monkeypatch.delenv("MODEL_ROUTER_CHILD", raising=False)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    return tmp_path


def _fake_claude(tmp_path, output: str, *, sleep: float = 0) -> str:
    """Write a `claude` that prints `output`, optionally hangs, and records its arguments and environment."""
    script = tmp_path / "claude"
    log = tmp_path / "claude.calls"
    script.write_text(f"""#!/bin/sh
echo "$* CHILD=$MODEL_ROUTER_CHILD TRIBUNAL=$MODEL_TRIBUNAL" >> {log}
cat >/dev/null
cat <<'OUT'
{output}
OUT
sleep {sleep}
""")
    script.chmod(0o755)
    return str(script)


def test_a_real_probe_reads_the_exact_reset_and_runs_as_the_router_s_own_session(home, tmp_path):
    fake = _fake_claude(tmp_path, '{"type": "system"}\n' + REJECTED)
    probe = limits.probe("claude-fable-5-1", binary=fake)
    assert probe.status == "rejected" and probe.until == RESET and probe.window == "seven_day_overage_included"
    call = (tmp_path / "claude.calls").read_text()
    assert "--model claude-fable-5-1" in call and "stream-json" in call and "CHILD=1 TRIBUNAL=0" in call


def test_a_probe_cut_by_its_timeout_keeps_the_event_already_printed(home, tmp_path):
    fake = _fake_claude(tmp_path, REJECTED, sleep=5)  # the CLI goes on retrying after the event
    probe = limits.probe("claude-fable-5-1", binary=fake, timeout=1)
    assert probe.status == "rejected" and probe.until == RESET
    assert limits.probe("claude-fable-5-1", binary=str(tmp_path / "missing")).status == "unknown"


def test_events_a_refusal_wins_and_the_windows_come_along():
    assert limits.parse_events(ALLOWED + "\n" + REJECTED).status == "rejected"
    probe = limits.parse_events("not json\n" + ALLOWED)
    assert probe.status == "allowed" and probe.windows is not None and probe.windows["five_hour"]["utilization"] == 0.2
    assert limits.parse_events("").status == "unknown"


def test_a_limit_holds_until_the_exact_reset_and_no_longer(home, monkeypatch):
    limit = limits.record_failure("fable", limits.parse_events(REJECTED))
    assert limit is not None and limit.until == RESET and "fable" in limits.active()
    assert "claude-fable-5-1" not in available_models() and "claude-fable-5-1" in configured_models()
    monkeypatch.setattr(clock, "now", lambda: RESET - timedelta(seconds=1))
    assert "fable" in limits.active()
    monkeypatch.setattr(clock, "now", lambda: RESET)
    assert limits.active() == {} and "claude-fable-5-1" in available_models()


def test_a_failed_probe_never_replaces_a_known_reset(home):
    limits.record_failure("fable", limits.parse_events(REJECTED))
    after_failure = limits.record_failure("fable", limits.Probe("unknown"))
    after_check = limits.record_check("fable", limits.Probe("unknown"))
    assert after_failure is not None and after_failure.until == RESET  # still Saturday, not 10 minutes
    assert after_check is not None and after_check.until == RESET


def test_unknown_holds_briefly_after_a_failure_and_not_at_all_after_a_check(home, monkeypatch):
    assert limits.record_check("opus", limits.Probe("unknown")) is None and limits.active() == {}
    held = limits.record_failure("opus", limits.Probe("unknown"))
    assert held is not None and held.until is None
    monkeypatch.setattr(clock, "now", lambda: NOW + limits.RECHECK)
    assert limits.active() == {}


def test_an_answer_clears_the_model_and_the_account_and_a_past_reset_is_only_a_brief_hold(home):
    limits.record_failure("fable", limits.parse_events(REJECTED))
    limits.record_failure("haiku", limits.parse_events(_event("rejected", window="five_hour")))
    assert set(limits.active()) == {"fable", limits.ALL_MODELS}
    assert limits.record_check("haiku", limits.parse_events(ALLOWED)) is None
    assert set(limits.active()) == {"fable"}  # an answered call: the account is not spent
    past = limits.parse_events(_event("rejected", reset=NOW - timedelta(hours=1)))
    held = limits.record_failure("opus", past)  # a reset already gone: no time to show, a brief hold only
    assert held is not None and held.until is None


def test_a_spent_account_window_is_shown_but_routing_goes_on(home):
    limits.record_failure("haiku", limits.parse_events(_event("rejected", window="five_hour")))
    assert limits.ALL_MODELS in limits.active()
    assert available_models() == configured_models()  # nowhere else to go: nothing is left out
    context = handle({"hook_event_name": "SessionStart", "source": "startup", "cwd": "/"})
    assert context is not None and "every model is refused" in context["hookSpecificOutput"]["additionalContext"]


def test_the_failure_text_names_a_known_model_or_the_account():
    assert limits.model_in_failure(FABLE_MESSAGE, CLAUDE_MODELS) == "fable"
    assert limits.model_in_failure("API Error: Request rejected (429) · You've reached your Fable limit.",
                                   CLAUDE_MODELS) == "fable"
    for account in ("You've reached your weekly limit", "Claude usage limit reached", "You've hit your limit"):
        assert limits.model_in_failure(account, CLAUDE_MODELS) == limits.ALL_MODELS
    assert limits.model_in_failure("Connection reset by peer", CLAUDE_MODELS) is None


def test_stop_failure_asks_the_named_model_and_keeps_its_reset(home, tmp_path, monkeypatch):
    _fake_claude(tmp_path, REJECTED)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    out = handle({"hook_event_name": "StopFailure", "error": "rate_limit", "agent_id": "a1",
                  "last_assistant_message": FABLE_MESSAGE})
    assert out is not None and "fable limited until" in out["systemMessage"]
    assert "--model claude-fable-5-1" in (tmp_path / "claude.calls").read_text()
    assert limits.active()["fable"].until == RESET
    assert handle({"hook_event_name": "StopFailure", "error": "server_error"}) is None


def test_stop_failure_in_a_session_the_router_started_does_not_probe_again(home, tmp_path, monkeypatch):
    _fake_claude(tmp_path, REJECTED)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MODEL_ROUTER_CHILD", "1")
    event = {"hook_event_name": "StopFailure", "error": "rate_limit", "last_assistant_message": FABLE_MESSAGE}
    assert handle(event) is None
    assert not (tmp_path / "claude.calls").exists() and limits.active() == {}


def test_the_hook_routes_around_a_limited_model_and_refuses_it_when_named(home):
    limits.record_failure("fable", limits.parse_events(REJECTED))
    brief = "kind: analysis, risk: critical, access: write\nAnalyse the design"
    out = handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "cwd": "/",
                  "tool_input": {"subagent_type": "general-purpose", "prompt": brief}})
    assert out is not None and out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"
    assert "fable limited until" in out["systemMessage"]
    for named in ("fable", "claude-fable-5-1", "Fable", "fable[1m]"):
        out = handle({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s", "cwd": "/",
                      "tool_input": {"subagent_type": "general-purpose", "model": named, "prompt": "x"}})
        assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny", named
    context = handle({"hook_event_name": "SessionStart", "source": "startup", "cwd": "/"})
    assert context is not None and "Usage limits: fable until" in context["hookSpecificOutput"]["additionalContext"]


def test_router_run_learns_the_limit_of_the_model_its_message_names(home, tmp_path):
    from model_router.catalog import parse_cell
    from model_router.cli_client import CliClient
    from model_router.client import ClientUnavailableError

    client = CliClient.__new__(CliClient)
    client.config_dir, client.binary = tmp_path, _fake_claude(tmp_path, REJECTED)
    with pytest.raises(ClientUnavailableError, match="Fable limit"):
        client.to_completion({"is_error": True, "api_error_status": 429, "result": FABLE_MESSAGE},
                             parse_cell("sonnet/low"), None, 0.5)  # the cell that failed is not the model named
    assert "--model claude-fable-5-1" in (tmp_path / "claude.calls").read_text()
    assert limits.active()["fable"].until == RESET


def test_router_limits_shows_each_model_until_its_reset_and_the_windows(home, capsys):
    from model_router.cli import main

    limits.record_failure("fable", limits.parse_events(REJECTED))
    assert main(["limits"]) == 0
    out = capsys.readouterr().out
    assert "fable" in out and "seven_day_overage_included" in out and "five_hour" in out and "20%" in out
