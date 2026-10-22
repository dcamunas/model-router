"""The tribunal's door to Codex: ChatGPT through `codex exec`, by subscription, on a Codex home given explicitly.
The router itself works with Claude models only; this door exists for the tribunal plugin's second judge.

Every call runs in an empty directory, read-only, with nothing persisted and Codex's shell tool turned off:
with a shell, the read-only sandbox still reads any file the user can read, and what it reads goes to OpenAI.
Without it Codex has no way to open a file, so it sees only what the prompt carries. A structured verdict is
asked for with `--output-schema`, validated here, and tried once more when it does not come back as JSON.
`MultiClient` sends Anthropic cells to the usual backend and Codex cells to this door.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .catalog import PROVIDER_EFFORTS, Cell, Usage
from .cli_client import DEFAULT_SYSTEM
from .client import NON_STREAMING_MAX_TOKENS, ClaudeClient, ClientUnavailableError, Completion
from .paths import CODEX_HOME_ENV, TRIBUNAL_ENV, config_path, read_config
from .task import json_payload


@dataclass(frozen=True)
class Provider:
    """How to talk to one door: its transport (`api`, `codex` or `gemini`), where and with which key for the API,
    and the request fields that differ between providers.
    """

    name: str
    base_url: str
    key_env: str
    max_tokens_field: str  # OpenAI's reasoning models want `max_completion_tokens`; the others `max_tokens`
    reasoning_field: str | None  # the request field (or Codex config key) for the effort, when there is one
    efforts: tuple[str, ...]  # the values it accepts, cheapest first
    transport: str = "api"


PROVIDERS: dict[str, Provider] = {
    "codex": Provider("codex", "", "", "max_completion_tokens", "model_reasoning_effort", PROVIDER_EFFORTS["codex"],
                      transport="codex"),
}
EFFORT_TO_REASONING: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
FINISH_TO_STOP: dict[str, str] = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
TIMEOUT_SECONDS = 300.0
CLI_TIMEOUT_SECONDS = 900.0
ERROR_DETAIL_CHARS = 300
SCHEMA_NOTE = "\n\nAnswer with one JSON object and nothing else; it must validate against this JSON schema:\n"
Opener = Callable[..., Any]  # urllib.request.urlopen or a test double with the same shape
Run = Callable[..., Any]  # subprocess.run or a test double that returns a CompletedProcess
# Kept from `codex exec`: the home decides the account, and an API key or base URL in the environment would move the
# call to API billing, maybe on another account.
CODEX_ENV_DROPPED = ("CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")


def reasoning_effort(effort: str, accepted: tuple[str, ...]) -> str | None:
    """Map a cell's effort to what the provider accepts: the same word when it does, else the highest accepted
    value at or below it, else the lowest it accepts; None when the provider has no such field.
    """
    if not accepted:
        return None
    order = ("low", "medium", "high", "xhigh")
    want = EFFORT_TO_REASONING[effort]
    for candidate in reversed(order[: order.index(want) + 1]):
        if candidate in accepted:
            return candidate
    return accepted[0]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` as OpenAI's strict structured output wants it: every object closed
    (`additionalProperties: false`) with every property required, `$defs` included. Pydantic leaves a field with a
    default out of `required`, and Codex rejects the schema for that.
    """
    out = dict(schema)
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["properties"] = {k: strict_schema(v) if isinstance(v, dict) else v for k, v in out["properties"].items()}
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    for key in ("items", "additionalProperties"):
        if isinstance(out.get(key), dict):
            out[key] = strict_schema(out[key])
    if isinstance(out.get("prefixItems"), list):
        out["prefixItems"] = [strict_schema(v) if isinstance(v, dict) else v for v in out["prefixItems"]]
    for key in ("$defs", "definitions"):
        if isinstance(out.get(key), dict):
            out[key] = {k: strict_schema(v) if isinstance(v, dict) else v for k, v in out[key].items()}
    for key in ("anyOf", "oneOf", "allOf"):
        if isinstance(out.get(key), list):
            out[key] = [strict_schema(v) if isinstance(v, dict) else v for v in out[key]]
    return out


def _structured(text: str, output_format: type[BaseModel] | None) -> BaseModel | None:
    """Parse `text` into `output_format` (the first fenced block, else the whole text); None when it does not
    validate, which the verifier reports as a judge error.
    """
    if output_format is None or not text:
        return None
    try:
        return output_format.model_validate(json_payload(text))
    except (ValueError, ValidationError):
        return None


def resolve_codex_home() -> tuple[Path, str]:
    """Return the Codex home (the ChatGPT account) `codex` cells run on and where it came from: the
    MODEL_ROUTER_CODEX_HOME variable, then `codex_home` in config.json (`router config --codex-home`). Nothing
    else: the ambient ~/.codex may be another account, and the router must never land on it by accident.

    Raises:
        ClientUnavailableError: neither names one.
    """
    value = os.environ.get(CODEX_HOME_ENV)
    if value:
        return Path(value).expanduser(), CODEX_HOME_ENV
    value = read_config().get("codex_home")
    if value:
        return Path(str(value)).expanduser(), str(config_path())
    raise ClientUnavailableError(
        f"Codex home not set: run `router config --codex-home ~/.codex-personal` (a home signed in with the ChatGPT "
        f"account the router may use) or export {CODEX_HOME_ENV}"
    )


def _find(binary: str, hint: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise ClientUnavailableError(f"cannot find the executable {binary!r}; {hint}")
    return path


def _events(stdout: str) -> list[dict[str, Any]]:
    """Parse the JSONL events `codex exec --json` prints; lines that are not JSON objects are skipped."""
    out: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


class CodexClient:
    """Completions through `codex exec`: ChatGPT by subscription. Satisfies `client.ClaudeClient`. The prompt goes
    on stdin with the instructions in front (the CLI has no system prompt); a structured answer is asked for with
    `--output-schema` and validated here; `-o` gives the final message and the `turn.completed` event the usage.
    Each call runs in an empty directory, read-only sandbox, no shell, ephemeral session, user config ignored, on the Codex
    home the router was given.
    """

    def __init__(self, home: Path, *, binary: str = "codex", timeout: float = CLI_TIMEOUT_SECONDS,
                 run: Run = subprocess.run) -> None:
        self.binary = _find(binary, "install the Codex CLI")
        home = home.expanduser().resolve()  # the child runs elsewhere: a relative home would point somewhere else there
        if not home.is_dir():  # a typo must not make codex start a fresh home there
            raise ClientUnavailableError(f"Codex home {home} does not exist: sign in there first "
                                         f"(`CODEX_HOME={home} codex login`)")
        self.home = home
        self.timeout = timeout
        self.run = run

    def complete(
        self, cell: Cell, *, prompt: str, system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,  # noqa: ARG002 - the CLI manages output length itself
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        text, usage, seconds = self._exec(cell, prompt, system, output_format)
        parsed = _structured(text, output_format)
        if output_format is not None and parsed is None:  # once more: the CLI does not always honour the schema
            text, more, extra = self._exec(cell, prompt, system, output_format)
            usage, seconds, parsed = usage + more, seconds + extra, _structured(text, output_format)
        return Completion(text=text, usage=usage, stop_reason="end_turn", model=cell.model, parsed=parsed, seconds=seconds)

    def _exec(self, cell: Cell, prompt: str, system: str | None, output_format: type[BaseModel] | None,
              ) -> tuple[str, Usage, float]:
        effort = reasoning_effort(cell.effort, PROVIDERS["codex"].efforts)
        with tempfile.TemporaryDirectory(prefix="router-codex-") as scratch:
            last = Path(scratch) / "last-message.md"
            args = [self.binary, "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                    "--color", "never", "-s", "read-only", "--disable", "shell_tool", "-C", scratch, "-m", cell.spec.id,
                    "-c", f'model_reasoning_effort="{effort}"', "-o", str(last)]
            if output_format is not None:
                schema = Path(scratch) / "schema.json"
                schema.write_text(json.dumps(strict_schema(output_format.model_json_schema())), encoding="utf-8")
                args += ["--output-schema", str(schema)]
            args.append("-")
            env = {k: v for k, v in os.environ.items() if k not in CODEX_ENV_DROPPED}
            env["CODEX_HOME"] = str(self.home)
            env[TRIBUNAL_ENV] = "0"
            t0 = time.monotonic()
            try:
                proc = self.run(args, input=f"{system or DEFAULT_SYSTEM}\n\n{prompt}", capture_output=True, text=True,
                                env=env, cwd=scratch, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ClientUnavailableError(f"codex exec exceeded {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise ClientUnavailableError(f"codex exec could not start: {exc}") from exc
            seconds = time.monotonic() - t0
            events = _events(proc.stdout or "")
            errors = _codex_errors(events)
            if proc.returncode != 0:
                # never stdout: it carries the answer, and this text ends in the ledger
                detail = (errors[0] if errors else (proc.stderr or "").strip() or "no error message")[:ERROR_DETAIL_CHARS]
                raise ClientUnavailableError(f"codex exec exited with code {proc.returncode}: {detail}")
            text = (last.read_text(encoding="utf-8").strip() if last.exists() else "") or _last_message(events)
            if not text:  # an error event with no answer behind it (with an answer it was transient), or no answer at all
                detail = errors[0][:ERROR_DETAIL_CHARS] if errors else "no final message and no error event"
                raise ClientUnavailableError(f"codex exec gave no answer: {detail}")
        return text, _codex_usage(events), seconds


def _codex_errors(events: list[dict[str, Any]]) -> list[str]:
    """Return the messages of the `error` and `turn.failed` events among `events`."""
    out = []
    for event in events:
        if event.get("type") in ("error", "turn.failed"):
            error = event.get("error")
            message = _dict(error).get("message") if isinstance(error, dict) else error
            out.append(str(message or event.get("message") or event))
    return out


def _last_message(events: list[dict[str, Any]]) -> str:
    """Return the text of the last `agent_message` item among `events`, or an empty string."""
    text = ""
    for event in events:
        item = _dict(event.get("item"))
        if event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = str(item.get("text") or "")
    return text


def _codex_usage(events: list[dict[str, Any]]) -> Usage:
    """Sum the usage of every `turn.completed` event: input tokens net of the cached ones, which are kept apart."""
    usage = Usage()
    for event in events:
        if event.get("type") != "turn.completed":
            continue
        u = _dict(event.get("usage"))
        cached = int(u.get("cached_input_tokens") or 0)
        usage = usage + Usage(input_tokens=max(0, int(u.get("input_tokens") or 0) - cached),
                              output_tokens=int(u.get("output_tokens") or 0), cache_read_tokens=cached)
    return usage


def build_client(provider: Provider) -> ClaudeClient:  # noqa: ARG001 - one door today; the table names it
    """Return the client for a non-Anthropic door: the Codex CLI on the configured home."""
    return CodexClient(resolve_codex_home()[0])


class MultiClient:
    """Sends each cell to its door: Anthropic cells to `anthropic` (the api or cli backend), the rest to the client
    `build_client` makes on first use, so a missing key or home only fails when that provider is asked.
    """

    def __init__(self, anthropic: ClaudeClient, *, factory: Callable[[Provider], ClaudeClient] | None = None) -> None:
        self.anthropic = anthropic
        self.factory = factory or build_client
        self._clients: dict[str, ClaudeClient] = {}

    def complete(
        self, cell: Cell, *, prompt: str, system: str | None = None, max_tokens: int = NON_STREAMING_MAX_TOKENS,
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        name = cell.spec.provider
        if name == "anthropic":
            client = self.anthropic
        else:
            provider = PROVIDERS.get(name)
            if provider is None:
                raise ClientUnavailableError(f"no backend for provider {name!r}; known: {', '.join(PROVIDERS)}")
            if name not in self._clients:
                self._clients[name] = self.factory(provider)
            client = self._clients[name]
        return client.complete(cell, prompt=prompt, system=system, max_tokens=max_tokens, output_format=output_format)
