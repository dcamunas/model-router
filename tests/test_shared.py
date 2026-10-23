import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from model_router import projects, shared
from model_router.catalog import parse_cell
from model_router.hook import decide, handle, start_in_force
from model_router.paths import write_config
from model_router.policy import Policy
from model_router.task import Task

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)  # a Thursday
SETTLED = NOW - timedelta(days=14)  # a week that closed more than a day ago
WEEK = projects.week_of(SETTLED)


def STATE(tmp_path: Path) -> Path:  # noqa: N802 - reads as the folder it names
    return tmp_path / "home" / "model-router"


@pytest.fixture(autouse=True)
def _frozen(freeze_clock):
    freeze_clock(NOW)


def _repo(tmp_path, monkeypatch, *, share=True):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("MODEL_ROUTER_SHARE", raising=False)
    (STATE(tmp_path)).mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    if share:
        shared.shared_dir(str(repo)).mkdir(parents=True)
    return str(repo.resolve())


def _rec(project, cell, i, at, *, kind="analysis", risk="medium", chose=True):
    return {"project": project, "routed": cell, "kind": kind, "risk": risk, "agent_id": f"a{i}",
            "router_chose": chose, "at": at.isoformat(), "sig": [1, 2], "prompt_hash": "x"}


def _mine(project):
    return shared.own_id(project, projects.install_key()[0])


def _used(project, *ids):
    for install_id in ids:
        shared.remember(project, install_id)


def _theirs(project, install, week, counts):
    path = shared.shared_dir(project) / f"{install}-{week.replace('-', '')}.json"
    path.write_text(json.dumps({"v": 1, "c": counts}))
    return path


def test_each_settled_week_is_one_small_file_written_once(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    records = [_rec(project, "sonnet/low", i, SETTLED) for i in range(3)]
    records += [_rec(project, "sonnet/low", 9, NOW)]  # this week is still open
    records += [_rec(project, "sonnet/low", 8, SETTLED, chose=False)]  # Claude picked the effort: no evidence
    records += [_rec("/elsewhere", "sonnet/low", 7, SETTLED)]
    records += [_rec(project, "sonnet/low", 6, NOW - timedelta(weeks=20))]  # beyond the window
    log = STATE(tmp_path) / "hook.jsonl"
    log.write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    [path] = shared.write_own(project, records, decision_log=log)
    assert path.name == f"{_mine(project)}-{WEEK.replace('-', '')}.json"
    text = path.read_text()
    assert json.loads(text) == {"v": 1, "c": {"analysis|medium|sonnet/low": [3, 1]}}
    assert project not in text and len(text) < 60
    records += [_rec(project, "sonnet/low", 5, SETTLED)]
    assert shared.write_own(project, records, decision_log=log) == [] and path.read_text() == text  # never changed
    assert shared.own_id(project, b"k" * 32) != shared.own_id(project + "2", b"k" * 32)


def test_a_week_is_written_a_day_after_it_closes_and_deleted_when_it_leaves_the_window(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    monday = projects.week_start(projects.week_of(NOW))
    assert monday is not None
    sunday_night = monday - timedelta(minutes=10)
    records = [_rec(project, "sonnet/low", 1, sunday_night)]
    assert shared.write_own(project, records, decision_log=tmp_path / "none", now=monday + timedelta(hours=1)) == []
    assert len(shared.write_own(project, records, decision_log=tmp_path / "none", now=monday + timedelta(days=1, hours=1))) == 1
    old = _theirs(project, _mine(project), projects.week_of(NOW - timedelta(weeks=20)), {})
    gone = _theirs(project, "abcdefabcdef", WEEK, {"analysis|medium|sonnet/low": [5, 5]})
    _used(project, "abcdefabcdef")  # an id this install used before for this path
    shared.prune(project)
    shared.write_own(project, records, decision_log=tmp_path / "none")
    assert not old.exists() and not gone.exists()


def test_nothing_is_shared_unless_the_repository_and_this_installation_take_part(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, share=False)
    records = [_rec(project, "sonnet/low", 1, SETTLED)]
    assert shared.write_own(project, records, decision_log=tmp_path / "none") == []
    assert not (tmp_path / "repo" / ".claude").exists() and shared.pooled(project) == ({}, 0)
    shared.shared_dir(project).mkdir(parents=True)
    _theirs(project, "aaaaaaaaaaaa", WEEK, {"analysis|medium|sonnet/low": [6, 6]})
    monkeypatch.setenv("MODEL_ROUTER_SHARE", "0")
    assert shared.write_own(project, records, decision_log=tmp_path / "none") == [] and shared.pooled(project) == ({}, 0)


def test_a_salt_that_cannot_be_kept_shares_nothing(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "repo" / ".git" / "nope"))
    (tmp_path / "repo" / ".git" / "nope").write_text("a file where a directory should be")
    assert projects.install_key()[1] is False
    assert shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], decision_log=tmp_path / "none") == []


def test_pooled_counts_skip_this_install_old_weeks_bad_data_and_cap_each_installation(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    _theirs(project, "aaaaaaaaaaaa", WEEK, {
        "analysis|medium|sonnet/low": [4, 1], "analysis|medium|nope/low": [9, 9], "bad|medium|sonnet/low": [9, 9],
        "analysis|medium|haiku/low": [1, 5], "review|high|opus/medium": [True, 0]})
    _theirs(project, "aaaaaaaaaaaa", projects.week_of(NOW - timedelta(weeks=20)), {"analysis|medium|sonnet/low": [50, 50]})
    _theirs(project, "bbbbbbbbbbbb", WEEK, {"analysis|medium|sonnet/low": [2, 1]})
    _theirs(project, _mine(project), WEEK, {"analysis|medium|sonnet/low": [100, 100]})  # mine: counted locally
    _used(project, "cccccccccccc")
    _theirs(project, "cccccccccccc", WEEK, {"analysis|medium|sonnet/low": [7, 7]})  # mine too, under an older id
    _theirs(project, "dddddddddddd", WEEK, {}).write_text("not json")
    _theirs(project, "eeeeeeeeeeee", WEEK, {}).write_text(json.dumps({"v": 2, "c": {"analysis|medium|sonnet/low": [9, 9]}}))
    (shared.shared_dir(project) / "notes.json").write_text(json.dumps({"v": 1, "c": {"analysis|medium|sonnet/low": [9, 9]}}))
    ev, installs = shared.pooled(project)
    assert ev == {("analysis", "medium", "sonnet/low"): {"n": 6, "fails": 2}} and installs == 2
    assert shared.pooled(project, since=NOW.isoformat()) == ({}, 0)  # a reset: earlier weeks were weighed
    heavy = projects.week_of(NOW - timedelta(days=21))
    _theirs(project, "ffffffffffff", WEEK, {"search|low|haiku/low": [100, 10]})
    _theirs(project, "ffffffffffff", heavy, {"search|low|haiku/low": [100, 10]})
    ev, _ = shared.pooled(project)
    assert ev[("search", "low", "haiku/low")] == {"n": shared.FILE_CAP, "fails": 3}  # capped, its rate kept


def test_the_team_counts_alone_only_propose_a_raise_and_one_failure_here_makes_it_happen(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    cell = start_in_force({})("analysis", "medium").label
    _theirs(project, "aaaaaaaaaaaa", WEEK, {f"analysis|medium|{cell}": [6, 6]})
    home = STATE(tmp_path)
    event = {"hook_event_name": "SessionStart", "session_id": "s", "cwd": project}
    out = handle(event)
    assert out is not None and "systemMessage" not in out and projects.load_tuning(project).get("start", {}) == {}
    (home / "subagents.jsonl").write_text(json.dumps(_rec(project, cell, 1, SETTLED)) + "\n")
    (home / "hook.jsonl").write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    out = handle(event)
    assert out is not None and "now starts on" in out["systemMessage"]
    assert projects.load_tuning(project)["start"]["analysis"]["medium"]["base"] == cell
    own = shared.shared_dir(project) / f"{_mine(project)}-{WEEK.replace('-', '')}.json"
    assert json.loads(own.read_text())["c"] == {f"analysis|medium|{cell}": [1, 1]}


def test_router_project_share_turns_it_on_and_applies_a_team_raise_by_hand(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch, share=False)
    assert main(["project", project, "--share"]) == 0
    assert (shared.shared_dir(project) / ".gitkeep").exists() and "commit it" in capsys.readouterr().out
    cell = start_in_force({})("search", "low").label
    _theirs(project, "aaaaaaaaaaaa", WEEK, {f"search|low|{cell}": [6, 3]})
    assert main(["project", project]) == 0
    out = capsys.readouterr().out
    assert "1 other installation(s)" in out and "n from others" in out and f"raised search (low) from {cell} to" in out
    assert main(["project", project, "--apply"]) == 0
    assert projects.load_tuning(project)["start"]["search"]["low"]["base"] == cell


def test_a_tuned_cell_never_runs_below_the_start_it_replaced():
    write_config(models=["haiku", "sonnet", "opus"])
    plain = Policy().plan(Task(prompt="x", kind="review", risk="critical")).start
    tuning = {"name": "repo", "start": {"review": {"critical": {"cell": "fable/low", "base": plain.label}}}}
    plan = Policy(tuning=tuning).plan(Task(prompt="x", kind="review", risk="critical"))
    assert plan.start.capability >= plain.capability
    assert any("ignored" in line for line in plan.rationale)


def test_a_cross_checked_review_is_no_evidence_about_its_start(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    _, record = decide({"subagent_type": "general-purpose", "description": "review",
                        "prompt": "kind: review, risk: critical\nReview the payment code"},
                       chat_model=parse_cell(start_in_force({})("review", "critical").label).claude_code()["model"])
    assert record.get("cross") and record["router_chose"] is False


def test_another_clone_of_the_same_repository_is_a_teammate_and_old_weeks_go_whoever_wrote_them(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    _used(project + "-worktree", "121212121212")  # this machine wrote it from a worktree: other path, other log
    worktree = _theirs(project, "121212121212", WEEK, {"analysis|medium|sonnet/low": [2, 1]})
    stale = _theirs(project, "343434343434", projects.week_of(NOW - timedelta(weeks=20)), {})  # someone who left
    shared.prune(project)
    shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], decision_log=tmp_path / "none")
    assert worktree.exists() and not stale.exists()
    assert shared.pooled(project)[0] == {("analysis", "medium", "sonnet/low"): {"n": 2, "fails": 1}}


def test_links_in_the_shared_directory_are_never_followed(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    folder = shared.shared_dir(project)
    victim = tmp_path / "bashrc"
    victim.write_text("mine")
    target = folder / f"{_mine(project)}-{WEEK.replace('-', '')}.json"
    target.symlink_to(victim)  # a teammate commits a link under my next file name
    (folder / f"aaaaaaaaaaaa-{WEEK.replace('-', '')}.json").symlink_to("/dev/zero")  # endless if read
    big = _theirs(project, "bbbbbbbbbbbb", WEEK, {})
    big.write_text(" " * (shared.MAX_FILE_BYTES + 10) + "{}")
    shared.prune(project)
    shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], decision_log=tmp_path / "none")
    assert victim.read_text() == "mine"
    assert shared.pooled(project) == ({}, 0)


def test_a_shared_directory_that_links_elsewhere_is_not_used(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, share=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path(project) / ".claude").mkdir()
    shared.shared_dir(project).symlink_to(elsewhere)
    assert not shared.sharing(project)
    assert shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], decision_log=tmp_path / "none") == []
    assert list(elsewhere.iterdir()) == []


def test_an_applied_cheaper_start_is_in_force(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "model-router").mkdir(exist_ok=True)
    table = start_in_force({})("search", "medium")
    cheaper = projects.cheaper_below(table, projects.lattice())
    assert cheaper is not None
    tuning = {"start": {"search": {"medium": {"cell": cheaper.label, "base": table.label}}}}
    assert start_in_force(tuning)("search", "medium") == cheaper
    critical = start_in_force({})("review", "critical")
    below = {"start": {"review": {"critical": {"cell": "haiku/low", "base": critical.label}}}}
    assert start_in_force(below)("review", "critical") == critical  # never below a floor


def test_an_undeletable_entry_never_stops_sharing_nor_hides_a_raise(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    (shared.shared_dir(project) / "343434343434-2020W01.json").mkdir()  # a directory with a file's name
    cell = start_in_force({})("analysis", "medium").label
    _theirs(project, "aaaaaaaaaaaa", WEEK, {f"analysis|medium|{cell}": [5, 2]})
    home = STATE(tmp_path)
    (home / "subagents.jsonl").write_text(json.dumps(_rec(project, cell, 1, SETTLED)) + "\n")
    (home / "hook.jsonl").write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    out = handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": project})
    assert out is not None and "now starts on" in out["systemMessage"]
    assert (shared.shared_dir(project) / f"{_mine(project)}-{WEEK.replace('-', '')}.json").exists()


def test_a_week_just_out_of_the_window_is_kept_a_little_longer(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    edge = projects.week_of(projects.window_start() - timedelta(days=3))
    kept = _theirs(project, "aaaaaaaaaaaa", edge, {"analysis|medium|sonnet/low": [1, 0]})
    shared.prune(project)
    shared.write_own(project, [], decision_log=tmp_path / "none")
    assert kept.exists() and shared.pooled(project) == ({}, 0)  # not counted, but not deleted yet


def test_share_refuses_a_claude_directory_that_is_a_link(tmp_path, monkeypatch):
    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch, share=False)
    (tmp_path / "tools").mkdir()
    (Path(project) / ".claude").symlink_to(tmp_path / "tools")
    with pytest.raises(SystemExit, match="through a link"):
        main(["project", project, "--share"])
    assert list((tmp_path / "tools").iterdir()) == []


def test_a_raise_whose_model_is_gone_keeps_a_stronger_stand_in():
    plain = Policy().plan(Task(prompt="x", kind="analysis", risk="medium")).start
    raised = projects.step_up(plain, projects.lattice())
    assert raised is not None
    tuning = {"name": "repo", "start": {"analysis": {"medium": {"cell": raised.label, "base": plain.label}}}}
    others = [m for m in ("haiku", "sonnet", "opus", "fable") if m != raised.spec.short]
    write_config(models=others)
    start = Policy(tuning=tuning).plan(Task(prompt="x", kind="analysis", risk="medium")).start
    assert start.capability >= plain.capability


def test_a_time_without_a_zone_is_read_as_utc():
    from model_router import clock

    assert clock.parse_moment("2026-10-22T12:00:00") == NOW
    assert clock.parse_moment("2026-10-22T12:00:00Z") == NOW and clock.parse_moment("not a time") is None


def test_shared_counts_are_written_even_when_tuning_fails(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    (STATE(tmp_path) / "subagents.jsonl").write_text(json.dumps(_rec(project, "sonnet/low", 1, SETTLED)) + "\n")

    def broken(tuning):
        raise RuntimeError("tuning broke")

    monkeypatch.setattr("model_router.hook.start_in_force", broken)
    out = handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": project})
    assert out is not None and "systemMessage" not in out
    assert (shared.shared_dir(project) / f"{_mine(project)}-{WEEK.replace('-', '')}.json").exists()


def test_share_says_what_to_do_when_this_installation_is_kept_out(tmp_path, monkeypatch):
    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch, share=False)
    monkeypatch.setenv("MODEL_ROUTER_SHARE", "0")
    with pytest.raises(SystemExit, match="Unset it and run"):
        main(["project", project, "--share"])


def _agent_run(project, agent, i, at, *, cell=("opus", "xhigh"), session="s", sig=None, seconds=60):
    return {"project": project, "agent_type": agent, "agent_id": f"{agent}-{i}", "session": f"{session}{i}",
            "model": cell[0], "effort": cell[1], "routed": None, "at": at.isoformat(), "seconds": seconds,
            "sig": sig or [i, i + 1, i + 2], "turns": 5, "cost_usd": 1.0}


def _agent_file(project, name, model="opus", *, commit=True):
    """Write an agent file into the repository and, unless `commit` is False, add it to git's index."""
    import subprocess

    folder = Path(project) / ".claude" / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.md").write_text(f"---\nname: {name}\nmodel: {model}\neffort: xhigh\n---\n")
    if not (Path(project) / ".git" / "HEAD").exists():
        subprocess.run(["git", "init", "-q", project], check=True)
    if commit:
        subprocess.run(["git", "-C", project, "add", f".claude/agents/{name}.md"], check=True)


def test_the_week_file_carries_the_repository_s_own_agents_and_nothing_from_elsewhere(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    _agent_file(project, "curator")
    _agent_file(project, "helper", model="inherit")
    records = [_agent_run(project, "curator", i, SETTLED) for i in range(3)]
    records += [_agent_run(project, "helper", 9, SETTLED)]  # inherits the session's model: not fixed by hand
    records += [_agent_run(project, "my-profile-agent", 8, SETTLED)]  # defined outside the repository
    _agent_file(project, "untracked", commit=False)  # on disk, never added: a private agent
    records += [_agent_run(project, "untracked", 10, SETTLED)]
    profile_agent = tmp_path / "profile-agents" / "client-secret.md"
    profile_agent.parent.mkdir()
    profile_agent.write_text("---\nmodel: opus\neffort: xhigh\n---\n")
    (Path(project) / ".claude" / "agents" / "client-secret.md").symlink_to(profile_agent)
    import subprocess
    subprocess.run(["git", "-C", project, "add", ".claude/agents/client-secret.md"], check=True)  # a link, even committed
    records += [_agent_run(project, "client-secret", 11, SETTLED)]
    records += [_agent_run(project, "curator", 7, NOW)]  # this week is still open
    [path] = shared.write_own(project, records, decision_log=STATE(tmp_path) / "hook.jsonl")
    assert json.loads(path.read_text()) == {"v": 1, "c": {}, "a": {"curator|opus/xhigh": [3, 0]}}
    text = path.read_text()
    assert not any(name in text for name in ("my-profile-agent", "untracked", "client-secret", project))


def test_teammates_agent_runs_are_pooled_capped_and_checked(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    def theirs(install, week, runs):
        path = shared.shared_dir(project) / f"{install}-{week.replace('-', '')}.json"
        path.write_text(json.dumps({"v": 1, "c": {}, "a": runs}))
    theirs("aaaaaaaaaaaa", WEEK, {"curator|opus/xhigh": [4, 1], "bad name|opus/xhigh": [9, 9],
                                  "curator|nope/low": [9, 9], "curator|opus/high": [1, 5]})
    theirs("bbbbbbbbbbbb", WEEK, {"curator|opus/xhigh": [100, 10]})
    theirs(_mine(project), WEEK, {"curator|opus/xhigh": [50, 50]})  # mine: counted from the local log
    runs, installs = shared.pooled_agents(project, keep=lambda agent, label: True)
    assert runs == {("curator", "opus/xhigh"): {"n": 4 + shared.FILE_CAP, "fails": 1 + 3}} and installs == 2
    theirs("cccccccccccc", WEEK, {"gone|opus/high": [5, 0], "curator|opus/xhigh": [0, 0]})
    _, installs = shared.pooled_agents(project, keep=lambda agent, label: agent == "curator")
    assert installs == 2  # a file with only a gone agent or an empty entry is no teammate's contribution


def test_router_agents_advises_on_the_whole_team_s_runs(tmp_path, monkeypatch, capsys):
    from model_router.agents import advise
    from model_router.catalog import lattice
    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch)
    _agent_file(project, "curator")
    _agent_file(project, "documenter", model="sonnet")
    mine = [_agent_run(project, "curator", i, SETTLED) for i in range(4)]
    team = {("curator", "opus/xhigh"): {"n": 6, "fails": 0}, ("documenter", "sonnet/xhigh"): {"n": 3, "fails": 0},
            ("gone", "opus/high"): {"n": 20, "fails": 0},  # no longer in the repository: no advice
            ("curator", "opus/high"): {"n": 20, "fails": 0}}  # a cell the file no longer names: no advice
    advice = {a.agent: a for a in advise(mine, project, cells=lattice(), team=team)}
    assert set(advice) == {"curator", "documenter"}
    assert advice["curator"].runs == 10 and advice["curator"].team_runs == 6 and advice["curator"].action == "lower"
    assert advice["documenter"].action == "measuring" and advice["documenter"].mean_turns == 0
    (STATE(tmp_path) / "subagents.jsonl").write_text("".join(json.dumps({**r, "final": True}) + "\n" for r in mine))
    path = shared.shared_dir(project) / f"aaaaaaaaaaaa-{WEEK.replace('-', '')}.json"
    path.write_text(json.dumps({"v": 1, "c": {}, "a": {"curator|opus/xhigh": [6, 0]}}))
    assert main(["agents", project]) == 0
    out = capsys.readouterr().out
    assert "1 teammate installation" in out and "lower to opus/" in out
