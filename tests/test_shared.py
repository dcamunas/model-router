import json
from datetime import UTC, datetime, timedelta

from model_router import projects, shared
from model_router.hook import handle, start_in_force

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)  # a Thursday of week 43


def _repo(tmp_path, monkeypatch, *, share=True):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    if share:
        shared.shared_dir(str(repo)).mkdir(parents=True)
    return str(repo.resolve())


def _rec(project, cell, i, at, *, kind="analysis", risk="medium", chose=True):
    return {"project": project, "routed": cell, "kind": kind, "risk": risk, "agent_id": f"a{i}",
            "router_chose": chose, "at": at.isoformat(), "sig": [1, 2], "prompt_hash": "x"}


def test_the_shared_file_holds_counts_of_closed_weeks_and_nothing_else(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    last_week = NOW - timedelta(days=7)
    records = [_rec(project, "sonnet/low", i, last_week) for i in range(3)]
    records += [_rec(project, "sonnet/low", 9, NOW)]  # this week is still open: not shared yet
    records += [_rec(project, "sonnet/low", 8, last_week, chose=False)]  # Claude picked the effort: no evidence
    records += [_rec("/elsewhere", "sonnet/low", 7, last_week)]
    records += [_rec(project, "sonnet/low", 6, NOW - timedelta(weeks=20))]  # beyond the window
    log = tmp_path / "home" / "hook.jsonl"
    log.parent.mkdir()
    log.write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    path = shared.write_own(project, records, log, now=NOW)
    assert path is not None and path.parent == shared.shared_dir(project)
    data = json.loads(path.read_text())
    assert data == {"v": 1, "weeks": {"2026-W42": {"analysis|medium|sonnet/low": [3, 1]}}}
    assert "/" not in path.stem and project not in path.read_text() and len(path.read_text()) < 100
    assert shared.write_own(project, records, log, now=NOW) is None  # unchanged: not rewritten
    assert shared.own_id(project) != shared.own_id(project + "2")


def test_nothing_is_written_or_read_unless_the_repository_shares(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch, share=False)
    records = [_rec(project, "sonnet/low", 1, NOW - timedelta(days=7))]
    assert shared.write_own(project, records, tmp_path / "none.jsonl", now=NOW) is None
    assert not (tmp_path / "repo" / ".claude").exists()
    assert shared.pooled(project) == ({}, 0)


def test_pooled_counts_skip_this_install_old_weeks_and_anything_malformed(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    folder = shared.shared_dir(project)
    week = shared.week_of(datetime.now(UTC) - timedelta(days=7))
    old = shared.week_of(datetime.now(UTC) - timedelta(weeks=20))
    (folder / "aaa.json").write_text(json.dumps({"v": 1, "weeks": {
        week: {"analysis|medium|sonnet/low": [4, 1], "analysis|medium|nope/low": [9, 9], "bad|medium|sonnet/low": [9, 9],
               "analysis|medium|haiku/low": [1, 5], "review|high|opus/medium": [True, 0]},
        old: {"analysis|medium|sonnet/low": [50, 50]}, "garbage": {"analysis|medium|sonnet/low": [50, 50]}}}))
    (folder / "bbb.json").write_text(json.dumps({"v": 1, "weeks": {week: {"analysis|medium|sonnet/low": [2, 1]}}}))
    (folder / f"{shared.own_id(project)}.json").write_text(json.dumps({"v": 1, "weeks": {
        week: {"analysis|medium|sonnet/low": [100, 100]}}}))  # mine: already counted from the local log
    (folder / "ccc.json").write_text("not json")
    (folder / "ddd.json").write_text(json.dumps({"v": 2, "weeks": {week: {"analysis|medium|sonnet/low": [9, 9]}}}))
    ev, files = shared.pooled(project)
    assert ev == {("analysis", "medium", "sonnet/low"): {"n": 6, "fails": 2}} and files == 2
    assert shared.pooled(project, since=datetime.now(UTC).isoformat()) == ({}, 0)  # a reset: earlier weeks are weighed


def test_the_hook_raises_on_the_team_s_evidence_and_writes_its_own_counts(tmp_path, monkeypatch):
    project = _repo(tmp_path, monkeypatch)
    cell = start_in_force({})("analysis", "medium").label
    week = shared.week_of(datetime.now(UTC) - timedelta(days=7))
    folder = shared.shared_dir(project)
    (folder / "aaa.json").write_text(json.dumps({"v": 1, "weeks": {week: {f"analysis|medium|{cell}": [3, 1]}}}))
    (folder / "bbb.json").write_text(json.dumps({"v": 1, "weeks": {week: {f"analysis|medium|{cell}": [2, 0]}}}))
    home = tmp_path / "home"
    home.mkdir()
    # alone, neither install nor this machine has enough: together they do (6 launches, 2 relaunched)
    mine = _rec(project, cell, 1, datetime.now(UTC) - timedelta(days=7))
    (home / "subagents.jsonl").write_text(json.dumps(mine) + "\n")
    (home / "hook.jsonl").write_text(json.dumps({"relaunch_of": "a1"}) + "\n")
    out = handle({"hook_event_name": "SessionStart", "session_id": "s", "cwd": project})
    assert out is not None and "now starts on" in out["systemMessage"]
    assert projects.load_tuning(project)["start"]["analysis"]["medium"]["base"] == cell
    own = json.loads((folder / f"{shared.own_id(project)}.json").read_text())
    assert own["weeks"][week] == {f"analysis|medium|{cell}": [1, 1]}


def test_router_project_share_turns_it_on_and_shows_the_team_s_counts(tmp_path, monkeypatch, capsys):
    from model_router.cli import main

    project = _repo(tmp_path, monkeypatch, share=False)
    (tmp_path / "home").mkdir()
    assert main(["project", project, "--share"]) == 0
    assert shared.sharing(project) and "commit this directory" in capsys.readouterr().out
    week = shared.week_of(datetime.now(UTC) - timedelta(days=7))
    (shared.shared_dir(project) / "aaa.json").write_text(json.dumps({"v": 1, "weeks": {
        week: {"search|low|haiku/low": [4, 0]}}}))
    assert main(["project", project]) == 0
    out = capsys.readouterr().out
    assert "1 other installation(s)" in out and "n from others" in out and "haiku/low" in out
