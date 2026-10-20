"""Verification by risk. Order of trust: the task's programmatic checker (free, authoritative when
it fails), then a judge model with a structured verdict. `light` uses the worker's own cell in a
fresh context; `full` uses the configured judge cell on another model than the worker, a blind
cross-check in a fresh context; `panel` asks both, cheapest first, and passes only when both pass.
The second judge is not asked when the first fails, and no judge sees another's verdict.
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
    """What verification decided and what it cost; `source` says who decided (checker, judge, panel, none,
    judge-error). `cell` is the judge that decided; `judges` lists every judge asked, in order.
    """

    passed: bool
    source: str  # checker | judge | panel | none | judge-error
    reason: str = ""
    cell: Cell | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    confidence: float | None = None
    judges: list[Cell] = field(default_factory=list)


class Verifier:
    """Verifies answers by mode: the task's checker first, then the judge cells the policy chooses; a judge
    verdict under `min_confidence` fails, and a panel fails as soon as one of its judges fails.
    """

    def __init__(self, client: ClaudeClient, policy: PolicyProtocol, *, min_confidence: float = 0.5) -> None:
        self.client = client
        self.policy = policy
        self.min_confidence = min_confidence

    def verify(self, task: Task, answer: str, mode: str, worker: Cell) -> VerifyResult:
        """Verify `answer` to `task` in `mode` (none, light, full, panel); `worker` is the cell that answered,
        so the judges are chosen relative to it. A panel that the policy reduces to one judge is reported
        as that one judge.
        """
        if task.checker is not None:
            check = task.checker(answer)
            if not check.passed:
                return VerifyResult(False, "checker", check.reason)
            if mode == "none":
                return VerifyResult(True, "checker", check.reason)
        elif mode == "none":
            return VerifyResult(True, "none", "no verification requested")
        judges = self.policy.judges_for(worker, mode)
        if not judges:
            return VerifyResult(True, "none", "no verifier cell")
        if mode != "panel" or len(judges) == 1:
            return self._ask(task, answer, judges[0])
        return self._panel(task, answer, judges)

    def _ask(self, task: Task, answer: str, cell: Cell) -> VerifyResult:
        completion = self.client.complete(
            cell, prompt=judge_prompt(task, answer), system=JUDGE_SYSTEM, max_tokens=JUDGE_MAX_TOKENS, output_format=Verdict
        )
        cost = cost_usd(cell.model, completion.usage)
        verdict = completion.parsed
        if completion.refused or not isinstance(verdict, Verdict):
            why = f"refused ({completion.refusal_category})" if completion.refused else "no structured verdict"
            return VerifyResult(False, "judge-error", f"verifier returned no verdict: {why}", cell, completion.usage, cost,
                                judges=[cell])
        confidence = max(0.0, min(1.0, verdict.confidence))
        passed = verdict.passed and confidence >= self.min_confidence
        issues = "; ".join(verdict.issues[:MAX_ISSUES_SHOWN])
        reason = verdict.reason if passed or not verdict.issues else f"{verdict.reason} | {issues}"
        return VerifyResult(passed, "judge", reason, cell, completion.usage, cost, confidence, judges=[cell])

    def _panel(self, task: Task, answer: str, judges: list[Cell]) -> VerifyResult:
        """Ask the judges in order and stop at the first fail: the panel passes only when every judge passes."""
        asked: list[VerifyResult] = []
        for cell in judges:
            asked.append(self._ask(task, answer, cell))
            if not asked[-1].passed:
                break
        last = asked[-1]
        usage = Usage()
        for r in asked:
            usage = usage + r.usage
        cost = sum(r.cost_usd for r in asked)
        confidences = [r.confidence for r in asked if r.confidence is not None]
        confidence = min(confidences) if confidences else None
        cells = [r.cell for r in asked if r.cell is not None]
        names = " + ".join(map(str, cells))
        if last.source == "judge-error":
            reason = f"panel ({names}): {last.reason}"
            return VerifyResult(False, "judge-error", reason, last.cell, usage, cost, confidence, cells)
        if last.passed:
            reason = f"panel agreed ({names}): {last.reason}"
        elif len(asked) == 1:
            reason = f"panel: {last.cell} failed, the other judge was not asked: {last.reason}"
        else:
            passed_by = ", ".join(map(str, cells[:-1]))
            reason = f"panel split: {passed_by} passed, {last.cell} failed: {last.reason}"
        return VerifyResult(last.passed, "panel", reason, last.cell, usage, cost, confidence, cells)
