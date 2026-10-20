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


# ---- the CLI doors: Codex (ChatGPT by subscription) and Gemini ----------------------------------------------------

import subprocess  # noqa: E402 - the doors' tests need it after the API ones
from pathlib import Path  # noqa: E402

from model_router.providers import CodexClient, GeminiClient, build_client, resolve_codex_home  # noqa: E402

VERDICT = {"passed": True, "confidence": 0.9, "reason": "ok", "issues": []}


def _codex_events(text, *, input_tokens=120, cached=20, output_tokens=30):
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": {"input_tokens": input_tokens, "cached_input_tokens": cached,
                                             "output_tokens": output_tokens}},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FakeRun:
    """Stands in for subprocess.run: records each call, writes the `-o` file as Codex would (`last`: the text,
    "" for an empty file, None for no file at all), answers in turn.
    """

    def __init__(self, *outputs, returncode=0, stderr="", last: str | None = "from events"):
        self.outputs, self.returncode, self.stderr, self.last, self.calls = list(outputs), returncode, stderr, last, []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        stdout = self.outputs.pop(0) if self.outputs else ""
        if "-o" in args and self.last is not None:  # a Codex run
            text = self.last
            if text == "from events":
                events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
                texts = [e["item"]["text"] for e in events if e.get("type") == "item.completed"]
                text = texts[-1] if texts else ""
            Path(args[args.index("-o") + 1]).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(args, self.returncode, stdout=stdout, stderr=self.stderr)


@pytest.fixture
def doors():
    load_external({"external": {
        "luna": {"provider": "codex", "id": "gpt-5.5", "input": 5, "output": 30},
        "gem": {"provider": "gemini", "id": "gemini-3-pro", "input": 2, "output": 12},
    }})
    yield
    load_external({})


def test_codex_runs_exec_on_the_given_home_with_the_schema_and_reads_usage_and_the_last_message(doors, tmp_path):
    run = FakeRun(_codex_events(json.dumps(VERDICT)))
    (tmp_path / "codex-home").mkdir()
    client = CodexClient(tmp_path / "codex-home", binary="python3", run=run)
    c = client.complete(parse_cell("luna/xhigh"), prompt="judge this", system="be strict", output_format=Verdict)
    call = run.calls[0]
    args = call["args"]
    assert args[1:5] == ["exec", "--json", "--ephemeral", "--ignore-user-config"] and "--skip-git-repo-check" in args
    assert args[args.index("-m") + 1] == "gpt-5.5" and args[args.index("-s") + 1] == "read-only"
    assert 'model_reasoning_effort="xhigh"' in args and args[-1] == "-" and "--output-schema" in args
    assert call["env"]["CODEX_HOME"] == str(tmp_path / "codex-home") and call["input"].startswith("be strict\n\njudge this")
    assert call["cwd"] == args[args.index("-C") + 1]
    assert isinstance(c.parsed, Verdict) and c.parsed.passed and c.model == "gpt-5.5"
    assert (c.usage.input_tokens, c.usage.output_tokens, c.usage.cache_read_tokens) == (100, 30, 20)
    assert c.text == json.dumps(VERDICT) and c.stop_reason == "end_turn"


def test_codex_tries_once_more_when_a_verdict_was_wanted_and_did_not_come_and_then_gives_up(doors, tmp_path):
    run = FakeRun(_codex_events("not json"), _codex_events(json.dumps(VERDICT)))
    c = CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x", output_format=Verdict)
    assert len(run.calls) == 2 and isinstance(c.parsed, Verdict) and c.usage.output_tokens == 60
    run = FakeRun(_codex_events("still not json"), _codex_events("no"))
    c = CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x", output_format=Verdict)
    assert len(run.calls) == 2 and c.parsed is None  # the verifier will report a judge error
    run = FakeRun(_codex_events("plain answer"))
    c = CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/high"), prompt="x")
    assert len(run.calls) == 1 and c.text == "plain answer" and 'model_reasoning_effort="high"' in run.calls[0]["args"]


def test_codex_reads_the_last_message_from_events_when_the_output_file_is_empty_or_missing(doors, tmp_path):
    for last in ("", None):
        run = FakeRun(_codex_events("from the events"), last=last)
        c = CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x")
        assert c.text == "from the events"
    run = FakeRun(_codex_events("from the events"), last="from the file")
    assert CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x").text == "from the file"


def test_codex_errors_and_timeouts_fail_closed(doors, tmp_path):
    bad = json.dumps({"type": "error", "message": "not signed in"}) + "\n"
    with pytest.raises(ClientUnavailableError, match="not signed in"):
        CodexClient(tmp_path, binary="python3", run=FakeRun(bad, last=None)).complete(parse_cell("luna/low"), prompt="x")
    transient = bad + _codex_events("answered all the same")  # a stream hiccup the CLI recovered from
    c = CodexClient(tmp_path, binary="python3", run=FakeRun(transient)).complete(parse_cell("luna/low"), prompt="x")
    assert c.text == "answered all the same"
    failed = json.dumps({"type": "turn.failed", "error": {"message": "context too long"}}) + "\n"
    with pytest.raises(ClientUnavailableError, match="context too long"):
        CodexClient(tmp_path, binary="python3", run=FakeRun(failed, last=None)).complete(parse_cell("luna/low"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="does not exist"):
        CodexClient(tmp_path / "typo", binary="python3", run=FakeRun())
    with pytest.raises(ClientUnavailableError, match="code 2.*boom"):
        CodexClient(tmp_path, binary="python3", run=FakeRun("", returncode=2, stderr="boom")).complete(
            parse_cell("luna/low"), prompt="x")

    def slow(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    with pytest.raises(ClientUnavailableError, match="exceeded"):
        CodexClient(tmp_path, binary="python3", run=slow).complete(parse_cell("luna/low"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="cannot find the executable"):
        CodexClient(tmp_path, binary="no-such-binary-here")


def test_gemini_runs_headless_in_plan_mode_and_reads_the_json_answer(doors):
    answer = {"response": "fine", "stats": {"models": {"gemini-3-pro": {"tokens": {"prompt": 50, "candidates": 7,
                                                                                    "cached": 10, "total": 57}}}}}
    run = FakeRun(json.dumps(answer))
    c = GeminiClient(binary="python3", run=run).complete(parse_cell("gem"), prompt="say fine", system="be brief")
    args, call = run.calls[0]["args"], run.calls[0]
    assert args[1] == "-p" and call["input"].startswith("be brief\n\nsay fine") and args[args.index("-m") + 1] == "gemini-3-pro"
    assert args[args.index("-o") + 1] == "json" and args[-2:] == ["--approval-mode", "plan"] and call["cwd"]
    assert "say fine" not in " ".join(args)  # the task travels on stdin, not in the command line
    assert c.text == "fine" and c.model == "gemini-3-pro" and (c.usage.input_tokens, c.usage.output_tokens) == (40, 7)
    verdict = dict(answer, response="```json\n" + json.dumps(VERDICT) + "\n```")
    c = GeminiClient(binary="python3", run=FakeRun(json.dumps(verdict))).complete(parse_cell("gem"), prompt="x",
                                                                                   output_format=Verdict)
    assert isinstance(c.parsed, Verdict)
    with pytest.raises(ClientUnavailableError, match="quota"):
        GeminiClient(binary="python3", run=FakeRun(json.dumps({"error": {"message": "quota exceeded"}}))).complete(
            parse_cell("gem"), prompt="x")
    with pytest.raises(ClientUnavailableError, match="no JSON object"):
        GeminiClient(binary="python3", run=FakeRun("Loading...", returncode=1)).complete(parse_cell("gem"), prompt="x")


def test_codex_home_comes_from_the_environment_then_config_and_never_the_ambient_one(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_CODEX_HOME", raising=False)
    with pytest.raises(ClientUnavailableError, match="--codex-home"):
        resolve_codex_home()
    from model_router.paths import write_config

    write_config(codex_home="~/.codex-personal")
    assert resolve_codex_home() == (Path("~/.codex-personal").expanduser(), str(tmp_path / "config.json"))
    monkeypatch.setenv("MODEL_ROUTER_CODEX_HOME", str(tmp_path / "other"))
    assert resolve_codex_home() == (tmp_path / "other", "MODEL_ROUTER_CODEX_HOME")


def test_multi_client_opens_each_door_on_first_use_and_a_missing_home_fails_only_then(doors, tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path))
    monkeypatch.delenv("MODEL_ROUTER_CODEX_HOME", raising=False)
    client = MultiClient(FakeClient(script={"*": [Reply("claude")]}))
    assert client.complete(parse_cell("opus/low"), prompt="x").text == "claude"
    with pytest.raises(ClientUnavailableError, match="Codex home not set"):
        client.complete(parse_cell("luna/low"), prompt="x")
    assert type(build_client(PROVIDERS["openai"])).__name__ == "OpenAICompatibleClient"
