"""A scripted client for tests: no network, deterministic, records every call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .catalog import Cell, Usage
from .client import NON_STREAMING_MAX_TOKENS, Completion


@dataclass
class Reply:
    text: str = "ok"
    usage: Usage = field(default_factory=lambda: Usage(1000, 500))
    stop_reason: str = "end_turn"
    parsed: Any = None
    refused: bool = False
    refusal_category: str | None = None
    model: str | None = None  # served model, when a fallback should appear to have run


class FakeClient:
    """`script` maps a cell label (or "*") to replies for worker calls; `judge` does the same for
    structured verifier calls. Replies are consumed in order and the last one repeats."""

    def __init__(self, script: dict[str, list[Reply]] | None = None, judge: dict[str, list[Reply]] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.judge = {k: list(v) for k, v in (judge or {}).items()}
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        cell: Cell,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = NON_STREAMING_MAX_TOKENS,
        output_format: type[BaseModel] | None = None,
    ) -> Completion:
        self.calls.append({"cell": cell.label, "prompt": prompt, "system": system, "max_tokens": max_tokens,
                           "output_format": output_format.__name__ if output_format else None})
        table = self.judge if output_format is not None else self.script
        queue = table.get(cell.label) or table.get("*")
        if not queue:
            raise KeyError(f"no scripted reply for {cell.label} ({'judge' if output_format else 'worker'})")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        return Completion(
            text=reply.text, usage=reply.usage, stop_reason=reply.stop_reason, model=reply.model or cell.model,
            parsed=reply.parsed, refused=reply.refused, refusal_category=reply.refusal_category,
            fallback_ran=reply.model is not None and reply.model != cell.model,
        )
