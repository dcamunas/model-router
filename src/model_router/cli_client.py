"""Backend without an API key: Claude through the `claude` CLI in headless mode (`claude -p`), so
the router runs on a Claude.ai subscription. Each completion is one short-lived CLI process with a
fresh context, the cell's model and effort, no tools, no MCP servers and a minimal system prompt
(without `--system-prompt` the CLI sends its own prompt of about 100K tokens on every call).

What differs from the API backend (`client.LiveClient`):

* `max_tokens` is not a CLI option; the CLI manages output length itself.
* No server-side refusal fallbacks and no refusal category; a refusal is only what the CLI reports
  as `stop_reason == "refusal"`.
* Structured outputs go through `--json-schema` with the Pydantic model's JSON schema; the CLI
  validates and returns `structured_output`.
* Dollar amounts in the ledger are list-price equivalents: a subscription is billed by quota, not
  per token, so read them as a proxy for quota consumption.

Profile: the subprocess runs on the Claude profile named by MODEL_ROUTER_CLAUDE_CONFIG_DIR, else the
one recorded by `router install`, else CLAUDE_CONFIG_DIR, else it refuses to start, so the router
never uses another profile by accident.
CLAUDECODE is dropped from the environment so the CLI does not think it is nested in a session.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .catalog import Cell, Usage
from .client import NON_STREAMING_MAX_TOKENS, ClientUnavailableError, Completion
from .paths import CONFIG_DIR_ENV, config_path, read_config

DEFAULT_SYSTEM = "Haz la tarea del mensaje con cuidado y responde solo con lo pedido."
NO_MCP = '{"mcpServers":{}}'
NOT_LOGGED_IN = "not logged in"
ERROR_DETAIL_CHARS = 300  # how much of the CLI's stderr/stdout an error message carries


def resolve_profile() -> tuple[Path, str]:
    """Return the Claude profile directory for the cli backend and where it came from: the
    MODEL_ROUTER_CLAUDE_CONFIG_DIR variable, then `config.json` (written by `router install`), then the
    ambient CLAUDE_CONFIG_DIR. Nothing else: the router must never land on another profile by accident.
    """
    value = os.environ.get(CONFIG_DIR_ENV)
    if value:
        return Path(value).expanduser(), CONFIG_DIR_ENV
    value = read_config().get("claude_config_dir")
    if value:
        return Path(str(value)).expanduser(), str(config_path())
    value = os.environ.get("CLAUDE_CONFIG_DIR")
    if value:
        return Path(value).expanduser(), "CLAUDE_CONFIG_DIR"
    raise ClientUnavailableError(
        f"perfil de Claude sin fijar: ejecuta `router install` en el perfil que debe usar el router o exporta "
        f"{CONFIG_DIR_ENV}, por ejemplo ~/.claude-personal"
    )


def resolve_config_dir() -> Path:
    return resolve_profile()[0]


class CliClient:
    """Completions through `claude -p`. Satisfies `client.ClaudeClient`."""

    def __init__(
        self,
        *,
        binary: str = "claude",
        config_dir: Path | None = None,
        timeout: float = 900.0,
        extra_args: Sequence[str] = (),
    ) -> None:
        path = shutil.which(binary)
        if path is None:
            raise ClientUnavailableError(f"no encuentro el ejecutable {binary!r}; instala Claude Code o pasa --backend api")
        self.binary = path
        self.config_dir = config_dir or resolve_config_dir()
        self.timeout = timeout
        self.extra_args = list(extra_args)

    def complete(
        self,
        cell: Cell,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,  # noqa: ARG002 - the CLI has no such option
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        args = [
            self.binary, "-p", "--model", cell.model, "--effort", cell.effort, "--output-format", "json",
            "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--mcp-config", NO_MCP,
            "--system-prompt", system or DEFAULT_SYSTEM, *self.extra_args,
        ]
        if output_format is not None:
            args += ["--json-schema", json.dumps(output_format.model_json_schema())]
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env.setdefault("ENABLE_CLAUDEAI_MCP_SERVERS", "false")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(args, input=prompt, capture_output=True, text=True, env=env, timeout=self.timeout,
                                  check=False)
        except subprocess.TimeoutExpired as exc:
            raise ClientUnavailableError(f"claude -p supero los {self.timeout:.0f}s") from exc
        seconds = time.monotonic() - t0
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            detail = (proc.stderr or proc.stdout).strip()[:ERROR_DETAIL_CHARS]
            raise ClientUnavailableError(f"claude -p termino con codigo {proc.returncode} sin JSON: {detail}") from exc
        return self.to_completion(data, cell, output_format, seconds)

    def to_completion(self, data: dict[str, Any], cell: Cell, output_format: type[BaseModel] | None,
                      seconds: float) -> Completion:
        text = str(data.get("result") or "")
        if data.get("is_error"):
            if NOT_LOGGED_IN in text.lower():
                raise ClientUnavailableError(
                    f"el perfil {self.config_dir} no tiene sesion iniciada: abre claude con ese perfil y haz /login")
            raise ClientUnavailableError(f"claude -p: {text[:ERROR_DETAIL_CHARS] or 'error sin detalle'}")
        u = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )
        model_usage = data.get("modelUsage") or {}
        served = next(iter(model_usage), None) or cell.model
        parsed = None
        structured = data.get("structured_output")
        if output_format is not None and structured is not None:
            try:
                parsed = output_format.model_validate(structured)
            except ValidationError:
                parsed = None
        stop = str(data.get("stop_reason") or "end_turn")
        if parsed is not None and stop == "tool_use":  # the CLI implements structured output as a tool call
            stop = "end_turn"
        refused = stop == "refusal"
        return Completion(
            text=text, usage=usage, stop_reason=stop, model=served, parsed=None if refused else parsed,
            request_id=data.get("session_id"), refused=refused, refusal_category=None,
            fallback_ran=served != cell.model, seconds=seconds,
        )
