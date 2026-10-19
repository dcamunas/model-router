import pytest

from model_router.catalog import lattice, parse_cell
from model_router.feedback import FeedbackStore
from model_router.policy import Policy, choose_verifier
from model_router.task import Task


def test_start_cell_follows_kind_and_risk():
    plan = Policy().plan(Task(prompt="x", kind="classify", risk="low"))
    assert plan.start.label == "haiku/low"
    assert plan.verify == "none" and plan.max_attempts == 2
    plan = Policy().plan(Task(prompt="x", kind="code", risk="high"))
    assert plan.start.label == "opus/medium"
    assert plan.verify == "light" and plan.budget_usd == 2.0
    plan = Policy().plan(Task(prompt="x", kind="code", risk="critical"))
    assert plan.verify == "full" and plan.budget_usd == 10.0


def test_risk_floor_overrides_feedback_exploration():
    store = FeedbackStore(explore=1.0, min_n=1)
    for _ in range(3):
        store.record("code", parse_cell("opus/high"), True)
    plan = Policy(feedback=store).plan(Task(prompt="x", kind="code", risk="critical"))
    assert plan.start.capability >= parse_cell("opus/medium").capability
    assert any("suelo" in r for r in plan.rationale) or plan.start.label == "opus/high"


def test_task_overrides_budget_attempts_and_verify():
    plan = Policy().plan(Task(prompt="x", kind="chat", risk="low", budget_usd=3.0, max_attempts=4, verify="full"))
    assert plan.budget_usd == 3.0 and plan.max_attempts == 4 and plan.verify == "full"
    assert len(plan.ladder) <= 4


def test_budget_grows_with_the_size_of_the_task():
    small = Policy().plan(Task(prompt="x" * 100, kind="summarize", risk="low"))
    big = Policy().plan(Task(prompt="x" * 400_000, kind="summarize", risk="low"))
    assert small.budget_usd == 0.10
    assert big.budget_usd > 0.10
    assert big.budget_usd == pytest.approx(3 * big.start.expected_cost(100_050, 1500))


def test_break_even_is_cost_ratio_including_verification():
    policy = Policy()
    start = parse_cell("opus/low")
    none = policy.break_even(start, "none", 2000, 1500)
    full = policy.break_even(start, "full", 2000, 1500)
    assert none == pytest.approx(start.expected_cost() / parse_cell("fable/low").expected_cost())
    assert full > none
    assert policy.break_even(parse_cell("fable/xhigh"), "none", 2000, 1500) is None


def test_feedback_demotes_below_the_break_even(tmp_path):
    store = FeedbackStore(min_n=4)
    for passed in (True, False, False, False):  # 25 % first-attempt pass rate, below the ~40 % break-even
        store.record("code", parse_cell("opus/low"), passed)
    plan = Policy(feedback=store).plan(Task(prompt="x", kind="code", risk="medium"))
    assert plan.start.label == "fable/low" and any("umbral" in r for r in plan.rationale)


def test_light_verifier_is_cheapest_cell_at_least_as_capable():
    cells = lattice()
    assert choose_verifier(parse_cell("haiku/medium"), "light", cells).label == "haiku/medium"
    assert choose_verifier(parse_cell("sonnet/medium"), "light", cells).label == "sonnet/medium"


def test_full_verifier_is_another_model_clearly_more_capable():
    cells = lattice()
    v = choose_verifier(parse_cell("sonnet/medium"), "full", cells)
    assert v.spec.short == "opus"
    v = choose_verifier(parse_cell("opus/low"), "full", cells)
    assert v.spec.short == "fable"
    v = choose_verifier(parse_cell("fable/xhigh"), "full", cells)
    assert v.spec.short == "fable" and v != parse_cell("fable/xhigh")
    assert choose_verifier(parse_cell("opus/low"), "none", cells) is None


def test_invalid_task_values_are_rejected():
    with pytest.raises(ValueError):
        Task(prompt="x", kind="poem")
    with pytest.raises(ValueError):
        Task(prompt="x", risk="huge")
    with pytest.raises(ValueError):
        Task(prompt="   ")
