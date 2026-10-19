"""The loop: plan, attempt, verify, escalate, stop on budget. Every attempt is recorded with its real
usage and cost so the ledger can report cost per solved task, which is the only number that
decides whether a cheaper cell was actually cheaper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .catalog import PLAN_OUTPUT_TOKENS_AT_HIGH, Cell, Usage, cost_usd
from .client import ClaudeClient, ClientUnavailableError
from .policy import Policy, PolicyProtocol
from .task import Task
from .verify import Verifier

if TYPE_CHECKING:
    from .feedback import FeedbackStore
    from .ledger import Ledger

STATUSES: tuple[str, ...] = ("solved", "unsolved", "refused", "budget", "error")


@dataclass
class Attempt:
    """One call to a worker cell and what came of it: usage and cost, whether it was refused or cut short,
    the verdict with its source (checker, judge, none) and, for judged attempts, the verifier's cell and cost.
    """

    n: int
    cell: Cell
    usage: Usage
    cost_usd: float
    seconds: float
    stop_reason: str
    served_model: str
    request_id: str | None
    refused: bool
    verdict: bool | None  # None when nothing was judged (refusal, truncation, error)
    verdict_source: str
    reason: str
    verifier: Cell | None = None
    verify_usage: Usage = field(default_factory=Usage)
    verify_cost_usd: float = 0.0

    @property
    def fallback_ran(self) -> bool:
        return self.served_model != self.cell.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "cell": self.cell.label, "usage": self.usage.to_dict(), "cost_usd": round(self.cost_usd, 6),
            "seconds": round(self.seconds, 2), "stop_reason": self.stop_reason, "served_model": self.served_model,
            "request_id": self.request_id, "refused": self.refused, "verdict": self.verdict,
            "verdict_source": self.verdict_source, "reason": self.reason,
            "verifier": self.verifier.label if self.verifier else None, "verify_usage": self.verify_usage.to_dict(),
            "verify_cost_usd": round(self.verify_cost_usd, 6),
        }


@dataclass
class TaskResult:
    """Everything the router did for one task: the plan it followed, every attempt, the answer it settled on,
    the status (solved, unsolved, refused, budget, error) and the totals the ledger records.
    """

    task_id: str
    kind: str
    risk: str
    plan: list[str]
    verify: str
    budget_usd: float
    attempts: list[Attempt]
    answer: str
    status: str
    final_cell: Cell | None
    started_at: str
    elapsed: float
    parsed: Any = None
    rationale: list[str] = field(default_factory=list)

    @property
    def solved(self) -> bool:
        return self.status == "solved"

    @property
    def work_cost_usd(self) -> float:
        return sum(a.cost_usd for a in self.attempts)

    @property
    def verify_cost_usd(self) -> float:
        return sum(a.verify_cost_usd for a in self.attempts)

    @property
    def total_cost_usd(self) -> float:
        return self.work_cost_usd + self.verify_cost_usd

    @property
    def retries(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def usage(self) -> Usage:
        total = Usage()
        for a in self.attempts:
            total = total + a.usage + a.verify_usage
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "kind": self.kind, "risk": self.risk, "plan": self.plan, "verify": self.verify,
            "budget_usd": self.budget_usd, "status": self.status,
            "final_cell": self.final_cell.label if self.final_cell else None,
            "started_at": self.started_at, "elapsed": round(self.elapsed, 2), "attempts": [a.to_dict() for a in self.attempts],
            "work_cost_usd": round(self.work_cost_usd, 6), "verify_cost_usd": round(self.verify_cost_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6), "retries": self.retries, "usage": self.usage.to_dict(),
            "answer_chars": len(self.answer), "rationale": self.rationale,
        }


class Router:
    """The loop: plan, attempt, verify, escalate, stop on budget. Every result goes to the ledger and, when a
    feedback store is given, every verdict goes to it.
    """

    def __init__(
        self,
        client: ClaudeClient,
        *,
        policy: PolicyProtocol | None = None,
        ledger: Ledger | None = None,
        feedback: FeedbackStore | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or Policy(feedback=feedback)
        self.verifier = verifier or Verifier(client, self.policy)
        self.feedback = feedback
        if ledger is None:
            from .ledger import Ledger

            ledger = Ledger(None)
        self.ledger = ledger

    def run(self, task: Task) -> TaskResult:  # noqa: C901 - the attempt loop; splitting it would hide the stop conditions
        """Run `task` through its plan and return the result. Side effects: the result is appended to the
        ledger and, with a feedback store, every verdict is recorded and the store saved.
        """
        plan = self.policy.plan(task)
        est_input = task.estimated_input_tokens
        attempts: list[Attempt] = []
        spent = 0.0
        answer, parsed, final, status = "", None, None, "unsolved"
        max_tokens = task.max_tokens
        truncated_retry = False
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        t0 = time.monotonic()
        i = 0
        while i < len(plan.ladder) and len(attempts) < plan.max_attempts:
            cell = plan.ladder[i]
            n = len(attempts) + 1
            if attempts and spent + cell.expected_cost(est_input, min(PLAN_OUTPUT_TOKENS_AT_HIGH, max_tokens)) > plan.budget_usd:
                status = "budget"
                break
            try:
                completion = self.client.complete(
                    cell, prompt=task.prompt, system=task.system, max_tokens=max_tokens, output_format=task.output_schema
                )
            except ClientUnavailableError as exc:
                attempts.append(Attempt(n, cell, Usage(), 0.0, 0.0, "error", cell.model, None, False, None, "error", str(exc)))
                status = "error"
                break
            cost = cost_usd(cell.model, completion.usage)
            spent += cost
            base = Attempt(n, cell, completion.usage, cost, completion.seconds, completion.stop_reason, completion.model,
                           completion.request_id, refused=False, verdict=None, verdict_source="", reason="")
            if completion.refused:
                attempts.append(replace(base, refused=True, verdict_source="refusal",
                                        reason=f"rechazada ({completion.refusal_category or 'sin categoría'})"))
                if sum(a.refused for a in attempts) >= 2:
                    status = "refused"
                    break
                i += 1
                continue
            if completion.stop_reason == "max_tokens" and not truncated_retry and max_tokens * 2 <= cell.spec.max_output:
                truncated_retry = True
                attempts.append(replace(base, verdict_source="truncated",
                                        reason=f"cortada en max_tokens={max_tokens}; se repite con {max_tokens * 2}"))
                max_tokens *= 2
                continue
            verdict = self.verifier.verify(task, completion.text, plan.verify, cell)
            spent += verdict.cost_usd
            attempts.append(replace(base, verdict=verdict.passed, verdict_source=verdict.source, reason=verdict.reason,
                                    verifier=verdict.cell, verify_usage=verdict.usage, verify_cost_usd=verdict.cost_usd))
            answer, parsed, final = completion.text, completion.parsed, cell
            if verdict.passed:
                status = "solved"
                break
            if spent > plan.budget_usd:
                status = "budget"
                break
            i += 1
        if status == "unsolved" and attempts and all(a.refused for a in attempts):
            status = "refused"
        result = TaskResult(
            task_id=task.id, kind=task.kind, risk=task.risk, plan=[c.label for c in plan.ladder], verify=plan.verify,
            budget_usd=plan.budget_usd, attempts=attempts, answer=answer, status=status, final_cell=final,
            started_at=started_at, elapsed=time.monotonic() - t0, parsed=parsed, rationale=plan.rationale,
        )
        if self.feedback is not None:
            first = True
            for a in attempts:
                if a.verdict is not None:
                    self.feedback.record(task.kind, a.cell, a.verdict, first=first)
                    first = False
            self.feedback.save()
        self.ledger.append(result)
        return result
