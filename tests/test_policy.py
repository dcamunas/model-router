import pytest

from model_router.catalog import lattice, parse_cell
from model_router.paths import write_config
from model_router.policy import Policy, choose_verifier, default_judge
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


def test_light_verifier_is_the_nearest_other_model_at_low_effort():
    cells = lattice()
    light = choose_verifier(parse_cell("haiku/medium"), "light", cells)
    assert light is not None and light.label == "sonnet/low"  # the tier above first
    light = choose_verifier(parse_cell("fable/max"), "light", cells)
    assert light is not None and light.label == "opus/low"  # nothing above Fable: the tier below
    write_config(models=["opus"])
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


def test_judge_comes_from_the_config_then_the_default():
    assert default_judge().label == "opus/xhigh"
    write_config(judge="sonnet/max")
    assert default_judge().label == "sonnet/xhigh"  # capped at xhigh
    write_config(judge="fable/low")
    assert default_judge().label == "fable/low" and Policy().judge.label == "fable/low"
    write_config(models=["haiku", "sonnet", "opus"])
    assert default_judge().label == "opus/low"  # fable missing: nearest available model, same effort
    write_config(judge="opus/turbo")
    with pytest.raises(ValueError):
        default_judge()
    policy = Policy()  # an unusable judge setting: the default, and the plan says so
    assert policy.judge.label == "opus/xhigh" and any("judge setting cannot be used" in n for n in policy.notes)


def test_judge_on_a_single_model_falls_back_to_the_model_below_or_another_cell():
    write_config(models=["sonnet", "opus"])
    cells = lattice()
    v = choose_verifier(parse_cell("opus/medium"), "full", cells)
    assert v is not None and v.label == "sonnet/xhigh"  # nothing above opus here: the model below at the judge's effort
    write_config(models=["opus"])
    cells = lattice()
    assert choose_verifier(parse_cell("opus/medium"), "full", cells) == parse_cell("opus/xhigh")
    assert choose_verifier(parse_cell("opus/xhigh"), "full", cells) == parse_cell("opus/high")


def test_light_judge_stays_within_a_tier_and_falls_back_to_the_worker():
    write_config(models=["haiku", "opus"])
    cells = lattice()
    light = choose_verifier(parse_cell("opus/high"), "light", cells)
    assert light is not None and light.label == "opus/high"  # Haiku is two tiers down: no check, itself instead
    write_config(models=["opus", "fable"])
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


def test_policy_falls_back_when_the_table_names_a_missing_model():
    write_config(models=["haiku", "sonnet", "opus"])
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


def test_judges_for_lists_the_one_judge():
    policy = Policy()
    cell = parse_cell("opus/high")
    assert policy.judges_for(cell, "none") == []
    assert policy.judges_for(cell, "full") == [choose_verifier(cell, "full", policy.cells)]


def test_the_router_never_chooses_max_effort_on_its_own():
    cells = lattice()
    assert all(c.effort != "max" for c in cells)
    for start in ("haiku/low", "sonnet/medium", "opus/high", "opus/max"):
        tuning = {"start": {"write": {"high": {"cell": start}}}}
        plan = Policy(cells=cells, tuning=tuning).plan(Task(prompt="x", kind="write", risk="high"))
        assert all(c.effort != "max" for c in plan.ladder)
    tuned = Policy(cells=cells, tuning={"start": {"write": {"high": {"cell": "fable/max"}}}})
    plan = tuned.plan(Task(prompt="x", kind="write", risk="high"))
    assert plan.start.label == "fable/xhigh" and any("never chooses max" in r for r in plan.rationale)  # capped
    assert parse_cell("opus/max").effort == "max"  # named by hand, it is still a cell
