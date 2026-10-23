"""Routing policy: which cell starts a task, how it escalates, how it is verified and what it may
spend. Everything is a table you can read and edit, and a project's tuning (`projects.py`) moves a start
cell where its subagents keep failing. The router verifies in three modes: none; `light`, the nearest
other model at low effort in a fresh context; `full`, a fixed judge cell (opus/xhigh unless configured)
on another model than the worker. Verifying is easier than producing, so the judge does not have to
outrank the worker: it has to be independent and good enough, and it never thinks above `xhigh`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any  # Any: a tuning file's decoded JSON (`projects.load_tuning`)

from . import limits
from .catalog import (
    AUTO_EFFORTS,
    EFFORTS,
    MODELS,
    Cell,
    ladder,
    lattice,
    listed_models,
    nearest_available,
    nearest_effort,
    parse_cell,
    unknown_listed,
    worker_cell,
)
from .paths import read_config
from .task import Task

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
# A task never starts below this capability, whatever a project's tuning says.
FLOOR: dict[str, str] = {"high": "sonnet/low", "critical": "opus/medium"}
VERIFY_BY_RISK: dict[str, str] = {"low": "none", "medium": "none", "high": "light", "critical": "full"}
BUDGET_USD_BY_RISK: dict[str, float] = {"low": 0.10, "medium": 0.50, "high": 2.00, "critical": 10.00}
MAX_ATTEMPTS_BY_RISK: dict[str, int] = {"low": 2, "medium": 3, "high": 4, "critical": 5}
BUDGET_ATTEMPTS = 3.0  # the budget is at least this many first attempts, so big prompts are not starved

DEFAULT_JUDGE = "opus/xhigh"  # the `full` judge unless `judge` in config.json says otherwise
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


def _at(model: str, effort: str) -> Cell:
    """Return `model` at `effort`, or at the nearest effort its door accepts: a cell that names an effort the door
    ignores would be priced and recorded as something other than what is sent.
    """
    return Cell(model, nearest_effort(effort, MODELS[model].efforts))


def _by_tier(cells: list[Cell]) -> list[str]:
    """Return the models of `cells`, once each, by tier, then by name, so that the choice never depends on set order."""
    return sorted({c.model for c in cells}, key=lambda m: (MODELS[m].tier, m))


def _cap_effort(cell: Cell) -> Cell:
    capped = EFFORTS[min(cell.effort_rank, EFFORTS.index(JUDGE_MAX_EFFORT))]
    return _at(cell.model, capped)


def models_note() -> str | None:
    """Return the note a plan carries when the models setting names models the catalogue does not know (a typo, an
    older version's model), or None when every name is known.
    """
    unknown = unknown_listed()
    if not unknown:
        return None
    if len(unknown) == len(listed_models()):
        return (f"the models setting lists no model this router knows ({', '.join(unknown)}): every Claude model "
                "is used; `router config --models` fixes it")
    return f"the models setting names models this router does not know, ignored: {', '.join(unknown)}"


def default_judge_cell(models: Iterable[str] | None = None) -> Cell:
    """Return DEFAULT_JUDGE on the nearest model this installation has, whatever the settings say."""
    return _cap_effort(nearest_available(parse_cell(DEFAULT_JUDGE), models))


def default_judge(models: Iterable[str] | None = None) -> Cell:
    """Return the configured judge cell: `judge` in config.json, else DEFAULT_JUDGE;
    its effort is capped at JUDGE_MAX_EFFORT and its model replaced by the nearest one in `models` (the
    available models when None) when this installation lacks it.

    Raises:
        UnknownModelError: the configured model is not in the catalogue.
        ValueError: the configured effort does not exist.
    """
    raw = read_config().get("judge") or DEFAULT_JUDGE
    return _cap_effort(nearest_available(parse_cell(str(raw)), models))


def _light_judge(worker: Cell, cells: list[Cell]) -> Cell:
    """Return the light judge of `worker`: the nearest other model at low effort, the tiers above first (the
    lowest of them), then another model of the worker's tier, then the tier just below, never further down.
    The worker's own cell, effort capped, when no such model is available: a worker judging itself in a fresh
    context passed a quarter of the answers an independent judge failed, but a judge two tiers down is no
    check either.
    """
    models = _by_tier(cells)
    above = [m for m in models if MODELS[m].tier > worker.tier]
    same = [m for m in models if MODELS[m].tier == worker.tier and m != worker.model]
    below = [m for m in models if MODELS[m].tier == worker.tier - 1]
    first = next(iter(above + same + below), None)
    return _at(first, "low") if first else _cap_effort(worker)


def _full_judge(worker: Cell, *, judge: Cell, cells: list[Cell]) -> Cell:
    """Return `judge` when the worker is on another model; else the next model up at low effort, else the model
    below at the judge's effort, else another cell of the worker's model at or under it, else the worker's own.
    """
    if worker.model != judge.model:
        return judge
    models = _by_tier(cells)
    above = [m for m in models if MODELS[m].tier > worker.tier]
    if above:
        return _at(above[0], "low")
    below = [m for m in models if MODELS[m].tier < worker.tier]
    if below:  # the highest tier below
        return _at(max(below, key=lambda m: MODELS[m].tier), judge.effort)
    same = [c for c in cells if c.model == worker.model and c != worker and c.effort_rank <= judge.effort_rank]
    if not same:
        return _cap_effort(worker)
    return max(same, key=lambda c: c.effort_rank)


def choose_verifier(worker: Cell, mode: str, *, cells: list[Cell], judge: Cell | None = None) -> Cell | None:
    """Return the judge for `mode`, or None for `none`. light: the nearest other model at low effort (the
    tier above first; see `_light_judge`). full: `judge` (the configured one when None) in a fresh context;
    when the worker is on the judge's model, so that the check stays a cross-check between models, the
    next model up at low effort, else the model below at the judge's effort, else another cell of that
    model at or under the judge's effort, else the worker's own cell.
    """
    if mode == "none":
        return None
    if mode == "light":
        return _light_judge(worker, cells)
    judge = _cap_effort(judge) if judge else default_judge({c.model for c in cells})
    return _full_judge(worker, judge=judge, cells=cells)


class Policy:
    """The default policy: the START table, the risk floors, a project's tuning, cost-aware escalation and a fixed
    judge (`judge`, else the configured one) for full verification.
    """

    def __init__(
        self,
        *,
        cells: list[Cell] | None = None,
        judge: Cell | None = None,
        tuning: dict[str, Any] | None = None,  # Any: a tuning file's decoded JSON (`projects.load_tuning`)
    ) -> None:
        self.cells = cells or lattice()
        self.tuning = tuning or {}  # a project's own start cells (`projects.py`), on top of the table
        self.models = sorted({c.model for c in self.cells}, key=lambda m: MODELS[m].tier)
        self.notes: list[str] = []
        note = models_note()
        if note:
            self.notes.append(note)
        try:
            self.judge = _cap_effort(nearest_available(judge, self.models)) if judge else default_judge(self.models)
        except ValueError as exc:  # a typo, or a model this version does not know
            self.judge = default_judge_cell(self.models)
            self.notes.append(f"judge setting cannot be used ({exc}): {self.judge} instead; `router config --judge` "
                              f"sets another, `router doctor` shows it")

    def _available(self, cell: Cell, rationale: list[str]) -> Cell:
        """Replace a cell whose model this installation lacks with the one `worker_cell` picks, and a cell at an
        effort the router never chooses (`max`, from a tuning) with the same model at the top of
        AUTO_EFFORTS, and say so.
        """
        near = worker_cell(cell, self.models)
        if near != cell:
            held = limits.active().get(cell.spec.short)
            why = f"over its usage limit until {held.when()}" if held else "not available"
            rationale.append(f"{cell.spec.short} {why}: {cell} -> {near}")
        if near.effort not in AUTO_EFFORTS:
            capped = _at(near.model, AUTO_EFFORTS[-1])
            rationale.append(f"{near}: the router never chooses {near.effort} effort -> {capped}")
            near = capped
        return near

    def plan(self, task: Task) -> Plan:
        """Plan `task`: start cell from the table, risk floor, the project's tuning, ladder, verification mode and
        budget, with the reasons.
        """
        kind = task.kind if task.kind in START else "unknown"
        risk = task.risk
        start = parse_cell(START[kind][risk])
        rationale = [f"table {kind} x {risk} -> {start}"]
        start = self._available(start, rationale)
        verify = task.verify or VERIFY_BY_RISK[risk]
        rationale += self.notes
        floor = FLOOR.get(risk)
        if floor:
            floor_cell = self._available(parse_cell(floor), [])
            if start.capability < floor_cell.capability:
                rationale.append(f"risk floor {risk}: {start} -> {floor_cell}")
                start = floor_cell
        start = self._tuned(kind, risk, start=start, rationale=rationale)
        max_attempts = task.max_attempts if task.max_attempts is not None else MAX_ATTEMPTS_BY_RISK[risk]
        cells = ladder(start, self.cells, steps=max_attempts)
        first_attempt = start.expected_cost(task.estimated_input_tokens, task.planned_output_tokens)
        default_budget = max(BUDGET_USD_BY_RISK[risk], BUDGET_ATTEMPTS * first_attempt)
        budget = task.budget_usd if task.budget_usd is not None else default_budget
        rationale.append(f"ladder: {' -> '.join(map(str, cells))}")
        rationale.append(f"verification {verify} (risk {risk}); budget ${budget:.2f}; max {max_attempts} attempts")
        return Plan(cells, verify, budget, max_attempts, rationale)

    def _tuned(self, kind: str, risk: str, *, start: Cell, rationale: list[str]) -> Cell:
        """Apply the project's tuning (`projects.py`) to the start the table and the floors gave: its cell stands in
        for `start` only while `start` is still the cell it replaced; a tuning whose base no longer comes out (the
        table or the models changed) is ignored and said so.
        """
        entry = (self.tuning.get("start") or {}).get(kind, {}).get(risk)
        if not isinstance(entry, dict) or not entry.get("cell"):
            return start
        name = self.tuning.get("name", "this project")
        if entry.get("base") and entry["base"] != start.label:
            rationale.append(f"tuning for {name} ignored: it replaced {entry['base']}, the start is now {start}")
            return start
        try:
            wanted = parse_cell(str(entry["cell"]))
        except ValueError:
            return start
        tuned = self._available(wanted, rationale)
        if tuned != wanted and tuned.capability < start.capability:
            # its model is not available here, and what stands in is weaker than the start: a raise would undo
            # itself, a cheaper start would go further down than anyone weighed
            rationale.append(f"tuning for {name} ignored: {wanted} would run as {tuned}")
            return start
        floor = FLOOR.get(risk)
        if floor and tuned.capability < self._available(parse_cell(floor), []).capability:
            rationale.append(f"tuning for {name} ignored: {tuned} is below the {risk} floor")
            return start
        rationale.append(f"tuned for {name}: starts on {tuned} instead of {start}")
        return tuned

    def verifier_for(self, worker: Cell, mode: str) -> Cell | None:
        """Return the cell that verifies `worker`'s answer in `mode`, or None for `none`."""
        return choose_verifier(worker, mode, cells=self.cells, judge=self.judge)

    def judges_for(self, worker: Cell, mode: str) -> list[Cell]:
        """Return the judges `mode` consults on `worker`'s answer: none for `none`, one for `light` and `full`."""
        one = self.verifier_for(worker, mode)
        return [] if one is None else [one]
