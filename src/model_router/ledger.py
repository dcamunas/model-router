"""Append-only record of every task the router ran, and the summary that turns it into cost per
solved task. Human overrides of a verdict are appended as their own records, never rewritten.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._fmt import money, table

if TYPE_CHECKING:
    from .runner import TaskResult


_LOCK = threading.Lock()


class Ledger:
    """Append-only JSON-lines file of task results and human overrides; with `path=None` it lives in memory
    (benchmarks, tests).
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: list[dict[str, Any]] = []
        self.corrupt = 0  # lines of the file that were not JSON on the last load

    def append(self, result: TaskResult) -> None:
        """Append `result` as a task record."""
        self._write({"type": "task", **result.to_dict()})

    def override(self, task_id: str, passed: bool, *, note: str = "") -> None:
        """Append a human correction of `task_id`'s verdict; `tasks()` applies it and the original record stays."""
        self._write({"type": "override", "task_id": task_id, "passed": passed, "note": note,
                     "at": datetime.now(UTC).isoformat(timespec="seconds")})

    def _write(self, record: dict[str, Any]) -> None:
        self._memory.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _LOCK, self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        """Return every record in file order. Lines that are not JSON (a write cut short by another
        process) are skipped and counted in `corrupt`.
        """
        if self.path is None or not self.path.exists():
            return list(self._memory)
        out: list[dict[str, Any]] = []
        self.corrupt = 0
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    self.corrupt += 1
        return out

    def tasks(self) -> list[dict[str, Any]]:
        """Return the task records with human overrides applied; the last override wins."""
        records = self.load()
        by_id = {r["task_id"]: dict(r) for r in records if r.get("type") == "task"}  # copies: the originals stay as written
        for r in records:
            if r.get("type") == "override" and r["task_id"] in by_id:
                t = by_id[r["task_id"]]
                t["status"] = "solved" if r["passed"] else "unsolved"
                t["overridden"] = True
        return list(by_id.values())

    def find(self, task_id: str) -> dict[str, Any] | None:
        """Return the task record with `task_id` as it was written (overrides not applied), or None."""
        for r in self.load():
            if r.get("type") == "task" and r["task_id"] == task_id:
                return dict(r)
        return None

    def last_override(self, task_id: str) -> bool | None:
        """Return the verdict of the last human override of `task_id`, or None when it was never overridden."""
        last = None
        for r in self.load():
            if r.get("type") == "override" and r.get("task_id") == task_id:
                last = bool(r["passed"])
        return last


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
    tokens: int = 0
    work_cost: float = 0.0
    verify_cost: float = 0.0
    by_cell: dict[str, dict[str, float]] = field(default_factory=dict)
    by_kind: dict[str, dict[str, float]] = field(default_factory=dict)

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
            f"attempts {self.attempts} · retries {self.retries} · tokens {self.tokens:,}",
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
        return "\n".join(lines)


def summarize(task_records: list[dict[str, Any]]) -> Summary:
    """Aggregate `task_records` (as `Ledger.tasks` returns them) into a Summary."""
    s = Summary()
    cells: dict[str, dict[str, float]] = defaultdict(lambda: {"attempts": 0, "judged": 0, "passes": 0, "cost": 0.0})
    kinds: dict[str, dict[str, float]] = defaultdict(lambda: {"tasks": 0, "solved": 0, "cost": 0.0})
    for t in task_records:
        s.tasks += 1
        solved = t["status"] == "solved"
        s.solved += solved
        s.by_status[t["status"]] = s.by_status.get(t["status"], 0) + 1
        s.attempts += len(t["attempts"])
        s.retries += t.get("retries", 0)
        s.tokens += sum(t.get("usage", {}).values())
        s.work_cost += t.get("work_cost_usd", 0.0)
        s.verify_cost += t.get("verify_cost_usd", 0.0)
        for a in t["attempts"]:
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
    return s
