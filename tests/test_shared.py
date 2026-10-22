import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from model_router import projects, shared
from model_router.catalog import parse_cell
from model_router.hook import decide, handle, start_in_force
from model_router.policy import Policy
from model_router.task import Task

NOW = datetime.now(UTC)
SETTLED = NOW - timedelta(days=14)  # a week that closed more than a day ago
WEEK = projects.week_of(SETTLED)


def _repo(tmp_path, monkeypatch, *, share=True):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MODEL_ROUTER_SHARE", raising=False)
    (tmp_path / "home").mkdir()
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
    (shared.home() / shared.IDS_FILE).write_text(json.dumps({shared._ids_key(project): list(ids)}))


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
    log = tmp_path / "home" / "hook.jsonl"
    log.write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    [path] = shared.write_own(project, records, log)
    assert path.name == f"{_mine(project)}-{WEEK.replace('-', '')}.json"
    text = path.read_text()
    assert json.loads(text) == {"v": 1, "c": {"analysis|medium|sonnet/low": [3, 1]}}
    assert project not in text and len(text) < 60
    records += [_rec(project, "sonnet/low", 5, SETTLED)]
    assert shared.write_own(project, records, log) == [] and path.read_text() == text  # never changed
    assert shared.own_id(project, b"k" * 32) != shared.own_id(project + "2", b"k" * 32)


def test_a_week_is_written_a_day_after_it_closes_and_deleted_when_it_leaves_the_window(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    monday = projects.week_start(projects.week_of(NOW))
    assert monday is not None
    sunday_night = monday - timedelta(minutes=10)
    records = [_rec(project, "sonnet/low", 1, sunday_night)]
    assert shared.write_own(project, records, tmp_path / "none", now=monday + timedelta(hours=1)) == []
    assert len(shared.write_own(project, records, tmp_path / "none", now=monday + timedelta(days=1, hours=1))) == 1
    old = _theirs(project, _mine(project), projects.week_of(NOW - timedelta(weeks=20)), {})
    gone = _theirs(project, "abcdefabcdef", WEEK, {"analysis|medium|sonnet/low": [5, 5]})
    _used(project, "abcdefabcdef")  # an id this install used before for this path
    shared.write_own(project, records, tmp_path / "none")
    assert not old.exists() and not gone.exists()


def test_nothing_is_shared_unless_the_repository_and_this_installation_take_part(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, share=False)
    records = [_rec(project, "sonnet/low", 1, SETTLED)]
    assert shared.write_own(project, records, tmp_path / "none") == []
    assert not (tmp_path / "repo" / ".claude").exists() and shared.pooled(project) == ({}, 0)
    shared.shared_dir(project).mkdir(parents=True)
    _theirs(project, "aaaaaaaaaaaa", WEEK, {"analysis|medium|sonnet/low": [6, 6]})
    monkeypatch.setenv("MODEL_ROUTER_SHARE", "0")
    assert shared.write_own(project, records, tmp_path / "none") == [] and shared.pooled(project) == ({}, 0)


def test_a_salt_that_cannot_be_kept_shares_nothing(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "repo" / ".git" / "nope" / "x"))
    (tmp_path / "repo" / ".git" / "nope").write_text("a file where a directory should be")
    assert projects.install_key()[1] is False
    assert shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], tmp_path / "none") == []


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


def test_the_team_s_counts_alone_only_propose_a_raise_and_one_failure_here_makes_it_happen(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    cell = start_in_force({})("analysis", "medium").label
    _theirs(project, "aaaaaaaaaaaa", WEEK, {f"analysis|medium|{cell}": [6, 6]})
    home = tmp_path / "home"
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
    assert "1 other installation(s)" in out and "n from others" in out and f"raised search (low): {cell}" in out
    assert main(["project", project, "--apply"]) == 0
    assert projects.load_tuning(project)["start"]["search"]["low"]["base"] == cell


def test_a_tuned_cell_never_runs_below_the_start_it_replaced(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus")
    plain = Policy(calibration=None).plan(Task(prompt="x", kind="review", risk="critical")).start
    tuning = {"name": "repo", "start": {"review": {"critical": {"cell": "fable/low", "base": plain.label}}}}
    plan = Policy(calibration=None, tuning=tuning).plan(Task(prompt="x", kind="review", risk="critical"))
    assert plan.start.capability >= plain.capability
    assert any("ignored" in line for line in plan.rationale)


def test_a_cross_checked_review_is_no_evidence_about_its_start(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    _, record = decide({"subagent_type": "general-purpose", "description": "review",
                        "prompt": "kind: review, risk: critical\nReview the payment code"},
                       chat_model=parse_cell(start_in_force({})("review", "critical").label).claude_code()["model"])
    assert record.get("cross") and record["router_chose"] is False


def test_another_clone_of_the_same_repository_is_a_teammate_and_old_weeks_go_whoever_wrote_them(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    _used(project + "-worktree", "121212121212")  # this machine wrote it from a worktree: other path, other log
    worktree = _theirs(project, "121212121212", WEEK, {"analysis|medium|sonnet/low": [2, 1]})
    stale = _theirs(project, "343434343434", projects.week_of(NOW - timedelta(weeks=20)), {})  # someone who left
    shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], tmp_path / "none")
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
    shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], tmp_path / "none")
    assert victim.read_text() == "mine"
    assert shared.pooled(project) == ({}, 0)


def test_a_shared_directory_that_links_elsewhere_is_not_used(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, share=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path(project) / ".claude").mkdir()
    shared.shared_dir(project).symlink_to(elsewhere)
    assert not shared.sharing(project)
    assert shared.write_own(project, [_rec(project, "sonnet/low", 1, SETTLED)], tmp_path / "none") == []
    assert list(elsewhere.iterdir()) == []


def test_an_applied_cheaper_start_is_in_force(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    table = start_in_force({})("search", "medium")
    cheaper = projects._cheaper_below(table, projects.lattice())
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
    home = tmp_path / "home"
    (home / "subagents.jsonl").write_text(json.dumps(_rec(project, cell, 1, SETTLED)) + "\n")
    (home / "hook.jsonl").write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    out = handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": project})
    assert out is not None and "now starts on" in out["systemMessage"]
    assert (shared.shared_dir(project) / f"{_mine(project)}-{WEEK.replace('-', '')}.json").exists()


def test_a_week_just_out_of_the_window_is_kept_a_little_longer(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    edge = projects.week_of(projects.window_start() - timedelta(days=3))
    kept = _theirs(project, "aaaaaaaaaaaa", edge, {"analysis|medium|sonnet/low": [1, 0]})
    shared.write_own(project, [], tmp_path / "none")
    assert kept.exists() and shared.pooled(project) == ({}, 0)  # not counted, but not deleted yet


def test_share_refuses_a_claude_directory_that_is_a_link(tmp_path, monkeypatch):
    import pytest

    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch, share=False)
    (tmp_path / "tools").mkdir()
    (Path(project) / ".claude").symlink_to(tmp_path / "tools")
    with pytest.raises(SystemExit, match="through a link"):
        main(["project", project, "--share"])
    assert list((tmp_path / "tools").iterdir()) == []


def test_a_raise_whose_model_is_gone_keeps_a_stronger_stand_in(monkeypatch):
    plain = Policy(calibration=None).plan(Task(prompt="x", kind="analysis", risk="medium")).start
    raised = projects.step_up(plain, projects.lattice())
    assert raised is not None
    tuning = {"name": "repo", "start": {"analysis": {"medium": {"cell": raised.label, "base": plain.label}}}}
    others = [m for m in ("haiku", "sonnet", "opus", "fable") if m != raised.spec.short]
    monkeypatch.setenv("MODEL_ROUTER_MODELS", ",".join(others))
    start = Policy(calibration=None, tuning=tuning).plan(Task(prompt="x", kind="analysis", risk="medium")).start
    assert start.capability >= plain.capability
