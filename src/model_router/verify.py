"""Verification by risk. Order of trust: the task's programmatic checker (free, authoritative when
it fails), then a judge model with a structured verdict. `light` uses the cheapest cell at least as
capable as the worker; `full` uses a clearly more capable cell on another model, a blind
cross-check in a fresh context.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .catalog import Cell, Usage, cost_usd
from .client import ClaudeClient
from .policy import PolicyProtocol
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


@dataclass
class VerifyResult:
    """What verification decided and what it cost; `source` says who decided (checker, judge, none, judge-error)."""

    passed: bool
    source: str  # checker | judge | none | judge-error
    reason: str = ""
    cell: Cell | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    confidence: float | None = None


class Verifier:
    """Verifies answers by mode: the task's checker first, then a judge cell chosen by the policy; a judge
    verdict under `min_confidence` fails.
    """

    def __init__(self, client: ClaudeClient, policy: PolicyProtocol, *, min_confidence: float = 0.5) -> None:
        self.client = client
        self.policy = policy
        self.min_confidence = min_confidence

    def verify(self, task: Task, answer: str, mode: str, worker: Cell) -> VerifyResult:
        """Verify `answer` to `task` in `mode` (none, light, full); `worker` is the cell that answered, so
        the judge is chosen relative to it.
        """
        if task.checker is not None:
            check = task.checker(answer)
            if not check.passed:
                return VerifyResult(False, "checker", check.reason)
            if mode == "none":
                return VerifyResult(True, "checker", check.reason)
        elif mode == "none":
            return VerifyResult(True, "none", "sin verificación")
        cell = self.policy.verifier_for(worker, mode)
        if cell is None:
            return VerifyResult(True, "none", "sin celda verificadora")
        completion = self.client.complete(
            cell, prompt=judge_prompt(task, answer), system=JUDGE_SYSTEM, max_tokens=JUDGE_MAX_TOKENS, output_format=Verdict
        )
        cost = cost_usd(cell.model, completion.usage)
        verdict = completion.parsed
        if completion.refused or not isinstance(verdict, Verdict):
            why = f"rechazada ({completion.refusal_category})" if completion.refused else "sin veredicto estructurado"
            return VerifyResult(False, "judge-error", f"el verificador no dio veredicto: {why}", cell, completion.usage, cost)
        confidence = max(0.0, min(1.0, verdict.confidence))
        passed = verdict.passed and confidence >= self.min_confidence
        issues = "; ".join(verdict.issues[:MAX_ISSUES_SHOWN])
        reason = verdict.reason if passed or not verdict.issues else f"{verdict.reason} | {issues}"
        return VerifyResult(passed, "judge", reason, cell, completion.usage, cost, confidence)
