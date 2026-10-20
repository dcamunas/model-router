"""Judges from other providers. OpenAI, DeepSeek and xAI serve the same chat-completions API, so one
small client covers the three: the model id, the prices and the short name come from config.json
(`router config --external`), the API key from the provider's environment variable, and the request
asks for a JSON object when a structured verdict is wanted. `MultiClient` sends every Anthropic cell
to the usual backend and every other cell here, so the router, the policy and the ledger never know
the difference. No SDK: a POST with the standard library.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from .catalog import Cell, Usage
from .cli_client import DEFAULT_SYSTEM
from .client import NON_STREAMING_MAX_TOKENS, ClaudeClient, ClientUnavailableError, Completion
from .task import json_payload


@dataclass(frozen=True)
class Provider:
    """How to talk to one provider: where, with which key, and the two request fields that differ between them."""

    name: str
    base_url: str
    key_env: str
    max_tokens_field: str  # OpenAI's reasoning models want `max_completion_tokens`; the others `max_tokens`
    reasoning_field: str | None  # the request field for the effort, when the provider has one
    efforts: tuple[str, ...]  # the values that field accepts, cheapest first


PROVIDERS: dict[str, Provider] = {
    "openai": Provider("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "max_completion_tokens",
                       "reasoning_effort", ("low", "medium", "high")),
    "deepseek": Provider("deepseek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "max_tokens", None, ()),
    "xai": Provider("xai", "https://api.x.ai/v1", "XAI_API_KEY", "max_tokens", "reasoning_effort", ("low", "high")),
}
EFFORT_TO_REASONING: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}
FINISH_TO_STOP: dict[str, str] = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
TIMEOUT_SECONDS = 300.0
ERROR_DETAIL_CHARS = 300
Opener = Callable[..., Any]  # urllib.request.urlopen or a test double with the same shape


def reasoning_effort(effort: str, accepted: tuple[str, ...]) -> str | None:
    """Map a cell's effort to what the provider accepts: the same word when it does, else the highest accepted
    value at or below it, else the lowest it accepts; None when the provider has no such field.
    """
    if not accepted:
        return None
    order = ("low", "medium", "high")
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
            instructions += ("\n\nAnswer with one JSON object and nothing else; it must validate against this JSON "
                             "schema:\n" + json.dumps(output_format.model_json_schema()))
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
        parsed = None
        if output_format is not None and text:
            try:
                parsed = output_format.model_validate(json_payload(text))
            except (ValueError, ValidationError):
                parsed = None
        refused = stop == "refusal"
        return Completion(text=text, usage=usage, stop_reason=stop, model=str(data.get("model") or cell.model),
                          parsed=None if refused else parsed, request_id=data.get("id"), refused=refused, seconds=seconds)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class MultiClient:
    """Sends each cell to its provider: Anthropic cells to `anthropic` (the api or cli backend), the rest to an
    OpenAI-compatible client built on first use, so a missing key only fails when that provider is asked.
    """

    def __init__(self, anthropic: ClaudeClient, *, factory: Callable[[Provider], ClaudeClient] | None = None) -> None:
        self.anthropic = anthropic
        self.factory = factory or OpenAICompatibleClient
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
