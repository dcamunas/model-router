import pytest

from model_router.catalog import MODELS, Cell, Usage, cost_usd, ladder, lattice, parse_cell, step_down, step_up


def test_prices_match_catalogue_rates():
    one_million_in = Usage(input_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5-5", one_million_in) == 2.00
    assert cost_usd("claude-opus-5-5", Usage(output_tokens=1_000_000)) == 20.00
    assert cost_usd("claude-fable-5-1", Usage(input_tokens=100_000, output_tokens=10_000)) == 1.0 + 0.5


def test_cache_tokens_are_priced_separately():
    usage = Usage(input_tokens=1000, cache_read_tokens=100_000, cache_write_tokens=10_000)
    cost = cost_usd("claude-opus-5-5", usage)
    assert abs(cost - (1000 * 4 + 100_000 * 0.20 + 10_000 * 5.00) / 1e6) < 1e-12


def test_haiku_long_prompts_bill_five_times():
    short = cost_usd("claude-haiku-5-5", Usage(input_tokens=90_000))
    long = cost_usd("claude-haiku-5-5", Usage(input_tokens=110_000))
    assert long / short > 5


def test_parse_cell_accepts_short_names_ids_and_the_default_effort():
    assert parse_cell("opus/medium") == Cell("claude-opus-5-5", "medium")
    assert parse_cell("sonnet").effort == MODELS["claude-sonnet-5-5"].default_effort
    assert parse_cell("claude-fable-5-1/low").claude_code() == {"model": "fable", "effort": "low"}


def test_lattice_is_sorted_by_expected_cost():
    cells = lattice()
    costs = [c.expected_cost() for c in cells]
    assert costs == sorted(costs)
    assert cells[0].label == "haiku/low" and cells[-1].label == "fable/xhigh"


def test_capability_tier_dominates_effort():
    assert parse_cell("fable/low").capability > parse_cell("opus/max").capability
    assert parse_cell("opus/low").capability > parse_cell("sonnet/max").capability


def test_escalation_prefers_frontier_low_effort_over_lower_tier_max_effort():
    cells = lattice()
    assert step_up(parse_cell("sonnet/medium"), cells) == parse_cell("opus/low")
    assert step_up(parse_cell("opus/low"), cells) == parse_cell("fable/low")
    assert step_up(parse_cell("fable/low"), cells) == parse_cell("fable/xhigh")
    assert step_up(parse_cell("fable/xhigh"), cells) is None


def test_ladder_stops_when_nothing_is_more_capable():
    steps = ladder(parse_cell("opus/high"), lattice(), steps=6)
    assert [c.label for c in steps] == ["opus/high", "fable/low", "fable/xhigh"]


def test_step_down_gives_up_little_capability_for_a_real_saving():
    cells = lattice()
    assert step_down(parse_cell("opus/medium"), cells) == parse_cell("opus/low")
    assert step_down(parse_cell("haiku/low"), cells) is None


def test_available_models_come_from_the_config_else_the_catalogue():
    from model_router.catalog import available_models, nearest_available, parse_models
    from model_router.paths import write_config

    assert available_models() == list(MODELS)
    write_config(models=["opus", "haiku"])
    assert available_models() == ["claude-haiku-5-5", "claude-opus-5-5"]  # catalogue order, not config order
    write_config(models="sonnet, claude-opus-5-5")
    assert available_models() == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert [c.label for c in lattice()][-1] == "opus/xhigh"
    write_config(models=["sonet"])
    assert available_models() == list(MODELS)  # a name it does not know is ignored, and the plan says so
    write_config(models=["sonet", "opus"])
    assert available_models() == ["claude-opus-5-5"]
    with pytest.raises(ValueError):
        parse_models("")
    # nearest: same effort on the best model below; the first one above when nothing is below
    assert nearest_available(parse_cell("fable/low"), ["haiku", "sonnet", "opus"]).label == "opus/low"
    assert nearest_available(parse_cell("haiku/medium"), ["sonnet", "opus"]).label == "sonnet/medium"
    assert nearest_available(parse_cell("opus/high"), ["opus"]).label == "opus/high"


def test_a_registered_model_can_be_named_and_priced_but_never_enters_the_router_s_lattice():
    from model_router.catalog import (
        MODELS,
        ModelSpec,
        available_models,
        lattice,
        nearest_available,
        nearest_effort,
        parse_cell,
        register,
        unregister,
        worker_cell,
    )
    from model_router.paths import write_config

    other = ModelSpec("vendor-model-1", "vendor", 0, 0.1, 0.5, 0.1, 0.1, "medium", False, provider="vendor",
                      efforts=("low", "medium", "high", "xhigh"))
    with pytest.raises(ValueError, match="Claude model's name"):
        register(ModelSpec("vendor-opus", "opus", 2, 1, 1, 1, 1, "medium", False, provider="vendor"))
    with pytest.raises(ValueError, match="Claude model's id"):
        register(ModelSpec("claude-opus-5-5", "vendor2", 2, 1, 1, 1, 1, "medium", False, provider="vendor"))
    register(other)
    try:
        claude = [m for m, s in MODELS.items() if s.claude]
        assert parse_cell("vendor/high").capability == 0.40 and parse_cell("vendor").effort == "medium"  # its tier's
        assert nearest_available(parse_cell("vendor/high"), ["opus"]) == parse_cell("vendor/high")  # its registrar serves it
        write_config(models=["haiku", "sonnet", "opus", "fable", "vendor", "nosuch"])  # an older version's list
        assert available_models() == claude and len(lattice()) == 16  # registered, listed, and still out
        assert worker_cell(parse_cell("haiku/low"), ["vendor"]).label == "vendor/low"  # asked for by name, it stands in
        assert nearest_effort("max", ("low", "high")) == "high" and nearest_effort("low", ("medium",)) == "medium"
        register(ModelSpec("vendor-model-1", "vendor-b", 1, 1, 1, 1, 1, "low", False, provider="vendor"))
        assert parse_cell("vendor-b/low").tier == 1  # the same id again: the first short name is gone
        with pytest.raises(ValueError):
            parse_cell("vendor/low")
    finally:
        unregister("vendor-model-1")
        unregister("claude-opus-5-5")  # a Claude model is left alone
    assert "vendor-model-1" not in MODELS and "claude-opus-5-5" in MODELS
