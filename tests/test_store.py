import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from model_router.catalog import Usage
from model_router.ledger import Ledger, summarize
from model_router.runner import Router
from model_router.store import Store
from model_router.task import Task, contains_checker
from model_router.testing import FakeClient, Reply
from model_router.verify import Verdict

OLD_RECORD = {  # a 0.7.0 line: no fingerprint, version, calibration or carried_reason
    "type": "task", "task_id": "old1", "kind": "classify", "risk": "low", "plan": ["haiku/low"], "verify": "none",
    "budget_usd": 0.1, "status": "solved", "final_cell": "haiku/low", "started_at": "2026-10-19T08:00:00+00:00",
    "elapsed": 1.0, "attempts": [{
        "n": 1, "cell": "haiku/low", "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0},
        "cost_usd": 0.0001, "seconds": 1.0, "stop_reason": "end_turn", "served_model": "claude-haiku-5-5",
        "request_id": None, "refused": False, "verdict": True, "verdict_source": "checker", "reason": "matches",
        "verifier": None, "verify_usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "verify_cost_usd": 0.0, "judges": [], "verdicts": [], "arbiter": None,
    }],
    "work_cost_usd": 0.0001, "verify_cost_usd": 0.0, "total_cost_usd": 0.0001, "retries": 0,
    "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0}, "answer_chars": 3, "rationale": [],
}


def _escalating_run(ledger):
    client = FakeClient(script={"opus/low": [Reply("def f(): return 1")], "fable/low": [Reply("def f(): return 42")]})
    task = Task(prompt="write f", kind="code", risk="medium", verify="none", checker=contains_checker("42"))
    return Router(client, ledger=ledger).run(task)


def test_live_writes_index_the_record_and_the_index_matches_the_ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(OLD_RECORD) + "\n")
    store = Store(tmp_path / "router.db")
    files = [(path, None)]
    assert store.stale(files)  # the old line was written without the index
    ledger = Ledger(path, store=store)
    result = _escalating_run(ledger)
    ledger.override(result.task_id, False, note="not really")
    assert store.sync(files)  # rebuilt, because of the old line
    counts = store.counts()
    assert counts == {"tasks": 2, "attempts": 3, "verdicts": 0, "overrides": 1, "sources": 1}
    assert summarize(store.task_records(source="ledger.jsonl")).text() == summarize(ledger.tasks()).text()
    assert not store.stale(files) and not store.sync(files)
    assert store.rebuild(files) == counts  # rebuilding again changes nothing
    ledger.override(result.task_id, True)  # a live write keeps the index in sync without a rebuild
    assert not store.stale(files)
    assert store.task_records(source="ledger.jsonl")[1]["status"] == "solved"


def test_verdict_rows_carry_the_provider_and_the_role_and_task_rows_the_version(tmp_path):
    ok = Reply(parsed=Verdict(passed=True, confidence=0.9, reason="fine"), usage=Usage(2000, 100))
    ko = Reply(parsed=Verdict(passed=False, confidence=0.9, reason="thin"), usage=Usage(2000, 100))
    client = FakeClient(script={"*": [Reply("the notice")]},
                        judge={"opus/medium": [ok], "fable/low": [ko], "fable/medium": [ok]})
    store = Store(tmp_path / "router.db")
    task = Task(prompt="write the incident report", kind="write", risk="critical")
    Router(client, ledger=Ledger(tmp_path / "ledger.jsonl", store=store)).run(task)
    with sqlite3.connect(store.path) as conn:
        verdicts = conn.execute("SELECT cell, provider, role, passed FROM verdict ORDER BY idx").fetchall()
        row = conn.execute("SELECT fingerprint, router_version, calibration_id, eval_id, repeat, strategy FROM task").fetchone()
        attempt = conn.execute("SELECT verdict, verdict_source, arbiter, carried_reason FROM attempt").fetchone()
    assert verdicts == [("opus/medium", "anthropic", "judge", 1), ("fable/low", "anthropic", "judge", 0),
                        ("fable/medium", "anthropic", "arbiter", 1)]
    assert row[0] == task.fingerprint and row[1] and row[2] == "builtin" and row[3] is None and row[4] is None
    assert row[5] is None and attempt == (1, "tribunal", "fable/medium", 0)


def test_a_bench_ledger_names_its_strategy_in_a_note_that_a_rebuild_recovers(tmp_path):
    store = Store(tmp_path / "router.db")
    path = tmp_path / "bench" / "20261020-x.jsonl"
    ledger = Ledger(path, store=store, strategy="fable/low")
    ledger.note(strategy="fable/low", eval_id="abc123", repeat=2)
    task = Task(prompt="say ok", kind="chat", risk="low", id="t1.r2")
    Router(FakeClient(script={"*": [Reply("ok")]}), ledger=ledger).run(task, eval_id="abc123", repeat=2)
    query = "SELECT strategy, eval_id, repeat FROM task"
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(query).fetchall() == [("fable/low", "abc123", 2)]
    store.rebuild([(path, None)])  # the strategy comes from the note, not from the caller
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(query).fetchall() == [("fable/low", "abc123", 2)]
    assert ledger.tasks()[0]["task_id"] == "t1.r2"  # the note is not a task


def test_two_threads_writing_two_ledgers_lose_nothing(tmp_path):
    store = Store(tmp_path / "router.db")
    paths = [tmp_path / f"l{i}.jsonl" for i in range(2)]
    ledgers = [Ledger(path, store=store) for path in paths]

    def work(ledger: Ledger) -> None:
        for n in range(5):
            task = Task(prompt=f"say ok {n}", kind="chat", risk="low")
            Router(FakeClient(script={"*": [Reply("ok")]}), ledger=ledger).run(task)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, ledgers))
    assert store.counts()["tasks"] == 10 and store.counts()["sources"] == 2
    assert not store.stale([(path, None) for path in paths])


def test_rebuild_skips_lines_that_are_not_json(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(OLD_RECORD) + "\n{not json\n\n")
    store = Store(tmp_path / "router.db")
    assert store.rebuild([(path, None), (tmp_path / "missing.jsonl", None)])["tasks"] == 1
    assert not store.stale([(path, None)])
