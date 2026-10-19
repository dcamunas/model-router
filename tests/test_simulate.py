from model_router.catalog import parse_cell
from model_router.simulate import default_strategies, format_report, make_tasks, pass_probability, run_simulation


def test_simulation_is_deterministic_and_covers_all_strategies():
    tasks_a, reports_a = run_simulation(30, seed=7)
    tasks_b, reports_b = run_simulation(30, seed=7)
    assert tasks_a == tasks_b
    key = lambda rs: [(r.name, r.solved, round(r.total_cost, 9)) for r in rs]  # noqa: E731
    assert key(reports_a) == key(reports_b)
    assert [r.name for r in reports_a] == list(default_strategies())
    text = format_report(tasks_a, reports_a, 7)
    assert "router" in text and "$/solved" in text


def test_frontier_solves_at_least_as_much_as_sonnet_and_costs_more():
    _, reports = run_simulation(80, seed=3)
    by = {r.name: r for r in reports}
    assert by["fable/xhigh"].solved >= by["sonnet/high"].solved
    assert by["fable/xhigh"].total_cost > by["sonnet/high"].total_cost
    assert by["router"].retries > 0
    assert by["router"].verify_cost > 0


def test_pass_probability_moves_with_capability_and_difficulty():
    easy, hard = 0.2, 0.9
    assert pass_probability(parse_cell("haiku/low"), easy) > 0.8
    assert pass_probability(parse_cell("haiku/low"), hard) < 0.05
    assert pass_probability(parse_cell("fable/xhigh"), hard) > pass_probability(parse_cell("opus/high"), hard)


def test_tasks_have_a_hard_tail():
    tasks = make_tasks(200, seed=11)
    assert sum(t.difficulty > 0.8 for t in tasks) > 5
    assert {t.kind for t in tasks} >= {"classify", "code", "agentic"}
