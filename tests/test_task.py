import json

import pytest

from model_router.task import Task, json_payload


@pytest.mark.parametrize("answer", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    '```json\n{"a": 1}```',
    '```json \n{"a": 1}\n```',
    '```\r\n{"a": 1}\r\n```',
    'Here it is:\n```json\n{"a": 1}\n```\nDone.',
])
def test_json_payload_unwraps_fenced_blocks_in_every_common_shape(answer):
    assert json_payload(answer) == {"a": 1}


def test_json_payload_still_rejects_what_is_not_json():
    with pytest.raises(json.JSONDecodeError):
        json_payload("```json\n{oops}\n```")
    assert json_payload("[1, 2]") == [1, 2]


def test_invalid_task_values_are_rejected():
    with pytest.raises(ValueError):
        Task(prompt="x", kind="poem")
    with pytest.raises(ValueError):
        Task(prompt="x", risk="huge")
    with pytest.raises(ValueError):
        Task(prompt="   ")
