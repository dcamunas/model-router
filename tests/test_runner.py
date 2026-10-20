from pathlib import Path

import pytest

from model_router.catalog import Usage, parse_cell
from model_router.client import ClientUnavailableError
from model_router.feedback import FeedbackStore
from model_router.ledger import Ledger, summarize
from model_router.policy import LadderPolicy, Policy
from model_router.runner import Router
from model_router.task import Task, contains_checker, regex_checker
from model_router.testing import FakeClient, Reply
from model_router.verify import Verdict


def ok(reason="fine"):
    return Reply(parsed=Verdict(passed=True, confidence=0.9, reason=reason), usage=Usage(2000, 100))


def ko(reason="wrong"):
    return Reply(parsed=Verdict(passed=False, confidence=0.9, reason=reason, issues=["off by one"]), usage=Usage(2000, 100))


def test_checker_failure_escalates_to_the_next_cell_and_counts_costs():
    client = FakeClient(script={"opus/low": [Reply("def f(): return 1")], "fable/low": [Reply("def f(): return 42")]})
    task = Task(prompt="write f", kind="code", risk="medium", verify="none", checker=contains_checker("42"))
    result = Router(client).run(task)
    assert result.solved and result.final_cell == parse_cell("fable/low")
    assert [a.cell.label for a in result.attempts] == ["opus/low", "fable/low"]
    assert result.attempts[0].verdict is False and result.attempts[0].verdict_source == "checker"
    assert result.retries == 1
    expected = (1000 * 4 + 500 * 20) / 1e6 + (1000 * 10 + 500 * 50) / 1e6
    assert abs(result.total_cost_usd - expected) < 1e-9
    assert result.verify_cost_usd == 0.0


def test_judge_verdict_drives_escalation_and_full_verifier_is_cross_model():
    client = FakeClient(
        script={"opus/medium": [Reply("meh")], "fable/low": [Reply("good")]},
        judge={"fable/low": [ko()], "opus/xhigh": [ok()]},
    )
    task = Task(prompt="implement the migration", kind="code", risk="high", verify="full")
    result = Router(client).run(task)
    assert result.solved
    assert [a.cell.label for a in result.attempts] == ["opus/medium", "fable/low"]
    assert [a.verifier.label if a.verifier else None for a in result.attempts] == ["fable/low", "opus/xhigh"]
    assert "off by one" in result.attempts[0].reason
    assert result.verify_cost_usd > 0
    judge_calls = [c for c in client.calls if c["output_format"] == "Verdict"]
    assert len(judge_calls) == 2 and "<answer>" in judge_calls[0]["prompt"]


def test_panel_needs_both_judges_and_a_split_escalates():
    client = FakeClient(
        script={"opus/medium": [Reply("draft")], "fable/low": [Reply("better draft")]},
        judge={"opus/medium": [ok()], "fable/low": [ko("thin"), ok()], "opus/xhigh": [ok()]},
    )
    result = Router(client).run(Task(prompt="write the incident report", kind="write", risk="critical", verify="panel"))
    assert result.verify == "panel" and result.solved
    first, second = result.attempts
    assert first.cell.label == "opus/medium" and first.verdict is False and first.verdict_source == "panel"
    assert [j.label for j in first.judges] == ["opus/medium", "fable/low"] and first.verifier == parse_cell("fable/low")
    assert "panel split: opus/medium passed, fable/low failed" in first.reason and "thin" in first.reason
    assert second.cell.label == "fable/low" and second.verdict is True
    assert [j.label for j in second.judges] == ["opus/xhigh", "fable/low"] and "panel agreed" in second.reason
    judge_calls = [c["cell"] for c in client.calls if c["output_format"] == "Verdict"]
    assert judge_calls == ["opus/medium", "fable/low", "opus/xhigh", "fable/low"]
    assert first.verify_cost_usd == pytest.approx(0.035)  # opus/medium 0.010 + fable/low 0.025 on Usage(2000, 100)
    assert result.verify_cost_usd == pytest.approx(first.verify_cost_usd + second.verify_cost_usd) == pytest.approx(0.070)
    assert result.to_dict()["attempts"][0]["judges"] == ["opus/medium", "fable/low"]


def test_tribunal_asks_both_judges_and_the_arbiter_settles_a_split(tmp_path: Path):
    client = FakeClient(
        script={"*": [Reply("the notice")]},
        judge={"opus/medium": [ok("fine")], "fable/low": [ko("thin")], "fable/medium": [ok("judge A was right")]},
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    result = Router(client, ledger=ledger).run(Task(prompt="write the incident report", kind="write", risk="critical"))
    assert result.verify == "tribunal" and result.solved and len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.verdict is True and attempt.verdict_source == "tribunal" and attempt.arbiter == parse_cell("fable/medium")
    assert [j.label for j in attempt.judges] == ["opus/medium", "fable/low"]
    assert [(v.cell.label, v.passed, v.role) for v in attempt.verdicts] == [
        ("opus/medium", True, "judge"), ("fable/low", False, "judge"), ("fable/medium", True, "arbiter"),
    ]
    assert "tribunal split: opus/medium passed, fable/low failed; arbiter fable/medium sided with opus/medium" in attempt.reason
    calls = [c for c in client.calls if c["output_format"] == "Verdict"]
    assert [c["cell"] for c in calls] == ["opus/medium", "fable/low", "fable/medium"]
    ruling = calls[-1]["prompt"]
    assert "<verdicts>" in ruling and "Judge A passed" in ruling and "Judge B failed" in ruling
    assert "fable" not in ruling.split("<verdicts>")[1]  # the arbiter does not see who the judges were
    assert attempt.verify_cost_usd == pytest.approx(0.010 + 0.025 + 0.025)
    record = ledger.tasks()[0]["attempts"][0]
    assert record["arbiter"] == "fable/medium" and [v["role"] for v in record["verdicts"]] == ["judge", "judge", "arbiter"]


def test_tribunal_agreement_needs_no_arbiter_and_a_split_without_one_fails(monkeypatch):
    client = FakeClient(script={"*": [Reply("x")]}, judge={"*": [ok()]})
    result = Router(client).run(Task(prompt="x", kind="write", risk="critical"))
    attempt = result.attempts[0]
    assert result.solved and attempt.arbiter is None and "tribunal agreed" in attempt.reason
    assert len([c for c in client.calls if c["output_format"] == "Verdict"]) == 2
    client = FakeClient(script={"*": [Reply("x")]}, judge={"*": [ko()]})
    result = Router(client).run(Task(prompt="x", kind="write", risk="critical", max_attempts=1))
    assert result.status == "unsolved" and "tribunal agreed" in result.attempts[0].reason
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    client = FakeClient(script={"*": [Reply("x")]}, judge={"opus/high": [ok()], "opus/xhigh": [ko("no")]})
    router = Router(client, policy=LadderPolicy([parse_cell("opus/high")], verify="tribunal"))
    result = router.run(Task(prompt="x", kind="write", risk="critical"))
    attempt = result.attempts[0]
    assert result.status == "unsolved" and attempt.verdict is False and "no arbiter available" in attempt.reason
    assert attempt.arbiter is None and len(attempt.verdicts) == 2


def test_scoreboard_counts_agreement_arbiter_backing_and_human_contradictions(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = FakeClient(
        script={"*": [Reply("x")]},
        judge={"opus/medium": [ok(), ok(), ok()], "fable/low": [ko("thin"), ok(), ok()], "fable/medium": [ok()]},
    )
    router = Router(client, ledger=ledger)
    for i in range(3):
        router.run(Task(prompt="x", kind="write", risk="critical", id=f"w{i}"))
    ledger.override("w2", passed=False, note="it was bad")
    board = summarize(ledger.tasks()).by_judge
    assert board["opus/medium"] == {"verdicts": 3, "passes": 3, "agreed": 2, "split": 1, "backed": 1, "overruled": 0,
                                    "contradicted": 1}
    assert board["fable/low"] == {"verdicts": 3, "passes": 2, "agreed": 2, "split": 1, "backed": 0, "overruled": 1,
                                  "contradicted": 1}
    assert "fable/medium" not in board  # the arbiter is not a judge on the scoreboard
    text = summarize(ledger.tasks()).text()
    assert "judges' scoreboard" in text and "opus/medium" in text


def test_panel_stops_at_the_first_failing_judge():
    client = FakeClient(script={"*": [Reply("draft")]}, judge={"opus/medium": [ko("vague")], "*": [ok()]})
    result = Router(client).run(Task(prompt="x", kind="write", risk="critical", max_attempts=1, verify="panel"))
    assert result.status == "unsolved"
    attempt = result.attempts[0]
    assert attempt.verdict is False and attempt.verdict_source == "panel"
    assert [j.label for j in attempt.judges] == ["opus/medium"] and "was not asked" in attempt.reason
    assert len([c for c in client.calls if c["output_format"] == "Verdict"]) == 1


def test_panel_reduced_to_one_judge_is_reported_as_a_judge(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    client = FakeClient(script={"*": [Reply("x")]}, judge={"*": [ok()]})
    router = Router(client, policy=LadderPolicy([parse_cell("opus/max")], verify="panel"))
    result = router.run(Task(prompt="x", kind="analysis", risk="critical"))
    attempt = result.attempts[0]
    assert result.solved and attempt.verdict_source == "judge" and [j.label for j in attempt.judges] == ["opus/xhigh"]


def test_judge_records_an_external_answer_without_worker_cost(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = FeedbackStore(tmp_path / "fb.json")
    client = FakeClient(judge={"*": [ok("solid")]})
    router = Router(client, ledger=ledger, feedback=store)
    result = router.judge(Task(prompt="write the notice", kind="write", risk="critical", id="ext1"), "the notice")
    assert result.solved and result.verify == "tribunal" and result.answer == "the notice"
    attempt = result.attempts[0]
    assert attempt.stop_reason == "external" and attempt.cost_usd == 0 and attempt.usage.total == 0
    assert attempt.verdict is True and attempt.verdict_source == "tribunal" and len(attempt.judges) == 2
    assert result.verify_cost_usd > 0 and "judged only" in result.rationale[-1]
    assert all(c["output_format"] == "Verdict" for c in client.calls)  # no worker call
    assert ledger.tasks()[0]["attempts"][0]["stop_reason"] == "external" and store.data == {}
    light = router.judge(Task(prompt="x", kind="chat", risk="low"), "hi", worker=parse_cell("sonnet/low"), mode="light")
    assert light.attempts[0].verdict_source == "judge" and light.attempts[0].judges == [parse_cell("sonnet/low")]
    failed = router.judge(Task(prompt="x", kind="code", risk="low", checker=contains_checker("42")), "41")
    assert failed.status == "unsolved" and failed.attempts[0].verdict_source == "checker"

    class Down(FakeClient):
        def complete(self, cell, **kw):
            raise ClientUnavailableError("rate limited")

    broken = Router(Down(), ledger=ledger).judge(Task(prompt="x", kind="write", risk="high"), "draft")
    assert broken.status == "error" and "rate limited" in broken.attempts[0].reason


def test_low_risk_without_checker_accepts_first_answer():
    client = FakeClient(script={"*": [Reply("hello")]})
    result = Router(client).run(Task(prompt="hello?", kind="chat", risk="low"))
    assert result.solved and len(result.attempts) == 1
    assert result.attempts[0].verdict_source == "none"
    assert len(client.calls) == 1


def test_exhausted_budget_stops_escalation_with_status_budget():
    client = FakeClient(script={"*": [Reply("no", usage=Usage(1000, 1000))]}, judge={"*": [ko()]})
    task = Task(prompt="hard", kind="analysis", risk="high", budget_usd=0.03)
    result = Router(client).run(task)
    assert result.status == "budget"
    assert len(result.attempts) == 1


def test_two_refusals_end_the_task_as_refused():
    client = FakeClient(script={"*": [Reply("", refused=True, refusal_category="general_harms", stop_reason="refusal")]})
    result = Router(client).run(Task(prompt="x", kind="write", risk="medium"))
    assert result.status == "refused"
    assert len(result.attempts) == 2 and all(a.refused for a in result.attempts)


def test_truncated_answer_is_retried_once_on_the_same_cell_with_more_room():
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens"), Reply("whole answer")]})
    task = Task(prompt="chat", kind="chat", risk="low", max_tokens=1000)
    result = Router(client).run(task)
    assert result.solved
    assert [a.verdict_source for a in result.attempts] == ["truncated", "none"]
    assert [c["max_tokens"] for c in client.calls] == [1000, 2000]


def test_judge_outage_keeps_the_attempt_and_ends_the_task_as_error(tmp_path: Path):
    class JudgeDown(FakeClient):
        def complete(self, cell, **kw):
            if kw.get("output_format") is not None:
                raise ClientUnavailableError("rate limited")
            return super().complete(cell, **kw)

    client = JudgeDown(script={"*": [Reply("draft")]})
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = FeedbackStore(tmp_path / "fb.json")
    result = Router(client, ledger=ledger, feedback=store).run(Task(prompt="x", kind="write", risk="high"))
    assert result.status == "error" and result.answer == "draft" and result.final_cell == parse_cell("opus/low")
    attempt = result.attempts[0]
    assert attempt.verdict is None and attempt.verdict_source == "judge-error" and "rate limited" in attempt.reason
    assert attempt.cost_usd > 0 and result.verify_cost_usd == 0
    assert ledger.tasks()[0]["status"] == "error"
    assert store.data == {}  # a judge that could not answer is no evidence against the worker


def test_feedback_learns_only_from_verified_verdicts(tmp_path: Path):
    store = FeedbackStore(tmp_path / "fb.json")
    client = FakeClient(script={"*": [Reply("hello")]}, judge={"*": [Reply("{}", parsed=None)]})
    router = Router(client, feedback=store)
    router.run(Task(prompt="hi", kind="chat", risk="low"))  # accepted without verification
    assert store.data == {}
    result = router.run(Task(prompt="x", kind="write", risk="high", max_attempts=1))  # judge returns no verdict
    assert result.attempts[0].verdict_source == "judge-error"
    assert store.data == {}
    router.run(Task(prompt="x", kind="chat", risk="low", checker=contains_checker("hello")))
    assert store.first_attempt_stats("chat", parse_cell("sonnet/low")) == (1, 1.0)


def test_first_attempt_flag_belongs_to_the_start_cell_only(tmp_path: Path):
    store = FeedbackStore(tmp_path / "fb.json")
    refusal = Reply("", refused=True, refusal_category="x", stop_reason="refusal")
    client = FakeClient(script={"opus/low": [refusal], "fable/low": [Reply("42")]})
    result = Router(client, feedback=store).run(Task(prompt="f", kind="code", risk="medium", checker=contains_checker("42")))
    assert result.solved and [a.cell.label for a in result.attempts] == ["opus/low", "fable/low"]
    assert store.first_attempt_stats("code", parse_cell("fable/low")) == (0, None)  # escalated, not a first attempt
    assert store.data["code"]["fable/low"] == {"n": 1, "pass": 1, "n_first": 0, "pass_first": 0}
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens"), Reply("42")]})
    Router(client, feedback=store).run(Task(prompt="f", kind="chat", risk="low", max_tokens=1000, checker=contains_checker("42")))
    assert store.first_attempt_stats("chat", parse_cell("sonnet/low")) == (1, 1.0)  # the retry on the start cell is first


def test_truncation_retry_stops_at_the_models_output_cap():
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens"), Reply("whole")]})
    result = Router(client).run(Task(prompt="chat", kind="chat", risk="low", max_tokens=100_000))
    assert result.solved and [c["max_tokens"] for c in client.calls] == [100_000, 128_000]
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens")]})
    result = Router(client).run(Task(prompt="chat", kind="chat", risk="low", max_tokens=128_000))
    assert len(client.calls) == 1 and result.attempts[0].verdict_source == "none"  # no room left: no retry


def test_in_memory_ledger_keeps_the_original_record_after_an_override():
    ledger = Ledger(None)
    client = FakeClient(script={"*": [Reply("42")]})
    Router(client, ledger=ledger).run(Task(prompt="f", kind="code", risk="medium", checker=contains_checker("42"), id="m1"))
    ledger.override("m1", passed=False)
    assert ledger.tasks()[0]["status"] == "unsolved"
    found = ledger.find("m1")
    assert found is not None and found["status"] == "solved" and "overridden" not in found
    assert ledger.last_override("m1") is False and ledger.last_override("nope") is None


def test_api_unavailable_is_recorded_and_not_escalated():
    class Down:
        def complete(self, cell, **kw):
            raise ClientUnavailableError("rate limited")

    result = Router(Down()).run(Task(prompt="x", kind="chat", risk="low"))
    assert result.status == "error" and len(result.attempts) == 1 and result.attempts[0].cost_usd == 0


def test_fixed_ladder_policy_never_escalates_past_its_cells():
    client = FakeClient(script={"*": [Reply("bad")]}, judge={"*": [ko()]})
    router = Router(client, policy=LadderPolicy([parse_cell("sonnet/high")]))
    result = router.run(Task(prompt="x", kind="code", risk="high", checker=regex_checker("good")))
    assert result.status == "unsolved" and len(result.attempts) == 1


def test_ledger_persists_and_summarises(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = FakeClient(script={"opus/low": [Reply("1")], "fable/low": [Reply("42")]})
    router = Router(client, ledger=ledger)
    router.run(Task(prompt="f", kind="code", risk="medium", verify="none", checker=contains_checker("42"), id="a1"))
    router.run(Task(prompt="g", kind="code", risk="medium", verify="none", checker=contains_checker("1"), id="a2"))
    records = ledger.tasks()
    assert [r["task_id"] for r in records] == ["a1", "a2"]
    s = summarize(records)
    assert s.tasks == 2 and s.solved == 2 and s.retries == 1 and s.attempts == 3
    assert s.by_cell["opus/low"]["passes"] == 1 and s.by_cell["opus/low"]["judged"] == 2
    assert s.cost_per_solved is not None and abs(s.cost_per_solved - s.total_cost / 2) < 1e-12
    ledger.override("a2", passed=False, note="wrong after all")
    s2 = summarize(ledger.tasks())
    assert s2.solved == 1
    assert "cost per solved task" in s2.text()


def test_feedback_learns_first_attempt_rate_and_moves_the_start(tmp_path: Path):
    store = FeedbackStore(tmp_path / "fb.json", min_n=3)
    client = FakeClient(script={"opus/low": [Reply("1")], "fable/low": [Reply("42")]}, judge={"*": [ok()]})
    router = Router(client, policy=Policy(feedback=store), feedback=store)
    for i in range(3):
        router.run(Task(prompt="f", kind="code", risk="medium", checker=contains_checker("42"), id=f"t{i}"))
    n, rate = store.first_attempt_stats("code", parse_cell("opus/low"))
    assert (n, rate) == (3, 0.0)
    plan = Policy(feedback=store).plan(Task(prompt="f", kind="code", risk="medium"))
    assert plan.start.label == "fable/low"
    assert any("feedback" in r for r in plan.rationale)
    reloaded = FeedbackStore(tmp_path / "fb.json", min_n=3)
    assert reloaded.first_attempt_stats("code", parse_cell("opus/low")) == (3, 0.0)
    assert reloaded.rows()[0][:2] == ["code", "fable/low"] or reloaded.rows()[0][:2] == ["code", "opus/low"]
