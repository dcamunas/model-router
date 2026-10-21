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
    assert cells[0].label == "haiku/low" and cells[-1].label == "fable/max"


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
    steps = ladder(parse_cell("opus/high"), lattice(), 6)
    assert [c.label for c in steps] == ["opus/high", "fable/low", "fable/xhigh"]


def test_step_down_gives_up_little_capability_for_a_real_saving():
    cells = lattice()
    assert step_down(parse_cell("opus/medium"), cells) == parse_cell("opus/low")
    assert step_down(parse_cell("haiku/low"), cells) is None


def test_available_models_env_then_config_then_catalogue(tmp_path, monkeypatch):
    from model_router.catalog import MODELS_ENV, available_models, nearest_available, parse_models
    from model_router.paths import write_config

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv(MODELS_ENV, raising=False)
    assert available_models() == list(MODELS)
    write_config(models=["opus", "haiku"])
    assert available_models() == ["claude-haiku-5-5", "claude-opus-5-5"]  # catalogue order, not config order
    monkeypatch.setenv(MODELS_ENV, "sonnet, claude-opus-5-5")
    assert available_models() == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert [c.label for c in lattice()][-1] == "opus/max"
    monkeypatch.setenv(MODELS_ENV, "sonet")
    with pytest.raises(ValueError):
        available_models()
    with pytest.raises(ValueError):
        parse_models("")
    # nearest: same effort on the best model below; the first one above when nothing is below
    assert nearest_available(parse_cell("fable/low"), ["haiku", "sonnet", "opus"]).label == "opus/low"
    assert nearest_available(parse_cell("haiku/medium"), ["sonnet", "opus"]).label == "sonnet/medium"
    assert nearest_available(parse_cell("opus/high"), ["opus"]).label == "opus/high"


def test_declared_models_get_their_doors_efforts_and_family_and_work_only_when_listed(monkeypatch, tmp_path):
    from model_router.catalog import (
        MODELS,
        available_models,
        external_models,
        lattice,
        load_external,
        nearest_available,
        nearest_effort,
        parse_external,
        worker_cell,
    )

    name, luna = parse_external("luna=codex/gpt-5.5@5/30")
    assert name == "luna" and luna is not None and (luna.provider, luna.family) == ("codex", "openai")
    assert luna.efforts == ("low", "medium", "high", "xhigh") and luna.default_effort == "medium"
    assert luna.tier == 2  # Opus-class until the declaration says otherwise
    _, weak = parse_external("weak=codex/gpt-6-luna@2.5/12.5/0")
    assert weak is not None and weak.tier == 0
    with pytest.raises(ValueError, match="one declaration per model id"):
        parse_external("x=codex/claude-opus-5-5@1/1")  # a Claude id through another door would replace Opus
    with pytest.raises(ValueError, match=r"\[/TIER\]"):
        parse_external("weak=codex/gpt-6-luna@2.5/12.5/4")
    _, gem = parse_external("gem=gemini/gemini-3-pro@2/12")
    assert gem is not None and (gem.family, gem.efforts, gem.default_effort) == ("google", ("medium",), "medium")
    _, grok = parse_external("grok=xai/grok-4.7@2/6")
    assert grok is not None and grok.default_effort == "low"  # xAI knows low and high only
    with pytest.raises(ValueError, match="known: openai, codex, deepseek, xai, gemini"):
        parse_external("x=mistral/m@1/1")
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    load_external({"external": {"luna": {"provider": "codex", "id": "gpt-5.5", "input": 5, "output": 30},
                                "gem": {"provider": "gemini", "id": "gemini-3-pro", "input": 2, "output": 12}}})
    try:
        anthropic = [m for m, s in MODELS.items() if s.provider == "anthropic"]
        assert available_models() == anthropic and len(lattice()) == 20  # declared, not listed: judges only
        assert [s.short for s in external_models()] == ["luna", "gem"]
        with pytest.raises(ValueError, match="one declaration per model id"):
            parse_external("gpt=openai/gpt-5.5@5/30")  # gpt-5.5 is already `luna`: the catalogue is keyed by id
        assert parse_external("luna=codex/gpt-5.5@6/30")[1] is not None  # the same name may be declared again
        twice = {"luna": {"provider": "codex", "id": "gpt-5.5", "input": 5, "output": 30},
                 "gpt": {"provider": "openai", "id": "gpt-5.5", "input": 5, "output": 30}}
        assert load_external({"external": twice}) == ["luna"]  # the second declaration of an id is skipped
        load_external({"external": {"luna": twice["luna"],
                                    "gem": {"provider": "gemini", "id": "gemini-3-pro", "input": 2, "output": 12}}})
        cell = parse_cell("luna/high")
        assert nearest_available(cell, anthropic) == cell  # a judge is always available
        assert worker_cell(cell, anthropic).label == "opus/high"  # a worker is not: the Claude of its tier takes over
        assert worker_cell(parse_cell("gem"), ["haiku", "sonnet"]).label == "sonnet/medium"
        assert worker_cell(parse_cell("opus/xhigh"), ["gem", "haiku"]).label == "gem/medium"  # gem's only effort
        assert worker_cell(parse_cell("opus/xhigh"), ["gem", "opus"]).label == "opus/xhigh"  # Claude first at a tier
        assert worker_cell(parse_cell("opus/xhigh"), []) == parse_cell("opus/xhigh")  # nothing to choose from
        assert nearest_effort("max", ("low", "high")) == "high" and nearest_effort("low", ("medium",)) == "medium"
        monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus,fable,luna,gem")
        assert available_models()[-2:] == ["gpt-5.5", "gemini-3-pro"]
        cells = lattice()
        assert len(cells) == 25 and sum(c.spec.short == "luna" for c in cells) == 4
        assert sum(c.spec.short == "gem" for c in cells) == 1
        assert worker_cell(cell).label == "luna/high"
    finally:
        load_external({})


def test_a_declared_tier_places_the_model_in_the_ladder(monkeypatch, tmp_path):
    from model_router.catalog import load_external, parse_cell, worker_cell

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_MODELS", raising=False)
    declared = {"weak": {"provider": "codex", "id": "gpt-6-luna", "input": 2.5, "output": 12.5, "tier": 0},
                "bad": {"provider": "codex", "id": "x", "input": 1, "output": 2, "tier": 9}}
    assert load_external({"external": declared}) == ["weak"]  # a tier outside 0..3 does not load
    try:
        assert parse_cell("weak/medium").capability == 0.37  # Haiku-class, not Opus-class
        assert worker_cell(parse_cell("haiku/low"), ["weak"]).label == "weak/low"  # it stands in for Haiku, not Opus
        assert worker_cell(parse_cell("opus/high"), ["weak"]).label == "weak/high"  # the only model there is
    finally:
        load_external({})
