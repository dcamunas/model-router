import pytest

from model_router.catalog import lattice, parse_cell
from model_router.feedback import FeedbackStore
from model_router.policy import START, LadderPolicy, Policy, choose_verifier, default_judge
from model_router.task import Task, contains_checker


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
    assert any("floor" in r for r in plan.rationale) or plan.start.label == "opus/high"


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
    assert none is not None and full is not None and full > none
    assert policy.break_even(parse_cell("fable/xhigh"), "none", 2000, 1500) is None


def test_feedback_demotes_below_the_break_even(tmp_path):
    store = FeedbackStore(min_n=4)
    for passed in (True, False, False, False):  # 25 % first-attempt pass rate, below the ~40 % break-even
        store.record("code", parse_cell("opus/low"), passed)
    plan = Policy(feedback=store).plan(Task(prompt="x", kind="code", risk="medium"))
    assert plan.start.label == "fable/low" and any("threshold" in r for r in plan.rationale)


def test_light_verifier_is_the_nearest_other_model_at_low_effort(monkeypatch):
    cells = lattice()
    light = choose_verifier(parse_cell("haiku/medium"), "light", cells)
    assert light is not None and light.label == "sonnet/low"  # the tier above first
    light = choose_verifier(parse_cell("fable/max"), "light", cells)
    assert light is not None and light.label == "opus/low"  # nothing above Fable: the tier below
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    light = choose_verifier(parse_cell("opus/max"), "light", lattice())
    assert light is not None and light.label == "opus/xhigh"  # one model only: its own cell, effort capped


def test_full_verifier_is_the_fixed_judge_on_another_model_than_the_worker():
    cells = lattice()
    v = choose_verifier(parse_cell("sonnet/medium"), "full", cells)
    assert v is not None and v.label == "opus/xhigh"  # the default judge
    v = choose_verifier(parse_cell("fable/xhigh"), "full", cells)
    assert v is not None and v.label == "opus/xhigh"  # a judge does not have to outrank the worker
    v = choose_verifier(parse_cell("opus/low"), "full", cells)
    assert v is not None and v.label == "fable/low"  # worker on the judge's model: next model up, low effort
    v = choose_verifier(parse_cell("opus/low"), "full", cells, judge=parse_cell("fable/max"))
    assert v is not None and v.label == "fable/xhigh"  # an explicit judge is capped at xhigh
    assert choose_verifier(parse_cell("opus/low"), "none", cells) is None


def test_judge_comes_from_the_environment_then_config_then_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_JUDGE", raising=False)
    assert default_judge().label == "opus/xhigh"
    (tmp_path / "config.json").write_text('{"judge": "sonnet/max"}')
    assert default_judge().label == "sonnet/xhigh"
    monkeypatch.setenv("MODEL_ROUTER_JUDGE", "fable/low")
    assert default_judge().label == "fable/low"
    assert Policy().judge.label == "fable/low"
    assert LadderPolicy([parse_cell("opus/high")]).judge.label == "fable/low"
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus")
    assert default_judge().label == "opus/low"  # fable missing: nearest available model, same effort
    monkeypatch.setenv("MODEL_ROUTER_JUDGE", "opus/turbo")
    with pytest.raises(ValueError):
        default_judge()


def test_judge_on_a_single_model_falls_back_to_the_model_below_or_another_cell(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "sonnet,opus")
    cells = lattice()
    v = choose_verifier(parse_cell("opus/medium"), "full", cells)
    assert v is not None and v.label == "sonnet/xhigh"  # nothing above opus here: the model below at the judge's effort
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    cells = lattice()
    assert choose_verifier(parse_cell("opus/medium"), "full", cells) == parse_cell("opus/xhigh")
    assert choose_verifier(parse_cell("opus/xhigh"), "full", cells) == parse_cell("opus/high")


def test_light_judge_stays_within_a_tier_and_falls_back_to_the_worker(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,opus")
    cells = lattice()
    light = choose_verifier(parse_cell("opus/high"), "light", cells)
    assert light is not None and light.label == "opus/high"  # Haiku is two tiers down: no check, itself instead
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus,fable")
    cells = lattice()
    light = choose_verifier(parse_cell("opus/high"), "light", cells)
    assert light is not None and light.label == "fable/low"  # the tier above, at low effort
    full = choose_verifier(parse_cell("fable/low"), "full", cells)
    assert full is not None and full.label == "opus/xhigh"  # the configured judge: another model than the worker


def test_critical_risk_gets_one_full_judge_and_high_risk_a_light_one():
    policy = Policy()
    for kind in ("analysis", "write", "review", "code"):
        assert policy.plan(Task(prompt="x", kind=kind, risk="critical")).verify == "full"
    assert len(policy.judges_for(parse_cell("opus/medium"), "full")) == 1
    assert policy.plan(Task(prompt="x", kind="analysis", risk="high")).verify == "light"
    with_checker = Task(prompt="x", kind="analysis", risk="critical", checker=contains_checker("42"))
    assert policy.plan(with_checker).verify == "full"
    assert policy.plan(Task(prompt="x", kind="analysis", risk="critical", verify="full")).verify == "full"
    with pytest.raises(ValueError, match="unknown verify mode"):
        Task(prompt="x", kind="code", risk="low", verify="twice")


def test_invalid_task_values_are_rejected():
    with pytest.raises(ValueError):
        Task(prompt="x", kind="poem")
    with pytest.raises(ValueError):
        Task(prompt="x", risk="huge")
    with pytest.raises(ValueError):
        Task(prompt="   ")
    with pytest.raises(ValueError):
        Task(prompt="x", verify="council")


def test_policy_falls_back_when_the_table_names_a_missing_model(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus")
    policy = Policy()
    plan = policy.plan(Task(prompt="x", kind="analysis", risk="critical"))  # table says fable/low
    assert plan.start.label == "opus/medium"  # fable/low -> opus/low, then the critical floor lifts it
    assert any("fable not available: fable/low -> opus/low" in r for r in plan.rationale)
    assert any("risk floor critical" in r for r in plan.rationale)
    assert all(c.spec.short != "fable" for c in plan.ladder)
    full = policy.verifier_for(plan.start, "full")
    assert full is not None and full.spec.short != "fable"
    plan = policy.plan(Task(prompt="x", kind="code", risk="critical"))  # floor opus/medium still applies
    assert plan.start.capability >= parse_cell("opus/medium").capability
    assert all(c.spec.short != "fable" for c in plan.ladder)


def test_judges_for_lists_the_one_judge_and_its_cost_counts_in_the_attempt():
    policy = Policy()
    cell = parse_cell("opus/high")
    assert policy.judges_for(cell, "none") == []
    assert policy.judges_for(cell, "full") == [choose_verifier(cell, "full", policy.cells)]
    assert policy.attempt_cost(cell, "full", 2000, 1500) > policy.attempt_cost(cell, "none", 2000, 1500)


def test_the_router_never_chooses_max_effort_on_its_own():
    cells = lattice()
    assert all(c.effort != "max" for c in cells)
    for start in ladder_starts():
        plan = Policy(cells=cells, start_table=start, calibration=None).plan(Task(prompt="x", kind="write", risk="high"))
        assert all(c.effort != "max" for c in plan.ladder)
    plan = Policy(cells=cells, start_table={"write": {"high": "opus/max"}, "unknown": START["unknown"]},
                  calibration=None).plan(Task(prompt="x", kind="write", risk="high"))
    assert plan.start.label == "opus/xhigh" and any("never chooses max" in r for r in plan.rationale)
    tuned = Policy(cells=cells, calibration=None, tuning={"start": {"write": {"high": {"cell": "fable/max"}}}})
    assert tuned.plan(Task(prompt="x", kind="write", risk="high")).start.label == "fable/xhigh"  # applied, capped
    assert parse_cell("opus/max").effort == "max"  # named by hand, it is still a cell


def ladder_starts() -> list[dict[str, dict[str, str]]]:
    return [{"write": {"high": cell}, "unknown": START["unknown"]} for cell in ("haiku/low", "sonnet/medium", "opus/high")]
