import pytest

from model_router.catalog import MODELS, SHORT_TO_ID, lattice, parse_cell
from model_router.feedback import FeedbackStore
from model_router.policy import LadderPolicy, Policy, choose_arbiter, choose_panel, choose_verifier, default_judge
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


def test_light_verifier_is_the_workers_own_cell_capped_at_xhigh():
    cells = lattice()
    light = choose_verifier(parse_cell("haiku/medium"), "light", cells)
    assert light is not None and light.label == "haiku/medium"
    light = choose_verifier(parse_cell("fable/max"), "light", cells)
    assert light is not None and light.label == "fable/xhigh"


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


def test_panel_pairs_the_workers_fresh_context_with_the_fixed_judge():
    cells = lattice()
    panel = choose_panel(parse_cell("opus/high"), cells)
    assert [c.label for c in panel] == ["opus/high", "fable/low"]  # cheapest first
    panel = choose_panel(parse_cell("sonnet/medium"), cells)
    assert [c.label for c in panel] == ["sonnet/medium", "opus/xhigh"]
    panel = choose_panel(parse_cell("fable/max"), cells)
    assert [c.label for c in panel] == ["opus/xhigh", "fable/xhigh"]  # nobody judges above xhigh
    assert len({c.model for c in panel}) == 2


def test_panel_with_one_model_uses_two_cells_and_degrades_to_one_at_the_top(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    cells = lattice()
    panel = choose_panel(parse_cell("opus/medium"), cells)
    assert [c.label for c in panel] == ["opus/medium", "opus/xhigh"]
    assert choose_panel(parse_cell("opus/max"), cells) == [parse_cell("opus/xhigh")]


def test_tribunal_is_automatic_at_critical_risk_for_judgement_kinds_without_checker():
    policy = Policy()
    assert policy.plan(Task(prompt="x", kind="analysis", risk="critical")).verify == "tribunal"
    assert policy.plan(Task(prompt="x", kind="write", risk="critical")).verify == "tribunal"
    assert policy.plan(Task(prompt="x", kind="code", risk="critical")).verify == "full"
    assert policy.plan(Task(prompt="x", kind="analysis", risk="high")).verify == "light"
    with_checker = Task(prompt="x", kind="analysis", risk="critical", checker=contains_checker("42"))
    assert policy.plan(with_checker).verify == "full"
    assert policy.plan(Task(prompt="x", kind="analysis", risk="critical", verify="full")).verify == "full"
    assert policy.plan(Task(prompt="x", kind="code", risk="low", verify="panel")).verify == "panel"
    assert Policy(panel_kinds=frozenset()).plan(Task(prompt="x", kind="analysis", risk="critical")).verify == "full"
    assert Policy(critical_mode="panel").plan(Task(prompt="x", kind="analysis", risk="critical")).verify == "panel"
    plan = policy.plan(Task(prompt="x", kind="review", risk="critical"))
    assert any(r.startswith("tribunal:") and "arbiter" in r for r in plan.rationale)
    with pytest.raises(ValueError):
        Policy(critical_mode="council")


def test_arbiter_is_the_next_model_up_from_the_judge_else_one_effort_up(monkeypatch):
    monkeypatch.delenv("MODEL_ROUTER_ARBITER", raising=False)
    cells = lattice()
    # judges sonnet/medium + opus/xhigh: the model above the judge, at low
    assert choose_arbiter(parse_cell("sonnet/medium"), cells) == parse_cell("fable/low")
    # judges opus/medium + fable/low: nothing above fable, so the judge one effort up
    assert choose_arbiter(parse_cell("opus/medium"), cells) == parse_cell("fable/medium")
    # judges fable/low + opus/xhigh: fable/low and opus/xhigh are taken, so the worker's model one effort up
    assert choose_arbiter(parse_cell("fable/low"), cells) == parse_cell("fable/medium")
    # a configured arbiter wins unless it is one of the judges
    assert choose_arbiter(parse_cell("opus/medium"), cells, arbiter=parse_cell("sonnet/max")) == parse_cell("sonnet/xhigh")
    assert choose_arbiter(parse_cell("opus/medium"), cells, arbiter=parse_cell("fable/low")) == parse_cell("fable/medium")
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus")
    cells = lattice()
    assert choose_arbiter(parse_cell("opus/medium"), cells) == parse_cell("opus/high")  # judges opus/medium + opus/xhigh
    assert choose_arbiter(parse_cell("opus/high"), cells) is None  # judges opus/high + opus/xhigh: nothing is left


def test_external_models_judge_and_arbitrate_but_never_route(monkeypatch, tmp_path):
    from model_router.catalog import available_models, external_models, load_external, nearest_available, parse_external

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    name, spec = parse_external("gpt=openai/gpt-5.5@5/30")
    assert name == "gpt" and spec is not None
    assert (spec.id, spec.provider, spec.input, spec.output) == ("gpt-5.5", "openai", 5.0, 30.0)
    assert parse_external("gpt=") == ("gpt", None)
    for bad in ("opus=openai/x@1/2", "gpt=mistral/x@1/2", "gpt=openai/x", "GPT=openai/x@1/2"):
        with pytest.raises(ValueError):
            parse_external(bad)
    try:
        assert load_external({"external": {"gpt": {"provider": "openai", "id": "gpt-5.5", "input": 5, "output": 30}}}) == ["gpt"]
        gpt = parse_cell("gpt/high")
        assert gpt.tier == 2 and gpt.capability == parse_cell("opus/high").capability and gpt.spec.provider == "openai"
        assert "gpt-5.5" not in available_models() and all(c.spec.provider == "anthropic" for c in lattice())
        assert nearest_available(gpt, ["claude-opus-5-5"]) == gpt
        assert [s.short for s in external_models()] == ["gpt"]
        policy = Policy(judge=gpt)
        worker = parse_cell("fable/low")
        assert choose_panel(worker, policy.cells, judge=gpt) == [gpt, worker]  # gpt/high judges for less than fable/low
        assert policy.arbiter_for(worker) == parse_cell("gpt/xhigh")  # not the worker's company, not a judge
        assert policy.attempt_cost(worker, "full", 2000, 1500) > worker.expected_cost(2000, 1500)
        assert load_external({"external": {"gpt": {"provider": "openai", "id": "gpt-6", "input": 1, "output": 2}}}) == ["gpt"]
        assert parse_cell("gpt").spec.id == "gpt-6" and "gpt-5.5" not in MODELS
    finally:
        load_external({})
    assert "gpt" not in SHORT_TO_ID


def test_arbiter_comes_from_the_environment_or_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_ARBITER", raising=False)
    assert Policy().arbiter is None
    (tmp_path / "config.json").write_text('{"arbiter": "fable/max"}')
    assert Policy().arbiter == parse_cell("fable/xhigh")
    monkeypatch.setenv("MODEL_ROUTER_ARBITER", "sonnet/high")
    assert Policy().arbiter == parse_cell("sonnet/high")
    assert LadderPolicy([parse_cell("opus/low")]).arbiter == parse_cell("sonnet/high")
    assert Policy().arbiter_for(parse_cell("opus/medium")) == parse_cell("sonnet/high")


def test_judges_for_lists_every_judge_and_the_panel_cost_counts_both():
    policy = Policy()
    cell = parse_cell("opus/high")
    assert policy.judges_for(cell, "none") == []
    assert policy.judges_for(cell, "full") == [choose_verifier(cell, "full", policy.cells)]
    assert policy.judges_for(cell, "panel") == choose_panel(cell, policy.cells)
    assert policy.attempt_cost(cell, "panel", 2000, 1500) > policy.attempt_cost(cell, "full", 2000, 1500)


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


def test_a_declared_model_works_only_when_listed_and_the_plan_says_so(monkeypatch, tmp_path):
    from model_router.catalog import load_external

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    load_external({"external": {"luna": {"provider": "codex", "id": "gpt-5.5", "input": 5, "output": 30}}})
    try:
        table = {kind: dict(row) for kind, row in __import__("model_router.policy", fromlist=["START"]).START.items()}
        table["write"]["medium"] = "luna/medium"
        monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
        plan = Policy(start_table=table, calibration=None).plan(Task(prompt="x", kind="write", risk="medium"))
        assert plan.start.label == "opus/medium"
        assert any("luna not listed to work (router config --models): luna/medium -> opus/medium" in r for r in plan.rationale)
        monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus,fable,luna")
        policy = Policy(start_table=table, calibration=None)
        plan = policy.plan(Task(prompt="x", kind="write", risk="medium"))
        assert plan.start.label == "luna/medium" and plan.ladder[0].label == "luna/medium"
        full = policy.verifier_for(plan.start, "full")
        assert full is not None and full.label == "opus/xhigh"  # another model judges the ChatGPT worker
        assert set(policy.judges_for(plan.start, "tribunal")) == {parse_cell("luna/medium"), parse_cell("opus/xhigh")}
        assert policy.arbiter_for(plan.start) == parse_cell("fable/low")  # not luna/high: the worker's own company
    finally:
        load_external({})
