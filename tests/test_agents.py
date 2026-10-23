"""Advice on agents fixed by hand: repeats found by session, time and prompt, and the four answers."""

import json
from datetime import UTC, datetime, timedelta

from model_router import agents
from model_router.catalog import lattice
from model_router.cli import main
from model_router.projects import signature

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
REPO = "/repo"


def _run(i: int, agent: str = "curator", *, model: str = "opus", effort: str = "xhigh", session: str = "s",
         prompt: str | None = None, minutes: int = 0, seconds: int = 60, routed: str | None = None) -> dict:
    return {"agent_id": f"{agent}-{i}", "agent_type": agent, "session": f"{session}{i}", "project": REPO,
            "model": model, "effort": effort, "routed": routed, "at": (T0 + timedelta(minutes=minutes)).isoformat(),
            "seconds": seconds, "turns": 10, "cost_usd": 1.0, "input_tokens": 1000, "output_tokens": 2000,
            "cache_read_tokens": 100_000, "cache_write_tokens": 5000,
            "sig": signature(prompt or f"curate migration batch {i} of the client tables with all the checks")}


def test_a_run_is_repeated_when_another_starts_soon_after_on_an_alike_prompt_in_its_session():
    brief = "curate the migration of the equipment tables, steps four eight and ten, with every check"
    first = {**_run(1, prompt=brief), "session": "s"}
    again = {**_run(2, agent="general-purpose", model="fable", effort="low", prompt=brief, minutes=10, seconds=300),
             "session": "s"}
    other_session = {**_run(3, prompt=brief, minutes=10, seconds=300), "session": "t"}
    too_late = {**_run(4, prompt=brief, minutes=90), "session": "s"}
    unlike = {**_run(5, prompt="write the weekly report for the data team", minutes=5), "session": "s"}
    assert agents.repeated_ids([first, again, other_session, unlike]) == {"curator-1"}
    assert agents.repeated_ids([first, too_late]) == set()


def test_the_same_agent_looping_over_files_with_one_template_is_not_repeated():
    template = "review the file {} against the rules of the repository and report every finding with its line"
    loop = [{**_run(i, prompt=template.format(f"src/module_{i}.py"), minutes=3 * i), "session": "s"} for i in range(6)]
    assert agents.repeated_ids(loop) == set()
    assert agents.advise(loop, REPO, cells=lattice())[0].action == "keep"  # not "raise to fable"


def test_fewer_than_six_runs_are_only_measured():
    assert agents.advise([_run(i) for i in range(3)], REPO, cells=lattice())[0].action == "measuring"


def test_ten_runs_none_repeated_advise_a_cheaper_cell_of_the_same_model_without_pricing_it():
    advice = agents.advise([_run(i) for i in range(10)], REPO, cells=lattice())[0]
    assert advice.action == "lower" and advice.to is not None and advice.to.model == advice.cell.model
    assert advice.saving is None  # an effort change is not priced


def test_repeated_runs_advise_one_step_up_and_price_the_new_model():
    brief = "curate migration batch of the client tables with all the checks and the totals"
    flaky = []
    for i in range(6):
        flaky.append({**_run(i, prompt=brief, minutes=i * 60), "session": "s"})
        if i < 2:
            flaky.append({**_run(100 + i, agent="general-purpose", model="fable", effort="low", prompt=brief,
                                 minutes=i * 60 + 5), "session": "s"})
    advice = agents.advise(flaky, REPO, cells=lattice())[0]
    assert advice.action == "raise" and advice.repeated == 2 and advice.to is not None
    assert advice.to.model == "claude-fable-5-1" and advice.saving is not None and advice.saving < 0  # costs more


def test_between_measuring_and_lowering_the_advice_is_to_keep():
    assert agents.advise([_run(i) for i in range(8)], REPO, cells=lattice())[0].action == "keep"


def test_only_agents_fixed_by_hand_in_this_repository_are_advised():
    records = [_run(1), _run(2, agent="general-purpose"), _run(3, agent="workflow-subagent"),
               _run(4, agent="model-router:reader"), _run(5, agent="Explore"), _run(6, routed="haiku/low"),
               {**_run(7), "project": "/other"}, {**_run(8), "model": "other-model"}]
    assert [(a.agent, a.runs) for a in agents.advise(records, REPO, cells=lattice())] == [("curator", 1)]


def test_agents_that_inherit_the_model_or_are_built_in_get_no_advice(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "helper.md").write_text("---\nname: helper\nmodel: inherit\n---\nhelp")
    (repo / ".claude" / "agents" / "plain.md").write_text("---\nname: plain\n---\nno model")
    (repo / ".claude" / "agents" / "fixed.md").write_text("---\nname: fixed\nmodel: sonnet\n---\nfixed")
    records = [{**_run(i, agent=name), "project": str(repo)}
               for i, name in enumerate(["helper", "plain", "fixed", "claude-code-guide", "statusline-setup", "plugin:x"])]
    assert sorted(a.agent for a in agents.advise(records, str(repo), cells=lattice())) == ["fixed", "plugin:x"]


def test_the_frontmatter_reader_drops_quotes_and_trailing_comments():
    from model_router.agents import frontmatter

    assert frontmatter('---\nname: x\nmodel: "opus"  # QA wants depth\neffort: \'high\'\n---\nbody') == {
        "model": "opus", "effort": "high"}
    assert frontmatter("---\nmodel:\n---\n") == {"model": ""} and frontmatter("no frontmatter") == {}


def test_router_agents_prints_the_advice_and_where_the_agent_lives(tmp_path, monkeypatch, capsys):
    home = tmp_path / "profile" / "model-router"
    home.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.parent))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "curator.md").write_text("---\nname: curator\nmodel: opus\neffort: xhigh\n---\n")
    lines = [{**_run(i), "project": str(repo), "final": True} for i in range(10)]
    (home / "subagents.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
    assert main(["agents", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "curator" in out and "opus/xhigh" in out and "lower to opus/" in out and "curator.md" in out
    assert main(["agents", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "lower"
