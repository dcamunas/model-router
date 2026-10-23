from pathlib import Path

from model_router.catalog import Usage, parse_cell
from model_router.client import ClientUnavailableError
from model_router.ledger import Ledger, summarize
from model_router.runner import Router
from model_router.task import Task, contains_checker
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


def test_judge_records_an_external_answer_without_worker_cost(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = FakeClient(judge={"*": [ok("solid")]})
    router = Router(client, ledger=ledger)
    result = router.judge(Task(prompt="write the notice", kind="write", risk="critical", id="ext1"), "the notice",
                          mode="full")
    assert result.solved and result.verify == "full" and result.answer == "the notice"
    attempt = result.attempts[0]
    assert attempt.stop_reason == "external" and attempt.cost_usd == 0 and attempt.usage.total == 0
    assert attempt.verdict is True and attempt.verdict_source == "judge" and len(attempt.judges) == 1
    assert result.verify_cost_usd > 0 and "judged only" in result.rationale[-1]
    assert all(c["output_format"] == "Verdict" for c in client.calls)  # no worker call
    assert ledger.tasks()[0]["attempts"][0]["stop_reason"] == "external"
    light = router.judge(Task(prompt="x", kind="chat", risk="low"), "hi", worker=parse_cell("sonnet/low"), mode="light")
    assert light.attempts[0].verdict_source == "judge" and light.attempts[0].judges == [parse_cell("opus/low")]
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
    result = Router(client, ledger=ledger).run(Task(prompt="x", kind="write", risk="high"))
    assert result.status == "error" and result.answer == "draft" and result.final_cell == parse_cell("opus/low")
    attempt = result.attempts[0]
    assert attempt.verdict is None and attempt.verdict_source == "judge-error" and "rate limited" in attempt.reason
    assert attempt.cost_usd > 0 and result.verify_cost_usd == 0
    assert ledger.tasks()[0]["status"] == "error"


def test_truncation_retry_stops_at_the_models_output_cap():
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens"), Reply("whole")]})
    result = Router(client).run(Task(prompt="chat", kind="chat", risk="low", max_tokens=100_000))
    assert result.solved and [c["max_tokens"] for c in client.calls] == [100_000, 128_000]
    client = FakeClient(script={"sonnet/low": [Reply("half", stop_reason="max_tokens")]})
    result = Router(client).run(Task(prompt="chat", kind="chat", risk="low", max_tokens=128_000))
    assert len(client.calls) == 1 and result.attempts[0].verdict_source == "none"  # no room left: no retry


def test_api_unavailable_is_recorded_and_not_escalated():
    class Down:
        def complete(self, cell, **kw):
            raise ClientUnavailableError("rate limited")

    result = Router(Down()).run(Task(prompt="x", kind="chat", risk="low"))
    assert result.status == "error" and len(result.attempts) == 1 and result.attempts[0].cost_usd == 0


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
    assert "cost per solved task" in s.text()


def test_escalation_carries_the_failure_reason_to_the_next_cell():
    client = FakeClient(script={"opus/low": [Reply("def f(): return 1")], "fable/low": [Reply("def f(): return 42")]})
    task = Task(prompt="write f", kind="code", risk="medium", verify="none", checker=contains_checker("42"))
    result = Router(client).run(task)
    first, second = (c["prompt"] for c in client.calls)
    assert first == "write f"
    assert second.startswith("write f\n\nA previous attempt at this task (by opus) was rejected") and "missing: 42" in second
    assert [a.carried_reason for a in result.attempts] == [False, True]
    assert result.to_dict()["attempts"][1]["carried_reason"] is True


def test_the_summary_counts_each_judge_s_verdicts_and_the_record_names_the_version(tmp_path: Path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    client = FakeClient(script={"*": [Reply("x")]}, judge={"fable/low": [ok(), ok(), ko("thin")]})
    router = Router(client, ledger=ledger)
    for i in range(3):
        router.run(Task(prompt="x", kind="write", risk="critical", id=f"w{i}", max_attempts=1))
    summary = summarize(ledger.tasks())
    assert summary.by_judge["fable/low"] == {"verdicts": 3, "passes": 2}
    assert "fable/low" in summary.text()
    assert all(record["router_version"] for record in ledger.tasks())


def test_ask_takes_another_prompt_system_and_role_for_a_seat_of_someone_else_s():
    from model_router.verify import Verifier

    client = FakeClient(judge={"*": [ok("the second judge was right")]})
    verifier = Verifier(client, Router(client).policy)
    task = Task(prompt="write the notice", kind="write", risk="critical")
    result = verifier.ask(task, "the notice", parse_cell("sonnet/high"), prompt="<opinions>both</opinions>",
                          system="You read two opinions and decide.", role="second-reader")
    assert result.passed and result.source == "judge" and result.verdicts[0].role == "second-reader"
    assert client.calls[0]["prompt"] == "<opinions>both</opinions>"
    assert client.calls[0]["system"] == "You read two opinions and decide."
    plain = verifier.ask(task, "the notice", parse_cell("sonnet/high"))
    assert "<task>" in client.calls[1]["prompt"] and plain.verdicts[0].role == "judge"
