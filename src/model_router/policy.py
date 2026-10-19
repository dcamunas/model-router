"""Routing policy: which cell starts a task, how it escalates, how it is verified and what it may
spend. Everything is a table you can read and edit; the feedback store nudges the start cell once
it has seen enough verdicts for a kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .catalog import Cell, ladder, lattice, nearest_available, parse_cell, step_up
from .task import Task

if TYPE_CHECKING:
    from .feedback import FeedbackStore

# Start cell by kind x risk. Read: "classification at medium risk starts on Haiku at medium effort".
START: dict[str, dict[str, str]] = {
    "classify": {"low": "haiku/low", "medium": "haiku/medium", "high": "sonnet/low", "critical": "sonnet/medium"},
    "extract": {"low": "haiku/medium", "medium": "sonnet/low", "high": "sonnet/medium", "critical": "opus/low"},
    "summarize": {"low": "haiku/medium", "medium": "sonnet/low", "high": "sonnet/medium", "critical": "opus/low"},
    "search": {"low": "haiku/medium", "medium": "sonnet/low", "high": "sonnet/medium", "critical": "opus/low"},
    "chat": {"low": "sonnet/low", "medium": "sonnet/low", "high": "sonnet/medium", "critical": "opus/low"},
    "write": {"low": "sonnet/low", "medium": "sonnet/medium", "high": "opus/low", "critical": "opus/medium"},
    "code": {"low": "sonnet/medium", "medium": "opus/low", "high": "opus/medium", "critical": "opus/high"},
    "review": {"low": "opus/low", "medium": "opus/low", "high": "opus/medium", "critical": "opus/high"},
    "analysis": {"low": "sonnet/medium", "medium": "opus/low", "high": "opus/medium", "critical": "fable/low"},
    "agentic": {"low": "opus/medium", "medium": "opus/high", "high": "fable/low", "critical": "fable/high"},
    "unknown": {"low": "sonnet/low", "medium": "sonnet/medium", "high": "opus/low", "critical": "opus/high"},
}
# A task never starts below this capability, whatever the feedback store suggests.
FLOOR: dict[str, str] = {"high": "sonnet/low", "critical": "opus/medium"}
VERIFY_BY_RISK: dict[str, str] = {"low": "none", "medium": "none", "high": "light", "critical": "full"}
BUDGET_USD_BY_RISK: dict[str, float] = {"low": 0.10, "medium": 0.50, "high": 2.00, "critical": 10.00}
MAX_ATTEMPTS_BY_RISK: dict[str, int] = {"low": 2, "medium": 3, "high": 4, "critical": 5}
BUDGET_ATTEMPTS = 3.0  # the budget is at least this many first attempts, so big prompts are not starved

# Reference request for pricing a verification call: it reads the task and the answer, writes little.
JUDGE_INPUT_TOKENS = 3000
JUDGE_OUTPUT_TOKENS_AT_HIGH = 300
JUDGE_PROMPT_OVERHEAD_TOKENS = 300  # the verdict instructions and tags around the task and the answer
FULL_VERIFY_GAIN = 0.08  # a full verifier must be this much more capable than the worker


@dataclass
class Plan:
    """What the router will do with one task: the ladder of cells to try in order, the verification mode,
    the budget and the attempt cap, plus the reasons `router plan` prints.
    """

    ladder: list[Cell]
    verify: str
    budget_usd: float
    max_attempts: int
    rationale: list[str] = field(default_factory=list)

    @property
    def start(self) -> Cell:
        return self.ladder[0]


class PolicyProtocol(Protocol):
    """What the Router needs from a policy: a plan for a task and a verifier cell for a worker cell."""

    def plan(self, task: Task) -> Plan: ...

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None: ...


def _judge_cost(cell: Cell) -> float:
    return cell.expected_cost(JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS_AT_HIGH)


def choose_verifier(worker: Cell, mode: str, cells: list[Cell]) -> Cell | None:
    """light: the cheapest cell at least as capable as the worker (may be the worker's own cell, in
    a fresh context). full: the cheapest cell clearly more capable AND on another model, so the
    check is a blind cross-check; when no other model qualifies, a fresh context on the same model.
    """
    if mode == "none":
        return None
    if mode == "light":
        cands = [c for c in cells if c.capability >= worker.capability - 1e-9]
        return min(cands, key=_judge_cost)
    cands = [c for c in cells if c.capability >= worker.capability + FULL_VERIFY_GAIN - 1e-9 and c.model != worker.model]
    if not cands:
        cands = [c for c in cells if c.capability >= worker.capability - 1e-9 and c != worker]
    return min(cands, key=_judge_cost) if cands else worker


class Policy:
    """The default policy: the START table, the risk floors, feedback nudges, cost-aware escalation."""

    def __init__(
        self,
        *,
        cells: list[Cell] | None = None,
        start_table: dict[str, dict[str, str]] | None = None,
        feedback: FeedbackStore | None = None,
        min_gain: float = 0.08,
    ) -> None:
        self.cells = cells or lattice()
        self.models = sorted({c.model for c in self.cells}, key=lambda m: Cell(m, "low").tier)
        self.start_table = start_table or START
        self.feedback = feedback
        self.min_gain = min_gain

    def _available(self, cell: Cell, rationale: list[str]) -> Cell:
        """Replace a cell whose model this installation does not have with the nearest available one."""
        near = nearest_available(cell, self.models)
        if near != cell:
            rationale.append(f"{cell.spec.short} not available: {cell} -> {near}")
        return near

    def plan(self, task: Task) -> Plan:
        """Plan `task`: start cell from the table or the feedback store, risk floor, ladder, verification
        mode and budget, with the reasons.
        """
        kind = task.kind if task.kind in self.start_table else "unknown"
        risk = task.risk
        start = parse_cell(self.start_table[kind][risk])
        rationale = [f"table {kind} x {risk} -> {start}"]
        start = self._available(start, rationale)
        verify = task.verify or VERIFY_BY_RISK[risk]
        est_input = task.estimated_input_tokens
        out_tokens = task.planned_output_tokens
        if self.feedback is not None:
            suggested, why = self.feedback.suggest_start(
                kind, start, self.cells, break_even=self.break_even(start, verify, est_input, out_tokens)
            )
            if suggested != start and why:
                rationale.append(why)
                start = suggested
        floor = FLOOR.get(risk)
        if floor:
            floor_cell = self._available(parse_cell(floor), [])
            if start.capability < floor_cell.capability:
                rationale.append(f"risk floor {risk}: {start} -> {floor_cell}")
                start = floor_cell
        max_attempts = task.max_attempts or MAX_ATTEMPTS_BY_RISK[risk]
        cells = ladder(start, self.cells, max_attempts, min_gain=self.min_gain)
        first_attempt = start.expected_cost(est_input, out_tokens)
        budget = task.budget_usd or max(BUDGET_USD_BY_RISK[risk], BUDGET_ATTEMPTS * first_attempt)
        rationale.append(f"ladder: {' -> '.join(map(str, cells))}")
        rationale.append(f"verification {verify} (risk {risk}); budget ${budget:.2f}; max {max_attempts} attempts")
        return Plan(cells, verify, budget, max_attempts, rationale)

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None:
        """Return the cell that verifies `worker`'s answer in `mode`, or None for `none`."""
        return choose_verifier(worker, mode, self.cells)

    def attempt_cost(self, cell: Cell, verify: str, est_input: int, out_tokens: int) -> float:
        """Return the expected cost of one attempt on `cell` including its verification call."""
        cost = cell.expected_cost(est_input, out_tokens)
        judge = self.verifier_for(cell, verify)
        if judge is not None:
            cost += judge.expected_cost(est_input + out_tokens + JUDGE_PROMPT_OVERHEAD_TOKENS, JUDGE_OUTPUT_TOKENS_AT_HIGH)
        return cost

    def break_even(self, start: Cell, verify: str, est_input: int, out_tokens: int) -> float | None:
        """Return the first-attempt pass rate below which starting on `start` is dearer than starting one
        step up, or None when there is no step up: c1 + (1 - p) * c2 < c2  <=>  p > c1 / c2, each cost
        including verification.
        """
        up = step_up(start, self.cells, min_gain=self.min_gain)
        if up is None:
            return None
        c1 = self.attempt_cost(start, verify, est_input, out_tokens)
        c2 = self.attempt_cost(up, verify, est_input, out_tokens)
        return min(0.95, c1 / c2) if c2 > 0 else None


class LadderPolicy:
    """A fixed ladder, for benchmarks: 'always Opus high', 'Sonnet then Opus on failure', etc."""

    def __init__(self, cells: list[Cell], *, verify: str = "none", budget_usd: float = 100.0) -> None:
        if not cells:
            raise ValueError("a ladder needs at least one cell")
        self.ladder_cells = cells
        self.verify = verify
        self.budget_usd = budget_usd
        self._all = lattice()

    def plan(self, task: Task) -> Plan:
        cells = list(self.ladder_cells)
        return Plan(cells, task.verify or self.verify, task.budget_usd or self.budget_usd, len(cells),
                    [f"fixed ladder: {' -> '.join(map(str, cells))}"])

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None:
        """Return the cell that verifies `worker`'s answer in `mode`, chosen from the whole lattice."""
        return choose_verifier(worker, mode, self._all)
