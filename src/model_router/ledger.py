"""Append-only record of every task the router ran, and the summary that turns it into cost per
solved task.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any  # Any: ledger records are JSON lines

from .fmt import money, table
from .task import EXTERNAL_STOP, VERIFY_MODES

if TYPE_CHECKING:
    from .runner import TaskResult

_LOCK = threading.Lock()


class Ledger:
    """Append-only JSON-lines file of task results; with `path=None` it lives in memory (tests)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: list[dict[str, Any]] = []
        self.corrupt = 0  # lines of the file that were not JSON on the last load

    def append(self, result: TaskResult) -> None:
        """Append `result` as a task record."""
        record = {"type": "task", **result.to_dict()}
        self._memory.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        """Return every record in file order. Lines that are not UTF-8 JSON objects (a write cut short by
        another process) are skipped and counted in `corrupt`.
        """
        if self.path is None or not self.path.exists():
            return list(self._memory)
        out: list[dict[str, Any]] = []
        self.corrupt = 0
        with self.path.open("rb") as fh:
            for raw in fh:
                try:
                    record = json.loads(raw.decode("utf-8"))
                except ValueError:  # not UTF-8 or not JSON; a blank line is neither
                    self.corrupt += bool(raw.strip())
                    continue
                if isinstance(record, dict):
                    out.append(record)
                else:
                    self.corrupt += 1
        return out

    def tasks(self) -> list[dict[str, Any]]:
        """Return the task records, the last one of each task id."""
        return list({r["task_id"]: r for r in self.load() if r.get("type") == "task" and r.get("task_id")}.values())


@dataclass
class Summary:
    """Totals of a set of task records: counts by status, cost split into work and verification, and the
    breakdown by cell and by kind.
    """

    tasks: int = 0
    solved: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    attempts: int = 0
    retries: int = 0
    same_reason_retries: int = 0  # retries whose verified fail gives the reason the attempt before gave
    tokens: int = 0
    work_cost: float = 0.0
    verify_cost: float = 0.0
    by_cell: dict[str, dict[str, float]] = field(default_factory=dict)
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)
    by_judge: dict[str, dict[str, int]] = field(default_factory=dict)  # verdicts and passes per judge cell

    @property
    def total_cost(self) -> float:
        return self.work_cost + self.verify_cost

    @property
    def cost_per_solved(self) -> float | None:
        return self.total_cost / self.solved if self.solved else None

    def text(self) -> str:
        """Render the summary as the text `router stats` prints."""
        lines = [
            f"tasks {self.tasks} · solved {self.solved} · "
            + ", ".join(f"{k} {v}" for k, v in sorted(self.by_status.items())),
            f"attempts {self.attempts} · retries {self.retries}"
            + (f" ({self.same_reason_retries} repeating the previous reason)" if self.retries else "")
            + f" · tokens {self.tokens:,}",
            f"work cost {money(self.work_cost)} · verification {money(self.verify_cost)} · total {money(self.total_cost)}",
            f"cost per solved task: {money(self.cost_per_solved) if self.cost_per_solved is not None else 'n/a'}",
        ]
        if self.by_cell:
            rows = []
            for label, s in sorted(self.by_cell.items(), key=lambda kv: -kv[1]["attempts"]):
                rate = s["passes"] / s["judged"] if s["judged"] else None
                rows.append([label, int(s["attempts"]), f"{rate:.0%}" if rate is not None else "-", money(s["cost"])])
            lines.append("")
            lines.append(table(["cell", "attempts", "pass rate", "cost"], rows))
        if self.by_kind:
            rows = [
                [k, int(s["tasks"]), int(s["solved"]), money(s["cost"]), money(s["cost"] / s["solved"]) if s["solved"] else "-"]
                for k, s in sorted(self.by_kind.items())
            ]
            lines.append("")
            lines.append(table(["kind", "tasks", "solved", "cost", "cost/solved"], rows))
        if self.by_judge:
            rows = [[label, s["verdicts"], f"{s['passes'] / s['verdicts']:.0%}"]
                    for label, s in sorted(self.by_judge.items(), key=lambda kv: -kv[1]["verdicts"])]
            lines.append("")
            lines.append(table(["judge", "verdicts", "pass rate"], rows))
        return "\n".join(lines)


def summarize(task_records: list[dict[str, Any]]) -> Summary:
    """Aggregate `task_records` (as `Ledger.tasks` returns them) into a Summary. A record of a verification mode
    this router does not have (written by another version, or by another program sharing the ledger) is left out.
    """
    s = Summary()
    cells: dict[str, dict[str, float]] = defaultdict(lambda: {"attempts": 0, "judged": 0, "passes": 0, "cost": 0.0})
    kinds: dict[str, dict[str, float]] = defaultdict(lambda: {"tasks": 0, "solved": 0, "cost": 0.0})
    judges: dict[str, dict[str, int]] = defaultdict(lambda: {"verdicts": 0, "passes": 0})
    for t in task_records:
        if t.get("verify") is not None and t.get("verify") not in VERIFY_MODES:
            continue
        s.tasks += 1
        solved = t["status"] == "solved"
        s.solved += solved
        s.by_status[t["status"]] = s.by_status.get(t["status"], 0) + 1
        s.attempts += len(t["attempts"])
        s.retries += t.get("retries", 0)
        s.tokens += sum(t.get("usage", {}).values())
        s.work_cost += t.get("work_cost_usd", 0.0)
        s.verify_cost += t.get("verify_cost_usd", 0.0)
        for i, a in enumerate(t["attempts"]):
            if i and a.get("verdict") is False and t["attempts"][i - 1].get("verdict") is False \
                    and a.get("reason") == t["attempts"][i - 1].get("reason"):
                s.same_reason_retries += 1
            for v in a.get("verdicts") or []:
                judge = judges[v["cell"]]
                judge["verdicts"] += 1
                judge["passes"] += bool(v["passed"])
            if a.get("stop_reason") == EXTERNAL_STOP:  # an answer made elsewhere: no attempt of that cell under the router
                continue
            c = cells[a["cell"]]
            c["attempts"] += 1
            c["cost"] += a["cost_usd"] + a.get("verify_cost_usd", 0.0)
            if a.get("verdict") is not None:
                c["judged"] += 1
                c["passes"] += bool(a["verdict"])
        k = kinds[t["kind"]]
        k["tasks"] += 1
        k["solved"] += solved
        k["cost"] += t.get("total_cost_usd", 0.0)
    s.by_cell = dict(cells)
    s.by_kind = dict(kinds)
    s.by_judge = dict(judges)
    return s
