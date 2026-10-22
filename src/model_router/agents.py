"""Advice on the agents whose model and effort someone fixed by hand (`.claude/agents/*.md`, a plugin's agents).

The hook leaves those agents alone: their frontmatter decides. It still measures every one when it ends
(`subagents.py`), and that is enough to say, per agent and cell in a repository, whether the cell looks too low,
too high, or right. The signal is the same one per-project learning uses: a repeat. A finished agent is repeated
when, in the same session and within RELAUNCH_WINDOW of its end, another subagent (of any type) starts on an
alike prompt: whoever launched it was not satisfied.

    measuring  fewer than MIN_N runs: nothing to say yet
    raise      at least MIN_FAILS repeats and RAISE_RATE of the runs: one step up the ladder (`step_up`)
    lower      DOWN_N runs or more and none repeated: the most capable cell that costs LOWER_SAVING less
               while giving up little capability (`step_down`)
    keep       anything else

It never edits an agent: the advice is for a person to apply. When the advice changes the model, the saving or
extra cost is the measured tokens repriced at the new model's list prices; a change of effort also changes how
many tokens are spent, and that is not estimated. Both sides of a saving are priced from the same tokens, so the
difference is the model's price alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: measured subagent records, decoded JSON

from . import clock
from .catalog import Cell, Usage, cost_usd, step_down, step_up
from .projects import ALIKE, DOWN_N, LOWER_SAVING, MIN_FAILS, MIN_N, RAISE_RATE, RELAUNCH_WINDOW, alike, served_cell

# Not fixed by hand: the router's own choices, the built-in agents, the read-only agent and workflow agents.
NOT_FIXED_BY_HAND = frozenset({"", "general-purpose", "claude", "workflow-subagent", "Explore", "Plan"})
LOWER_MAX_LOSS = 0.08  # the capability a lower cell may give up


@dataclass(frozen=True)
class Advice:
    """Hold what the measurements say about one agent on one cell in one repository, and the cell they advise."""

    agent: str
    cell: Cell
    runs: int
    repeated: int
    mean_turns: float
    total_cost: float
    action: str  # measuring, raise, lower or keep
    to: Cell | None = None
    saving: float | None = None  # USD over the measured runs; negative when the advice costs more

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "cell": self.cell.label, "runs": self.runs, "repeated": self.repeated,
                "mean_turns": round(self.mean_turns, 1), "total_cost_usd": round(self.total_cost, 4),
                "action": self.action, "to": self.to.label if self.to else None,
                "saving_usd": round(self.saving, 4) if self.saving is not None else None}


def fixed_by_hand(record: dict[str, Any]) -> bool:
    """Say whether a measured subagent ran on a cell someone fixed: a named agent the router did not route."""
    agent = str(record.get("agent_type") or "")
    return not record.get("routed") and agent not in NOT_FIXED_BY_HAND and not agent.endswith("reader")


def repeated_ids(records: list[dict[str, Any]]) -> set[str]:
    """Return the agent ids of the finished subagents that another subagent repeated (see the module docstring)."""
    starts = []
    for r in records:
        end = clock.parse_moment(r.get("at"))
        if end is not None and r.get("sig"):
            starts.append((end - timedelta(seconds=float(r.get("seconds") or 0)), r))
    out: set[str] = set()
    for r in records:
        end = clock.parse_moment(r.get("at"))
        if end is None or not r.get("sig") or not r.get("agent_id"):
            continue
        for start, later in starts:
            if (later.get("session") == r.get("session") and later.get("agent_id") != r.get("agent_id")
                    and timedelta(0) <= start - end <= RELAUNCH_WINDOW and alike(r["sig"], later["sig"]) >= ALIKE):
                out.add(str(r["agent_id"]))
                break
    return out


def advise(records: list[dict[str, Any]], project: str, *, cells: list[Cell]) -> list[Advice]:
    """Return the advice for every agent fixed by hand that ran in `project`, the most spent first."""
    repeated = repeated_ids([r for r in records if r.get("project") == project])
    groups: dict[tuple[str, Cell], list[dict[str, Any]]] = {}
    for r in _latest(records):
        cell = served_cell(r)
        if r.get("project") == project and fixed_by_hand(r) and cell is not None:
            groups.setdefault((str(r["agent_type"]), cell), []).append(r)
    out = [_advice(agent, cell, rs, cells=cells, repeated=repeated) for (agent, cell), rs in groups.items()]
    return sorted(out, key=lambda a: -a.total_cost)


def _latest(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one record per subagent: a resumed one, or one measured twice, counts once (its last record)."""
    latest: dict[object, dict[str, Any]] = {}
    for i, r in enumerate(records):
        latest[(r.get("session"), r.get("agent_id")) if r.get("agent_id") else i] = r
    return list(latest.values())


def _advice(agent: str, cell: Cell, records: list[dict[str, Any]], *, cells: list[Cell], repeated: set[str]) -> Advice:
    runs = len(records)
    fails = sum(1 for r in records if str(r.get("agent_id")) in repeated)
    total = sum(float(r.get("cost_usd") or 0.0) for r in records)
    turns = sum(int(r.get("turns") or 0) for r in records) / runs
    base = Advice(agent, cell, runs, fails, turns, total, "keep")
    if runs < MIN_N:
        return _with(base, "measuring")
    if fails >= MIN_FAILS and fails / runs >= RAISE_RATE:
        up = step_up(cell, cells)
        return _with(base, "raise", to=up, records=records) if up else base
    if runs >= DOWN_N and fails == 0:
        down = step_down(cell, cells, max_loss=LOWER_MAX_LOSS, min_saving=LOWER_SAVING)
        return _with(base, "lower", to=down, records=records) if down else base
    return base


def _with(base: Advice, action: str, *, to: Cell | None = None, records: list[dict[str, Any]] | None = None) -> Advice:
    """Return `base` with its action, the cell it advises and, when the model changes, the measured tokens repriced."""
    saving = None
    if to is not None and records is not None and to.model != base.cell.model:
        usages = [_usage(r) for r in records]
        saving = sum(cost_usd(base.cell.model, u) - cost_usd(to.model, u) for u in usages)
    return Advice(base.agent, base.cell, base.runs, base.repeated, base.mean_turns, base.total_cost, action, to, saving)


def _usage(record: dict[str, Any]) -> Usage:
    return Usage(int(record.get("input_tokens") or 0), int(record.get("output_tokens") or 0),
                 int(record.get("cache_read_tokens") or 0), int(record.get("cache_write_tokens") or 0))


def agent_file(project: str, agent: str) -> Path | None:
    """Return the repository's definition of `agent` (`.claude/agents/<name>.md`), or None when it is not there."""
    path = Path(project) / ".claude" / "agents" / f"{agent}.md"
    return path if path.is_file() else None
