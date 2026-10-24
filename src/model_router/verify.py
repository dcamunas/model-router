"""One judge model, one structured verdict, in a fresh context. `ask` is the building block: one judge, one prompt,
one verdict. Another package (model-tribunal) composes more judges on it; the router itself asks none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any  # Any: verdicts are written out as JSON

from pydantic import BaseModel, Field

from .catalog import Cell, Usage, cost_usd
from .client import ClaudeClient
from .task import Task


class Verdict(BaseModel):
    """The judge's structured answer."""

    passed: bool
    confidence: float
    reason: str
    issues: list[str] = Field(default_factory=list)


JUDGE_SYSTEM = (
    "You are an independent verifier. You receive a task and an answer written by another model.\n\n"
    "Decide whether the answer completes the task as specified: correct, complete, follows every explicit "
    "constraint (format, language, length, scope), and does not invent facts the task did not supply. Be strict: "
    "an answer that is only partly right, that asks a question instead of answering, that stops early, or that "
    "violates a constraint fails. You cannot run code; judge code by reading it carefully and flag anything that "
    "would not run.\n\n"
    "Report your decision as the JSON object requested: `passed`, a `confidence` between 0 and 1, a one-sentence "
    "`reason`, and `issues` listing each concrete problem you found (empty if none)."
)

MAX_ANSWER_CHARS = 60_000
JUDGE_MAX_TOKENS = 8000  # thinking counts against max_tokens; a verdict is short but the reasoning is not
MAX_ISSUES_SHOWN = 3  # issues appended to a failed verdict's reason
MIN_CONFIDENCE = 0.5  # a pass the judge is less sure of than this is a fail


def judge_prompt(task: Task, answer: str) -> str:
    """Build the judge's prompt: the task, the instructions the answer had to follow and the answer,
    truncated at MAX_ANSWER_CHARS.
    """
    shown = answer if len(answer) <= MAX_ANSWER_CHARS else answer[:MAX_ANSWER_CHARS] + "\n[... truncated for review ...]"
    parts = [f"<task>\n{task.prompt}\n</task>"]
    if task.system:
        parts.append(f"<instructions_the_answer_had_to_follow>\n{task.system}\n</instructions_the_answer_had_to_follow>")
    parts.append(f"<answer>\n{shown}\n</answer>")
    return "\n\n".join(parts)


@dataclass(frozen=True)
class JudgeVerdict:
    """One judge's own verdict, kept apart from the attempt's final verdict."""

    cell: Cell
    passed: bool
    reason: str
    role: str = "judge"  # what seat gave it; another package may add seats of its own
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"cell": self.cell.label, "passed": self.passed, "reason": self.reason, "role": self.role,
                "confidence": self.confidence}


@dataclass
class VerifyResult:
    """What verification decided and what it cost; `source` says who decided (checker, judge, none, judge-error).
    `cell` is the judge that decided, `judges` lists every judge asked in order, `verdicts` keeps each judge's own
    verdict.
    """

    passed: bool
    source: str  # checker | judge | none | judge-error
    reason: str = ""
    cell: Cell | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    confidence: float | None = None
    judges: list[Cell] = field(default_factory=list)
    verdicts: list[JudgeVerdict] = field(default_factory=list)


def ask(client: ClaudeClient, task: Task, answer: str, *, cell: Cell, prompt: str | None = None,
        system: str = JUDGE_SYSTEM, role: str = "judge", min_confidence: float = MIN_CONFIDENCE) -> VerifyResult:
    """Ask one judge, `cell`, for a structured verdict on `answer` (`prompt` in place of the judge prompt when given)
    and return it as a result of source `judge`, or `judge-error` when no verdict came back. A pass under
    `min_confidence` is a fail.

    Raises:
        ClientUnavailableError: the judge could not be reached at all.
    """
    completion = client.complete(cell, prompt=prompt or judge_prompt(task, answer), system=system,
                                 max_tokens=JUDGE_MAX_TOKENS, output_format=Verdict)
    cost = cost_usd(cell.model, completion.usage)
    verdict = completion.parsed
    if completion.refused or not isinstance(verdict, Verdict):
        why = f"refused ({completion.refusal_category})" if completion.refused else "no structured verdict"
        return VerifyResult(False, "judge-error", f"verifier returned no verdict: {why}", cell, completion.usage, cost,
                            judges=[cell])
    confidence = max(0.0, min(1.0, verdict.confidence))
    passed = verdict.passed and confidence >= min_confidence
    issues = "; ".join(verdict.issues[:MAX_ISSUES_SHOWN])
    reason = verdict.reason if passed or not verdict.issues else f"{verdict.reason} | {issues}"
    own = JudgeVerdict(cell, passed, reason, role, confidence)
    return VerifyResult(passed, "judge", reason, cell, completion.usage, cost, confidence, judges=[cell], verdicts=[own])
