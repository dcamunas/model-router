import json
import stat

import pytest

from model_router.catalog import parse_cell
from model_router.cli_client import CliClient, resolve_config_dir
from model_router.client import ClientUnavailableError
from model_router.verify import Verdict

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
opt = lambda name: args[args.index(name) + 1] if name in args else None
echo = {"model": opt("--model"), "effort": opt("--effort"), "system": opt("--system-prompt"), "schema": opt("--json-schema"),
        "tools": opt("--tools"), "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"), "nested": os.environ.get("CLAUDECODE"),
        "prompt": prompt}
with open(os.environ["FAKE_CLAUDE_ECHO"], "w") as fh:
    json.dump(echo, fh)
out = {"type": "result", "is_error": False, "result": "respuesta a: " + prompt.strip(), "stop_reason": "end_turn",
       "session_id": "sess-1", "modelUsage": {opt("--model"): {"costUSD": 0.001}},
       "usage": {"input_tokens": 12, "output_tokens": 34, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 800}}
if prompt.strip() == "LOGOUT":
    out.update(is_error=True, result="Not logged in · Please run /login")
elif prompt.strip() == "GARBAGE":
    sys.stdout.write("not json"); sys.exit(1)
elif opt("--json-schema"):
    out["structured_output"] = {"passed": True, "confidence": 0.9, "reason": "ok", "issues": []}
    out["stop_reason"] = "tool_use"
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
    c = client.complete(parse_cell("opus/low"), prompt="hola", system="sé breve")
    e = json.loads(echo.read_text())
    assert (e["model"], e["effort"], e["system"], e["tools"], e["prompt"]) == ("claude-opus-5-5", "low", "sé breve", "", "hola")
    assert e["config_dir"].endswith("profile") and e["nested"] is None and e["schema"] is None
    assert c.text == "respuesta a: hola" and c.model == "claude-opus-5-5" and c.stop_reason == "end_turn"
    u = c.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens) == (12, 34, 5, 800)
    assert c.request_id == "sess-1" and not c.refused and not c.fallback_ran and c.parsed is None


def test_structured_output_becomes_the_parsed_model(fake):
    client, echo = fake
    c = client.complete(parse_cell("sonnet/medium"), prompt="juzga", output_format=Verdict)
    assert json.loads(json.loads(echo.read_text())["schema"])["required"] == ["passed", "confidence", "reason"]
    assert isinstance(c.parsed, Verdict) and c.parsed.passed and c.stop_reason == "end_turn"


def test_errors_become_client_unavailable(fake):
    client, _ = fake
    with pytest.raises(ClientUnavailableError, match="sesion iniciada"):
        client.complete(parse_cell("haiku/low"), prompt="LOGOUT")
    with pytest.raises(ClientUnavailableError, match="sin JSON"):
        client.complete(parse_cell("haiku/low"), prompt="GARBAGE")


def test_profile_must_be_explicit(monkeypatch, tmp_path):
    from model_router.cli_client import resolve_profile
    from model_router.paths import write_config

    monkeypatch.setenv("MODEL_ROUTER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with pytest.raises(ClientUnavailableError, match="MODEL_ROUTER_CLAUDE_CONFIG_DIR"):
        resolve_config_dir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "ambient"))
    assert resolve_profile() == (tmp_path / "ambient", "CLAUDE_CONFIG_DIR")
    write_config(claude_config_dir=str(tmp_path / "installed"))
    assert resolve_profile()[0] == tmp_path / "installed"  # config.json beats the ambient variable
    monkeypatch.setenv("MODEL_ROUTER_CLAUDE_CONFIG_DIR", str(tmp_path))
    assert resolve_config_dir() == tmp_path
    with pytest.raises(ClientUnavailableError, match="no encuentro"):
        CliClient(binary="claude-no-existe")
