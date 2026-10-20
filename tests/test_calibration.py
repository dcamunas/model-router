import json

import pytest

from model_router.calibration import (
    Calibration,
    active_calibration_name,
    classifier_report,
    load_calibration,
    measure,
    report,
    wilson,
)
from model_router.catalog import lattice, parse_cell, step_up
from model_router.paths import write_config
from model_router.policy import FLOOR, START, Policy
from model_router.task import Task


def _record(kind: str, cell: str, passed: bool, *, cost: float = 0.01, verify_cost: float = 0.0, source: str = "checker",
            eval_id: str | None = "ev1", overridden: bool = False, status: str | None = None) -> dict:
    return {
        "type": "task", "task_id": "t", "kind": kind, "risk": "low", "eval_id": eval_id,
        "status": status or ("solved" if passed else "unsolved"), "overridden": overridden,
        "attempts": [{"n": 1, "cell": cell, "verdict": passed, "verdict_source": source, "cost_usd": cost,
                      "verify_cost_usd": verify_cost}],
    }


def _records(kind: str, cell: str, passes: int, fails: int, **kw) -> list[dict]:
    return [_record(kind, cell, True, **kw) for _ in range(passes)] + [_record(kind, cell, False, **kw) for _ in range(fails)]


def test_wilson_interval_narrows_with_n_and_stays_in_range():
    assert wilson(0, 0) == (0.0, 1.0)
    low6, high6 = wilson(5, 6)
    low60, high60 = wilson(50, 60)
    assert 0.0 <= low6 < low60 < 50 / 60 < high60 < high6 <= 1.0


def test_only_verified_first_attempts_count_and_overrides_flip_a_single_verdict():
    records = _records("classify", "haiku/low", 5, 1)
    records += [_record("classify", "haiku/low", True, source="none")]  # unverified: no evidence
    records += [_record("classify", "haiku/low", True, overridden=True, status="unsolved")]  # a human said no
    records += [{"type": "task", "task_id": "x", "kind": "classify", "attempts": []}]
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    m = cal.measured["classify"]["haiku/low"]
    assert (cal.records, cal.evidence) == (9, 7) and (m["n"], m["passes"]) == (7, 5)
    assert cal.sources == {"bench": 8, "ledger": 1}  # the empty record carries no eval id


def test_a_cheaper_measured_cell_that_passes_replaces_an_unmeasured_start():
    cal = measure(_records("classify", "haiku/low", 11, 1), current=START, floors=FLOOR, min_n=6)
    assert cal.start["classify"]["medium"] == "haiku/low"  # was haiku/medium, unmeasured and dearer
    assert cal.start["classify"]["low"] == "haiku/low"  # unchanged: already there
    assert cal.start["classify"]["high"] == "sonnet/low"  # the floor keeps haiku out
    assert [(c["kind"], c["risk"], c["from"], c["to"], c["n"]) for c in cal.changes] == [
        ("classify", "medium", "haiku/medium", "haiku/low", 12)]
    assert "classify" not in cal.no_data and "write" in cal.no_data


def test_a_dearer_cell_never_replaces_an_unmeasured_start_without_evidence_against_it():
    cal = measure(_records("write", "opus/low", 12, 0), current=START, floors=FLOOR, min_n=6)
    assert cal.start["write"]["low"] == "sonnet/low" and cal.start["write"]["medium"] == "sonnet/medium"
    assert cal.start["write"]["high"] == "opus/low" and not cal.changes  # measured where the table already is


def test_a_measured_start_that_loses_on_cost_per_solved_moves_up():
    records = _records("code", "opus/low", 3, 9, cost=0.02)  # the current start for code x medium fails 75%
    records += _records("code", "opus/medium", 12, 0, cost=0.03)
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    assert cal.start["code"]["medium"] == "opus/medium"
    change = next(c for c in cal.changes if c["risk"] == "medium")
    assert (change["from_n"], change["from_rate"]) == (12, 0.25) and "was 25% of 12" in report(cal)
    low = parse_cell("opus/low")
    up = step_up(low, lattice())  # the ladder's next cell (fable/low), not measured: its price scaled as opus/low's was
    assert up is not None and up.label != "opus/medium"
    c_up = up.expected_cost() * 0.02 / low.expected_cost()
    assert cal.measured["code"]["opus/low"]["cost_per_solved"] == pytest.approx(0.02 + 0.75 * c_up, rel=1e-4)
    assert cal.measured["code"]["opus/medium"]["cost_per_solved"] == pytest.approx(0.03 * 1.0, rel=1e-4)


def test_a_saving_under_the_minimum_moves_nothing_and_tagged_records_count_as_bench():
    records = _records("code", "opus/low", 12, 0, cost=0.0100, eval_id=None)  # the start of code x medium, measured
    records += _records("code", "sonnet/medium", 12, 0, cost=0.0095, eval_id=None)  # 5 % cheaper: noise
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    assert cal.start["code"]["medium"] == "opus/low" and all(c["risk"] != "medium" for c in cal.changes)
    assert cal.start["code"]["high"] == "sonnet/medium"  # opus/medium was not measured: a cheaper measured cell may replace it
    cal = measure(records, current=START, floors=FLOOR, min_n=6, min_saving=0.0)
    assert cal.start["code"]["medium"] == "sonnet/medium"
    for r in records:
        r["source"] = "20261020-x.jsonl"
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    assert cal.sources == {"bench": 24, "ledger": 0}


def test_too_few_attempts_or_a_rate_under_the_minimum_change_nothing():
    cal = measure(_records("classify", "haiku/low", 4, 0), current=START, floors=FLOOR, min_n=6)
    assert not cal.changes and "classify" in cal.no_data
    cal = measure(_records("extract", "haiku/low", 3, 9), current=START, floors=FLOOR, min_n=6)
    assert not cal.changes and "extract" not in cal.no_data  # measured, but it fails: no start may use it


def test_eval_filter_and_the_report_name_the_evidence():
    records = _records("classify", "haiku/low", 6, 0, eval_id="ev1") + _records("classify", "haiku/low", 0, 6, eval_id=None)
    cal = measure(records, current=START, floors=FLOOR, min_n=6, eval_id="ev1")
    assert cal.records == 6 and cal.measured["classify"]["haiku/low"]["rate"] == 1.0 and cal.id.endswith("-ev1")
    text = report(cal)
    assert "6 first attempts with a verified verdict" in text
    assert "classify x medium: haiku/medium -> haiku/low (100% of 6, $0.010 per solved; was not measured)" in text
    assert cal.changes[0]["from_n"] == 0 and cal.changes[0]["from_cost_per_solved"] is None
    assert "haiku/low*" in text and "compared with the built-in table" in text
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    assert cal.measured["classify"]["haiku/low"]["rate"] == 0.5 and cal.sources == {"bench": 6, "ledger": 6}


def test_save_load_and_the_active_name_from_env_then_config(tmp_path, monkeypatch):
    cal = measure(_records("classify", "haiku/low", 12, 0), current=START, floors=FLOOR)
    first = cal.save(tmp_path / "calibrations")
    again = measure(_records("classify", "haiku/low", 12, 0), current=START, floors=FLOOR)
    second = again.save(tmp_path / "calibrations")
    assert first != second and again.id == cal.id + "-2"
    loaded = load_calibration(str(first))
    assert loaded == cal and loaded.start == cal.start and loaded.path == first
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    assert active_calibration_name() is None
    write_config(calibration=cal.id)
    assert active_calibration_name() == cal.id and load_calibration(cal.id).id == cal.id
    monkeypatch.setenv("MODEL_ROUTER_CALIBRATION", "none")
    assert active_calibration_name() is None
    monkeypatch.setenv("MODEL_ROUTER_CALIBRATION", str(second))
    assert load_calibration(active_calibration_name() or "").id == again.id
    (tmp_path / "bad.json").write_text(json.dumps({"id": "x"}))
    with pytest.raises(ValueError, match="not a calibration"):
        load_calibration(str(tmp_path / "bad.json"))
    with pytest.raises(OSError):
        load_calibration("never-made")


def test_policy_starts_from_the_calibration_and_carries_its_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    cal = measure(_records("classify", "haiku/low", 12, 0), current=START, floors=FLOOR)
    cal.save(tmp_path / "calibrations")
    task = Task(prompt="x", kind="classify", risk="medium")
    assert Policy().plan(task).start.label == "haiku/medium" and Policy().calibration_id == "builtin"
    write_config(calibration=cal.id)
    policy = Policy()
    plan = policy.plan(task)
    assert plan.start.label == "haiku/low" and policy.calibration_id == cal.id
    assert plan.rationale[0] == f"table classify x medium -> haiku/low (calibration {cal.id})"
    assert Policy(calibration=None).plan(task).start.label == "haiku/medium"
    assert Policy(calibration=cal).calibration_id == cal.id
    write_config(calibration="gone")
    policy = Policy()
    assert policy.calibration_id == "builtin" and policy.calibration_missing == "gone"
    assert any("calibration gone cannot be loaded" in r for r in policy.plan(task).rationale)


def test_classifier_report_counts_misses_and_whether_they_move_the_start():
    specs = [
        {"id": "a", "kind": "classify", "risk": "low", "prompt": "Classify these tickets as billing or bug"},
        {"id": "b", "kind": "write", "risk": "critical", "prompt": "Summarise the outage for the customers, critical"},
        {"id": "c", "kind": "classify", "risk": "low", "prompt": "Review each ticket and label it billing or bug"},
        {"id": "d", "prompt": "no labels here"},
    ]
    text = classifier_report(specs, start=lambda t: Policy(calibration=None).plan(t).start)
    assert "rules: kind right on 1/3, risk right on 2/3; 2 misses, 1 of them change the start cell" in text
    assert "expected \\ got" in text and "haiku/low -> opus/low" in text  # c: a classification read as a review
    assert "summarize/critical  same" in text  # b: the critical floor makes both starts opus/medium
    assert classifier_report([{"prompt": "x"}], start=lambda t: parse_cell("haiku/low")).startswith("no task")


def test_calibration_from_dict_rejects_a_table_this_router_cannot_plan_with(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not a calibration"):
        Calibration.from_dict({"id": "x"})
    data = measure([], current=START, floors=FLOOR).to_dict()
    for broken, message in (
        ("nope", "`start`"),
        ({k: v for k, v in START.items() if k != "unknown"}, "lacks the kinds unknown"),
        ({**START, "poetry": START["write"]}, "unknown kind 'poetry'"),
        ({**START, "code": {k: v for k, v in START["code"].items() if k != "critical"}}, "no cell for code x critical"),
        ({**START, "code": {**START["code"], "low": "gpt-9/low"}}, "code x low names 'gpt-9/low'"),
        ({**START, "code": {**START["code"], "low": 3}}, "no cell for code x low"),
    ):
        with pytest.raises(ValueError, match=message):
            Calibration.from_dict({**data, "start": broken})
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    bad = tmp_path / "calibrations" / "bad.json"
    bad.parent.mkdir()
    bad.write_text(json.dumps({**data, "start": {**START, "code": {**START["code"], "low": "gpt-9/low"}}}))
    write_config(calibration="bad")
    policy = Policy()  # the file is there but cannot be used: the built-in tables decide, and the plan says so
    assert policy.calibration_id == "builtin" and policy.calibration_missing == "bad"
    assert policy.plan(Task(prompt="x", kind="code", risk="low")).start.label == "sonnet/medium"


def test_a_record_without_a_cost_is_no_evidence_and_the_top_cell_counts_a_retry_on_itself():
    free = _records("code", "haiku/low", 3, 3)
    for r in free:
        del r["attempts"][0]["cost_usd"]
    records = free + _records("code", "sonnet/medium", 6, 0, cost=0.5)
    cal = measure(records, current=START, floors=FLOOR, min_n=6)
    assert "haiku/low" not in cal.measured.get("code", {}) and cal.evidence == 6
    assert cal.start["code"]["low"] == "sonnet/medium" and all(c["to"] != "haiku/low" for c in cal.changes)
    cal = measure(_records("write", "fable/max", 9, 3, cost=1.0), current=START, floors=FLOOR, min_n=6)
    assert cal.measured["write"]["fable/max"]["cost_per_solved"] == pytest.approx(1.0 + 0.25 * 1.0)
