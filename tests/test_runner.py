from pathlib import Path

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
        judge={"fable/low": [ko()], "fable/medium": [ok()]},
    )
    task = Task(prompt="implement the migration", kind="code", risk="high", verify="full")
    result = Router(client).run(task)
    assert result.solved
    assert [a.cell.label for a in result.attempts] == ["opus/medium", "fable/low"]
    assert [a.verifier.label if a.verifier else None for a in result.attempts] == ["fable/low", "fable/medium"]
    assert "off by one" in result.attempts[0].reason
    assert result.verify_cost_usd > 0
    judge_calls = [c for c in client.calls if c["output_format"] == "Verdict"]
    assert len(judge_calls) == 2 and "<answer>" in judge_calls[0]["prompt"]


def test_low_risk_without_checker_accepts_first_answer():
    client = FakeClient(script={"*": [Reply("hola")]})
    result = Router(client).run(Task(prompt="hola?", kind="chat", risk="low"))
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
    assert s2.solved == 1 and "KO" not in s2.text()
    assert "coste por tarea resuelta" in s2.text()


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
