"""Verification by risk. Order of trust: the task's programmatic checker (free, authoritative when
it fails), then a judge model with a structured verdict. `light` uses the worker's own cell in a
fresh context; `full` uses the configured judge cell on another model than the worker, a blind
cross-check in a fresh context; `panel` asks those two judges, cheapest first, and passes only when
both pass, skipping the second when the first fails; `tribunal` asks both, blind to each other, and
when they split an arbiter cell reads the task, the answer and both reasoned verdicts and decides.
Every judge's verdict is kept, so the ledger can keep a scoreboard of who agrees with whom, whom the
arbiter backs and whom a human corrects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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

ARBITER_SYSTEM = (
    "You are the arbiter of a tribunal. Two independent judges disagree on whether an answer completes a task. "
    "You receive the task, the answer and both verdicts with their reasons. Decide for yourself from the task "
    "and the answer; treat the judges' reasons as leads to check, not as votes, and apply the same standard: "
    "correct, complete, every explicit constraint met, no invented facts. You cannot run code.\n\n"
    "Report your decision as the JSON object requested: `passed`, a `confidence` between 0 and 1, a one-sentence "
    "`reason` that says which judge was right and why, and `issues` listing each concrete problem (empty if none)."
)

MAX_ANSWER_CHARS = 60_000
JUDGE_MAX_TOKENS = 8000  # thinking counts against max_tokens; a verdict is short but the reasoning is not
MAX_ISSUES_SHOWN = 3  # issues appended to a failed verdict's reason
VERIFIED_SOURCES: frozenset[str] = frozenset({"checker", "judge", "panel"})  # verdicts that count as evidence


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
    """One judge's (or the arbiter's) own verdict, kept apart from the attempt's final verdict for the scoreboard."""

    cell: Cell
    passed: bool
    reason: str
    role: str  # judge | arbiter
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"cell": self.cell.label, "passed": self.passed, "reason": self.reason, "role": self.role,
                "confidence": self.confidence}


def arbiter_prompt(task: Task, answer: str, verdicts: list[JudgeVerdict]) -> str:
    """Build the arbiter's prompt: the judge prompt plus both verdicts, anonymised as judge A and judge B."""
    lines = []
    for letter, v in zip("AB", verdicts, strict=False):
        lines.append(f"Judge {letter} {'passed' if v.passed else 'failed'} the answer: {v.reason}")
    return judge_prompt(task, answer) + "\n\n<verdicts>\n" + "\n".join(lines) + "\n</verdicts>"


@dataclass
class VerifyResult:
    """What verification decided and what it cost; `source` says who decided (checker, judge, panel, tribunal,
    none, judge-error). `cell` is the judge that decided, `judges` lists every judge asked in order, `verdicts`
    keeps each judge's own verdict and, after a split, the arbiter's.
    """

    passed: bool
    source: str  # checker | judge | panel | tribunal | none | judge-error
    reason: str = ""
    cell: Cell | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    confidence: float | None = None
    judges: list[Cell] = field(default_factory=list)
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    arbiter: Cell | None = None


class Verifier:
    """Verifies answers by mode: the task's checker first, then the judge cells the policy chooses; a judge
    verdict under `min_confidence` fails, a panel fails as soon as one of its judges fails, and a tribunal
    sends a split to the policy's arbiter.
    """

    def __init__(self, client: ClaudeClient, policy: PolicyProtocol, *, min_confidence: float = 0.5) -> None:
        self.client = client
        self.policy = policy
        self.min_confidence = min_confidence

    def verify(self, task: Task, answer: str, mode: str, worker: Cell) -> VerifyResult:
        """Verify `answer` to `task` in `mode` (none, light, full, panel, tribunal); `worker` is the cell that
        answered, so the judges are chosen relative to it. A panel or tribunal that the policy reduces to
        one judge is reported as that one judge.
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
        if mode not in ("panel", "tribunal") or len(judges) == 1:
            return self._ask(task, answer, judges[0])
        if mode == "panel":
            return self._panel(task, answer, judges)
        return self._tribunal(task, answer, judges, self.policy.arbiter_for(worker))

    def _ask(self, task: Task, answer: str, cell: Cell, *, prompt: str | None = None, system: str = JUDGE_SYSTEM,
             role: str = "judge") -> VerifyResult:
        completion = self.client.complete(
            cell, prompt=prompt or judge_prompt(task, answer), system=system, max_tokens=JUDGE_MAX_TOKENS,
            output_format=Verdict,
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
        own = JudgeVerdict(cell, passed, reason, role, confidence)
        return VerifyResult(passed, "judge", reason, cell, completion.usage, cost, confidence, judges=[cell], verdicts=[own])

    def _tribunal(self, task: Task, answer: str, judges: list[Cell], arbiter: Cell | None) -> VerifyResult:
        """Ask every judge, blind to each other; agreement decides, a split goes to the arbiter (and fails when
        there is none).
        """
        asked = [self._ask(task, answer, cell) for cell in judges]
        usage = Usage()
        for r in asked:
            usage = usage + r.usage
        cost = sum(r.cost_usd for r in asked)
        cells = [r.cell for r in asked if r.cell is not None]
        names = " + ".join(map(str, cells))
        verdicts = [v for r in asked for v in r.verdicts]
        broken = next((r for r in asked if r.source == "judge-error"), None)
        if broken is not None:
            reason = f"tribunal ({names}): {broken.reason}"
            return VerifyResult(False, "judge-error", reason, broken.cell, usage, cost, None, cells, verdicts)
        outcomes = {r.passed for r in asked}
        confidences = [r.confidence for r in asked if r.confidence is not None]
        if len(outcomes) == 1:
            passed = asked[0].passed
            reason = f"tribunal agreed ({names}): " + " | ".join(r.reason for r in asked)
            return VerifyResult(passed, "tribunal", reason, asked[-1].cell, usage, cost, min(confidences, default=None),
                                cells, verdicts)
        passed_by = ", ".join(str(r.cell) for r in asked if r.passed)
        failed_by = ", ".join(str(r.cell) for r in asked if not r.passed)
        split = f"tribunal split: {passed_by} passed, {failed_by} failed"
        if arbiter is None:
            reason = f"{split}; no arbiter available: " + " | ".join(r.reason for r in asked if not r.passed)
            return VerifyResult(False, "tribunal", reason, asked[-1].cell, usage, cost, min(confidences, default=None),
                                cells, verdicts)
        ruling = self._ask(task, answer, arbiter, prompt=arbiter_prompt(task, answer, verdicts), system=ARBITER_SYSTEM,
                           role="arbiter")
        usage, cost = usage + ruling.usage, cost + ruling.cost_usd
        verdicts += ruling.verdicts
        if ruling.source == "judge-error":
            reason = f"{split}; arbiter {arbiter}: {ruling.reason}"
            return VerifyResult(False, "judge-error", reason, arbiter, usage, cost, None, cells, verdicts, arbiter)
        sided = ", ".join(str(r.cell) for r in asked if r.passed == ruling.passed)
        reason = f"{split}; arbiter {arbiter} sided with {sided}: {ruling.reason}"
        return VerifyResult(ruling.passed, "tribunal", reason, arbiter, usage, cost, ruling.confidence, cells, verdicts,
                            arbiter)

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
        verdicts = [v for r in asked for v in r.verdicts]
        if last.source == "judge-error":
            reason = f"panel ({names}): {last.reason}"
            return VerifyResult(False, "judge-error", reason, last.cell, usage, cost, confidence, cells, verdicts)
        if last.passed:
            reason = f"panel agreed ({names}): {last.reason}"
        elif len(asked) == 1:
            reason = f"panel: {last.cell} failed, the other judge was not asked: {last.reason}"
        else:
            passed_by = ", ".join(map(str, cells[:-1]))
            reason = f"panel split: {passed_by} passed, {last.cell} failed: {last.reason}"
        return VerifyResult(last.passed, "panel", reason, last.cell, usage, cost, confidence, cells, verdicts)
