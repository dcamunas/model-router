import json
from pathlib import Path

from model_router.catalog import parse_cell
from model_router.feedback import FeedbackStore


def test_two_stores_on_one_file_keep_each_others_counts(tmp_path: Path):
    path = tmp_path / "feedback.json"
    a, b = FeedbackStore(path), FeedbackStore(path)
    a.record("code", parse_cell("opus/low"), True)
    b.record("code", parse_cell("opus/low"), False)
    b.record("write", parse_cell("sonnet/low"), True, first=False)
    a.save()
    b.save()
    data = json.loads(path.read_text())
    assert data["code"]["opus/low"] == {"n": 2, "pass": 1, "n_first": 2, "pass_first": 1}
    assert data["write"]["sonnet/low"] == {"n": 1, "pass": 1, "n_first": 0, "pass_first": 0}
    assert b.data == data  # a store learns what the other one added when it saves
    a.save()  # nothing new to add: the file does not change
    assert json.loads(path.read_text()) == data
    assert not list(tmp_path.glob("*.tmp"))


def test_an_unreadable_file_starts_empty_and_is_replaced_on_save(tmp_path: Path):
    path = tmp_path / "feedback.json"
    path.write_text("{not json")
    store = FeedbackStore(path)
    assert store.unreadable and store.data == {}
    store.record("code", parse_cell("opus/low"), True)
    store.save()
    assert not store.unreadable
    assert json.loads(path.read_text())["code"]["opus/low"]["n"] == 1
    path.write_text("[1, 2]")
    assert FeedbackStore(path).unreadable


def test_override_moves_one_count_and_is_merged_like_any_other_change(tmp_path: Path):
    path = tmp_path / "feedback.json"
    first = FeedbackStore(path)
    first.record("code", parse_cell("opus/low"), True)
    first.save()
    other = FeedbackStore(path)
    other.apply_override("code", "opus/low", False, first=True)
    other.save()
    assert json.loads(path.read_text())["code"]["opus/low"] == {"n": 1, "pass": 0, "n_first": 1, "pass_first": 0}
