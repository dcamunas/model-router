"""Models of other providers: judges, and workers when `router config --models` lists them. Three doors:
the chat-completions API (OpenAI, DeepSeek and xAI serve the same one, so one small client covers them,
the key from the provider's environment variable), the Codex CLI (`codex exec`, ChatGPT by subscription,
on a Codex home the router is given explicitly) and the Gemini CLI (`gemini -p`, Google by its own
sign-in or GEMINI_API_KEY). A structured verdict is asked for as a JSON object and validated here; a CLI
answer that was meant to be JSON and is not gets one more try, then the verifier sees a judge error.
`MultiClient` sends every Anthropic cell to the usual backend and every other cell to its door, built on
first use, so the router, the policy and the ledger never know the difference. No SDK: a POST or a
subprocess with the standard library.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .catalog import PROVIDER_EFFORTS, Cell, Usage
from .cli_client import DEFAULT_SYSTEM
from .client import NON_STREAMING_MAX_TOKENS, ClaudeClient, ClientUnavailableError, Completion
from .paths import CODEX_HOME_ENV, config_path, read_config
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
    "openai": Provider("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "max_completion_tokens",
                       "reasoning_effort", PROVIDER_EFFORTS["openai"]),
    "codex": Provider("codex", "", "", "max_completion_tokens", "model_reasoning_effort", PROVIDER_EFFORTS["codex"],
                      transport="codex"),
    "deepseek": Provider("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "max_tokens", None, ()),
    "xai": Provider("xai", "https://api.x.ai/v1", "XAI_API_KEY", "max_tokens", "reasoning_effort", PROVIDER_EFFORTS["xai"]),
    "gemini": Provider("gemini", "", "GEMINI_API_KEY", "max_tokens", None, (), transport="gemini"),
}
EFFORT_TO_REASONING: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
FINISH_TO_STOP: dict[str, str] = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
TIMEOUT_SECONDS = 300.0
CLI_TIMEOUT_SECONDS = 900.0
ERROR_DETAIL_CHARS = 300
SCHEMA_NOTE = "\n\nAnswer with one JSON object and nothing else; it must validate against this JSON schema:\n"
Opener = Callable[..., Any]  # urllib.request.urlopen or a test double with the same shape
Run = Callable[..., Any]  # subprocess.run or a test double that returns a CompletedProcess


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


class OpenAICompatibleClient:
    """Completions through a provider's chat-completions endpoint. Satisfies `client.ClaudeClient`."""

    def __init__(
        self, provider: Provider, *, api_key: str | None = None, base_url: str | None = None,
        timeout: float = TIMEOUT_SECONDS, opener: Opener = urllib.request.urlopen,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = (base_url or provider.base_url).rstrip("/")
        self.timeout = timeout
        self.opener = opener

    def complete(
        self, cell: Cell, *, prompt: str, system: str | None = None, max_tokens: int = NON_STREAMING_MAX_TOKENS,
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        key = self.api_key or os.environ.get(self.provider.key_env)
        if not key:
            raise ClientUnavailableError(f"{self.provider.key_env} is not set: a {self.provider.name} cell needs it")
        instructions = system or DEFAULT_SYSTEM
        body: dict[str, Any] = {"model": cell.spec.id, self.provider.max_tokens_field: max_tokens}
        if output_format is not None:
            body["response_format"] = {"type": "json_object"}
            instructions += SCHEMA_NOTE + json.dumps(output_format.model_json_schema())
        body["messages"] = [{"role": "system", "content": instructions}, {"role": "user", "content": prompt}]
        effort = reasoning_effort(cell.effort, self.provider.efforts)
        if self.provider.reasoning_field and effort:
            body[self.provider.reasoning_field] = effort
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        t0 = time.monotonic()
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:ERROR_DETAIL_CHARS]
            raise ClientUnavailableError(f"{self.provider.name} answered HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ClientUnavailableError(f"{self.provider.name} unreachable: {exc}") from exc
        seconds = time.monotonic() - t0
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ClientUnavailableError(f"{self.provider.name} answered no JSON: {raw[:ERROR_DETAIL_CHARS]!r}") from exc
        return self.to_completion(data, cell, output_format, seconds)

    def to_completion(self, data: Any, cell: Cell, output_format: type[BaseModel] | None, seconds: float) -> Completion:
        """Turn a chat-completions response into a Completion; a structured answer that does not validate
        leaves `parsed` None, which the verifier reports as a judge error.
        """
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ClientUnavailableError(f"{self.provider.name} answered without choices: {str(data)[:ERROR_DETAIL_CHARS]}")
        choice = choices[0]
        message = _dict(choice.get("message"))
        text = str(message.get("content") or "")
        finish = str(choice.get("finish_reason") or "stop")
        stop = FINISH_TO_STOP.get(finish, finish)
        u = _dict(data.get("usage"))
        details = _dict(u.get("prompt_tokens_details"))
        cached = int(details.get("cached_tokens") or 0)
        usage = Usage(input_tokens=max(0, int(u.get("prompt_tokens") or 0) - cached),
                      output_tokens=int(u.get("completion_tokens") or 0), cache_read_tokens=cached)
        parsed = _structured(text, output_format)
        refused = stop == "refusal"
        return Completion(text=text, usage=usage, stop_reason=stop, model=str(data.get("model") or cell.model),
                          parsed=None if refused else parsed, request_id=data.get("id"), refused=refused, seconds=seconds)


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
    if isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"])
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
    Each call runs in an empty directory, read-only sandbox, ephemeral session, user config ignored, on the Codex
    home the router was given.
    """

    def __init__(self, home: Path, *, binary: str = "codex", timeout: float = CLI_TIMEOUT_SECONDS,
                 run: Run = subprocess.run) -> None:
        self.binary = _find(binary, "install the Codex CLI")
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
                    "--color", "never", "-s", "read-only", "-C", scratch, "-m", cell.spec.id,
                    "-c", f'model_reasoning_effort="{effort}"', "-o", str(last)]
            if output_format is not None:
                schema = Path(scratch) / "schema.json"
                schema.write_text(json.dumps(strict_schema(output_format.model_json_schema())), encoding="utf-8")
                args += ["--output-schema", str(schema)]
            args.append("-")
            env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
            env["CODEX_HOME"] = str(self.home)
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
                detail = (errors[0] if errors else (proc.stderr or proc.stdout or "").strip())[:ERROR_DETAIL_CHARS]
                raise ClientUnavailableError(f"codex exec exited with code {proc.returncode}: {detail}")
            text = (last.read_text(encoding="utf-8").strip() if last.exists() else "") or _last_message(events)
            if not text and errors:  # an error event with no answer behind it; with an answer it was transient
                raise ClientUnavailableError(f"codex exec: {errors[0][:ERROR_DETAIL_CHARS]}")
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


class GeminiClient:
    """Completions through `gemini -p`: Google by the CLI's own sign-in or GEMINI_API_KEY, on the machine's own
    Gemini settings (the CLI has no home variable to isolate, so unlike Codex this door runs on whatever
    `~/.gemini` holds). Satisfies `client.ClaudeClient`. The instructions and the prompt go on stdin, `-p`
    only asks for the answer, so nothing long sits in the command line; `-o json` gives the response and the
    token counts; `--approval-mode plan` keeps the tools read-only. Each call runs in an empty directory.
    """

    def __init__(self, *, binary: str = "gemini", timeout: float = CLI_TIMEOUT_SECONDS, run: Run = subprocess.run) -> None:
        self.binary = _find(binary, "install the Gemini CLI")
        self.timeout = timeout
        self.run = run

    def complete(
        self, cell: Cell, *, prompt: str, system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,  # noqa: ARG002 - the CLI manages output length itself
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        text, usage, seconds, served = self._exec(cell, prompt, system, output_format)
        parsed = _structured(text, output_format)
        if output_format is not None and parsed is None:
            text, more, extra, served = self._exec(cell, prompt, system, output_format)
            usage, seconds, parsed = usage + more, seconds + extra, _structured(text, output_format)
        return Completion(text=text, usage=usage, stop_reason="end_turn", model=served or cell.model, parsed=parsed,
                          seconds=seconds)

    def _exec(self, cell: Cell, prompt: str, system: str | None, output_format: type[BaseModel] | None,
              ) -> tuple[str, Usage, float, str | None]:
        instructions = system or DEFAULT_SYSTEM
        if output_format is not None:
            instructions += SCHEMA_NOTE + json.dumps(output_format.model_json_schema())
        with tempfile.TemporaryDirectory(prefix="router-gemini-") as scratch:
            args = [self.binary, "-p", "Answer the task above.", "-m", cell.spec.id, "-o", "json", "--approval-mode", "plan"]
            t0 = time.monotonic()
            try:
                proc = self.run(args, input=f"{instructions}\n\n{prompt}", capture_output=True, text=True, cwd=scratch,
                                timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise ClientUnavailableError(f"gemini exceeded {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise ClientUnavailableError(f"gemini could not start: {exc}") from exc
        seconds = time.monotonic() - t0
        try:
            data = json.loads(proc.stdout or "")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            detail = (proc.stderr or proc.stdout or "").strip()[:ERROR_DETAIL_CHARS]
            raise ClientUnavailableError(f"gemini exited with code {proc.returncode} and no JSON object: {detail}")
        if data.get("error") or proc.returncode != 0:
            error = _dict(data.get("error"))
            detail = str(error.get("message") or error or data)[:ERROR_DETAIL_CHARS]
            raise ClientUnavailableError(f"gemini exited with code {proc.returncode}: {detail}")
        usage, served = _gemini_usage(_dict(_dict(data.get("stats")).get("models")))
        return str(data.get("response") or ""), usage, seconds, served


def _gemini_usage(models: dict[str, Any]) -> tuple[Usage, str | None]:
    """Sum the token counts `gemini -o json` reports per model; the model with the most output is the one served."""
    usage, served, most = Usage(), None, -1
    for name, entry in models.items():
        tokens = _dict(_dict(entry).get("tokens"))
        cached = int(tokens.get("cached") or 0)
        out = int(tokens.get("candidates") or 0)
        usage = usage + Usage(input_tokens=max(0, int(tokens.get("prompt") or 0) - cached), output_tokens=out,
                              cache_read_tokens=cached)
        if out > most:
            served, most = str(name), out
    return usage, served


def build_client(provider: Provider) -> ClaudeClient:
    """Return the client for `provider`'s door: the Codex CLI on the configured home, the Gemini CLI, or the API."""
    if provider.transport == "codex":
        return CodexClient(resolve_codex_home()[0])
    if provider.transport == "gemini":
        return GeminiClient()
    return OpenAICompatibleClient(provider)


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
