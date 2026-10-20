"""What the router receives: a task with its kind, its risk and, when one exists, a programmatic
checker. A checker is the cheapest and most trustworthy verification there is, so give one whenever
the output can be checked by code (tests pass, JSON validates, a regex matches).
"""

from __future__ import annotations

import hashlib
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
VERIFY_MODES: tuple[str, ...] = ("none", "light", "full", "panel", "tribunal")
VERIFIED_SOURCES: frozenset[str] = frozenset({"checker", "judge", "panel", "tribunal"})  # verdict sources that are evidence

DEFAULT_MAX_TOKENS = 16_000  # equals client.NON_STREAMING_MAX_TOKENS, so the api backend never streams by default
CHARS_PER_TOKEN = 4  # rough average for Spanish and English prose and code; planning only
PROMPT_OVERHEAD_TOKENS = 50  # message framing the API adds around prompt and system


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a programmatic checker: pass or fail, with the reason shown in the ledger."""

    passed: bool
    reason: str = ""


Checker = Callable[[str], CheckResult]


@dataclass
class Task:
    """What the router receives. Only `prompt` is required; `kind` and `risk` pick the start cell, the
    verification and the budget, and the optional fields override those defaults or add a `checker`.
    """

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
    verify: str | None = None  # overrides the risk default: none | light | full | panel | tribunal

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; known: {', '.join(KINDS)}")
        if self.risk not in RISKS:
            raise ValueError(f"unknown risk {self.risk!r}; known: {', '.join(RISKS)}")
        if self.verify is not None and self.verify not in VERIFY_MODES:
            raise ValueError(f"unknown verify mode {self.verify!r}; known: {', '.join(VERIFY_MODES)}")
        if not self.prompt.strip():
            raise ValueError("empty prompt")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("budget_usd must be positive")

    @property
    def fingerprint(self) -> str:
        """Return a stable 12-character id of what the task asks (kind, risk, prompt): the same task on another
        day or under another strategy has the same fingerprint, so the ledger can compare them.
        """
        return hashlib.sha256(f"{self.kind}\n{self.risk}\n{self.prompt}".encode()).hexdigest()[:12]

    @property
    def estimated_input_tokens(self) -> int:
        """Estimate the prompt size (prompt plus system) for planning, before any call is made."""
        return (len(self.prompt) + len(self.system or "")) // CHARS_PER_TOKEN + PROMPT_OVERHEAD_TOKENS

    @property
    def planned_output_tokens(self) -> int:
        """Return the output size assumed when pricing an attempt: the reference size, capped by `max_tokens`."""
        return min(PLAN_OUTPUT_TOKENS_AT_HIGH, self.max_tokens)


def regex_checker(pattern: str, flags: int = re.IGNORECASE | re.DOTALL) -> Checker:
    """Return a checker that passes when `pattern` is found in the answer."""
    rx = re.compile(pattern, flags)

    def check(answer: str) -> CheckResult:
        ok = rx.search(answer) is not None
        return CheckResult(ok, f"matches /{pattern}/" if ok else f"does not match /{pattern}/")

    return check


def contains_checker(*needles: str, all_of: bool = True) -> Checker:
    """Return a checker that passes when every needle (or any, with `all_of=False`) is in the answer, ignoring case."""
    def check(answer: str) -> CheckResult:
        hits = [n for n in needles if n.lower() in answer.lower()]
        ok = len(hits) == len(needles) if all_of else bool(hits)
        missing = [n for n in needles if n not in hits]
        return CheckResult(ok, "all present" if ok else f"missing: {', '.join(missing)}")

    return check


FENCED_BLOCK = re.compile(r"```[\w-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)


def json_payload(answer: str) -> object:
    """Parse the JSON in `answer`: the first fenced block when there is one, else the whole text.

    Raises:
        json.JSONDecodeError: it is not JSON.
    """
    text = answer.strip()
    fenced = FENCED_BLOCK.search(text)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


def json_checker(required_keys: tuple[str, ...] = ()) -> Checker:
    """Return a checker that passes when the answer is JSON (the first fenced block, when there is one,
    is what gets parsed) and, with `required_keys`, an object that has all of them.
    """
    def check(answer: str) -> CheckResult:
        try:
            data = json_payload(answer)
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
    """Return a checker that passes when the answer is JSON that validates against `model`."""
    def check(answer: str) -> CheckResult:
        try:
            model.model_validate(json_payload(answer))
        except json.JSONDecodeError as exc:
            return CheckResult(False, f"invalid JSON: {exc.msg}")
        except ValidationError as exc:
            return CheckResult(False, f"schema: {exc.errors()[0]['msg']}")
        return CheckResult(True, f"valid {model.__name__}")

    return check
