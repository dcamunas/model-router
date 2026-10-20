import io
import json
import urllib.error
from email.message import Message

import pytest

from model_router.catalog import MODELS, load_external, parse_cell
from model_router.client import ClientUnavailableError
from model_router.providers import PROVIDERS, MultiClient, OpenAICompatibleClient, reasoning_effort
from model_router.testing import FakeClient, Reply
from model_router.verify import Verdict

ANSWER = {"id": "chatcmpl-1", "model": "gpt-5.5-2026-04-23", "choices": [
    {"message": {"content": "fine"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 20}}}


class FakeHTTP:
    """Stands in for urllib's opener: records the request, answers with canned JSON or an HTTP error."""

    def __init__(self, answer=None, status=200):
        self.answer, self.status, self.requests, self.urls = answer or ANSWER, status, [], []

    def __call__(self, request, timeout):
        self.requests.append(json.loads(request.data))
        self.urls.append(request.full_url)
        self.auth = request.get_header("Authorization")
        if self.status != 200:
            raise urllib.error.HTTPError(request.full_url, self.status, "nope", Message(), io.BytesIO(b'{"error": "slow down"}'))
        payload = json.dumps(self.answer).encode() if isinstance(self.answer, dict) else self.answer
        return io.BytesIO(payload)


@pytest.fixture
def externals(monkeypatch):
    load_external({"external": {
        "gpt": {"provider": "openai", "id": "gpt-5.5", "input": 5, "output": 30},
        "deepseek": {"provider": "deepseek", "id": "deepseek-reasoner", "input": 0.55, "output": 2.19},
        "grok": {"provider": "xai", "id": "grok-4.7", "input": 2, "output": 6},
    }})
    yield
    load_external({})


def test_request_carries_model_effort_tokens_and_the_schema_when_a_verdict_is_wanted(externals):
    http = FakeHTTP()
    client = OpenAICompatibleClient(PROVIDERS["openai"], api_key="sk-test", opener=http)
    c = client.complete(parse_cell("gpt/xhigh"), prompt="judge this", system="be strict", max_tokens=500, output_format=Verdict)
    body = http.requests[0]
    assert body["model"] == "gpt-5.5" and body["max_completion_tokens"] == 500 and body["reasoning_effort"] == "high"
    assert body["response_format"] == {"type": "json_object"} and "max_tokens" not in body
    assert body["messages"][0]["role"] == "system" and body["messages"][0]["content"].startswith("be strict")
    assert '"passed"' in body["messages"][0]["content"] and body["messages"][1] == {"role": "user", "content": "judge this"}
    assert http.urls == ["https://api.openai.com/v1/chat/completions"] and http.auth == "Bearer sk-test"
    assert (c.usage.input_tokens, c.usage.output_tokens, c.usage.cache_read_tokens) == (100, 30, 20)
    assert c.model == "gpt-5.5-2026-04-23" and c.stop_reason == "end_turn" and c.request_id == "chatcmpl-1"
    assert c.parsed is None  # "fine" is not a verdict: the verifier will report a judge error


def test_each_provider_gets_its_own_token_field_and_effort_values(externals):
    http = FakeHTTP()
    OpenAICompatibleClient(PROVIDERS["deepseek"], api_key="k", opener=http).complete(parse_cell("deepseek/high"), prompt="x")
    OpenAICompatibleClient(PROVIDERS["xai"], api_key="k", opener=http).complete(parse_cell("grok/medium"), prompt="x")
    deepseek, grok = http.requests
    assert "max_tokens" in deepseek and "reasoning_effort" not in deepseek and deepseek["model"] == "deepseek-reasoner"
    assert grok["reasoning_effort"] == "low" and grok["max_tokens"] == 16000  # xAI knows low and high: medium rounds down
    assert reasoning_effort("max", ("low", "medium", "high")) == "high" and reasoning_effort("low", ()) is None


def test_structured_answers_are_parsed_and_finish_reasons_mapped(externals):
    verdict = {"passed": True, "confidence": 0.9, "reason": "ok", "issues": []}
    fenced = "```json\n" + json.dumps(verdict) + "\n```"
    answer = dict(ANSWER, choices=[{"message": {"content": fenced}, "finish_reason": "stop"}])
    c = _openai(FakeHTTP(answer)).complete(parse_cell("gpt/high"), prompt="x", output_format=Verdict)
    assert isinstance(c.parsed, Verdict) and c.parsed.passed
    cut = dict(ANSWER, choices=[{"message": {"content": "half"}, "finish_reason": "length"}])
    assert _openai(FakeHTTP(cut)).complete(parse_cell("gpt/high"), prompt="x").stop_reason == "max_tokens"
    blocked = dict(ANSWER, choices=[{"message": {"content": ""}, "finish_reason": "content_filter"}])
    c = _openai(FakeHTTP(blocked)).complete(parse_cell("gpt/high"), prompt="x")
    assert c.refused and c.stop_reason == "refusal"


def _openai(http):
    return OpenAICompatibleClient(PROVIDERS["openai"], api_key="k", opener=http)


def test_missing_key_http_errors_and_odd_answers_become_client_unavailable(externals, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ClientUnavailableError, match="OPENAI_API_KEY is not set"):
        OpenAICompatibleClient(PROVIDERS["openai"], opener=FakeHTTP()).complete(parse_cell("gpt/high"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="HTTP 429.*slow down"):
        _openai(FakeHTTP(status=429)).complete(parse_cell("gpt/high"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="no JSON"):
        _openai(FakeHTTP(b"<html>")).complete(parse_cell("gpt/high"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="without choices"):
        _openai(FakeHTTP({"choices": []})).complete(parse_cell("gpt/high"), prompt="x")


def test_multi_client_sends_each_cell_to_its_provider(externals):
    anthropic = FakeClient(script={"*": [Reply("claude says")]})
    built = []

    def factory(provider):
        built.append(provider.name)
        return FakeClient(script={"*": [Reply(f"{provider.name} says")]})

    client = MultiClient(anthropic, factory=factory)
    assert client.complete(parse_cell("opus/low"), prompt="x").text == "claude says"
    assert client.complete(parse_cell("gpt/high"), prompt="x").text == "openai says"
    assert client.complete(parse_cell("gpt/low"), prompt="x").text == "openai says" and built == ["openai"]
    assert client.complete(parse_cell("deepseek/high"), prompt="x").text == "deepseek says"
    assert anthropic.calls[0]["prompt"] == "x" and "gpt-5.5" in MODELS
