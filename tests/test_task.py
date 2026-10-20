import pytest

from model_router.task import Task, json_checker


@pytest.mark.parametrize("answer", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```json\n{"a": 1}```',
    '```json \n{"a": 1}\n```',
    '```\r\n{"a": 1}\r\n```',
    'Here it is:\n```json\n{"a": 1}\n```\nDone.',
])
def test_json_checker_unwraps_fenced_blocks_in_every_common_shape(answer):
    assert json_checker(("a",))(answer).passed


def test_json_checker_still_rejects_what_is_not_json():
    assert not json_checker()("```json\n{oops}\n```").passed
    assert not json_checker(("a",))("[1, 2]").passed


def test_task_rejects_zero_or_negative_overrides():
    with pytest.raises(ValueError, match="max_tokens"):
        Task(prompt="x", max_tokens=0)
    with pytest.raises(ValueError, match="max_attempts"):
        Task(prompt="x", max_attempts=0)
    with pytest.raises(ValueError, match="max_attempts"):
        Task(prompt="x", max_attempts=-1)
    with pytest.raises(ValueError, match="budget_usd"):
        Task(prompt="x", budget_usd=0.0)
    with pytest.raises(ValueError, match="budget_usd"):
        Task(prompt="x", budget_usd=-1.0)
    assert Task(prompt="x", max_attempts=1, budget_usd=0.01, max_tokens=1).budget_usd == 0.01
