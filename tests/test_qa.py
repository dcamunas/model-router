import json
import os
import subprocess

import pytest

from model_router import qa, tribunal_hook
from model_router.catalog import Usage, load_external, parse_cell
from model_router.client import ClientUnavailableError
from model_router.ledger import Ledger
from model_router.paths import write_config
from model_router.testing import FakeClient, Reply
from model_router.verify import Verdict


def ok(reason="fine"):
    return Reply(parsed=Verdict(passed=True, confidence=0.9, reason=reason), usage=Usage(2000, 100))


def ko(reason="wrong"):
    return Reply(parsed=Verdict(passed=False, confidence=0.9, reason=reason, issues=["calc.py:2 off by one"]),
                 usage=Usage(2000, 100))


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (path / ".gitignore").write_text("build/\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "start")
    return path.resolve()


def test_a_snapshot_sees_new_untracked_and_committed_work_and_touches_neither_index_nor_files(repo):
    index_before = (repo / ".git" / "index").read_bytes()
    base = qa.snapshot(repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    (repo / "new.py").write_text("x = 1\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.bin").write_text("ignored")
    status_before = git(repo, "status", "--porcelain")
    current = qa.snapshot(repo)
    assert (repo / ".git" / "index").read_bytes() == index_before and git(repo, "status", "--porcelain") == status_before
    assert qa.changed_files(repo, base, current) == ["calc.py", "new.py"]  # build/ is ignored
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "committed during the turn")
    assert qa.changed_files(repo, base, qa.snapshot(repo)) == ["calc.py", "new.py"]  # a commit stays in the change
    assert "+    return a + b + 1" in qa.diff_text(repo, base, current)
    assert qa.tree_of(repo, "HEAD") == qa.snapshot(repo)
    same_size = qa.snapshot(repo)
    stat = (repo / "new.py").stat()
    (repo / "new.py").write_text("x = 2\n")
    os.utime(repo / "new.py", ns=(stat.st_atime_ns, stat.st_mtime_ns))  # same size and timestamp: a stat cache misses it
    assert qa.changed_files(repo, same_size, qa.snapshot(repo)) == ["new.py"]
    assert qa.repo_root(repo / "build") == repo and qa.repo_root(repo.parent) is None
    assert qa.only_docs(["README.md", "docs/guía.md"]) and not qa.only_docs(["README.md", "calc.py"]) and not qa.only_docs([])
    assert not qa.only_docs(["requirements.txt"]) and not qa.only_docs(["CMakeLists.txt"])  # build inputs are code


def test_settings_come_from_machine_repo_and_brief_and_never_judge_with_the_chats_model(repo):
    s = qa.settings(repo)
    assert (s.mode, s.claude.label, s.rounds) == ("final", "opus/high", 2)
    assert s.codex == parse_cell("fable/medium") and s.notes  # no codex model declared: a second Claude judge
    assert s.arbiter == parse_cell("sonnet/high")
    s = qa.settings(repo, chat_model="opus")
    assert s.claude.spec.short != "opus" and s.arbiter is not None
    assert s.arbiter.spec.short not in {j.spec.short for j in s.judges}  # the chat's model only as a last resort
    try:
        load_external({"external": {"terra": {"provider": "codex", "id": "gpt-5.6-terra", "input": 2, "output": 12, "tier": 1},
                                    "luna": {"provider": "codex", "id": "gpt-6-luna", "input": 0.1, "output": 0.5, "tier": 0}}})
        s = qa.settings(repo)
        assert s.codex == parse_cell("terra/high") and not s.notes  # the declared codex model of the highest tier
        s = qa.settings(repo, chat_model="opus")
        assert s.claude.spec.short == "fable" and s.arbiter == parse_cell("sonnet/high")  # neither judge, nor the chat
        write_config(qa={"codex": "luna/medium", "rounds": 3})
        (repo / ".claude").mkdir()
        (repo / ".claude" / "tribunal.json").write_text(json.dumps({"mode": "off"}))
        s = qa.settings(repo)
        assert (s.mode, s.codex, s.rounds) == ("off", parse_cell("luna/medium"), 3)
        assert qa.settings(repo, "qa: final\nfix the bug").mode == "final"  # the brief wins over the repository
        s = qa.settings(repo, "Migrate the schema. qa: parallel")
        assert s.mode == "final" and any("parallel is not built yet" in n for n in s.notes)
        assert qa.settings(repo, "line one\nline two\nqa: final").mode == "off"  # only the first two lines declare
        (repo / ".claude" / "tribunal.json").write_text(json.dumps({"mode": "sideways"}))
        with pytest.raises(ValueError, match="unknown qa mode"):
            qa.settings(repo)
    finally:
        load_external({})


def test_run_qa_asks_both_judges_blind_and_the_arbiter_settles_a_split(repo):
    s = qa.settings(repo)
    ledger = Ledger(None)
    client = FakeClient(judge={"opus/high": [ok("does the job")], "fable/medium": [ko("off by one")],
                               "sonnet/high": [ko("judge B was right")]})
    result = qa.run_qa(repo, "make add return the sum", "diff --git a/calc.py ...", s, client=client, ledger=ledger)
    attempt = result.attempts[0]
    assert result.status == "unsolved" and attempt.verdict_source == "tribunal" and attempt.arbiter == parse_cell("sonnet/high")
    assert "arbiter sonnet/high sided with fable/medium" in attempt.reason and "calc.py:2" in attempt.reason
    calls = [c for c in client.calls if c["output_format"] == "Verdict"]
    assert {c["cell"] for c in calls[:2]} == {"opus/high", "fable/medium"} and calls[2]["cell"] == "sonnet/high"
    assert all("code change" in (c["system"] or "") or "code review" in (c["system"] or "") for c in calls)
    assert "<task>\nmake add return the sum\n</task>" in calls[0]["prompt"] and "diff --git" in calls[0]["prompt"]
    record = ledger.tasks()[0]
    assert record["attempts"][0]["stop_reason"] == "external" and record["calibration_id"] == "tribunal"
    assert record["attempts"][0]["cost_usd"] == 0 and record["attempts"][0]["verify_cost_usd"] > 0


def test_a_judge_that_cannot_be_reached_is_an_error_not_a_verdict(repo):
    class Down(FakeClient):
        def complete(self, cell, **kw):
            if cell.label == "fable/medium":
                raise ClientUnavailableError("usage limit reached")
            return super().complete(cell, **kw)

    result = qa.run_qa(repo, "x", "diff", qa.settings(repo), client=Down(judge={"*": [ok()]}), ledger=Ledger(None))
    assert result.status == "error" and "usage limit reached" in result.attempts[0].reason


@pytest.fixture
def hooked(repo, monkeypatch):
    monkeypatch.delenv(qa.CHILD_ENV, raising=False)
    client = FakeClient(judge={"*": [ko("still off by one")]})
    monkeypatch.setattr(qa, "repo_client", lambda path: client)

    def event(name, **extra):
        return {"hook_event_name": name, "session_id": "s1", "cwd": str(repo), **extra}

    return repo, client, event


def test_the_hook_blocks_a_failed_change_until_the_rounds_run_out(hooked):
    repo, client, event = hooked
    assert tribunal_hook.handle(event("UserPromptSubmit", prompt="make add return the sum")) is None
    assert tribunal_hook.handle(event("Stop")) is None  # nothing changed in this turn
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
    first = tribunal_hook.handle(event("Stop"))
    assert first is not None and first["decision"] == "block" and "review 1 of 2" in first["reason"]
    assert "still off by one" in first["reason"] and "calc.py:2" in first["reason"]
    assert tribunal_hook.handle(event("Stop")) is None  # the same tree is not judged twice
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b + 2\n")
    second = tribunal_hook.handle(event("Stop"))
    assert second is not None and "decision" not in second
    assert second["systemMessage"].startswith("tribunal: fail after 2 reviews")
    (repo / "calc.py").write_text("def add(a, b):\n    return b + a\n")
    assert tribunal_hook.handle(event("Stop")) is None  # out of rounds: reported once, then quiet
    client.judge = {"*": [ok("correct")]}
    assert tribunal_hook.handle(event("UserPromptSubmit", prompt="tidy it")) is None  # a new turn starts afresh
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    passed = tribunal_hook.handle(event("Stop"))
    assert passed is not None and passed["systemMessage"].startswith("tribunal: pass (") and "1 files" in passed["systemMessage"]


def test_the_hook_leaves_docs_off_repos_and_its_own_judges_alone(hooked, monkeypatch, tmp_path):
    repo, client, event = hooked
    tribunal_hook.handle(event("UserPromptSubmit", prompt="document it"))
    (repo / "README.md").write_text("# calc\n")
    assert tribunal_hook.handle(event("Stop")) is None  # documentation only
    tribunal_hook.handle(event("UserPromptSubmit", prompt="qa: off\nchange it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return 0\n")
    assert tribunal_hook.handle(event("Stop")) is None  # the request said off
    outside = tmp_path / "plain"
    outside.mkdir()
    assert tribunal_hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s2", "cwd": str(outside)}) is None
    assert tribunal_hook.handle({"hook_event_name": "Stop", "session_id": "s2", "cwd": str(outside)}) is None
    monkeypatch.setenv(qa.CHILD_ENV, "0")
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return 1\n")
    assert tribunal_hook.handle(event("Stop")) is None and not client.calls  # a judge's own session is never judged


def test_a_judge_error_is_reported_and_never_blocks(hooked, monkeypatch):
    repo, _, event = hooked

    class Down(FakeClient):
        def complete(self, cell, **kw):
            raise ClientUnavailableError("codex: usage limit reached")

    monkeypatch.setattr(qa, "repo_client", lambda path: Down())
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    out = tribunal_hook.handle(event("Stop"))
    assert out is not None and "decision" not in out and "could not judge" in out["systemMessage"]


def test_the_entry_point_never_fails_on_garbage():
    import sys
    from pathlib import Path

    proc = subprocess.run([str(Path(sys.executable).parent / "router-tribunal-hook")], input="not json",
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0 and proc.stdout == "" and "router-tribunal-hook" in proc.stderr


def test_snapshot_sees_ignored_tracked_files_skips_big_and_unreadable_untracked_ones(repo):
    (repo / "build").mkdir()
    (repo / "build" / "keep.cfg").write_text("a = 1\n")
    git(repo, "add", "-f", "build/keep.cfg")
    git(repo, "commit", "-qm", "a tracked file .gitignore matches")
    assert qa.snapshot(repo) == qa.tree_of(repo, "HEAD")  # a clean tree: no phantom deletion
    base = qa.snapshot(repo)
    (repo / "build" / "keep.cfg").write_text("a = 2\n")
    (repo / "big.db").write_bytes(b"x" * (qa.MAX_UNTRACKED_BYTES + 1))
    locked = repo / "locked.py"
    locked.write_text("secret = 1\n")
    locked.chmod(0)
    try:
        assert qa.changed_files(repo, base, qa.snapshot(repo)) == ["build/keep.cfg"]
    finally:
        locked.chmod(0o644)
    (repo / "latin.py").write_bytes("# café\n".encode("latin-1"))
    git(repo, "config", "diff.external", "false")  # a user's external diff tool must not replace the diff
    text = qa.diff_text(repo, base, qa.snapshot(repo))
    assert "+# caf\ufffd" in text and "latin.py" in text


def test_a_fresh_repository_and_no_head_work(tmp_path):
    path = tmp_path / "fresh"
    path.mkdir()
    git(path, "init", "-q")
    (path / "a.py").write_text("x = 1\n")
    assert not qa.has_head(path) and qa.tree_of(path, "HEAD") == qa.empty_tree(path)
    assert qa.changed_files(path, qa.empty_tree(path), qa.snapshot(path)) == ["a.py"]


def test_fallback_judges_stay_on_the_models_this_installation_has(repo, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "haiku,sonnet,opus")
    for chat in (None, "opus", "sonnet"):
        s = qa.settings(repo, chat_model=chat)
        assert all(c.spec.short != "fable" for c in [*s.judges, *([s.arbiter] if s.arbiter else [])]), chat
    assert qa.resolve({"rounds": 50}).rounds == qa.MAX_ROUNDS


def test_the_router_s_own_cli_calls_and_the_judges_never_wake_the_tribunal(monkeypatch, tmp_path):
    import os

    from model_router.catalog import parse_cell
    from model_router.cli_client import CliClient

    seen = {}

    def fake_run(args, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr("model_router.cli_client.shutil.which", lambda b: "/bin/claude")
    monkeypatch.setattr("model_router.cli_client.subprocess.run", fake_run)
    monkeypatch.delenv(qa.CHILD_ENV, raising=False)
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(tmp_path))  # no Claude profile on a CI runner
    CliClient(config_dir=tmp_path).complete(parse_cell("haiku/low"), prompt="x")
    assert seen[qa.CHILD_ENV] == "0" and qa.CHILD_ENV not in os.environ  # set for the child only
    qa.repo_client(tmp_path)
    assert qa.CHILD_ENV not in os.environ


def test_low_confidence_passes_never_block(hooked):
    repo, client, event = hooked
    client.judge = {"*": [Reply(parsed=Verdict(passed=True, confidence=0.3, reason="probably fine"), usage=Usage(1, 1))]}
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return b + a  # same\n")
    out = tribunal_hook.handle(event("Stop"))
    assert out is not None and out["systemMessage"].startswith("tribunal: pass")


def test_the_work_cannot_switch_off_or_stretch_its_own_tribunal(hooked):
    repo, client, event = hooked
    tribunal_hook.handle(event("UserPromptSubmit", prompt="make add return the sum"))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "tribunal.json").write_text(json.dumps({"mode": "off", "rounds": 50}))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    first = tribunal_hook.handle(event("Stop"))
    assert first is not None and first["decision"] == "block" and "review 1 of 2" in first["reason"]  # as when it began


def test_a_turn_that_cannot_start_is_never_judged_against_the_last_one(hooked, tmp_path):
    repo, client, event = hooked
    tribunal_hook.handle(event("UserPromptSubmit", prompt="turn 1: make add subtract"))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    tribunal_hook.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(outside),
                          "prompt": "turn 2, somewhere else"})
    assert tribunal_hook.handle(event("Stop")) is None and not client.calls


def test_only_a_session_that_edited_during_the_turn_turns_a_block_into_a_note(hooked, tmp_path):
    from datetime import UTC, datetime, timedelta

    repo, client, event = hooked

    def transcript(name, tools, minutes):
        path = tmp_path / f"{name}.jsonl"
        at = (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        lines = [{"timestamp": at, "message": {"role": "assistant", "content": [{"type": "tool_use", "name": t}]}}
                 for t in tools]
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
        return str(path)

    tribunal_hook.handle(event("UserPromptSubmit", prompt="change calc"))
    reader = {**event("UserPromptSubmit", prompt="explain calc"), "session_id": "s2",
              "transcript_path": transcript("reader", ["Read", "Grep"], 1)}
    tribunal_hook.handle(reader)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    out = tribunal_hook.handle(event("Stop"))
    assert out is not None and out.get("decision") == "block"  # s2 only read: s1's fail still goes back
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change calc again"))
    writer = {**event("UserPromptSubmit", prompt="change other"), "session_id": "s3",
              "transcript_path": transcript("writer", ["Edit"], 1)}
    tribunal_hook.handle(writer)
    (repo / "calc.py").write_text("def add(a, b):\n    return a * b\n")
    out = tribunal_hook.handle(event("Stop"))
    assert out is not None and "decision" not in out and "another session edited" in out["systemMessage"]
    tribunal_hook.handle({"hook_event_name": "SessionEnd", "session_id": "s3", "cwd": str(repo)})
    with tribunal_hook._state() as state:
        assert "s3" not in state and state["s1"]["open"] is False  # an ended session and a reported turn are closed


def test_a_turn_that_ends_without_a_review_is_closed_and_a_stale_one_is_ignored(hooked):
    repo, client, event = hooked
    tribunal_hook.handle(event("UserPromptSubmit", prompt="qa: off\nchange it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return 0\n")
    assert tribunal_hook.handle(event("Stop")) is None
    with tribunal_hook._state() as state:
        assert state["s1"]["open"] is False
        state["old"] = {"repo": str(repo), "open": True, "at": "2020-01-01T00:00:00+00:00", "base": "x"}
    tribunal_hook.handle({**event("UserPromptSubmit", prompt="go"), "session_id": "s4"})
    with tribunal_hook._state() as state:
        assert state["s4"]["overlaps"] == []  # a turn open since 2020 never reached its Stop: not an overlap


def test_one_judge_out_keeps_the_other_s_verdict_and_reports_it(hooked, monkeypatch):
    repo, _, event = hooked

    class CodexDown(FakeClient):
        def complete(self, cell, **kw):
            if cell.label == "fable/medium":
                raise ClientUnavailableError("not signed in")
            return super().complete(cell, **kw)

    monkeypatch.setattr(qa, "repo_client", lambda path: CodexDown(judge={"*": [ko("divides by zero")]}))
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change it"))
    (repo / "calc.py").write_text("def add(a, b):\n    return a / b\n")
    out = tribunal_hook.handle(event("Stop"))
    assert out is not None and "could not judge" in out["systemMessage"] and "divides by zero" in out["systemMessage"]


def test_a_big_diff_keeps_its_file_list_and_its_closing_tag():
    diff = "diff --git a/lock.json b/lock.json\n" + "+x\n" * 40_000 + "diff --git a/app.py b/app.py\n+bug\n"
    shown = qa.fit_diff(diff, limit=10_000)
    assert len(shown) < 11_000 and shown.startswith("Changed files (2):") and "a/app.py b/app.py" in shown
    assert shown.endswith("open the changed files in the repository to read the rest ...]")


def test_seating_never_repeats_a_model_and_one_judge_when_nothing_else_is_left(repo, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_MODELS", "opus,sonnet")
    s = qa.settings(repo, chat_model="sonnet")
    assert [c.label for c in s.judges] == ["opus/high"] and s.arbiter is None and s.notes  # no second model left
    with pytest.raises(ValueError, match="another model than judge A"):
        qa.resolve({"claude": "opus/high", "codex": "opus/low"})
    with pytest.raises(ValueError, match="arbiter"):
        qa.resolve({"claude": "opus/high", "arbiter": "opus/medium"})


def test_a_turn_started_while_the_last_one_was_judged_keeps_its_own_rounds(hooked, monkeypatch):
    repo, client, event = hooked
    real = qa.run_qa

    def judged_while_a_new_prompt_arrives(*a, **kw):
        result = real(*a, **kw)
        tribunal_hook.on_prompt(event("UserPromptSubmit", prompt="the next request"))
        return result

    monkeypatch.setattr(tribunal_hook, "run_qa", judged_while_a_new_prompt_arrives)
    tribunal_hook.handle(event("UserPromptSubmit", prompt="change calc"))
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    tribunal_hook.handle(event("Stop"))
    with tribunal_hook._state() as state:
        assert state["s1"]["rounds"] == 0 and state["s1"]["brief"] == "the next request"


def test_a_tribunal_code_review_is_no_routing_evidence(repo):
    from model_router.feedback import FeedbackStore
    from model_router.ledger import summarize
    from model_router.paths import home

    ledger = Ledger(home() / "ledger.jsonl")
    result = qa.run_qa(repo, "x", "diff", qa.settings(repo), client=FakeClient(judge={"*": [ko()]}), ledger=ledger)
    summary = summarize(ledger.tasks())
    assert summary.tasks == 0 and not summary.by_cell and summary.by_judge  # its judges count, nothing else
    assert summary.tribunal_reviews == 1 and summary.tribunal_cost > 0 and "tribunal (plugin): 1 reviews" in summary.text()
    from model_router.cli import main

    assert main(["feedback", result.task_id, "--ok"]) == 0
    assert FeedbackStore(home() / "feedback.json").data == {}


def test_the_tribunal_command_judges_texts_reviews_repositories_and_sets_its_judges(repo, monkeypatch, capsys):
    from model_router import tribunal_cli
    from model_router.paths import read_config

    answer = repo.parent / "notice.md"
    answer.write_text("The notice.")
    client = FakeClient(judge={"opus/high": [ok("clear")], "fable/medium": [ko("invents a date")],
                               "sonnet/high": [ok("judge A was right")]})
    monkeypatch.setattr(tribunal_cli, "_live_client", lambda args: client)
    argv = ["judge", "write the notice", "--kind", "write", "--risk", "critical", "--answer", str(answer)]
    assert tribunal_cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "arbiter sonnet/high: pass" in out and "verdict pass" in out
    assert {c["cell"] for c in client.calls} == {"opus/high", "fable/medium", "sonnet/high"}
    assert tribunal_cli.main(["config", "--arbiter", "opus/medium"]) == 2  # on judge A's model: refused, not written
    assert "qa" not in read_config()
    assert tribunal_cli.main(["config", "--rounds", "3", "--arbiter", "sonnet/medium"]) == 0
    assert "3 reviews per turn" in capsys.readouterr().out
    assert read_config()["qa"] == {"rounds": 3, "arbiter": "sonnet/medium"}
    assert tribunal_cli.main(["config", "--arbiter", "default"]) == 0
    assert read_config()["qa"] == {"rounds": 3}
    with pytest.raises(SystemExit):
        tribunal_cli.main(["config", "--rounds", "50"])
    assert tribunal_cli.main(["config", "--claude", "nosuch/high"]) == 2  # refused before anything is written
    write_config(qa="final")  # a malformed stored value is replaced, not fatal
    assert tribunal_cli.main(["config", "--rounds", "3"]) == 0
    assert read_config()["qa"] == {"rounds": 3}
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    assert tribunal_cli.main(["qa", "--repo", str(repo), "--dry-run"]) == 0
    assert "calc.py" in capsys.readouterr().out
    assert tribunal_cli.main(["qa", "--repo", str(repo), "--base", "no-such-branch"]) == 2
    assert "cannot measure the change from 'no-such-branch'" in capsys.readouterr().err


def test_tribunal_config_lists_what_a_codex_home_can_run_and_declares_only_what_is_named(tmp_path, monkeypatch, capsys):
    from model_router import tribunal_cli
    from model_router.catalog import external_models
    from model_router.paths import home

    monkeypatch.delenv("MODEL_ROUTER_CODEX_HOME", raising=False)
    monkeypatch.setattr(tribunal_cli.shutil, "which", lambda name: f"/opt/bin/{name}")
    codex_home = tmp_path / "codex-work"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    try:
        assert tribunal_cli.main(["config", "--codex-home", str(codex_home)]) == 0
        assert "no model list in" in capsys.readouterr().out  # Codex has not run with this home yet
        models = [{"slug": s, "visibility": "list"} for s in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna")]
        (codex_home / "models_cache.json").write_text(json.dumps({"models": models}))
        assert tribunal_cli.main(["config", "--codex-models"]) == 0
        out = capsys.readouterr().out
        assert "--declare sol=codex/gpt-6-sol@2/10/1" in out and "astra=codex/gpt-6-astra@10/50/3" in out
        assert "stay out until you name them" in out and not external_models()
        assert tribunal_cli.main(["config", "--codex-models", "sol,luna", "--codex", "sol/high"]) == 0
        out = capsys.readouterr().out  # declared first, so --codex in the same command may name one of them
        assert sorted(s.short for s in external_models()) == ["luna", "sol"]  # astra stays out: it was not named
        assert "judges opus/high + sol/high" in out and "ok    sol: codex gpt-6-sol" in out
        with pytest.raises(SystemExit, match="not in this home's list: nova"):
            tribunal_cli.main(["config", "--codex-models", "nova"])
        assert tribunal_cli.main(["config", "--declare", "sol=codex/gpt-6-sol@5/30/1"]) == 0
        assert "declared at 5/30, OpenAI lists 2/10" in capsys.readouterr().out  # a price typed by hand, not the list one
        assert tribunal_cli.main(["config", "--declare", "old=codex/gpt-6.1-sol@2/10/1"]) == 0
        assert "FAIL  old: codex gpt-6.1-sol" in capsys.readouterr().out  # not in this home's list
        assert tribunal_cli.main(["config", "--declare", "sol="]) == 0
        assert "sol was also removed from: qa" in capsys.readouterr().out  # the judge that named it goes too
        assert json.loads((home() / "config.json").read_text())["qa"] == {}
    finally:
        load_external({})
