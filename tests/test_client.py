"""LiveClient without network: a stub SDK client records the request and returns real SDK message
objects, so the request shape (effort, fallbacks, parse vs create) and the response mapping are
checked against the installed SDK types."""

from types import SimpleNamespace

from anthropic.types import Message
from anthropic.types.beta import BetaMessage

from model_router.catalog import Cell
from model_router.client import FALLBACK_BETA, LiveClient, to_completion
from model_router.verify import Verdict


def beta_message(**over):
    payload = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": "hello"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0},
    }
    payload.update(over)
    return BetaMessage.model_validate(payload)


class Recorder:
    def __init__(self, reply):
        self.reply = reply
        self.calls: list[tuple[str, dict]] = []

    def create(self, **kw):
        self.calls.append(("create", kw))
        return self.reply

    def parse(self, **kw):
        self.calls.append(("parse", kw))
        msg = self.reply
        msg.parsed_output = Verdict(passed=True, confidence=0.8, reason="ok")
        return msg


def stub_client(reply):
    beta = Recorder(reply)
    plain = Recorder(reply)
    return SimpleNamespace(beta=SimpleNamespace(messages=beta), messages=plain), beta, plain


def test_opus_request_carries_effort_and_default_fallbacks():
    sdk, beta, plain = stub_client(beta_message())
    client = LiveClient(sdk)
    c = client.complete(Cell("opus", "low"), prompt="hi", system="sys", max_tokens=500)
    assert plain.calls == []
    kind, kw = beta.calls[0]
    assert kind == "create"
    assert kw["model"] == "claude-opus-5-5" and kw["max_tokens"] == 500 and kw["system"] == "sys"
    assert kw["output_config"] == {"effort": "low"}
    assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "thinking" not in kw
    assert c.text == "hello" and c.usage.input_tokens == 120 and c.usage.cache_read_tokens == 1000
    assert c.model == "claude-opus-5-5" and not c.refused and not c.fallback_ran


def test_haiku_uses_plain_messages_without_fallbacks():
    plain_msg = Message.model_validate({
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "x"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    })
    sdk, beta, plain = stub_client(plain_msg)
    LiveClient(sdk).complete(Cell("haiku", "medium"), prompt="hi")
    assert beta.calls == []
    _, kw = plain.calls[0]
    assert "fallbacks" not in kw and "betas" not in kw and kw["output_config"] == {"effort": "medium"}


def test_structured_output_goes_through_parse():
    sdk, beta, _ = stub_client(beta_message())
    c = LiveClient(sdk).complete(Cell("sonnet", "low"), prompt="judge", output_format=Verdict)
    kind, kw = beta.calls[0]
    assert kind == "parse" and kw["output_format"] is Verdict
    assert isinstance(c.parsed, Verdict) and c.parsed.passed


def test_refusal_is_mapped_with_its_category():
    msg = beta_message(stop_reason="refusal", content=[],
                       stop_details={"type": "refusal", "category": "cyber", "explanation": "no"})
    c = to_completion(msg, 0.1)
    assert c.refused and c.refusal_category == "cyber" and c.text == "" and c.parsed is None


def test_fallbacks_disabled_uses_plain_endpoint():
    sdk, beta, plain = stub_client(beta_message())
    LiveClient(sdk, server_fallbacks=False).complete(Cell("fable", "low"), prompt="hi")
    assert beta.calls == [] and plain.calls[0][1]["model"] == "claude-fable-5-1"
