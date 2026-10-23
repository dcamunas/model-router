"""The loop: plan, attempt, verify, escalate, stop on budget. Every attempt is recorded with its real
usage and cost so the ledger can report cost per solved task, which is the only number that
decides whether a cheaper cell was actually cheaper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any  # Any: results are written out as JSON; structured output of any shape

from . import clock
from .catalog import PLAN_OUTPUT_TOKENS_AT_HIGH, Cell, Usage, cost_usd
from .client import ClaudeClient, ClientUnavailableError
from .policy import Plan, Policy
from .task import EXTERNAL_STOP, VERIFIED_SOURCES, Task
from .verify import JudgeVerdict, Verifier, VerifyResult
from .version import router_version

if TYPE_CHECKING:
    from .ledger import Ledger

CARRIED_REASON_CHARS = 600  # how much of a failed verdict's reason the next attempt's prompt carries
REFUSALS_TO_STOP = 2  # a task refused this many times is refused: another cell would only spend more


def with_failure(prompt: str, *, cell: Cell, reason: str) -> str:
    """Return `prompt` with a note on why the previous attempt (on `cell`) failed verification, so the next
    cell does not repeat the mistake. The note names the model, not the effort: the next worker needs the
    mistake, not the lattice.
    """
    return (f"{prompt}\n\nA previous attempt at this task (by {cell.spec.short}) was rejected by verification for "
            f"this reason: {reason[:CARRIED_REASON_CHARS]}\nDo not repeat that mistake; answer the whole task again.")


@dataclass
class Attempt:
    """One call to a worker cell and what came of it: usage and cost, whether it was refused or cut short,
    the verdict with its source (checker, judge, none) and, for judged attempts, the deciding judge's cell,
    every judge asked, each judge's own verdict and what they cost.
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
    judges: list[Cell] = field(default_factory=list)
    verdicts: list[JudgeVerdict] = field(default_factory=list)
    carried_reason: bool = False  # the prompt carried the reason the attempt before failed for

    @property
    def fallback_ran(self) -> bool:
        return self.served_model != self.cell.model

    def judged(self, verdict: VerifyResult) -> Attempt:
        """Return this attempt with the verification's verdict, judges and cost."""
        return replace(self, verdict=verdict.passed, verdict_source=verdict.source, reason=verdict.reason,
                       verifier=verdict.cell, verify_usage=verdict.usage, verify_cost_usd=verdict.cost_usd,
                       judges=verdict.judges, verdicts=verdict.verdicts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n, "cell": self.cell.label, "usage": self.usage.to_dict(), "cost_usd": round(self.cost_usd, 6),
            "seconds": round(self.seconds, 2), "stop_reason": self.stop_reason, "served_model": self.served_model,
            "request_id": self.request_id, "refused": self.refused, "verdict": self.verdict,
            "verdict_source": self.verdict_source, "reason": self.reason,
            "verifier": self.verifier.label if self.verifier else None, "verify_usage": self.verify_usage.to_dict(),
            "verify_cost_usd": round(self.verify_cost_usd, 6), "judges": [j.label for j in self.judges],
            "verdicts": [v.to_dict() for v in self.verdicts], "carried_reason": self.carried_reason,
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
    router_version: str = ""

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
            "answer_chars": len(self.answer), "rationale": self.rationale, "router_version": self.router_version,
        }


class Router:
    """The loop: plan, attempt, verify, escalate, stop on budget. Every result goes to the ledger."""

    def __init__(
        self,
        client: ClaudeClient,
        *,
        policy: Policy | None = None,
        ledger: Ledger | None = None,
        verifier: Verifier | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or Policy()
        self.verifier = verifier or Verifier(client, self.policy)
        if ledger is None:
            from .ledger import Ledger

            ledger = Ledger(None)
        self.ledger = ledger

    def run(self, task: Task) -> TaskResult:  # noqa: C901 - the attempt loop: its stop conditions read best together, which is also why it runs past 50 statements
        """Run `task` through its plan, append the result to the ledger and return it. A judge that cannot be
        reached ends the task with status `error`. After a verified fail, the next attempt's prompt carries the
        reason (`with_failure`).
        """
        plan = self.policy.plan(task)
        failure: tuple[Cell, str] | None = None  # the last verified fail: its cell and reason
        est_input = task.estimated_input_tokens
        attempts: list[Attempt] = []
        spent = 0.0
        answer, parsed, final, status = "", None, None, "unsolved"
        max_tokens = task.max_tokens
        truncated_retry = False
        started_at = clock.stamp()
        t0 = time.monotonic()
        i = 0
        while i < len(plan.ladder) and len(attempts) < plan.max_attempts:
            cell = plan.ladder[i]
            n = len(attempts) + 1
            if attempts and spent + cell.expected_cost(est_input, min(PLAN_OUTPUT_TOKENS_AT_HIGH, max_tokens)) > plan.budget_usd:
                status = "budget"
                break
            prompt = with_failure(task.prompt, cell=failure[0], reason=failure[1]) if failure else task.prompt
            try:
                completion = self.client.complete(
                    cell, prompt=prompt, system=task.system, max_tokens=max_tokens, output_format=task.output_schema
                )
            except ClientUnavailableError as exc:
                attempts.append(Attempt(n, cell, Usage(), cost_usd=0.0, seconds=0.0, stop_reason="error",
                                        served_model=cell.model, request_id=None, refused=False, verdict=None,
                                        verdict_source="error", reason=str(exc), carried_reason=failure is not None))
                status = "error"
                break
            cost = cost_usd(cell.model, completion.usage)
            spent += cost
            base = Attempt(n, cell, completion.usage, cost, completion.seconds, completion.stop_reason, completion.model,
                           completion.request_id, refused=False, verdict=None, verdict_source="", reason="",
                           carried_reason=failure is not None)
            if completion.refused:
                attempts.append(replace(base, refused=True, verdict_source="refusal",
                                        reason=f"refused ({completion.refusal_category or 'no category'})"))
                if sum(a.refused for a in attempts) >= REFUSALS_TO_STOP:
                    status = "refused"
                    break
                i += 1
                continue
            if completion.stop_reason == "max_tokens" and not truncated_retry and max_tokens < cell.spec.max_output:
                truncated_retry = True
                room = min(max_tokens * 2, cell.spec.max_output)
                attempts.append(replace(base, verdict_source="truncated",
                                        reason=f"cut at max_tokens={max_tokens}; retrying with {room}"))
                max_tokens = room
                continue
            try:
                verdict = self.verifier.verify(task, completion.text, mode=plan.verify, worker=cell)
            except ClientUnavailableError as exc:
                attempts.append(replace(base, verdict_source="judge-error", reason=f"verifier unavailable: {exc}"))
                answer, parsed, final = completion.text, completion.parsed, cell
                status = "error"
                break
            spent += verdict.cost_usd
            attempts.append(base.judged(verdict))
            answer, parsed, final = completion.text, completion.parsed, cell
            if verdict.passed:
                status = "solved"
                break
            if verdict.source in VERIFIED_SOURCES:
                failure = (cell, verdict.reason)
            if spent > plan.budget_usd:
                status = "budget"
                break
            i += 1
        if status == "unsolved" and attempts and all(a.refused for a in attempts):
            status = "refused"
        return self._record(task, plan, attempts=attempts, answer=answer, status=status, final=final,
                            started_at=started_at, elapsed=time.monotonic() - t0, parsed=parsed)

    def judge(self, task: Task, answer: str, *, worker: Cell | None = None, mode: str | None = None) -> TaskResult:
        """Verify an `answer` produced outside the router (a subagent, a person) as one attempt on `worker` (the
        plan's start cell when None) in `mode` (the plan's when None), with no worker cost, and record it in
        the ledger.
        """
        plan = self.policy.plan(task)
        worker = worker or plan.start
        mode = mode or plan.verify
        started_at = clock.stamp()
        t0 = time.monotonic()
        base = Attempt(1, worker, Usage(), cost_usd=0.0, seconds=0.0, stop_reason=EXTERNAL_STOP,
                       served_model=worker.model, request_id=None, refused=False, verdict=None, verdict_source="",
                       reason="")
        try:
            verdict = self.verifier.verify(task, answer, mode=mode, worker=worker)
        except ClientUnavailableError as exc:
            attempt = replace(base, verdict_source="judge-error", reason=f"verifier unavailable: {exc}")
            status = "error"
        else:
            attempt = base.judged(verdict)
            status = "solved" if verdict.passed else "unsolved"
        plan.verify = mode
        plan.rationale = [*plan.rationale, "answer produced outside the router: judged only"]
        return self._record(task, plan, attempts=[attempt], answer=answer, status=status, final=worker,
                            started_at=started_at, elapsed=time.monotonic() - t0)

    def _record(self, task: Task, plan: Plan, *, attempts: list[Attempt], answer: str, status: str,
                final: Cell | None, started_at: str, elapsed: float, parsed: Any = None) -> TaskResult:
        """Build the result of `task` under `plan`, append it to the ledger and return it."""
        result = TaskResult(
            task_id=task.id, kind=task.kind, risk=task.risk, plan=[c.label for c in plan.ladder], verify=plan.verify,
            budget_usd=plan.budget_usd, attempts=attempts, answer=answer, status=status, final_cell=final,
            started_at=started_at, elapsed=elapsed, parsed=parsed, rationale=plan.rationale,
            router_version=router_version(),
        )
        self.ledger.append(result)
        return result
