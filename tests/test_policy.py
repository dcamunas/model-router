import pytest

from model_router.catalog import lattice, parse_cell
from model_router.paths import write_config
from model_router.policy import Policy, choose_verifier, default_judge
from model_router.task import Task


def test_start_cell_follows_kind_and_risk():
    plan = Policy().plan(Task(prompt="x", kind="classify", risk="low"))
    assert plan.start.label == "haiku/low"
    plan = Policy().plan(Task(prompt="x", kind="code", risk="high"))
    assert plan.start.label == "opus/medium"
    plan = Policy().plan(Task(prompt="x", kind="code", risk="critical"))
    assert plan.start.label == "opus/high"


def test_the_cross_check_judge_is_the_fixed_judge_on_another_model_than_the_worker():
    cells = lattice()
    v = choose_verifier(parse_cell("sonnet/medium"), cells=cells)
    assert v is not None and v.label == "opus/xhigh"  # the default judge
    v = choose_verifier(parse_cell("fable/xhigh"), cells=cells)
    assert v is not None and v.label == "opus/xhigh"  # a judge does not have to outrank the worker
    v = choose_verifier(parse_cell("opus/low"), cells=cells)
    assert v is not None and v.label == "fable/low"  # worker on the judge's model: next model up, low effort
    v = choose_verifier(parse_cell("opus/low"), cells=cells, judge=parse_cell("fable/max"))
    assert v is not None and v.label == "fable/xhigh"  # an explicit judge is capped at xhigh


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
    v = choose_verifier(parse_cell("opus/medium"), cells=cells)
    assert v is not None and v.label == "sonnet/xhigh"  # nothing above opus here: the model below at the judge's effort
    write_config(models=["opus"])
    cells = lattice()
    assert choose_verifier(parse_cell("opus/medium"), cells=cells) == parse_cell("opus/xhigh")
    assert choose_verifier(parse_cell("opus/xhigh"), cells=cells) == parse_cell("opus/high")


def test_policy_falls_back_when_the_table_names_a_missing_model():
    write_config(models=["haiku", "sonnet", "opus"])
    policy = Policy()
    plan = policy.plan(Task(prompt="x", kind="analysis", risk="critical"))  # table says fable/low
    assert plan.start.label == "opus/medium"  # fable/low -> opus/low, then the critical floor lifts it
    assert any("fable not available: fable/low -> opus/low" in r for r in plan.rationale)
    assert any("risk floor critical" in r for r in plan.rationale)
    assert policy.verifier_for(plan.start).spec.short != "fable"
    plan = policy.plan(Task(prompt="x", kind="code", risk="critical"))  # floor opus/medium still applies
    assert plan.start.capability >= parse_cell("opus/medium").capability


def test_the_router_never_chooses_max_effort_on_its_own():
    cells = lattice()
    assert all(c.effort != "max" for c in cells)
    for start in ("haiku/low", "sonnet/medium", "opus/high", "opus/max"):
        tuning = {"start": {"write": {"high": {"cell": start}}}}
        plan = Policy(cells=cells, tuning=tuning).plan(Task(prompt="x", kind="write", risk="high"))
        assert plan.start.effort != "max"
    tuned = Policy(cells=cells, tuning={"start": {"write": {"high": {"cell": "fable/max"}}}})
    plan = tuned.plan(Task(prompt="x", kind="write", risk="high"))
    assert plan.start.label == "fable/xhigh" and any("never chooses max" in r for r in plan.rationale)  # capped
    assert parse_cell("opus/max").effort == "max"  # named by hand, it is still a cell


def test_the_minimum_effort_raises_a_start_on_its_own_model():
    plan = Policy(min_effort="medium").plan(Task(prompt="x", kind="search", risk="medium"))
    assert plan.start.label == "sonnet/medium"
    assert any("minimum effort medium: sonnet/low -> sonnet/medium" in r for r in plan.rationale)
    plan = Policy(min_effort="medium").plan(Task(prompt="x", kind="code", risk="high"))
    assert plan.start.label == "opus/medium"  # already at or above it: untouched
    assert not any("minimum effort" in r for r in plan.rationale)


def test_the_minimum_effort_comes_from_the_config_and_ignores_bad_values():
    assert Policy().plan(Task(prompt="x", kind="search", risk="medium")).start.label == "sonnet/low"
    write_config(min_effort="medium")
    assert Policy().plan(Task(prompt="x", kind="search", risk="medium")).start.label == "sonnet/medium"
    write_config(min_effort="max")  # never chosen on its own
    assert Policy().plan(Task(prompt="x", kind="search", risk="medium")).start.label == "sonnet/low"
