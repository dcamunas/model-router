"""What the router receives: a task with its kind, its risk and, when one exists, a programmatic
checker. A checker is the cheapest and most trustworthy verification there is, so give one whenever
the output can be checked by code (tests pass, JSON validates, a regex matches).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .catalog import PLAN_OUTPUT_TOKENS_AT_HIGH

KINDS: tuple[str, ...] = (
    "classify", "extract", "summarize", "search", "chat", "write", "code", "review", "analysis", "agentic", "unknown",
)
RISKS: tuple[str, ...] = ("low", "medium", "high", "critical")
VERIFY_MODES: tuple[str, ...] = ("none", "light", "full")

DEFAULT_MAX_TOKENS = 16_000  # equals client.NON_STREAMING_MAX_TOKENS, so the api backend never streams by default
CHARS_PER_TOKEN = 4  # rough average for Spanish and English prose and code; planning only
PROMPT_OVERHEAD_TOKENS = 50  # message framing the API adds around prompt and system


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reason: str = ""


Checker = Callable[[str], CheckResult]


@dataclass
class Task:
    prompt: str
    kind: str = "unknown"
    risk: str = "medium"
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    system: str | None = None
    checker: Checker | None = None
    output_schema: type[BaseModel] | None = None  # structured output for the worker
    max_tokens: int = DEFAULT_MAX_TOKENS
    budget_usd: float | None = None  # overrides the risk default
    max_attempts: int | None = None  # overrides the risk default
    verify: str | None = None  # overrides the risk default: none | light | full

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; known: {', '.join(KINDS)}")
        if self.risk not in RISKS:
            raise ValueError(f"unknown risk {self.risk!r}; known: {', '.join(RISKS)}")
        if self.verify is not None and self.verify not in VERIFY_MODES:
            raise ValueError(f"unknown verify mode {self.verify!r}; known: {', '.join(VERIFY_MODES)}")
        if not self.prompt.strip():
            raise ValueError("empty prompt")

    @property
    def estimated_input_tokens(self) -> int:
        """Estimate the prompt size (prompt plus system) for planning, before any call is made."""
        return (len(self.prompt) + len(self.system or "")) // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS

    @property
    def planned_output_tokens(self) -> int:
        """Return the output size assumed when pricing an attempt: the reference size, capped by `max_tokens`."""
        return min(PLAN_OUTPUT_TOKENS_AT_HIGH, self.max_tokens)


def regex_checker(pattern: str, flags: int = re.IGNORECASE | re.DOTALL) -> Checker:
    rx = re.compile(pattern, flags)

    def check(answer: str) -> CheckResult:
        ok = rx.search(answer) is not None
        return CheckResult(ok, f"matches /{pattern}/" if ok else f"does not match /{pattern}/")

    return check


def contains_checker(*needles: str, all_of: bool = True) -> Checker:
    def check(answer: str) -> CheckResult:
        hits = [n for n in needles if n.lower() in answer.lower()]
        ok = len(hits) == len(needles) if all_of else bool(hits)
        missing = [n for n in needles if n not in hits]
        return CheckResult(ok, "all present" if ok else f"missing: {', '.join(missing)}")

    return check


def _json_payload(answer: str) -> object:
    text = answer.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text)
    return json.loads(text)


def json_checker(required_keys: tuple[str, ...] = ()) -> Checker:
    def check(answer: str) -> CheckResult:
        try:
            data = _json_payload(answer)
        except json.JSONDecodeError as exc:
            return CheckResult(False, f"invalid JSON: {exc.msg}")
        if required_keys:
            if not isinstance(data, dict):
                return CheckResult(False, "JSON is not an object")
            missing = [k for k in required_keys if k not in data]
            if missing:
                return CheckResult(False, f"missing keys: {', '.join(missing)}")
        return CheckResult(True, "valid JSON")

    return check


def pydantic_checker(model: type[BaseModel]) -> Checker:
    def check(answer: str) -> CheckResult:
        try:
            model.model_validate(_json_payload(answer))
        except json.JSONDecodeError as exc:
            return CheckResult(False, f"invalid JSON: {exc.msg}")
        except ValidationError as exc:
            return CheckResult(False, f"schema: {exc.errors()[0]['msg']}")
        return CheckResult(True, f"valid {model.__name__}")

    return check
