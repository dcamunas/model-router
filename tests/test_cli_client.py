import json
import stat

import pytest

from model_router.catalog import parse_cell
from model_router.cli_client import CliClient, ClientUnavailableError

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
opt = lambda name: args[args.index(name) + 1] if name in args else None
echo = {"model": opt("--model"), "effort": opt("--effort"), "system": opt("--system-prompt"),
        "tools": opt("--tools"), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "nested": os.environ.get("CLAUDECODE"),
        "prompt": prompt}
with open(os.environ["FAKE_CLAUDE_ECHO"], "w") as fh:
    json.dump(echo, fh)
out = {"type": "result", "is_error": False, "result": "answer to: " + prompt.strip(), "stop_reason": "end_turn",
       "session_id": "sess-1", "modelUsage": {opt("--model"): {"costUSD": 0.001}},
       "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 800}}
if prompt.strip() == "LOGOUT":
    out.update(is_error=True, result="Not logged in · Please run /login")
elif prompt.strip() == "GARBAGE":
    sys.stdout.write("not json"); sys.exit(1)
elif prompt.strip() == "LIST":
    sys.stdout.write("[]"); sys.exit(0)
elif prompt.strip() == "CRASH":
    json.dump(out, sys.stdout); sys.exit(2)
json.dump(out, sys.stdout)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    echo = tmp_path / "echo.json"
    monkeypatch.setenv("FAKE_CLAUDE_ECHO", str(echo))
    monkeypatch.setenv("CLAUDECODE", "1")
    client = CliClient(binary=str(binary), config_dir=tmp_path / "profile")
    return client, echo


def test_passes_cell_profile_and_prompt_and_reads_usage(fake):
    client, echo = fake
    c = client.complete(parse_cell("opus/low"), prompt="hello", system="be brief")
    e = json.loads(echo.read_text())
    assert (e["model"], e["effort"], e["system"], e["tools"], e["prompt"]) == ("claude-opus-5-5", "low", "be brief", "", "hello")
    assert e["config_dir"].endswith("profile") and e["nested"] is None
    assert c.text == "answer to: hello" and c.model == "claude-opus-5-5"
    u = c.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (12, 34, 5, 800)


def test_errors_become_client_unavailable(fake):
    client, _ = fake
    with pytest.raises(ClientUnavailableError, match="not logged in"):
        client.complete(parse_cell("haiku/low"), prompt="LOGOUT")
    with pytest.raises(ClientUnavailableError, match="no JSON"):
        client.complete(parse_cell("haiku/low"), prompt="GARBAGE")


def test_unexpected_json_or_a_non_zero_exit_become_client_unavailable(fake):
    client, _ = fake
    with pytest.raises(ClientUnavailableError, match="no JSON object"):
        client.complete(parse_cell("haiku/low"), prompt="LIST")
    with pytest.raises(ClientUnavailableError, match="exited with code 2"):
        client.complete(parse_cell("haiku/low"), prompt="CRASH")


def test_served_model_is_the_requested_one_when_listed_else_the_busiest():
    from model_router.cli_client import _served_model

    two = {"claude-opus-5-5": {"outputTokens": 5}, "claude-haiku-5-5": {"outputTokens": 900}}
    assert _served_model(two, "claude-opus-5-5") == "claude-opus-5-5"
    fallback = {"claude-haiku-5-5": {"outputTokens": 900}, "claude-sonnet-5-5": {"outputTokens": 10}}
    assert _served_model(fallback, "claude-opus-5-5") == "claude-haiku-5-5"
    assert _served_model(None, "claude-opus-5-5") == "claude-opus-5-5"
    assert _served_model({}, "claude-opus-5-5") == "claude-opus-5-5"


def test_the_cli_backend_runs_on_the_profile_in_use(monkeypatch, tmp_path):
    monkeypatch.setattr("model_router.cli_client.shutil.which", lambda b: "/bin/claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work"))
    assert CliClient().config_dir == (tmp_path / "work").absolute()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CliClient().config_dir == (tmp_path / ".claude").absolute()
    assert CliClient(config_dir=tmp_path / "other").config_dir == tmp_path / "other"  # a caller may name another


def test_a_missing_claude_executable_is_one_clear_error():
    with pytest.raises(ClientUnavailableError, match="cannot find"):
        CliClient(binary="claude-does-not-exist")


def test_every_session_the_router_starts_is_marked_as_its_child(tmp_path, monkeypatch):
    import json
    import os
    import subprocess

    from model_router.catalog import parse_cell
    from model_router.cli_client import CliClient
    from model_router.paths import CHILD_ENV

    seen = {}

    def fake_run(args, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"result": "ok"}), stderr="")

    monkeypatch.setattr("model_router.cli_client.shutil.which", lambda b: "/bin/claude")
    monkeypatch.setattr("model_router.cli_client.subprocess.run", fake_run)
    CliClient(config_dir=tmp_path).complete(parse_cell("haiku/low"), prompt="x")
    assert seen[CHILD_ENV] == "1" and CHILD_ENV not in os.environ  # set for the child only
