"""Routing policy: which cell starts a task, how it escalates, how it is verified and what it may
spend. Everything is a table you can read and edit; the feedback store nudges the start cell once
it has seen enough verdicts for a kind. Verification has four modes: none; `light`, the worker's own
cell in a fresh context; `full`, a fixed judge cell (opus/xhigh unless configured) on another model
than the worker; `panel`, those two together, both must agree (critical risk, no checker, kinds
where only judgement can decide). Verifying is easier than producing, so the judge does not have to
outrank the worker: it has to be independent and good enough, and it never thinks above `xhigh`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .catalog import EFFORTS, Cell, ladder, lattice, nearest_available, parse_cell, step_up
from .paths import JUDGE_ENV, read_config
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
# Kinds where no checker can decide: when the risk calls for a full judge and the task has no checker,
# the policy asks a panel of two judges instead of one.
PANEL_KINDS: frozenset[str] = frozenset({"analysis", "write", "review"})
BUDGET_USD_BY_RISK: dict[str, float] = {"low": 0.10, "medium": 0.50, "high": 2.00, "critical": 10.00}
MAX_ATTEMPTS_BY_RISK: dict[str, int] = {"low": 2, "medium": 3, "high": 4, "critical": 5}
BUDGET_ATTEMPTS = 3.0  # the budget is at least this many first attempts, so big prompts are not starved

# Reference request for pricing a verification call: it reads the task and the answer, writes little.
JUDGE_INPUT_TOKENS = 3000
JUDGE_OUTPUT_TOKENS_AT_HIGH = 300
JUDGE_PROMPT_OVERHEAD_TOKENS = 300  # the verdict instructions and tags around the task and the answer
FULL_VERIFY_GAIN = 0.08  # the simulation treats a judge this much more capable than the worker as a `full`-grade one
DEFAULT_JUDGE = "opus/xhigh"  # the `full` judge unless MODEL_ROUTER_JUDGE or `judge` in config.json say otherwise
JUDGE_MAX_EFFORT = "xhigh"  # a judge reads and decides; thinking above this on a verdict is cost, not accuracy


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

    def judges_for(self, worker: Cell, mode: str) -> list[Cell]: ...


def _judge_cost(cell: Cell) -> float:
    return cell.expected_cost(JUDGE_INPUT_TOKENS, JUDGE_OUTPUT_TOKENS_AT_HIGH)


def _cap_effort(cell: Cell) -> Cell:
    if cell.effort_rank <= EFFORTS.index(JUDGE_MAX_EFFORT):
        return cell
    return Cell(cell.model, JUDGE_MAX_EFFORT)


def default_judge(models: Iterable[str] | None = None) -> Cell:
    """Return the configured judge cell: MODEL_ROUTER_JUDGE, else `judge` in config.json, else DEFAULT_JUDGE;
    its effort is capped at JUDGE_MAX_EFFORT and its model replaced by the nearest one in `models` (the
    available models when None) when this installation lacks it.

    Raises:
        UnknownModelError: the configured model is not in the catalogue.
        ValueError: the configured effort does not exist.
    """
    raw = os.environ.get(JUDGE_ENV) or read_config().get("judge") or DEFAULT_JUDGE
    return _cap_effort(nearest_available(parse_cell(str(raw)), models))


def _light_judge(worker: Cell) -> Cell:
    return _cap_effort(worker)


def _full_judge(worker: Cell, judge: Cell, cells: list[Cell]) -> Cell:
    if worker.model != judge.model:
        return judge
    models = sorted({c.model for c in cells}, key=lambda m: Cell(m, "low").tier)
    above = [m for m in models if Cell(m, "low").tier > worker.tier]
    if above:
        return Cell(above[0], "low")
    below = [m for m in models if Cell(m, "low").tier < worker.tier]
    if below:
        return Cell(below[-1], judge.effort)
    same = [c for c in cells if c.model == worker.model and c != worker and c.effort_rank <= judge.effort_rank]
    if not same:
        return _cap_effort(worker)
    return max(same, key=lambda c: c.effort_rank)


def choose_verifier(worker: Cell, mode: str, cells: list[Cell], *, judge: Cell | None = None) -> Cell | None:
    """Return the judge for `mode`, or None for `none`. light: the worker's own cell in a fresh context,
    effort capped at JUDGE_MAX_EFFORT. full: `judge` (the configured one when None) in a fresh context;
    when the worker is on the judge's model, so that the check stays a cross-check between models, the
    next model up at low effort, else the model below at the judge's effort, else another cell of that
    model at or under the judge's effort, else the worker's own cell.
    """
    if mode == "none":
        return None
    if mode == "light":
        return _light_judge(worker)
    judge = _cap_effort(judge) if judge else default_judge({c.model for c in cells})
    return _full_judge(worker, judge, cells)


def choose_panel(worker: Cell, cells: list[Cell], *, judge: Cell | None = None) -> list[Cell]:
    """Return the judges of a panel for `worker`, cheapest first: the light judge (the worker's own cell,
    fresh context) and the full judge (see `choose_verifier`). When both are the same cell, one judge.
    """
    judge = _cap_effort(judge) if judge else default_judge({c.model for c in cells})
    return sorted({_light_judge(worker), _full_judge(worker, judge, cells)}, key=_judge_cost)


class Policy:
    """The default policy: the START table, the risk floors, feedback nudges, cost-aware escalation and a
    fixed judge (`judge`, else the configured one) for full verification.
    """

    def __init__(
        self,
        *,
        cells: list[Cell] | None = None,
        start_table: dict[str, dict[str, str]] | None = None,
        feedback: FeedbackStore | None = None,
        min_gain: float = 0.08,
        panel_kinds: frozenset[str] = PANEL_KINDS,
        judge: Cell | None = None,
    ) -> None:
        self.cells = cells or lattice()
        self.models = sorted({c.model for c in self.cells}, key=lambda m: Cell(m, "low").tier)
        self.start_table = start_table or START
        self.feedback = feedback
        self.min_gain = min_gain
        self.panel_kinds = panel_kinds
        self.judge = _cap_effort(nearest_available(judge, self.models)) if judge else default_judge(self.models)

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
        if task.verify is None and verify == "full" and kind in self.panel_kinds and task.checker is None:
            verify = "panel"
            rationale.append(f"panel: {risk} {kind} with no checker, two judges must agree")
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
        max_attempts = task.max_attempts if task.max_attempts is not None else MAX_ATTEMPTS_BY_RISK[risk]
        cells = ladder(start, self.cells, max_attempts, min_gain=self.min_gain)
        first_attempt = start.expected_cost(est_input, out_tokens)
        default_budget = max(BUDGET_USD_BY_RISK[risk], BUDGET_ATTEMPTS * first_attempt)
        budget = task.budget_usd if task.budget_usd is not None else default_budget
        rationale.append(f"ladder: {' -> '.join(map(str, cells))}")
        rationale.append(f"verification {verify} (risk {risk}); budget ${budget:.2f}; max {max_attempts} attempts")
        return Plan(cells, verify, budget, max_attempts, rationale)

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None:
        """Return the cell that verifies `worker`'s answer in `mode`, or None for `none`; for `panel` it is the
        full judge alone, `judges_for` has both.
        """
        return choose_verifier(worker, mode, self.cells, judge=self.judge)

    def judges_for(self, worker: Cell, mode: str) -> list[Cell]:
        """Return every judge `mode` consults on `worker`'s answer, cheapest first: none for `none`, one
        for `light` and `full`, two for `panel`.
        """
        return _judges(worker, mode, self.cells, self.judge)

    def attempt_cost(self, cell: Cell, verify: str, est_input: int, out_tokens: int) -> float:
        """Return the expected cost of one attempt on `cell` including its verification calls (both judges
        of a panel, although the second is skipped when the first fails).
        """
        cost = cell.expected_cost(est_input, out_tokens)
        for judge in self.judges_for(cell, verify):
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

    def __init__(
        self, cells: list[Cell], *, verify: str = "none", budget_usd: float = 100.0, judge: Cell | None = None
    ) -> None:
        if not cells:
            raise ValueError("a ladder needs at least one cell")
        self.ladder_cells = cells
        self.verify = verify
        self.budget_usd = budget_usd
        self._all = lattice()
        models = {c.model for c in self._all}
        self.judge = _cap_effort(nearest_available(judge, models)) if judge else default_judge(models)

    def plan(self, task: Task) -> Plan:
        cells = list(self.ladder_cells)
        budget = task.budget_usd if task.budget_usd is not None else self.budget_usd
        return Plan(cells, task.verify or self.verify, budget, len(cells),
                    [f"fixed ladder: {' -> '.join(map(str, cells))}"])

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None:
        """Return the cell that verifies `worker`'s answer in `mode`, chosen from the whole lattice."""
        return choose_verifier(worker, mode, self._all, judge=self.judge)

    def judges_for(self, worker: Cell, mode: str) -> list[Cell]:
        """Return every judge `mode` consults on `worker`'s answer, chosen from the whole lattice."""
        return _judges(worker, mode, self._all, self.judge)


def _judges(worker: Cell, mode: str, cells: list[Cell], judge: Cell) -> list[Cell]:
    if mode == "panel":
        return choose_panel(worker, cells, judge=judge)
    one = choose_verifier(worker, mode, cells, judge=judge)
    return [] if one is None else [one]
