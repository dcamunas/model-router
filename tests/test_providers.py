import json
import subprocess
from pathlib import Path

import pytest

from model_router.catalog import load_external, parse_cell
from model_router.client import ClientUnavailableError
from model_router.providers import CodexClient, MultiClient, resolve_codex_home, strict_schema
from model_router.testing import FakeClient, Reply
from model_router.verify import Verdict

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
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text()) if "--output-schema" in args else None
        self.calls.append({"args": args, "schema": schema, **kwargs})
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
    assert args[args.index("--disable") + 1] == "shell_tool"  # no shell: the read-only sandbox would read any file
    assert Path(args[args.index("-C") + 1]).name.startswith("tmp") or "router-codex" in args[args.index("-C") + 1]
    assert 'model_reasoning_effort="xhigh"' in args and args[-1] == "-" and "--output-schema" in args
    assert call["schema"]["additionalProperties"] is False  # strict, as the Codex backend demands
    assert call["schema"]["required"] == ["passed", "confidence", "reason", "issues"]
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


def test_strict_schema_closes_every_object_and_requires_every_field():
    from pydantic import BaseModel

    class Inner(BaseModel):
        a: int
        b: str = ""

    class Outer(BaseModel):
        inner: Inner
        more: list[Inner] = []
        maybe: Inner | None = None

    schema = strict_schema(Outer.model_json_schema())
    assert schema["additionalProperties"] is False and schema["required"] == ["inner", "more", "maybe"]
    inner = schema["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["a", "b"]
    assert Outer.model_json_schema()["required"] == ["inner"]  # the model itself is untouched


def test_codex_resolves_a_relative_home_and_keeps_keys_out_of_the_child_environment(doors, tmp_path, monkeypatch):
    (tmp_path / "rel-home").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_API_KEY", "sk-not-this-account")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nor-this")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.example")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "ambient"))
    monkeypatch.setenv("UNRELATED", "stays")
    run = FakeRun(_codex_events("ok"))
    CodexClient(Path("rel-home"), binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x")
    env = run.calls[0]["env"]
    assert env["CODEX_HOME"] == str((tmp_path / "rel-home").resolve()) and env["UNRELATED"] == "stays"
    assert not any(k in env for k in ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"))


def test_cli_error_text_never_carries_the_answer(doors, tmp_path):
    answer = "THE ANSWER TEXT"
    # codex: a non-zero exit with the answer on stdout and nothing else
    run = FakeRun(_codex_events(answer), returncode=1, stderr="", last=None)
    with pytest.raises(ClientUnavailableError, match="code 1: no error message") as info:
        CodexClient(tmp_path, binary="python3", run=run).complete(parse_cell("luna/low"), prompt="x")
    assert answer not in str(info.value)
    # codex: exit 0 with no final message and no events is an error, not an empty answer at no cost
    with pytest.raises(ClientUnavailableError, match="gave no answer"):
        CodexClient(tmp_path, binary="python3", run=FakeRun("", last=None)).complete(parse_cell("luna/low"), prompt="x")


def test_strict_schema_recurses_into_tuples_and_mappings():
    from pydantic import BaseModel

    class Pair(BaseModel):
        a: int

    class Shape(BaseModel):
        pair: tuple[Pair, int]
        by_name: dict[str, Pair]

    schema = strict_schema(Shape.model_json_schema())
    assert schema["properties"]["pair"]["prefixItems"][0] == {"$ref": "#/$defs/Pair"}
    assert schema["properties"]["by_name"]["additionalProperties"] == {"$ref": "#/$defs/Pair"}
    assert schema["$defs"]["Pair"]["additionalProperties"] is False
