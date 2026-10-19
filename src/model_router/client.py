"""The one place that talks to the Claude API. Everything else sees a `ClaudeClient` that takes a
cell and a prompt and returns a `Completion` with real usage, so tests and the simulator can swap
in a fake."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from .catalog import MODELS, Cell, Usage

FALLBACK_BETA = "server-side-fallback-2026-07-01"
NON_STREAMING_MAX_TOKENS = 16_000


@dataclass
class Completion:
    text: str
    usage: Usage
    stop_reason: str
    model: str  # the model that actually served the request (a fallback may differ from the cell)
    parsed: Any = None
    request_id: str | None = None
    refused: bool = False
    refusal_category: str | None = None
    fallback_ran: bool = False
    seconds: float = 0.0


class ClaudeClient(Protocol):
    def complete(
        self,
        cell: Cell,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,
        output_format: type[BaseModel] | None = None,
    ) -> Completion: ...


class ClientUnavailable(RuntimeError):
    """The API could not serve the request after the SDK's own retries (rate limit, 5xx, network)."""


class LiveClient:
    """Real calls through the official SDK.

    * `output_config.effort` carries the cell's effort; `thinking` is omitted on purpose (adaptive
      is the default on every model in the catalogue and `disabled` is rejected on the 5.5 family).
    * Server-side refusal fallbacks (`fallbacks="default"`) are on by default for the models that
      accept them, so a safety-classifier decline is re-run on Anthropic's recommended substitute
      inside the same call. Haiku has no fallback; a decline there comes back as `refused`.
    * Structured outputs go through `parse`; outputs above NON_STREAMING_MAX_TOKENS stream.
    """

    def __init__(self, client: Any = None, *, server_fallbacks: bool = True, timeout: float = 600.0,
                 max_retries: int = 2) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = client or anthropic.Anthropic(timeout=timeout, max_retries=max_retries)
        self.server_fallbacks = server_fallbacks

    def complete(
        self,
        cell: Cell,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        spec = MODELS[cell.model]
        kwargs: dict[str, Any] = {
            "model": cell.model,
            "max_tokens": min(max_tokens, spec.max_output),
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": cell.effort},
        }
        if system:
            kwargs["system"] = system
        use_beta = self.server_fallbacks and spec.server_fallbacks
        if use_beta:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        api = self._client.beta.messages if use_beta else self._client.messages
        a = self._anthropic
        t0 = time.monotonic()
        try:
            if output_format is not None:
                msg = api.parse(output_format=output_format, **kwargs)
            elif kwargs["max_tokens"] > NON_STREAMING_MAX_TOKENS:
                with api.stream(**kwargs) as stream:
                    msg = stream.get_final_message()
            else:
                msg = api.create(**kwargs)
        except a.RateLimitError as exc:
            raise ClientUnavailable(f"rate limited: {exc}") from exc
        except a.APIConnectionError as exc:
            raise ClientUnavailable(f"connection: {exc}") from exc
        except a.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ClientUnavailable(f"server {exc.status_code}: {exc}") from exc
            raise
        return to_completion(msg, time.monotonic() - t0)


def to_completion(msg: Any, seconds: float) -> Completion:
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text")
    u = msg.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )
    refused = msg.stop_reason == "refusal"
    details = getattr(msg, "stop_details", None)
    category = getattr(details, "category", None) if refused and details is not None else None
    iterations = getattr(u, "iterations", None) or []
    fallback_ran = any(getattr(it, "type", None) == "fallback_message" for it in iterations)
    parsed = None if refused else getattr(msg, "parsed_output", None)
    return Completion(
        text=text,
        usage=usage,
        stop_reason=msg.stop_reason or "end_turn",
        model=msg.model,
        parsed=parsed,
        request_id=getattr(msg, "_request_id", None),
        refused=refused,
        refusal_category=category,
        fallback_ran=fallback_ran,
        seconds=seconds,
    )
