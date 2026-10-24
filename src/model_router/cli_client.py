"""Claude through the `claude` CLI in headless mode (`claude -p`), on the Claude.ai subscription of the profile
in use. Each completion is one short-lived CLI process with a
fresh context, the cell's model and effort, no MCP servers, a minimal system prompt (without `--system-prompt`
the CLI sends its own prompt of about 100K tokens on every call) and no tools unless the caller names some.

What differs from the API backend (`client.LiveClient`):

* `max_tokens` is not a CLI option; the CLI manages output length itself.
* No server-side refusal fallbacks and no refusal category; a refusal is only what the CLI reports
  as `stop_reason == "refusal"`.
* Structured outputs go through `--json-schema` with the Pydantic model's JSON schema; the CLI
  validates and returns `structured_output`.
* Dollar amounts priced from its usage are list-price equivalents: a subscription is billed by quota, not
  per token, so read them as a proxy for quota consumption.

Profile: the subprocess runs on the Claude profile in use ($CLAUDE_CONFIG_DIR, else ~/.claude), the one whose
folder holds the router's state, unless the caller names another. CLAUDECODE is dropped from the environment so
the CLI does not think it is nested in a session.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any  # Any: the JSON that `claude -p` prints

from pydantic import BaseModel, ValidationError

from . import limits
from .catalog import MODELS, Cell, Usage
from .client import NON_STREAMING_MAX_TOKENS, ClientUnavailableError, Completion
from .paths import NO_MCP, child_env, claude_profile, is_child

DEFAULT_SYSTEM = "Do the task in the message carefully and answer with only what was asked for."
TIMEOUT_SECONDS = 900.0  # a long answer at high effort takes minutes; past this the call is taken for lost
NOT_LOGGED_IN = "not logged in"
ERROR_DETAIL_CHARS = 300  # how much of the CLI's stderr/stdout an error message carries
RATE_LIMITED = 429


def _served_model(model_usage: Any, requested: str) -> str:
    """Return the model that answered according to `modelUsage`: the requested one when it is listed, else
    the one with the most output tokens (a fallback), else the requested one.
    """
    if not isinstance(model_usage, dict) or not model_usage or requested in model_usage:
        return requested

    def output_tokens(name: str) -> int:
        usage = model_usage.get(name)
        return int(usage.get("outputTokens") or 0) if isinstance(usage, dict) else 0

    return str(max(model_usage, key=output_tokens))


class CliClient:
    """Completions through `claude -p`. Satisfies `client.ClaudeClient`.

    Raises:
        ClientUnavailableError: the `claude` executable is not on the PATH.
    """

    def __init__(
        self,
        *,
        binary: str = "claude",
        config_dir: Path | None = None,
        timeout: float = TIMEOUT_SECONDS,
        extra_args: Sequence[str] = (),
        tools: str = "",
        cwd: Path | None = None,
    ) -> None:
        path = shutil.which(binary)
        if path is None:
            raise ClientUnavailableError(f"cannot find the executable {binary!r}; install Claude Code")
        self.binary = path
        self.config_dir = config_dir or claude_profile()
        self.timeout = timeout
        # For a caller that reviews a repository: built-in tools the call may use ("Read,Grep,Glob"; none by
        # default, and none pre-approved, so they only read inside `cwd`), where it runs, and any other flags.
        self.extra_args = list(extra_args)
        self.tools = tools
        self.cwd = cwd

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
            "--tools", self.tools, "--no-session-persistence", "--strict-mcp-config", "--mcp-config", NO_MCP,
            "--system-prompt", system or DEFAULT_SYSTEM, *self.extra_args,
        ]
        if output_format is not None:
            args += ["--json-schema", json.dumps(output_format.model_json_schema())]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(args, input=prompt, capture_output=True, text=True, env=child_env(self.config_dir),
                                  timeout=self.timeout, check=False, cwd=self.cwd)
        except subprocess.TimeoutExpired as exc:
            raise ClientUnavailableError(f"claude -p exceeded {self.timeout:.0f}s") from exc
        seconds = time.monotonic() - t0
        detail = (proc.stderr or proc.stdout).strip()[:ERROR_DETAIL_CHARS]
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ClientUnavailableError(f"claude -p exited with code {proc.returncode} and no JSON object: {detail}")
        if proc.returncode != 0 and not data.get("is_error"):
            raise ClientUnavailableError(f"claude -p exited with code {proc.returncode}: {detail}")
        return self.to_completion(data, cell, output_format=output_format, seconds=seconds)

    def _note_limit(self, cell: Cell, text: str) -> None:
        """Keep the usage limit a refused call hit, with its exact reset (`limits.py`). The next plans skip that
        model until then.
        """
        if is_child():
            return  # a session the router started: whoever started it learns the limit
        if self.config_dir.resolve() != claude_profile().resolve():
            return  # another profile's account: its own sessions learn its limits, in its own folder
        models = {spec.short: spec.id for spec in MODELS.values() if spec.claude}
        with contextlib.suppress(OSError, ValueError):  # learning the reset must never hide the refusal itself
            limits.learn(text or cell.spec.short, models=models, config_dir=self.config_dir, binary=self.binary)

    def to_completion(self, data: dict[str, Any], cell: Cell, *, output_format: type[BaseModel] | None,
                      seconds: float) -> Completion:
        """Turn the JSON `claude -p` printed for `cell` into a Completion. A usage limit it reports is kept with its
        exact reset (`limits.py`) before the call fails.

        Raises:
            ClientUnavailableError: the CLI reported an error, a profile not logged in or a spent usage limit.
        """
        text = str(data.get("result") or "")
        if data.get("is_error"):
            if NOT_LOGGED_IN in text.lower():
                raise ClientUnavailableError(
                    f"profile {self.config_dir} is not logged in: open claude with that profile and run /login")
            if data.get("api_error_status") == RATE_LIMITED:
                self._note_limit(cell, text)
            raise ClientUnavailableError(f"claude -p: {text[:ERROR_DETAIL_CHARS] or 'error without details'}")
        u = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_tokens=int(u.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(u.get("cache_creation_input_tokens") or 0),
        )
        served = _served_model(data.get("modelUsage"), cell.model)
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
